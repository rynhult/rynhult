"""Bounded, config-free Git object transfer. No network, checkout, hooks or source refs.

Only objects reachable from an explicitly selected commit leave staging. A rejected
pack is discarded, rather than leaving private dangling objects in the shared store.
Callers serialize publication and recheck authority before invoking it.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
import tempfile
import threading
import zlib
from pathlib import Path

LIMIT = 8 << 20  # also fits the broker's 16 MiB JSON frame after base64
TIMEOUT = 60
ZERO = '0' * 40
MAX_REFS = 128
MAX_STORE = 128 << 20
MAX_OBJECT = 128 << 20  # the broker's whole checkout budget
MAX_EXPANDED = 512 << 20
SHA = re.compile(r'^[0-9a-f]{40}$')
REF = re.compile(r'^refs/(heads/[A-Za-z0-9_/-]+|tags/[A-Za-z0-9_-]+)$')


def run(repo: Path, *args: str, data: bytes = b'', objects: Path | None = None, limit: int = LIMIT,
        known: Path | None = None) -> bytes:
    env = {'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent', 'LANG': 'C', 'LC_ALL': 'C',
           'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_NO_REPLACE_OBJECTS': '1',
           'GIT_TERMINAL_PROMPT': '0', 'GIT_ALLOW_PROTOCOL': '', 'GIT_ATTR_NOSYSTEM': '1'}
    if objects is not None:
        env['GIT_OBJECT_DIRECTORY'] = str(objects)
    if known is not None:
        # Service-owned store, read only: lets a quarantine name objects it already has.
        env['GIT_ALTERNATE_OBJECT_DIRECTORIES'] = str(known / 'objects')
    command = ['git', '--git-dir', str(repo), '-c', 'core.hooksPath=/dev/null', '-c', 'gc.auto=0',
               '-c', 'maintenance.auto=false', '-c', 'pack.threads=1', '-c', 'pack.windowMemory=16m',
               '-c', 'core.bigFileThreshold=16m', *args]
    with tempfile.TemporaryFile() as incoming, tempfile.TemporaryFile() as errors:
        incoming.write(data)
        incoming.seek(0)
        with subprocess.Popen([sys.executable, '-I', str(Path(__file__).resolve()), '--git-child', *command], stdin=incoming, stdout=subprocess.PIPE, stderr=errors, env=env) as process:
            timer = threading.Timer(TIMEOUT, process.kill)
            timer.start()
            try:
                output = bytearray()
                while chunk := process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > limit:
                        process.kill()
                        raise ValueError('Git exchange output exceeds limit')
                if process.wait():
                    raise ValueError('Git exchange rejected the operation')
                return bytes(output)
            finally:
                timer.cancel()


def private_directory(path: Path) -> None:
    """Fail closed on links, foreign ownership, public modes or extended ACLs."""
    for parent in [*reversed(path.parents), path]:
        info = parent.lstat()
        if stat.S_ISLNK(info.st_mode):
            raise ValueError('Git exchange path contains a symlink')
    info = path.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise ValueError('Git exchange directory must be private to this account')
    if 'system.posix_acl_access' in os.listxattr(path):
        raise ValueError('Git exchange directory has an extended ACL')


def validate_store(path: Path) -> None:
    private_directory(path)
    # No config from a contributor is ever imported. Reject administrative escape
    # hatches as well, before Git can resolve an object or follow a packed ref.
    for root, directories, files in os.walk(path):
        for name in directories + files:
            entry = Path(root) / name
            if entry.is_symlink():
                raise ValueError('Git metadata contains a symlink')
    for name in ('objects/info/alternates', 'objects/info/http-alternates', 'commondir', 'shallow'):
        if (path / name).exists():
            raise ValueError('Git exchange requires a standalone complete object store')


def initialize(path: Path, identity: str) -> Path:
    if not path.exists():
        path.mkdir(mode=0o700, parents=True)
        run(path, 'init', '--bare', '--template=', str(path))
        run(path, 'config', 'acc.project', identity)
    validate_store(path)
    if run(path, 'config', '--get', 'acc.project').decode().strip() != identity:
        raise ValueError('Git exchange project identity mismatch')
    # This service-owned config is never copied from a member repository.
    return path


def export(source: Path, commit: str, known: Path | None = None) -> bytes:
    if not SHA.fullmatch(commit) or commit == ZERO:
        raise ValueError('select a commit')
    # Source config/hooks/refs are deliberately ignored, including packObjectsHook.
    # Objects are read through a fresh bare repository using an explicit directory.
    for root, directories, files in os.walk(source / 'objects'):
        for name in directories + files:
            if (Path(root) / name).is_symlink():
                raise ValueError('object store contains a symlink')
    if (source / 'objects').is_symlink() or (source / 'objects/info/alternates').exists() or (source / 'objects/info/http-alternates').exists():
        raise ValueError('object store is not standalone')
    with tempfile.TemporaryDirectory(prefix='acc-export-') as scratch:
        clean = initialize(Path(scratch) / 'repo', 'transfer')
        if run(clean, 'cat-file', '-t', commit, objects=source / 'objects').strip() != b'commit':
            raise ValueError('only commits may be published')
        # With `known`, omit history that a complete store already reaches from its refs.
        exclude = ''.join('^' + sha + '\n' for sha in sorted(set(refs(known).values()))) if known else ''
        return run(clean, 'pack-objects', '--stdout', '--revs', data=(commit + '\n' + exclude).encode(),
                   objects=source / 'objects', known=known)


def refs(repo: Path) -> dict[str, str]:
    return {ref: sha for sha, ref in (line.split(' ', 1) for line in
            run(repo, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/heads', 'refs/tags').decode().splitlines())}


def published(repo: Path, commit: str) -> bool:
    # Publication is permanent while its history remains reachable. This also
    # keeps a selected snapshot valid when a writer advances its ref mid-fetch.
    if not SHA.fullmatch(commit):
        return False
    if commit in refs(repo).values():
        return True
    try:
        return bool(run(repo, 'for-each-ref', '--contains=' + commit, '--count=1',
                        '--format=%(refname)', 'refs/heads', 'refs/tags').strip())
    except ValueError:
        return False


def _varint(data, at: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[at]
        at += 1
        value |= (byte & 0x7f) << shift
        shift += 7
        if not byte & 0x80:
            return value, at


def inspect_pack(data: bytes) -> None:
    """Refuse decompression bombs from declared sizes before Git inflates anything.

    Inflation here stops at each object's declared size, so the work is bounded by
    MAX_EXPANDED. Delta results count too: a few copy instructions can expand hugely.
    """
    view = memoryview(data)
    if len(data) < 32 or data[:4] != b'PACK' or int.from_bytes(data[4:8], 'big') not in (2, 3):
        raise ValueError('invalid Git exchange pack')
    count, at, end, total = int.from_bytes(data[8:12], 'big'), 12, len(data) - 20, 0
    try:
        for _ in range(count):
            byte = data[at]
            kind, size, shift, at = (byte >> 4) & 7, byte & 15, 4, at + 1
            while byte & 0x80:
                byte, at = data[at], at + 1
                size |= (byte & 0x7f) << shift
                shift += 7
            if kind == 6:
                at = _varint(data, at)[1]
            elif kind == 7:
                at += 20
            elif kind not in (1, 2, 3, 4):
                raise ValueError('invalid Git exchange pack object')
            total += size
            if size > MAX_OBJECT or total > MAX_EXPANDED:
                raise ValueError('Git exchange pack would expand beyond 128 MiB per object or 512 MiB in total')
            inflate, produced, head = zlib.decompressobj(), 0, b''
            while not inflate.eof:
                if at >= end:
                    raise ValueError('truncated Git exchange pack')
                piece = view[at:min(at + 4096, end)]
                at += len(piece)
                out = inflate.decompress(piece, size + 1 - produced)
                produced += len(out)
                head += out[:max(0, 32 - len(head))]
                if produced > size:
                    raise ValueError('Git exchange pack object expands beyond its declared size')
            at -= len(inflate.unused_data)
            if produced != size:
                raise ValueError('Git exchange pack object is shorter than declared')
            if kind in (6, 7):
                result = _varint(head, _varint(head, 0)[1])[0]
                total += result
                if result > MAX_OBJECT or total > MAX_EXPANDED:
                    raise ValueError('Git exchange pack would expand beyond 128 MiB per object or 512 MiB in total')
    except (IndexError, zlib.error):
        raise ValueError('invalid Git exchange pack') from None
    if at != end:
        raise ValueError('invalid Git exchange pack')


def import_pack(repo: Path, data: bytes) -> None:
    if not data or len(data) > LIMIT:
        raise ValueError('invalid Git exchange pack size')
    inspect_pack(data)
    run(repo, 'index-pack', '--stdin', '--strict', f'--max-input-size={LIMIT}', data=data)


def publish(repo: Path, data: bytes, ref: str, old: str, new: str, *, max_refs: int = MAX_REFS,
            max_store: int = MAX_STORE) -> None:
    """Callers below a maintainer pass smaller budgets, leaving integration/tag/restore headroom."""
    if not REF.fullmatch(ref) or not SHA.fullmatch(old) or not SHA.fullmatch(new) or new == ZERO:
        raise ValueError('invalid ref transition')
    current = refs(repo)
    if ref not in current and len(current) >= max_refs:
        raise ValueError(f'Git exchange is limited to {max_refs} published refs here')
    size = sum(p.stat().st_size for p in (repo / 'objects').rglob('*') if p.is_file())
    if size > max_store:
        raise ValueError(f'Git exchange object store exceeds {max_store >> 20} MiB here; ask an owner to review storage')
    if current.get(ref, ZERO) != old:
        raise ValueError('published ref changed; fetch and review again')
    with tempfile.TemporaryDirectory(prefix='acc-receive-') as scratch:
        stage = initialize(Path(scratch) / 'repo', 'quarantine')
        import_pack(stage, data)
        # Re-export only the approved tip's closure; unrelated uploaded objects
        # never enter the shared store, even after a successful publication.
        # Objects the store already reaches stay out: no history copy per publish.
        # Anything missing from both the submission and the store still fails here.
        selected = export(stage, new, known=repo)
        # The promoted subset has no more objects than this quarantined pack,
        # so its index/reverse-index cannot exceed this metadata reservation.
        # Compressed pack bytes alone undercount many-small-object submissions.
        overhead = sum(p.stat().st_size for p in (stage / 'objects').rglob('*')
                       if p.is_file() and p.suffix != '.pack')
        if size + len(selected) + overhead > max_store:
            raise ValueError(f'Git exchange object store exceeds {max_store >> 20} MiB here including pack indexes')
        if old != ZERO:
            run(stage, 'merge-base', '--is-ancestor', old, new, known=repo)
        if int.from_bytes(selected[8:12], 'big'):
            import_pack(repo, selected)
    run(repo, 'update-ref', ref, new, old)


def read_file(repo: Path, commit: str, path: str) -> bytes:
    if not published(repo, commit):
        raise ValueError('select a published commit')
    if not path or path.startswith('/') or any(p in ('', '.', '..') for p in path.split('/')) or '\x00' in path:
        raise ValueError('invalid published path')
    entries = run(repo, 'ls-tree', '-z', commit, '--', ':(literal)' + path).split(b'\0')
    for entry in entries:
        if not entry:
            continue
        meta, name = entry.split(b'\t', 1)
        mode, kind, sha = meta.split()
        if name.decode() == path and mode in (b'100644', b'100755') and kind == b'blob':
            return run(repo, 'cat-file', 'blob', sha.decode(), limit=1 << 20)
    raise ValueError('no regular published file')


def files(repo: Path, commit: str) -> list[str]:
    if not published(repo, commit):
        raise ValueError('select a published commit')
    paths = []
    for entry in run(repo, 'ls-tree', '-rz', commit).split(b'\0'):
        if entry:
            meta, name = entry.split(b'\t', 1)
            mode, kind, _ = meta.split()
            if kind == b'blob' and mode in (b'100644', b'100755'):
                paths.append(name.decode('utf-8'))
    if len(paths) > 5000:
        raise ValueError('published tree exceeds 5000 files; fetch it to browse locally')
    return paths


if __name__ == '__main__' and sys.argv[1:2] == ['--git-child']:
    # Apply limits in a fresh child, never preexec_fn in the multithreaded server.
    import resource
    resource.setrlimit(resource.RLIMIT_AS, (512 << 20, 512 << 20))
    resource.setrlimit(resource.RLIMIT_FSIZE, (64 << 20, 64 << 20))
    resource.setrlimit(resource.RLIMIT_CPU, (60, 60))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
