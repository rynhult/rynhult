"""Immutable group recovery snapshots contain published history and safe metadata only.

Stored below git_dir so the existing off-site capture/retention copies them under
its Git maintenance lease. Personal backups never feed this store.
"""
import json
import shutil
import uuid
from pathlib import Path

from .. import git_exchange as git

MAX_SNAPSHOTS = 128
MAX_STORAGE = 512 << 20


def snapshot(source: Path, destination: Path, metadata: dict) -> str:
    selected = git.refs(source)
    if not selected or len(selected) > 128:
        raise ValueError('backups require 1–128 published refs')
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    git.private_directory(destination)
    if len(list(destination.iterdir())) >= MAX_SNAPSHOTS:
        raise ValueError('local recovery snapshot limit reached; ask an owner to review retention')
    used = sum(p.stat().st_size for p in destination.rglob('*') if p.is_file())
    if used + (64 << 20) > MAX_STORAGE:
        raise ValueError('local recovery storage budget reached; ask an owner to review retention')
    name = uuid.uuid4().hex
    target = destination / name
    target.mkdir(mode=0o700)
    try:
        identity = git.run(source, 'config', '--get', 'acc.project').decode().strip()
        repo = git.initialize(target / 'repo.git', identity)
        total = 0
        for ref, sha in selected.items():
            data = git.export(source, sha)
            total += len(data)
            if total > 64 << 20:
                raise ValueError('group snapshot exceeds 64 MiB transfer budget')
            git.publish(repo, data, ref, git.ZERO, sha)
        # Written last: an incomplete capture is never advertised as recoverable.
        manifest = target / 'metadata.json'
        manifest.write_text(json.dumps({'identity': identity, 'refs': selected, **metadata}, sort_keys=True))
        manifest.chmod(0o600)
        return name
    except Exception:
        shutil.rmtree(target)
        raise
