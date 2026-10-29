#!/usr/bin/python3 -I
"""Build-time checks of the assembled runtime tree, run in the assembled stage against /runtime (never in the image).

The final Containerfile stage has no RUN: Podman/buildah mounts its own /run/.containerenv (and /etc/hosts,
/etc/hostname, /etc/resolv.conf) during a RUN step and can record their removal as whiteouts at commit, deleting the
read-only rootfs's mount targets (qualification attempt 10: crun couldn't open rootfs/run/.containerenv). So the
checks that used to run there run here instead, on the runtime's own files, and must leave the tree exactly as it was.
"""
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path

PROVIDERS = ('claude', 'codex', 'copilot')
IMPORT = ('import importlib.util,sys; p=sys.argv[1]; s=importlib.util.spec_from_file_location("acc",p); '
          'm=importlib.util.module_from_spec(s); sys.modules["acc"]=m; exec(compile(open(p,"rb").read(),p,"exec"),m.__dict__); '
          'from acc.providers import get; [get(n) for n in sys.argv[2:]]')


def inside(root, path):
    """`path` resolved as if `root` were /: absolute symlinks stay in the tree (no chroot when building rootless)."""
    parts, resolved, hops = list(Path(path).relative_to('/').parts), root, 0
    while parts:
        candidate = resolved / parts.pop(0)
        if candidate.is_symlink():
            hops += 1
            if hops > 40:
                raise RuntimeError(f'symlink loop at {path}')
            target = Path(os.readlink(candidate))
            parts = list(target.parts[1:] if target.is_absolute() else target.parts) + parts
            resolved = root if target.is_absolute() else resolved
            continue
        resolved = candidate if candidate.name != '..' else resolved.parent if resolved != root else root
    return resolved


def codex_real(root):
    wrapper = inside(root, '/usr/local/bin/codex')
    real = wrapper.with_name(wrapper.name + '.acc-real')
    if not real.is_file():
        raise RuntimeError(f'no wrapped Codex binary beside {wrapper}')
    return real


def manifest(root):
    """Each entry's mode, owner and size, and a regular file's content hash or a link's target. Content, not mtime: a
    rewrite that keeps the size and resets the mtime still shows (about 1.5 GiB to hash, a few seconds per pass)."""
    out = {}
    for folder, dirs, files in os.walk(root):
        for name in ['.', *dirs, *files]:
            path = Path(folder) / name if name != '.' else Path(folder)
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                content = os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                with path.open('rb') as file:
                    content = hashlib.file_digest(file, 'sha256').hexdigest()
            else:
                content = None
            out[str(path.relative_to(root))] = (info.st_mode, info.st_uid, info.st_gid, info.st_size, content)
    return out


def run(argv, scratch, **extra):
    env = {'HOME': str(scratch / 'home'), 'XDG_CACHE_HOME': str(scratch / 'cache'), 'XDG_CONFIG_HOME': str(scratch / 'config'),
           'XDG_DATA_HOME': str(scratch / 'data'), 'XDG_STATE_HOME': str(scratch / 'state'), 'TMPDIR': str(scratch / 'tmp'),
           'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'LC_ALL': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1', **extra}
    result = subprocess.run([str(a) for a in argv], cwd=scratch, env=env, capture_output=True, text=True, timeout=120)
    if result.returncode:
        raise RuntimeError(f'{argv[0]} failed ({result.returncode}): {(result.stderr or result.stdout).strip()[-300:]}')
    return result.stdout.strip()


def check(root):
    before = manifest(root)
    scratch = Path(tempfile.mkdtemp(prefix='acc-rootfs-check-'))
    try:
        for name in ('home', 'cache', 'config', 'data', 'state', 'tmp'):
            (scratch / name).mkdir()
        # acc-session makes these on the session's /tmp tmpfs and refuses any outside /tmp/acc-state/: the same rule here.
        states = json.loads((root / 'opt/acc/state-directories.json').read_text())
        for state in states:
            if not state.startswith('/tmp/acc-state/') or '..' in Path(state).parts:
                raise RuntimeError(f'state directory outside /tmp/acc-state/: {state}')
            (scratch / 'tmp-mirror' / state.lstrip('/')).mkdir(parents=True, exist_ok=True)
        # The provider registry imports with the runtime's own Python and standard library.
        run([inside(root, '/usr/bin/python3'), '-I', '-B', '-c', IMPORT, root / 'opt/acc/src/acc/__init__.py', *PROVIDERS],
            scratch)
        versions = [run([inside(root, '/usr/local/bin/claude'), '--version'], scratch),
                    # the policy wrapper as the image runs it, and the runtime's own copy of the real binary
                    run([inside(root, '/usr/local/bin/codex'), '--version'], scratch),
                    run([codex_real(root), '--version'], scratch),
                    # /usr/local/bin/copilot is a wrapper naming absolute paths: run what it runs, from the tree
                    run([inside(root, '/usr/local/bin/node'), root / 'opt/acc/copilot/cli-main.js', '--version'], scratch,
                        COPILOT_AUTO_UPDATE='false'),
                    run([inside(root, '/usr/bin/git'), '--version'], scratch)]
    finally:
        shutil.rmtree(scratch)
    after = manifest(root)
    changed = sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))
    if changed:
        raise RuntimeError('the checks changed the runtime tree: ' + ', '.join(changed[:10]))
    return versions


if __name__ == '__main__':
    for line in check(Path(sys.argv[1])):
        print(line)
