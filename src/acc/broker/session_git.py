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


def operate(config, path, action, person='', message='', branch=None, default_branch=None, custom=None, repo=None, fingerprint=None, lease=None, expected_head=None):
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
        current = ops._git_ok(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD', env=env,
                              allowed=(0, 1) if action == 'status' else (0,)).strip()
        if action == 'status':
            preference = ops._git_ok(cwd, 'config', '--get', 'pull.rebase', env=env, allowed=(0, 1)).strip()
            return {'branch': current, 'commit': ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip(), 'base_mode': 'merge' if preference == 'false' else 'rebase', 'conflicts': ops._git_ok(cwd, 'diff', '--name-only', '--diff-filter=U', env=env).splitlines()[:200]}
        lock = ops._git_ok(cwd, 'rev-parse', '--path-format=absolute', '--git-path', 'index.lock', env=env).strip()
        if (root / lock).exists():
            raise ops.OpError('agent is busy, try again')
        push_options = f'--force-with-lease=refs/heads/{current}:{lease} ' if lease else ''
        commands = ['git add --all', 'git commit --cleanup=verbatim -F -'] if action == 'commit' else [f'git push {push_options}origin HEAD:refs/heads/{current}']
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
        if lease and (not re.fullmatch('[0-9a-f]{40}', lease) or commit != expected_head):
            raise ops.OpError('Rebased head changed; review again.')
        result = transport(cwd, ['push', '--porcelain', *([f'--force-with-lease=refs/heads/{current}:{lease}'] if lease else []), remote, f'HEAD:refs/heads/{current}'], commit=commit)
        return {'ok': result.returncode == 0, 'commit': commit, 'branch': current, 'remote_host': 'github.com',
                'file_count': len(set(changed_files.split('\0')) - {''}),
                'output': (result.stdout + result.stderr)[:16000]}


def transport(cwd, args, *, fetch=False, commit=None):
    env = ops._hardened_env(cwd)
    commit = commit or ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip()
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
        if fetch:
            env['GIT_OBJECT_DIRECTORY'] = objects
        result = subprocess.run([*argv, *args],
            env=env, text=True, capture_output=True, timeout=60)
    return result


def update_base(config, path, branch, repo, base, sha, head, mode, force_push_blocked, person='', custom=None):
    """Fetch a server-observed base through isolated transport, then stop on conflicts. Never pushes."""
    if (not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
            or not re.fullmatch('[0-9a-f]{40}', sha) or not re.fullmatch('[0-9a-f]{40}', head)
            or mode not in ('merge', 'rebase') or not isinstance(force_push_blocked, bool) or not person):
        raise ops.OpError('Invalid base update.')
    with workspace(config, path):
        cwd = ops._under_roots(config, path)
        if ops.containers.is_container_checkout(cwd):
            raise ops.OpError('Container checkouts permit read-only Git actions.')
        env = ops._hardened_env(cwd)
        env.update(GIT_EDITOR='true', GIT_SEQUENCE_EDITOR='true')
        for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'index.lock'):
            found = ops._git_ok(cwd, 'rev-parse', '--git-path', state, env=env).strip()
            if (cwd / found).exists():
                raise ops.OpError('A Git operation is already in progress; finish or abort it first.')
        current = ops._git_ok(cwd, 'symbolic-ref', '--short', 'HEAD', env=env).strip()
        if current != branch or current in {base, 'main', 'master'}:
            raise ops.OpError('Cannot update a changed or default branch.')
        if ops._git_ok(cwd, 'status', '--porcelain', '--untracked-files=all', env=env):
            raise ops.OpError('Commit or discard changes before updating the base.')
        # The project checkout's Git preference: false selects merge, otherwise rebase.
        preference = ops._git_ok(cwd, 'config', '--get', 'pull.rebase', env=env, allowed=(0, 1)).strip()
        configured = 'merge' if force_push_blocked or preference == 'false' else 'rebase'
        if mode != configured:
            raise ops.OpError('Base update mode changed. Refresh the checklist and confirm again.')
        command = f'git {mode} {sha}'
        for rule in custom or []:
            if re.search(rule['pattern'], command):
                raise ops.OpError(rule.get('reason') or 'Blocked by a project guardrail.')
        before = ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip()
        require_remote_head(cwd, head, before, env)
        fetched = transport(cwd, ['fetch', '--no-tags', '--no-recurse-submodules',
                                   f'https://github.com/{repo}.git', sha], fetch=True)
        if fetched.returncode:
            raise ops.OpError('Could not fetch the base branch from the project repository.')
        if (ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip() != before
                or ops._git_ok(cwd, 'symbolic-ref', '--short', 'HEAD', env=env).strip() != branch
                or ops._git_ok(cwd, 'status', '--porcelain', '--untracked-files=all', env=env)):
            raise ops.OpError('The worktree changed while fetching; review it and try again.')
        args = (['rebase', '--no-autostash', '--no-update-refs', '--no-autosquash', sha] if mode == 'rebase'
                else ['merge', '--no-edit', '--no-autostash', '--no-verify', sha])
        try:
            result = ops._git(cwd, *args, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            raise ops.OpError('Base update timed out. Inspect the worktree before retrying; it may need an abort.') from None
        conflicts = ops._git_ok(cwd, 'diff', '--name-only', '--diff-filter=U', env=ops._hardened_env(cwd)).splitlines()
        return {'ok': result.returncode == 0, 'mode': mode, 'conflicts': conflicts[:200],
                'before': before, 'commit': ops._git_ok(cwd, 'rev-parse', 'HEAD', env=ops._hardened_env(cwd)).strip(),
                'output': (result.stdout + result.stderr)[-8000:]}


def require_remote_head(cwd, head, before, env):
    if head != before and (ops._git(cwd, 'cat-file', '-e', head + '^{commit}', env=env).returncode
            or ops._git(cwd, 'merge-base', '--is-ancestor', head, before, env=env).returncode):
        raise ops.OpError('Pull the remote changes first: the branch on GitHub has commits this worktree does not contain.')


def completed_rebase(config, path, branch, base, head, expected_head, validated_lease=None):
    """Read-only admission for a new person-issued push consent; never infer authority from rebase files."""
    if any(not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha) for sha in (base, head, expected_head)):
        raise ops.OpError('Invalid completed rebase revision.')
    with workspace(config, path):
        cwd = ops._under_roots(config, path)
        if ops.containers.is_container_checkout(cwd):
            raise ops.OpError('Container checkouts permit read-only Git actions.')
        env = ops._hardened_env(cwd)
        for state in ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'rebase-merge', 'rebase-apply', 'index.lock'):
            found = ops._git_ok(cwd, 'rev-parse', '--git-path', state, env=env).strip()
            if (cwd / found).exists():
                raise ops.OpError('Finish or abort the rebase/merge first.')
        current = ops._git_ok(cwd, 'symbolic-ref', '--quiet', '--short', 'HEAD', env=env, allowed=(0, 1)).strip()
        if not current or current != branch or current in ('main', 'master'):
            raise ops.OpError('The session branch changed or HEAD is detached; review it first.')
        commit = ops._git_ok(cwd, 'rev-parse', 'HEAD', env=env).strip()
        if commit != expected_head:
            raise ops.OpError('Rebased head changed; refresh the checklist and review again.')
        if ops._git_ok(cwd, 'status', '--porcelain', '--untracked-files=all', env=env):
            raise ops.OpError('Commit or discard changes before pushing the completed rebase.')
        if validated_lease is not None:
            if validated_lease != head:
                raise ops.OpError('The branch changed on GitHub since the rebase started; review it and start again.')
        else:
            try:
                require_remote_head(cwd, head, commit, env)
            except ops.OpError:
                raise ops.OpError('The branch changed on GitHub since the rebase started, or no matching server record remains; review it and start again.') from None
        if ops._git(cwd, 'merge-base', '--is-ancestor', base, commit, env=env).returncode:
            raise ops.OpError('The completed branch does not contain the current base; update it first.')
        if ops._git(cwd, '-c', 'core.whitespace=-blank-at-eol,-blank-at-eof,-space-before-tab,-indent-with-non-tab,-tab-in-indent,-cr-at-eol',
                    'diff', '--check', '--no-ext-diff', '--no-textconv', base + '..' + commit, env=env).returncode:
            raise ops.OpError('The completed rebase contains leftover conflict markers; resolve them before pushing.')
        return {'commit': commit, 'branch': current}



def rebase_summary(config, path, base, before, head):
    """Bounded read-only confirmation evidence, using server-observed revision IDs only."""
    if any(not isinstance(sha, str) or not re.fullmatch('[0-9a-f]{40}', sha) for sha in (base, before, head)):
        raise ops.OpError('Invalid rebase summary revision.')
    with workspace(config, path):
        cwd = ops._under_roots(config, path)
        env = ops._hardened_env(cwd)
        def exists(sha):
            return ops._git(cwd, 'cat-file', '-e', sha + '^{commit}', env=env).returncode == 0
        def count(sha):
            return int(ops._git_ok(cwd, 'rev-list', '--count', base + '..' + sha, env=env))
        has_base = exists(base)
        before_count = count(before) if has_base and exists(before) else None
        after_count = count(head) if has_base and exists(head) else None
        subjects = (ops._git_ok(cwd, 'log', '--no-show-signature', '--no-notes', '--no-decorate', '--format=%s', '--reverse', '--max-count=20',
                               base + '..' + head, env=env).rstrip('\n').split('\n') if after_count else [])
        return {'before_count': before_count, 'after_count': after_count,
                'subjects': [subject[:240] for subject in subjects[:20]],
                'omitted': max(0, (after_count or 0) - min(len(subjects), 20))}
