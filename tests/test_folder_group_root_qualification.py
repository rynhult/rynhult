"""Disposable ROOT VM ONLY: creates/removes only its own temporary Unix identities.

PYTHONPATH=src .venv/bin/python -m pytest -q -n 1 --disposable-root-vm tests/test_folder_group_root_qualification.py
Never run on the shared host or live installation. No service is started/stopped.
"""
import base64
import grp
import json
import os
import pwd
import re
import secrets
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from acc.folder_host import LinuxFolderHost
from acc.folder_policy import FolderError
from acc.folder_worker import FolderWorker

pytestmark = pytest.mark.usefixtures('disposable_root_vm')


def command(*argv):
    subprocess.run(argv, check=True, capture_output=True, timeout=10)


def confirmed_group(host, operation):
    """Recover cleanup after apply failed, but never guess ownership of a group."""
    if not isinstance(operation, str) or not re.fullmatch('[0-9a-f]{32}', operation):
        return None
    try:
        record = json.loads((host / 'state' / (operation + '.json')).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get('request'), dict) or not isinstance(record.get('group'), dict):
        return None
    group = record['group']
    expected = 'ryn-repo-' + operation[:16]
    if (record['request'].get('operation_id') == operation and group.get('created') is True
            and group.get('creating') is not True and group.get('name') == expected):
        return expected
    return None


@pytest.fixture
def identities():
    suffix = secrets.token_hex(4)
    names = ['accqh' + suffix, 'accqa' + suffix]
    made = []
    try:
        for name in names:
            used = {p.pw_uid for p in pwd.getpwall()}
            uid = next(i for i in range(61000, 62000) if i not in used)
            command('/usr/sbin/useradd', '--no-create-home', '--no-user-group', '--uid', str(uid),
                '--gid', '65534', '--shell', '/usr/sbin/nologin', '--', name)
            made.append(name)
        yield [pwd.getpwnam(n) for n in names]
    finally:
        for name in reversed(made):
            command('/usr/sbin/userdel', '--', name)  # no -r; never deletes a home


@pytest.mark.parametrize('writable_original', [False, True])
@pytest.mark.parametrize('write', [False, True])
def test_real_group_membership_setgid_permissions_and_undo(identities, write, writable_original):
    human, agent = identities
    # A fixture-only numeric old GID, deliberately absent from both users'
    # primary/supplementary groups and from the system group database.
    used = {g.gr_gid for g in grp.getgrall()}
    old_gid = next(i for i in range(64999, 64000, -1) if i not in used)
    host = Path(tempfile.mkdtemp(prefix='acc-folder-group-vm-'))
    os.chmod(host, 0o755)
    created_group, operation = None, None
    try:
        repo = host / 'home/alex/src/repo'
        repo.mkdir(parents=True)
        for directory in [repo.parents[1], repo.parent, repo]:
            os.chown(directory, human.pw_uid, human.pw_gid)
            directory.chmod(0o700)
        path = repo / 'human.txt'
        path.write_text('human contents')
        os.chown(path, human.pw_uid, human.pw_gid)
        path.chmod(0o660 if writable_original else 0o600)
        from acc.folder_acl import ACL, snapshot
        os.chown(repo, human.pw_uid, old_gid)
        os.chown(path, human.pw_uid, old_gid)
        repo.chmod(0o770 if writable_original else 0o700)
        executable = repo / 'executable'
        executable.write_text('fixture bytes, never executed')
        os.chown(executable, human.pw_uid, old_gid)
        executable.chmod(0o770 if writable_original else 0o700)
        if writable_original:
            os.setxattr(repo, 'system.posix_acl_default', ACL([(1, 7, -1), (4, 7, -1), (32, 0, -1)]).encode())
        def metadata(path):
            fd = os.open(path, os.O_RDONLY)
            try:
                return snapshot(fd)
            finally:
                os.close(fd)
        originals = {p.name: metadata(p) for p in (repo, path, executable)}
        (host / 'state').mkdir(mode=0o700)
        class Host(LinuxFolderHost):
            @contextmanager
            def stopped(self, account):
                assert not self._processes(account['uid'])
                yield
        lifecycle = Host(host_root=host)
        account = {'uid': agent.pw_uid, 'owner_uid': human.pw_uid, 'owner_name': human.pw_name,
            'agent_name': agent.pw_name, 'homes': ['/home/alex'], 'protected': ['/srv/acc'],
            'roots': ['/home/alex-agent/workspaces'], 'groups': [agent.pw_gid]}
        mapping = {'machine_id': 'vm', 'qualified': True, 'shared_group_qualified': True, 'accounts': {agent.pw_name: account}}
        base = {'machine_id': 'vm', 'operation_id': secrets.token_hex(16), 'account': agent.pw_name, 'revision': 0}
        operation = base['operation_id']
        with FolderWorker(host / 'state', mapping, host_root=host, lifecycle=lifecycle) as worker:
            preview = worker.handle(base | {'action': 'preview', 'path': '/home/alex/src/repo', 'method': 'shared_group', 'write': write})
            result = worker.handle(base | {'action': 'apply', 'digest': preview['digest']})
            created_group = preview['group']['name']
            assert result['verification']['read'] is True
            assert result['verification']['write'] is write
            assert set(grp.getgrnam(created_group).gr_mem) == {human.pw_name, agent.pw_name}
            assert repo.stat().st_mode & 0o2000
            assert path.stat().st_uid == human.pw_uid
            assert not path.stat().st_mode & 0o111
            assert executable.stat().st_mode & 0o100
            original_records = [{'path': 'human.txt', 'before': {'identity': originals[path.name]['identity']}}]
            if writable_original:
                # The former group still has write, but the new read-only group
                # must not. The verifier must refuse unexpected positive write.
                assert lifecycle.verify_identity('/home/alex/src/repo', original_records,
                    agent.pw_uid, agent.pw_gid, [old_gid], True)['write']
                with pytest.raises(FolderError, match='verification'):
                    lifecycle.verify_identity('/home/alex/src/repo', original_records,
                        agent.pw_uid, agent.pw_gid, [old_gid], False)
            current = metadata(repo)
            if not write:
                assert not ACL.decode(base64.b64decode(current['access'])).effective(4, -1) & 2
                assert not ACL.decode(base64.b64decode(current['default'])).effective(4, -1) & 2
            # Root constructs only fixture bytes, no repository program is run.
            child = repo / 'new.txt'
            child.write_text('later')
            child_stat = child.stat()
            records = [{'path': 'new.txt', 'before': {'identity': [child_stat.st_dev, child_stat.st_ino]}}]
            groups = os.getgrouplist(agent.pw_name, agent.pw_gid)
            assert lifecycle.verify_identity('/home/alex/src/repo', records, agent.pw_uid, agent.pw_gid, groups, write)['read']
            if write:
                assert lifecycle.verify_identity('/home/alex/src/repo', records, agent.pw_uid, agent.pw_gid, groups, True)['write']
            else:
                with pytest.raises(FolderError):
                    lifecycle.verify_identity('/home/alex/src/repo', records, agent.pw_uid, agent.pw_gid, groups, True)
                child.unlink()  # fixture-created file only; product Undo never deletes files
            result = worker.handle(base | {'action': 'undo', 'digest': preview['digest'], 'revision': 1})
            assert result['state'] == ('repair' if write else 'undone')
            if not write:
                assert grp.getgrnam(created_group).gr_mem == []
            else:
                assert child.exists()  # unknown descendant requires repair, never deletion
            assert path.read_text() == 'human contents'
            assert path.stat().st_gid == old_gid
            assert path.stat().st_mode & 0o777 == (0o660 if writable_original else 0o600)
            assert metadata(path) == originals[path.name]
            assert metadata(executable) == originals[executable.name]
            assert metadata(repo) == originals[repo.name]
            assert not repo.stat().st_mode & 0o2000
    finally:
        # Apply can fail after creating a group. Trust only its confirmed private
        # creation record, not a matching pre-existing name or an uncertain intent.
        created_group = created_group or confirmed_group(host, operation)
        # Remove only this fixture tree, then its helper-created purpose group.
        shutil.rmtree(host)
        if created_group:
            command('/usr/sbin/groupdel', '--', created_group)
