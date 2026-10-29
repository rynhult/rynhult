"""Standard Git HTTP over per-request trusted staging, never the live exchange.

Receive-pack can negotiate only its granted ref. Upload-pack sees only the selected
published commit closure. Unselected received objects disappear with staging.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

from .. import git_exchange as git
from .gitbackup import HttpBackend


def inspect(body: bytes) -> None:
    """A receive-pack request is commands, a flush, then the pack: scan it before Git sees it.

    Push options and certificates are never advertised, so nothing else may follow the flush.
    """
    at = 0
    while (length := int(body[at:at + 4], 16)) != 0:
        if length < 4:
            raise ValueError('invalid Git push request')
        at += length
    if body[at + 4:]:
        git.inspect_pack(body[at + 4:])


def serve(target: Path, binding: dict, method: str, service: str, headers: dict, body: bytes,
          budget: dict | None = None) -> tuple[int, list, bytes, bool]:
    ref, old, new = binding['ref'], binding['old_sha'], binding['new_sha']
    writing = binding['operation'] == 'publish'
    with tempfile.TemporaryDirectory(prefix='acc-git-http-') as scratch:
        root = Path(scratch) / 'git'
        root.mkdir(mode=0o700)
        stage = git.initialize(root / 'repo.git', 'exchange-staging')
        if writing:
            if git.refs(target).get(ref, git.ZERO) != old:
                raise ValueError('published ref changed; fetch and review again')
            if old != git.ZERO:
                git.import_pack(stage, git.export(target, old))
                git.run(stage, 'update-ref', ref, old, git.ZERO)
            hooks = stage / 'hooks'
            hooks.mkdir(mode=0o700, exist_ok=True)
            hook = hooks / 'update'
            hook.write_text('#!/bin/sh\n' +
                f'[ "$1" = "{ref}" ] && [ "$2" = "{old}" ] && [ "$3" = "{new}" ] || exit 1\n')
            hook.chmod(0o700)
            head = ref
        else:
            if not git.published(target, new):
                raise ValueError('no such published commit')
            git.import_pack(stage, git.export(target, new))
            selected = {name: sha for name, sha in git.refs(target).items() if sha == new}
            head = next((name for name in selected if name.startswith('refs/heads/')), 'refs/heads/published')
            selected[head] = new
            for name, sha in selected.items():
                git.run(stage, 'update-ref', name, sha, git.ZERO)
        git.run(stage, 'symbolic-ref', 'HEAD', head)
        for key, value in (
            ('http.receivepack', 'true' if writing else 'false'),
            ('http.uploadpack', 'false' if writing else 'true'), ('http.getanyfile', 'false'),
            ('receive.denyNonFastForwards', 'true'), ('receive.denyDeletes', 'true'),
            ('receive.fsckObjects', 'true'), ('receive.maxInputSize', str(git.LIMIT)),
            ('receive.autogc', 'false'), ('uploadpack.allowAnySHA1InWant', 'false'),
            ('uploadpack.allowTipSHA1InWant', 'false'), ('uploadpack.allowReachableSHA1InWant', 'false'),
        ):
            git.run(stage, 'config', key, value)
        # Use protocol v0: a bounded capability covers one stateless negotiation,
        # and is consumed when a pack is returned, rather than on ls-refs (v2).
        clean_headers = {k.lower(): v for k, v in headers.items() if k.lower() != 'git-protocol'}
        backend = HttpBackend(root, git.LIMIT + 65536, git.TIMEOUT,
            command=(sys.executable, '-I', str(Path(git.__file__).resolve()), '--git-child', 'git', 'http-backend'))

        async def incoming():
            if body:
                yield body
        path = '/repo.git/' + ('info/refs' if method == 'GET' else service)
        query = 'service=' + service if method == 'GET' else ''
        status, response_headers, response = asyncio.run(backend.run(method, path, query, clean_headers, incoming()))
        changed = False
        if method == 'POST' and writing and git.refs(stage).get(ref) == new:
            git.publish(target, git.export(stage, new), ref, old, new, **(budget or {}))
            changed = True
        return status, response_headers, response, changed
