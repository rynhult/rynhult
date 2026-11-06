"""Person-requested Git mutations, executed by the session account's broker."""
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from . import ops
from .files import protected, workspace


def diff_fingerprint(cwd):
    """Full content identity, including binary/untracked files beyond the display limit."""
    env = ops._hardened_env(cwd)
    cwd = Path(ops._git_ok(cwd, 'rev-parse', '--show-toplevel', env=env).strip())
    digest = hashlib.sha256()
    for args in [('rev-parse', '--verify', '--quiet', 'HEAD'), ('status', '--porcelain=v1', '-z', '--untracked-files=all'),
                 ('diff', '--cached', '--no-ext-diff', '--no-textconv', '--binary')]:
        output = ops._git_ok(cwd, *args, env=env, allowed=(0, 1) if args[0] == 'rev-parse' else (0,)).encode()
        digest.update(len(output).to_bytes(8, 'big') + output)
    paths = ops._git_ok(cwd, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', env=env)
    for name in sorted(set(paths.split('\0')) - {''}):
        target = cwd / name
        digest.update(name.encode() + b'\0')
        if target.is_symlink():
            digest.update(b'link\0' + hashlib.sha256(os.readlink(target).encode()).digest())
        elif target.is_file():
            digest.update(b'file\0' + str(target.stat().st_mode).encode() + b'\0')
            content = hashlib.sha256()
            with target.open('rb') as stream:
                for chunk in iter(lambda: stream.read(65536), b''):
                    content.update(chunk)
            digest.update(content.digest())
        else:
            digest.update(b'missing')
    return digest.hexdigest()


def operate(config, path, action, person='', message='', branch=None, default_branch=None, custom=None, repo=None, fingerprint=None):
    if action not in ('status', 'commit', 'push'):
        raise ops.OpError('Unknown Git action.')
    with workspace(config, path) as (root, fd):
        cwd = ops._under_roots(config, path)
        if action != 'status' and ops.containers.is_container_checkout(cwd):
            raise ops.OpError('Container checkouts permit read-only Git actions.')
        env = ops._hardened_env(cwd)
        requested_cwd = cwd
        cwd = ops._under_roots(config, ops._git_ok(cwd, 'rev-parse', '--show-toplevel', env=env).strip())
        root = cwd
        current = ops._git_ok(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD', env=env).strip()
        if action == 'status':
            return {'branch': current}
        lock = ops._git_ok(cwd, 'rev-parse', '--path-format=absolute', '--git-path', 'index.lock', env=env).strip()
        if (root / lock).exists():
            raise ops.OpError('agent is busy, try again')
        commands = ['git add --all', 'git commit --cleanup=verbatim -F -'] if action == 'commit' else [f'git push origin HEAD:refs/heads/{current}']
        for rule in custom or []:
            if any(re.search(rule['pattern'], command) for command in commands):
                raise ops.OpError(rule.get('reason') or 'Blocked by a project guardrail.')
        if action == 'commit':
            if requested_cwd != cwd:
                raise ops.OpError('Commit all requires Changes from the repository root.')
            if not message.strip() or len(message) > 2000 or '\0' in message or not re.fullmatch(r'(?:[A-Za-z0-9_.-]+|@api)', person):
                raise ops.OpError('Enter a commit message and a valid person.')
            paths = ops._git_ok(cwd, 'ls-files', '-z', '--cached', '--others', '--exclude-standard', env=env).split('\0')
            if any(p and protected(config, root, p) for p in paths):
                raise ops.OpError('Protected files cannot be committed from Changes.')
            head = ops._git_ok(cwd, 'rev-parse', '--verify', '--quiet', 'HEAD', env=env, allowed=(0, 1)).strip()
            changed = ops._git_ok(cwd, 'diff', '--name-only', '-z', head or ops.EMPTY_TREE, env=env)
            untracked = ops._git_ok(cwd, 'ls-files', '--others', '--exclude-standard', '-z', env=env)
            file_count = len(set((changed + untracked).split('\0')) - {''})
            message = '\n'.join(line for line in message.splitlines()
                                if not re.match(r'^\s*Rynhult-Person\s*:', line, re.I)).rstrip()
            try:
                lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError:
                raise ops.OpError('agent is busy, try again') from None
            os.close(lock_fd)
            try:
                if not fingerprint or diff_fingerprint(cwd) != fingerprint:
                    raise ops.OpError('changes moved, review again')
                with tempfile.TemporaryDirectory(prefix='acc-commit-') as temporary:
                    index = Path(temporary) / 'index'
                    original = Path(lock.removesuffix('.lock'))
                    if original.exists():
                        shutil.copyfile(original, index)
                    env = {**env, 'GIT_INDEX_FILE': str(index)}
                    for args in [('add', '--all'), ('commit', '--cleanup=verbatim', '-F', '-')]:
                        if args[0] == 'commit' and diff_fingerprint(cwd) != fingerprint:
                            raise ops.OpError('changes moved, review again')
                        result = subprocess.run([*ops._git_argv(cwd), *args], env=env,
                            input=(message + '\n\nRynhult-Person: ' + person + '\n') if args[0] == 'commit' else None,
                            text=True, capture_output=True, timeout=30)
                        if result.returncode:
                            raise ops.OpError(result.stderr[:2000])
                    shutil.copyfile(index, lock)
                    os.replace(lock, original)
                return {'commit': ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip(), 'branch': current,
                        'file_count': file_count, 'output': result.stdout}
            finally:
                Path(lock).unlink(missing_ok=True)
        if current != branch or not default_branch or current in {default_branch, 'main', 'master'}:
            raise ops.OpError('Cannot push a detached, changed, default or protected branch.')
        remote = ops._git_ok(cwd, 'remote', 'get-url', '--push', '--all', 'origin', env=env).strip()
        if not repo or remote not in {f'https://github.com/{repo}', f'https://github.com/{repo}.git',
                                      f'git@github.com:{repo}', f'git@github.com:{repo}.git',
                                      f'ssh://git@github.com/{repo}', f'ssh://git@github.com/{repo}.git'}:
            raise ops.OpError('Origin must push to the project GitHub repository.')
        # A fresh bare transport repository never reads checkout config (including includes,
        # URL rewrites, remote options, proxies or helpers), even if it changes during push.
        commit = ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip()
        changed_files = ops._git_ok(cwd, 'diff-tree', '--root', '--no-commit-id', '--name-only', '-r', '-z', commit, env=env)
        objects = ops._git_ok(cwd, 'rev-parse', '--path-format=absolute', '--git-path', 'objects', env=env).strip()
        with tempfile.TemporaryDirectory(prefix='acc-push-') as temporary:
            transport = Path(temporary)
            (transport / 'objects').mkdir()
            (transport / 'refs').mkdir()
            (transport / 'HEAD').write_text(commit + '\n')
            (transport / 'config').write_text('[core]\n bare = true\n')
            # Only account-global credential settings are carried over; local credential
            # helpers never participate. Account credential stores and SSH agent still work.
            credentials = subprocess.run(['git', 'config', '--global', '--includes', '--null', '--list'],
                env={k: v for k, v in os.environ.items() if not k.startswith('GIT_')},
                text=True, capture_output=True, timeout=30)
            settings = [(key, value) for record in credentials.stdout.split('\0') if '\n' in record
                        for key, value in [record.split('\n', 1)] if key.lower().startswith('credential.')]
            if any('=' in key for key, _ in settings):
                raise ops.OpError('Credential configuration keys containing = are not supported.')
            # Reset helpers first, then append the account's configured helpers.
            settings = [('core.sshCommand', 'ssh'), ('credential.helper', ''), *settings]
            settings += [('http.proxy', ''), ('http.sslVerify', 'true'), ('push.gpgSign', 'false'),
                         ('push.recurseSubmodules', 'no'), ('push.followTags', 'false')]
            argv = ['git', '--git-dir=' + temporary]
            for key, value in settings:
                argv += ['-c', key + '=' + value]
            env.update(GIT_ALLOW_PROTOCOL='https:ssh', GIT_ALTERNATE_OBJECT_DIRECTORIES=objects)
            result = subprocess.run([*argv, 'push', '--porcelain', remote, f'HEAD:refs/heads/{current}'],
                env=env, text=True, capture_output=True, timeout=60)
        return {'ok': result.returncode == 0, 'commit': commit, 'branch': current, 'remote_host': 'github.com',
                'file_count': len(set(changed_files.split('\0')) - {''}),
                'output': (result.stdout + result.stderr)[:16000]}
