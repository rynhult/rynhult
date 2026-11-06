"""Folder provisioning refuses unsafe paths before invoking restic."""

import pytest

from acc.backup_server import Refused
from acc.server.local_backups import installed_backup, repository_path


@pytest.mark.parametrize('value', ['relative', '/tmp/../tmp', '/does-not-exist-rynhult', '/tmp/\x00'])
def test_invalid_folder(tmp_path, value):
    with pytest.raises(Refused):
        repository_path(value, tmp_path, True)


def test_folder_boundaries(tmp_path, monkeypatch):
    monkeypatch.setattr("acc.server.local_backups.refuse_volatile", lambda path: None)
    data = tmp_path / 'installation' / 'data'
    data.mkdir(parents=True)
    folder = tmp_path / 'usb'
    folder.mkdir()
    with pytest.raises(Refused, match='same disk'):
        repository_path(str(folder), data, False)
    assert repository_path(str(folder), data, True).parent == folder
    with pytest.raises(Refused, match='installation'):
        repository_path(str(data), data, True)
    with pytest.raises(Refused, match='installation'):
        repository_path('//' + str(data).lstrip('/'), data, True)
    link = tmp_path / 'link'
    link.symlink_to(folder, target_is_directory=True)
    for path in (link, link / 'nested'):
        with pytest.raises(Refused, match='symlink'):
            repository_path(str(path), data, True)
    file = folder / 'file'
    file.touch()
    with pytest.raises(Refused, match='exist'):
        repository_path(str(file), data, True)


def test_timer_properties(tmp_path):
    import json
    path = tmp_path / 'hoststate.json'
    assert installed_backup(path)['last_run'] is None
    status = {'repository': '/var/backups/acc', 'last_run': 1234, 'next_run': 5678,
              'bytes': 4096, 'state': 'success'}
    path.write_text(json.dumps({'installed_backup': status}))
    assert installed_backup(path) == status


async def test_folder_command_owner_only(api, env, member_accounts, monkeypatch):
    from test_auth import cookie_client, do_setup
    from test_users import browser, csrf, join

    from acc.backup_server import Service
    await do_setup(api, env)
    owner = cookie_client(api)
    def unexpected(*args, **kwargs):
        pytest.fail('HTTP must never initialize a repository')
    monkeypatch.setattr(Service, 'add_local_target', unexpected)
    route = '/api/host/backups/local-targets'
    body = {'confirmed': True, 'folder': '/mnt/owner disk', 'same_disk': True}
    response = await owner.post(route, json=body, headers=csrf(owner))
    assert response.status_code == 200, response.text
    assert response.json() == {'command': "sudo rynhult-admin backup add-folder --same-disk -- '/mnt/owner disk'"}
    assert (await owner.post(route, json={**body, 'folder': '/tmp/backup'}, headers=csrf(owner))).status_code == 409
    link = (await owner.post('/api/users/invites', json={'account': 'dana-agent'}, headers=csrf(owner))).json()['link']
    async with browser(api) as member:
        await join(member, link, 'alex')
        assert (await member.post(route, json=body, headers=csrf(member))).status_code == 403


def test_collector_timer_and_size_cache(monkeypatch):
    import runpy
    from pathlib import Path

    collector = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'deploy/hoststate/acc-hoststate'))
    function = collector['installed_backup']
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        if args[0] == 'du':
            return 0, '12345\t/var/backups/acc\n'
        if '--property=Result' in args:
            return 0, 'success\n'
        return 0, 'Wed 2026-11-04 04:30:00 UTC\n'
    monkeypatch.setitem(function.__globals__, 'run', run)
    result = function({})
    assert result['last_run'] == 1793766600
    assert result['next_run'] == 1793766600
    assert result['bytes'] == 12345
    assert result['state'] == 'success'
    assert function({'installed_backup': result})['bytes'] == 12345
    assert len([call for call in calls if call[0] == 'du']) == 1


@pytest.mark.parametrize('folder', ['/tmp', '/var/tmp', '/dev/shm', '/run'])
def test_volatile_paths(folder, tmp_path):
    with pytest.raises(Refused):
        repository_path(folder, tmp_path, True)


@pytest.mark.parametrize('magic', [0x01021994, 0x858458F6])
def test_tmpfs_magic(tmp_path, monkeypatch, magic):
    import ctypes

    from acc.server.local_backups import refuse_volatile
    class Libc:
        def statfs(self, path, buf):
            ctypes.cast(buf, ctypes.POINTER(ctypes.c_long))[0] = magic
            return 0
    monkeypatch.setattr(ctypes, 'CDLL', lambda *a, **k: Libc())
    with pytest.raises(Refused, match='volatile'):
        refuse_volatile(__import__('pathlib').Path('/mnt/disk'))


@pytest.mark.parametrize('folder', ['/etc', '/usr', '/proc', '/home', '/'])
def test_system_paths(folder, tmp_path):
    with pytest.raises(Refused):
        repository_path(folder, tmp_path, True)


def test_admin_provision_fake_systemd(tmp_path, monkeypatch):
    import json
    import os
    from types import SimpleNamespace

    from acc.admin_backups import add_folder
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'chown', lambda *a, **kw: None)
    monkeypatch.setattr('acc.admin_backups.trusted_directory', lambda *a: None)
    # Model persistent storage explicitly; normal /tmp remains refused in production.
    monkeypatch.setattr('acc.server.local_backups.refuse_volatile', lambda path: None)
    data = tmp_path / 'install' / 'data'
    data.mkdir(parents=True)
    folder = tmp_path / 'disk with % specifier'
    folder.mkdir()
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(stdout=json.dumps({'state': 'complete'} if '--wait-job' in argv else {'target_id': 'a'*32, 'first_job': {'id': 'b'*32, 'state':'queued'}}))
    kwargs = dict(env_file=tmp_path / 'accd', data=data, dropins=tmp_path / 'units', run=run, account=SimpleNamespace(pw_uid=1000, pw_gid=1000))
    with pytest.raises(Refused, match='same disk'):
        add_folder(str(folder), **kwargs)
    assert calls == []
    result = add_folder(str(folder), True, **kwargs)
    assert result['first_job']['state'] == 'complete'
    assert calls[0] == ['systemctl', 'daemon-reload']
    assert calls[2][:5] == ['/usr/sbin/runuser', '-u', 'acc', '--', __import__('sys').executable]
    assert calls[1] == ['systemctl', 'restart', 'accd.service']
    config = next((tmp_path / 'units').glob('*.conf')).read_text()
    assert 'ReadWritePaths="-' + str(folder).replace('%', '%%') + '/rynhult-' in config
    assert len(list(folder.iterdir())) == 1
    link = tmp_path / 'alias'
    link.symlink_to(folder)
    with pytest.raises(Refused, match='symlink'):
        add_folder(str(link), True, **kwargs)


@pytest.mark.parametrize('folder', ['relative', '/etc', '/run/user/1000', '/mnt/../etc', '/mnt/\x00', '/mnt/line\nfeed'])
def test_command_refuses_unsafe_input(folder):
    from acc.server.local_backups import folder_command
    with pytest.raises(Refused):
        folder_command(folder, False)


def test_console_argv_and_shell_quoting(monkeypatch, capsys):
    import shlex
    import sys

    from acc import admin_backups
    from acc.server.local_backups import folder_command
    path = "/mnt/owner's $(disk); storage"
    command = folder_command(path, True)
    assert shlex.split(command) == ['sudo', 'rynhult-admin', 'backup', 'add-folder', '--same-disk', '--', path]
    monkeypatch.setattr(sys, 'argv', ['admin', 'add-folder', '--same-disk', '--', path])
    calls = []
    monkeypatch.setattr(admin_backups, 'add_folder', lambda *args: calls.append(args) or {'first_job': {'state': 'complete'}})
    admin_backups.main()
    assert calls == [(path, True)]
    assert 'complete' in capsys.readouterr().out


def test_console_requires_root_before_any_changes(monkeypatch):
    import os

    from acc.admin_backups import add_folder
    monkeypatch.setattr(os, 'getuid', lambda: 1000)
    with pytest.raises(Refused, match='root'):
        add_folder('/mnt/backup', True)



def test_legacy_cli_refuses_temporary_repository(tmp_path, capsys):
    from acc.backup_server import main
    assert main(['add-local', '--data', str(tmp_path), '--repository', '/tmp/volatile-backup']) != 0
    assert 'volatile' in capsys.readouterr().err


@pytest.mark.parametrize('folder', ['//etc', '//etc/systemd', '//home/josh', '//home/x', '//root',
                                   '//usr/local', '/./etc', '/mnt/./disk', '/mnt/../disk',
                                   '//etc///', '/root/', '/mnt/ｅtc', '/mnt/∕etc', '/mnt/\udcff'])
def test_normalized_path_refusals(folder, tmp_path):
    from acc.server.local_backups import folder_command
    for validate in (lambda: folder_command(folder, False), lambda: repository_path(folder, tmp_path, True)):
        with pytest.raises(Refused):
            validate()


def test_command_normalizes_slashes():
    from acc.server.local_backups import folder_command
    assert folder_command('//mnt///disk///', False).endswith('-- /mnt/disk')


@pytest.mark.parametrize('uid,mode,ok', [(0, 0o755, True), (1000, 0o700, False),
                                      (1001, 0o700, False), (0, 0o775, False),
                                      (1000, 0o707, False), (0, 0o1777, False)])
def test_storage_permissions(uid, mode, ok):
    from types import SimpleNamespace

    from acc.admin_backups import trusted_directory
    info = SimpleNamespace(st_uid=uid, st_mode=mode)
    if ok:
        trusted_directory(info, 1000)
    else:
        with pytest.raises(Refused):
            trusted_directory(info, 1000)


def test_service_settings_file(tmp_path, monkeypatch):
    from acc.admin_backups import service_environment
    config = tmp_path / 'accd'
    data = tmp_path / 'configured'
    config.write_text(f'ACC_DATA_DIR="{data}"\nACC_DATABASE_URL=sqlite:///configured.db\n')
    monkeypatch.setenv('RYNHULT_DATA_DIR', '/wrong')
    monkeypatch.setenv('ACC_DATABASE_URL', 'sqlite:///wrong.db')
    settings = service_environment(config)
    assert settings['ACC_DATA_DIR'] == str(data)
    assert settings['ACC_DATABASE_URL'] == 'sqlite:///configured.db'


@pytest.mark.parametrize('failure', ['reload', 'restart', 'init', 'wait', 'capture'])
def test_setup_rollback(tmp_path, monkeypatch, failure):
    import json
    import os
    import subprocess
    from types import SimpleNamespace

    from acc.admin_backups import add_folder
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'chown', lambda *a, **kw: None)
    monkeypatch.setattr('acc.admin_backups.trusted_directory', lambda *a: None)
    monkeypatch.setattr('acc.server.local_backups.refuse_volatile', lambda path: None)
    data = tmp_path / 'install' / 'data'
    data.mkdir(parents=True)
    folder = tmp_path / 'disk'
    folder.mkdir()
    calls = []
    failed = False
    def run(argv, **kwargs):
        nonlocal failed
        calls.append(argv)
        stage = ('reload' if argv == ['systemctl', 'daemon-reload'] else
                 'restart' if argv == ['systemctl', 'restart', 'accd.service'] else
                 'init' if '--provision' in argv else 'wait' if '--wait-job' in argv else 'rollback')
        if stage == failure and not failed:
            failed = True
            raise subprocess.CalledProcessError(1, argv)
        if stage == 'wait' and failure == 'capture':
            return SimpleNamespace(stdout=json.dumps({'state': 'failed'}))
        if stage == 'rollback':
            assert __import__('pathlib').Path(argv[-1]).is_dir()
        return SimpleNamespace(stdout=json.dumps({'target_id': 'a'*32, 'first_job': {'id': 'b'*32}}))
    with pytest.raises(Refused if failure == 'capture' else subprocess.CalledProcessError):
        add_folder(str(folder), True, env_file=tmp_path / 'accd', data=data, dropins=tmp_path / 'units', run=run,
                   account=SimpleNamespace(pw_uid=1000, pw_gid=1000))
    assert not list((tmp_path / 'units').glob('*.conf'))
    assert not list(folder.iterdir())
    assert any('--rollback' in call for call in calls)
    assert calls[-2:] == [['systemctl', 'daemon-reload'], ['systemctl', 'restart', 'accd.service']]


def test_open_folder_checks_every_component(tmp_path, monkeypatch):
    import os

    from acc.admin_backups import open_folder
    seen = []
    def check(info, uid):
        seen.append((info.st_dev, info.st_ino))
    monkeypatch.setattr('acc.admin_backups.trusted_directory', check)
    folder = tmp_path / 'parent' / 'disk'
    folder.mkdir(parents=True)
    fd = open_folder(folder, os.getuid())
    os.close(fd)
    assert len(seen) == len(folder.parts)
    assert seen[-1] == (folder.stat().st_dev, folder.stat().st_ino)


def test_repository_normalizes_trailing_and_repeated_slashes(tmp_path, monkeypatch):
    monkeypatch.setattr('acc.server.local_backups.refuse_volatile', lambda path: None)
    folder = tmp_path / 'disk'
    folder.mkdir()
    data = tmp_path / 'installation' / 'data'
    data.mkdir(parents=True)
    assert repository_path('//' + str(folder).lstrip('/') + '///', data, True).parent == folder


def test_child_environment_is_minimal(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from acc.admin_backups import add_folder, service_settings
    config = tmp_path / 'accd'
    data = tmp_path / 'install' / 'data'
    data.mkdir(parents=True)
    config.write_text(f'RYNHULT_DATA_DIR={data}\nACC_DATABASE_URL=sqlite:///configured.db\n'
                      'UNRELATED_SECRET=do-not-forward\nPYTHONPATH=/untrusted\n')
    config.chmod(0o600)
    folder = tmp_path / 'disk'
    folder.mkdir()
    monkeypatch.setattr(os, 'getuid', lambda: 0)
    monkeypatch.setattr(os, 'geteuid', lambda: 0)
    monkeypatch.setattr(os, 'chown', lambda *a, **kw: None)
    monkeypatch.setattr('acc.admin_backups.trusted_directory', lambda *a: None)
    monkeypatch.setattr('acc.server.local_backups.refuse_volatile', lambda path: None)
    calls = []
    def run(argv, **kwargs):
        if argv[0] == 'systemctl':
            return SimpleNamespace()
        calls.append((argv, kwargs['env']))
        # Changing the defaults after the root read must not change child settings.
        config.write_text('ACC_DATA_DIR=/wrong\n')
        with monkeypatch.context() as child:
            child.setattr(os, 'environ', kwargs['env'])
            settings = service_settings()
            assert settings.data_dir == data
            assert settings.db_url == 'sqlite:///configured.db'
        if '--provision' in argv:
            raise RuntimeError('force actual rollback')
        from acc.admin_backups import rollback
        with monkeypatch.context() as child:
            child.setattr(os, 'geteuid', lambda: 1000)
            child.setattr(os, 'environ', kwargs['env'])
            rollback(argv[-1])
        return SimpleNamespace(stdout='{}')
    with pytest.raises(RuntimeError, match='force actual rollback'):
        add_folder(str(folder), True, env_file=config, dropins=tmp_path / 'units', run=run,
                   account=SimpleNamespace(pw_uid=1000, pw_gid=1000))
    assert not list(folder.iterdir())
    assert len(calls) == 2
    for argv, env in calls:
        assert env == {'PATH': '/usr/local/bin:/usr/bin:/bin', 'ACC_DATA_DIR': str(data),
                       'ACC_DATABASE_URL': 'sqlite:///configured.db'}
        assert 'sqlite:///configured.db' not in argv


@pytest.mark.parametrize('protected', [False, True])
def test_defaults_with_real_child(tmp_path, protected):
    """Run on a privileged test host; no actual systemd or admin invocation."""
    import os
    import pwd
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from acc.admin_backups import service_environment
    if protected and os.geteuid() != 0:
        pytest.skip('requires root to create a 0600 file owned by another uid and drop child uid')
    if not protected and os.geteuid() == 0:
        pytest.skip('same-uid provision child must be non-root; separate-uid case covers root runs')
    user = pwd.getpwnam('nobody') if protected else pwd.getpwuid(os.getuid())
    # A traversable scratch root lets the child see the protected file's metadata.
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        root.chmod(0o755)
        config = root / 'accd'
        data = root / 'data'
        data.mkdir()
        if protected:
            os.chown(data, user.pw_uid, user.pw_gid)
        repository = data / ('rynhult-' + 'a' * 32)
        repository.mkdir()
        if protected:
            os.chown(repository, user.pw_uid, user.pw_gid)
        config.write_text(f'ACC_DATA_DIR={data}\nACC_DATABASE_URL=sqlite:///{data}/test.db\n'
                          'UNRELATED_SECRET=excluded\n')
        config.chmod(0o600)
        env = service_environment(config)
        code = '''
import os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from acc.admin_backups import service_settings, provision, wait_job, rollback
from acc.backup_server import Service
from acc.server.backup_jobs import BackupJobs
import acc.admin_backups as admin
config, repository = map(Path, sys.argv[2:4])
protected = sys.argv[4] == 'True'
assert (config.stat().st_uid != os.geteuid()) == protected
assert config.stat().st_mode & 0o777 == 0o600
try:
    config.read_text()
except PermissionError:
    pass
else:
    assert not protected, 'defaults readable by child'
assert service_settings().data_dir == repository.parent
assert 'UNRELATED_SECRET' not in os.environ
# Model persistence and external restic/job work, while settings, engine creation,
# child entry points and rollback use the real implementations.
admin.refuse_volatile = lambda path: None
def add_local(self, path, prepared=False):
    assert self.settings.data_dir == repository.parent
    assert prepared and path == str(repository)
    return {'id': 'b' * 32}
Service.add_local_target = add_local
def jobs_init(self, settings, sessions):
    assert settings.data_dir == repository.parent
    assert settings.db_url == 'sqlite:///' + str(repository.parent / 'test.db')
BackupJobs.__init__ = jobs_init
BackupJobs.request = lambda *args: {'id': 'c' * 32, 'state': 'queued'}
BackupJobs.jobs = lambda self: [{'id': 'c' * 32, 'state': 'complete'}]
result = provision(str(repository))
assert result['first_job']['id'] == 'c' * 32
assert wait_job('c' * 32)['state'] == 'complete'
assert rollback(str(repository)) == {'rolled_back': True}
assert repository.is_dir() and not list(repository.iterdir())
'''
        subprocess.run([sys.executable, '-I', '-c', code, str(Path(__file__).resolve().parents[1] / 'src'),
                        str(config), str(repository), str(protected)], env=env,
                       **({'user': user.pw_uid, 'group': user.pw_gid, 'extra_groups': []} if protected else {}),
                       check=True, capture_output=True, text=True)
        assert repository.is_dir() and not list(repository.iterdir())


def test_open_folder_refuses_acc_owned_parent(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace

    from acc.admin_backups import open_folder
    folder = tmp_path / 'acc-owned-disk'
    folder.mkdir()
    inode = folder.stat().st_ino
    real_fstat = os.fstat
    def metadata(fd):
        info = real_fstat(fd)
        # Model protected ancestors and an acc-owned selected folder.
        return SimpleNamespace(st_uid=1000 if info.st_ino == inode else 0, st_mode=0o755)
    monkeypatch.setattr(os, 'fstat', metadata)
    with pytest.raises(Refused, match='root-owned'):
        open_folder(folder, 1000)
    assert list(folder.iterdir()) == []


@pytest.mark.parametrize('replacement', ['none', 'symlink', 'directory', 'parent', 'nonempty'])
def test_root_rollback_rmdir_checks_identity(tmp_path, monkeypatch, replacement):
    from acc.admin_backups import remove_repository
    # Model root ownership only; exercise real no-follow opens, inode checks and rmdir.
    monkeypatch.setattr('acc.admin_backups.trusted_directory', lambda *args: None)
    parent = tmp_path / 'disk'
    parent.mkdir()
    repository = parent / ('rynhult-' + 'a' * 32)
    repository.mkdir()
    parent_info, repository_info = parent.stat(), repository.stat()
    if replacement in ('symlink', 'directory'):
        repository.rename(parent / 'original')  # Keep original inode allocated.
        if replacement == 'symlink':
            repository.symlink_to(parent / 'original', target_is_directory=True)
        else:
            repository.mkdir()
    elif replacement == 'parent':
        parent.rename(tmp_path / 'old-disk')
        parent.mkdir()
        repository.mkdir()
    elif replacement == 'nonempty':
        (repository / 'keep').write_text('untouched')
    if replacement == 'none':
        remove_repository(repository, parent_info, repository_info, 1000)
        assert not repository.exists()
    else:
        with pytest.raises((Refused, OSError)):
            remove_repository(repository, parent_info, repository_info, 1000)
        assert repository.is_dir()
        if replacement == 'symlink':
            assert repository.is_symlink()
            assert (parent / 'original').is_dir()
        if replacement == 'nonempty':
            assert (repository / 'keep').read_text() == 'untouched'


def test_rollback_child_with_root_owned_parent(tmp_path, monkeypatch):
    import os
    import pwd
    import subprocess
    import sys
    import tempfile
    from pathlib import Path

    from acc.admin_backups import remove_repository
    if os.geteuid() != 0:
        pytest.skip('requires root to create root-owned storage and run cleanup as a second uid')
    user = pwd.getpwnam('nobody')
    with tempfile.TemporaryDirectory() as scratch:
        root = Path(scratch)
        root.chmod(0o755)
        parent = root / 'disk'
        parent.mkdir(mode=0o755)
        data = root / 'data'
        data.mkdir()
        os.chown(data, user.pw_uid, user.pw_gid)
        repository = parent / ('rynhult-' + 'a' * 32)
        repository.mkdir()
        os.chown(repository, user.pw_uid, user.pw_gid)
        (repository / 'partial').write_text('incomplete')
        parent_info, repository_info = parent.stat(), repository.stat()
        code = '''
import sys
sys.path.insert(0, sys.argv[1])
from acc.admin_backups import rollback
assert rollback(sys.argv[2]) == {'rolled_back': True}
'''
        subprocess.run([sys.executable, '-I', '-c', code,
                        str(Path(__file__).resolve().parents[1] / 'src'), str(repository)],
                       env={'ACC_DATA_DIR': str(data), 'ACC_DATABASE_URL': 'sqlite://'},
                       user=user.pw_uid, group=user.pw_gid, extra_groups=[],
                       check=True, capture_output=True, text=True)
        assert repository.is_dir() and not list(repository.iterdir())
        # /tmp is intentionally writable; pin the real root-owned scratch parent
        # directly, retaining the production ownership check for this directory.
        def pin(path, uid):
            from acc.admin_backups import trusted_directory
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            trusted_directory(os.fstat(fd), uid)
            return fd
        monkeypatch.setattr('acc.admin_backups.open_folder', pin)
        remove_repository(repository, parent_info, repository_info, user.pw_uid)
        assert not repository.exists()
