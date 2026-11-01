"""Run ONLY in a disposable root VM; no account/group/database or service changes.

PYTHONPATH=src .venv/bin/python -m pytest -q -n 1 --disposable-root-vm tests/test_folder_root_qualification.py
"""
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

pytestmark = pytest.mark.usefixtures('disposable_root_vm')


class DisposableHost:
    """The VM fixture owns no broker and never changes a real service."""
    @contextmanager
    def stopped(self, account):
        from acc.folder_host import LinuxFolderHost
        assert not LinuxFolderHost._processes(account['uid'])
        yield

    def verify(self, path, records, account, write, group=None):
        from acc.folder_host import LinuxFolderHost
        return LinuxFolderHost(host_root=self.root).verify_identity(path, records, account['uid'], 65520, [], write)


def as_agent(path, write=False):
    read, send = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        try:
            os.setgroups([])
            os.setgid(65520)
            os.setuid(65520)
            with open(path, 'a' if write else 'r') as file:
                if write:
                    file.write('agent')
                else:
                    file.read()
            result = True
        except PermissionError:
            result = False
        os.write(send, json.dumps(result).encode())
        os._exit(0)
    os.close(send)
    try:
        result = json.loads(os.read(read, 100))
        _, status = os.waitpid(pid, 0)
        assert status == 0
        return result
    finally:
        os.close(read)


@pytest.mark.parametrize('write', [False, True])
def test_real_separate_uid_named_acl_and_default_inheritance(write):
    from acc.folder_worker import FolderWorker
    host = Path(tempfile.mkdtemp(prefix='acc-folder-qualification-'))
    os.chmod(host, 0o755)
    try:
        repo = host / 'home/alex/src/repo'
        repo.mkdir(parents=True, mode=0o700)
        (host / 'state').mkdir(mode=0o700)
        (host / 'home/alex').chmod(0o700)
        (host / 'home/alex/src').chmod(0o700)
        path = repo / 'human.txt'
        path.write_text('human')
        path.chmod(0o600)
        policy = {'machine_id': 'disposable', 'qualified': True, 'accounts': {'alex-agent': {
            'uid': 65520, 'owner_uid': 0, 'homes': ['/home/alex'], 'protected': ['/srv/acc'],
            'roots': ['/home/alex-agent/workspaces']}}}
        request = {'operation_id': 'b' * 32, 'machine_id': 'disposable', 'account': 'alex-agent', 'revision': 0}
        assert not as_agent(path)
        lifecycle = DisposableHost()
        lifecycle.root = host
        with FolderWorker(host / 'state', policy, host_root=host, lifecycle=lifecycle) as worker:
            preview = worker.handle(request | {'action': 'preview', 'path': '/home/alex/src/repo', 'method': 'named_acl', 'write': write})
            worker.handle(request | {'action': 'apply', 'digest': preview['digest']})
            assert as_agent(path)
            assert as_agent(path, write=True) is write
            inherited = repo / 'inherited.txt'
            inherited.write_text('new child')
            assert as_agent(inherited)
            restrictive = repo / 'restricted.txt'
            fd = os.open(restrictive, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
            assert not as_agent(restrictive)  # default ACLs never promise access forever
            result = worker.handle(request | {'action': 'undo', 'digest': preview['digest'], 'revision': 1})
            assert result['state'] in ('undone', 'repair')
            assert not as_agent(path)
            assert path.exists() and inherited.exists() and restrictive.exists()
    finally:
        shutil.rmtree(host)


@pytest.mark.parametrize('scenario', ['protected_alias', 'nested_crossing', 'nested_inside', 'rebind_after_preview'])
def test_real_private_mount_namespace_refuses_policy_aliases(scenario):
    """Only this child gets a mount namespace; no host namespace mount changes."""
    import ctypes
    import select
    import signal

    from acc.folder_policy import FolderError
    from acc.folder_worker import FolderWorker
    host = Path(tempfile.mkdtemp(prefix='acc-folder-mount-vm-'))
    repo = host / 'home/alex/src/repo'
    repo.mkdir(parents=True)
    private = host / 'srv/acc'
    private.mkdir(parents=True)
    (host / 'state').mkdir(mode=0o700)
    read, send = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read)
        try:
            libc = ctypes.CDLL(None, use_errno=True)
            assert libc.unshare(0x00020000) == 0, f'unshare failed: errno {ctypes.get_errno()}'
            assert libc.mount(None, b'/', None, 16384 | (1 << 18), None) == 0, 'private mount namespace required'
            def bind(source, destination):
                assert libc.mount(os.fsencode(source), os.fsencode(destination), None, 4096, None) == 0, f'bind failed: errno {ctypes.get_errno()}'
            policy = {'machine_id': 'vm', 'policy_qualified': True, 'accounts': {'alex-agent': {
                'uid': 65520, 'owner_uid': 0, 'homes': ['/home/alex'], 'protected': ['/srv/acc'],
                'roots': ['/home/alex-agent/workspaces']}}}
            base = {'operation_id': 'c' * 32, 'machine_id': 'vm', 'account': 'alex-agent', 'revision': 0}
            with FolderWorker(host / 'state', policy, host_root=host,
                              reload=lambda *args: pytest.fail('Unsafe mount must not be published')) as worker:
                if scenario == 'rebind_after_preview':
                    preview = worker.handle(base | {'action': 'policy_preview', 'roots': [
                        '/home/alex-agent/workspaces', '/home/alex/src/repo'], 'allowed_folders': False})
                    bind(private, repo)
                    with pytest.raises(FolderError):
                        worker.handle(base | {'action': 'policy_apply', 'digest': preview['digest']})
                else:
                    if scenario == 'nested_inside':
                        nested = repo / 'nested'
                        nested.mkdir()
                        bind(private, nested)
                    else:
                        bind(private if scenario == 'protected_alias' else repo.parent, repo if scenario == 'protected_alias' else repo.parent)
                    for enabled in (False, True):
                        with pytest.raises(FolderError):
                            worker.handle(base | {'action': 'policy_preview', 'roots': [
                                '/home/alex-agent/workspaces', '/home/alex/src/repo'], 'allowed_folders': enabled})
            os.write(send, b'passed')
        except BaseException as error:
            os.write(send, (type(error).__name__ + ': ' + str(error))[:2000].encode())
        os._exit(0)
    os.close(send)
    try:
        ready, _, _ = select.select([read], [], [], 20)
        if not ready:
            os.kill(pid, signal.SIGKILL)
        result = os.read(read, 4096) if ready else b'child timeout'
        os.waitpid(pid, 0)
        assert result == b'passed', result.decode()
    finally:
        os.close(read)
        shutil.rmtree(host)
