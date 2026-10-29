"""Backup refusals the main flow tests don't reach: receiver disk guard entry points, target identity at check time,
removal preconditions and the offline restore's argument gates. Temporary files and fakes only."""
import asyncio
import json
import os
import sys
from types import SimpleNamespace

import pytest
from helpers import pause_supervisor, write_db
from test_backup_fixes_review import guard

from acc import backup_receiver_check as check
from acc import backup_server as backup
from acc.backup_targets import RECEIVER_GENERIC, RECEIVER_REFUSALS
from acc.server.models import AuditLog, Node, Observation


def run_guard(monkeypatch, capsys, *argv):
    monkeypatch.setattr(sys, 'argv', ['acc-backup-receiver-check', *argv])
    code = check.main()
    return code, capsys.readouterr()


@pytest.fixture
def receiver(tmp_path, monkeypatch):
    """The guard's fixed paths in a temporary folder, run as 'acc-backup' (this test's own account)."""
    data, binding = tmp_path / 'data', tmp_path / 'backup-target.json'
    data.mkdir(mode=0o700)
    monkeypatch.setattr(check, 'DATA', data)
    monkeypatch.setattr(check, 'BINDING', binding)
    monkeypatch.setattr(check, 'filesystem_root', lambda path: False)
    monkeypatch.setattr(check.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.geteuid()))
    return data, binding


def test_guard_records_and_then_verifies_the_same_disk(receiver, monkeypatch, capsys):
    data, binding = receiver
    code, out = run_guard(monkeypatch, capsys, '--record')
    assert code == 0, out.err
    recorded = json.loads(out.out)
    assert sorted(recorded) == ['device', 'fsid']
    binding.write_text(json.dumps({'target_id': 'a' * 32, **recorded}))
    assert run_guard(monkeypatch, capsys, )[0] == 0
    binding.write_text(json.dumps({'target_id': 'a' * 32, **recorded, 'fsid': recorded['fsid'] + 1}))
    code, out = run_guard(monkeypatch, capsys)
    assert code == 1 and 'disk check failed' in out.err


@pytest.mark.parametrize('content', ['[]', '"device"', 'null', '{"device": "1", "fsid": "2"}', 'not json'])
def test_guard_refuses_a_binding_that_is_not_an_identity_record(receiver, monkeypatch, capsys, content):
    # A damaged binding must fail closed with the owner's advice, never with a traceback.
    _, binding = receiver
    binding.write_text(content)
    code, out = run_guard(monkeypatch, capsys)
    assert code == 1 and 'disk check failed' in out.err and 'Traceback' not in out.err


def test_guard_refuses_other_accounts_unknown_arguments_and_unsafe_folders(receiver, monkeypatch, capsys):
    data, binding = receiver
    for args in (['--bogus'], ['--record', '--rerecord'], ['extra']):
        code, out = run_guard(monkeypatch, capsys, *args)
        assert code == 1 and 'Unsupported receiver guard arguments' in out.err
    monkeypatch.setattr(check.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.geteuid() + 1))
    code, out = run_guard(monkeypatch, capsys, '--record')
    assert code == 1 and 'must run as acc-backup' in out.err
    monkeypatch.setattr(check.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.geteuid()))
    data.chmod(0o750)
    code, out = run_guard(monkeypatch, capsys, '--record')
    assert code == 1 and 'disk check failed' in out.err
    data.chmod(0o700)
    (data / 'linked').symlink_to('/etc')
    code, out = run_guard(monkeypatch, capsys, '--record')
    assert code == 1 and 'disk check failed' in out.err and out.out == ''


def test_guard_preflight_needs_root_and_a_real_data_folder(receiver, monkeypatch, capsys):
    data, _ = receiver
    if os.geteuid() != 0:
        assert run_guard(monkeypatch, capsys, '--preflight')[0] == 1
    monkeypatch.setattr(check.os, 'geteuid', lambda: 0)
    assert run_guard(monkeypatch, capsys, '--preflight')[0] == 0
    monkeypatch.setattr(check, 'filesystem_root', lambda path: True)
    assert run_guard(monkeypatch, capsys, '--preflight')[0] == 1
    monkeypatch.setattr(check, 'filesystem_root', lambda path: False)
    data.rmdir()
    data.symlink_to('/var/tmp')
    assert run_guard(monkeypatch, capsys, '--preflight')[0] == 1


def test_the_filesystem_root_itself_is_never_a_receiver_folder():
    assert check.filesystem_root('/')


@pytest.mark.parametrize('case', ['writable', 'list', 'extra_key', 'text_value'])
def test_rerecord_keeps_the_binding_unless_the_file_and_the_scan_are_exactly_right(tmp_path, monkeypatch, case):
    old = {'target_id': 'a' * 32, 'installation_id': 'b' * 32, 'port': 18762, 'device': 1, 'fsid': 2}
    checker, path = guard(tmp_path, monkeypatch, [] if case == 'list' else old)
    before = path.read_text()
    if case == 'writable':
        path.chmod(0o666)
    scan = {'device': 7, 'fsid': 9, **({'extra': 1} if case == 'extra_key' else {}),
            **({'fsid': '9'} if case == 'text_value' else {})}
    runner = lambda argv, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(scan), stderr='')
    with pytest.raises(ValueError):
        checker.rerecord(runner=runner)
    assert path.read_text() == before
    assert not list(tmp_path.glob('.backup-target.json.new'))


def restore_args(tmp_path, *extra):
    kit = tmp_path / 'kit.json'
    kit.write_text('{}')
    return ['restore', '--kit', str(kit), '--target-id', 'a' * 32, '--backup-id', 'b' * 32,
            '--destination', str(tmp_path / 'restored'), *extra]


def test_offline_restore_gates_replacement_selections_and_root(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(backup, 'restore', lambda *args, **kwargs: calls.append(kwargs) or {
        'backup_id': 'b' * 32, 'started_at': 1, 'coverage_exclusions': []})
    reviewed = tmp_path / 'reviewed.json'
    reviewed.write_text(json.dumps({'c' * 32: '0' * 64}))
    assert backup.main(restore_args(tmp_path, '--resume-session', 'd' * 32)) == 1
    assert backup.main(restore_args(tmp_path, '--reviewed-nodes', str(reviewed))) == 1
    assert calls == []
    monkeypatch.setattr(backup.os, 'geteuid', lambda: 0)
    assert backup.main(restore_args(tmp_path, '--prepare-replacement')) == 1
    assert calls == []
    monkeypatch.setattr(backup.os, 'geteuid', lambda: 1000)
    assert backup.main(restore_args(tmp_path, '--prepare-replacement', '--reviewed-nodes', str(reviewed))) == 0
    assert calls[0]['prepare_replacement'] is True and calls[0]['reviewed_nodes'] == {'c' * 32: '0' * 64}
    assert str(reviewed) not in capsys.readouterr().err


async def node_target(api, name='archive-box', node_id='a' * 32, status='active'):
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_node_target(node_id, name)
    with write_db(jobs.db) as db:
        db.add(Node(id=node_id, name=name, status=status))
        db.commit()
    return jobs, target


def check_audits(jobs, target_id):
    with jobs.db() as db:
        return [row.detail for row in db.query(AuditLog).filter_by(action='backup.target_check', target=target_id)]


async def test_target_check_refuses_a_link_whose_enrollment_changed(api, monkeypatch):
    # A machine re-enrolled under the old display name must not receive the backup stream.
    jobs, target = await node_target(api)
    hub = api.app.state.ctx.broker.hub
    opened = []

    class Link:
        node_id = 'b' * 32
        _loop = asyncio.get_running_loop()

        async def open_backup(self, *args):
            opened.append(args)
    monkeypatch.setattr(hub, 'get', lambda name: Link())
    before = jobs.service.target(target['id'])
    response = await api.post(f"/api/host/backups/{target['id']}/check")
    assert response.status_code == 409 and response.json()['detail'] == "This target's machine changed since setup. Check Host → Machines, then try again."
    assert opened == [] and check_audits(jobs, target['id']) == [{'ok': False}]
    assert jobs.service.target(target['id']) == before


async def test_target_check_stops_at_a_failed_receiver_disk_check(api, monkeypatch):
    jobs, target = await node_target(api)
    hub = api.app.state.ctx.broker.hub

    class Link:
        node_id = 'a' * 32
        _loop = asyncio.get_running_loop()

        async def open_backup(self, *args):
            # The real node emits this bounded, allow-listed wire message, not arbitrary stderr.
            raise ConnectionError(RECEIVER_REFUSALS['disk'])
    monkeypatch.setattr(hub, 'get', lambda name: Link())
    used = []
    monkeypatch.setattr(jobs.service, '_restic', lambda *a, **kw: used.append(a), raising=False)
    response = await api.post(f"/api/host/backups/{target['id']}/check")
    assert response.status_code == 409 and 'recorded device/fsid' in response.json()['detail']
    assert '--rerecord' in response.json()['detail']
    assert used == [] and check_audits(jobs, target['id']) == [{'ok': False}]
    assert not jobs.service.target(target['id']).get('setup_verified_at')


async def test_only_an_idle_target_of_a_removed_machine_can_be_removed(api):
    jobs, target = await node_target(api)
    url = f"/api/host/backups/{target['id']}/remove"
    response = await api.post(url, json={'confirmed': True})
    assert response.status_code == 409 and 'machine was removed' in response.json()['detail']
    with write_db(jobs.db) as db:
        db.get(Node, 'a' * 32).status = 'revoked'
        db.add(Observation(key='server-backup.job.' + 'e' * 32, source='test', observed_at=1, value={
            'id': 'e' * 32, 'target_id': target['id'], 'action': 'capture', 'state': 'queued', 'at': 1}))
        db.commit()
    response = await api.post(url, json={'confirmed': True})
    assert response.status_code == 409 and 'A backup task is still running for this target' in response.json()['detail']
    assert [t['id'] for t in jobs.service.targets()] == [target['id']]
    with jobs.db() as db:
        assert db.query(AuditLog).filter_by(action='backup.target_remove').count() == 0


@pytest.mark.parametrize('claimed', [{'via': 'public', 'digest': 'new'}, {'digest': 'new'}, {'via': 'private'}])
async def test_a_node_success_without_its_token_exchange_is_a_failed_backup(api, env, project, monkeypatch, claimed):
    # The machine must have exchanged this job's grant on the path it reports; a bare success claim records nothing.
    from test_gitbackup import make_repo
    from test_roaming_git_backup import NODE, enable
    await pause_supervisor(api)
    pid = project['id']
    enable(api, pid)
    make_repo(env['workspaces'] / 'demo')
    supervisor = api.app.state.supervisor
    object.__setattr__(env['settings'], 'backup_url', 'https://acc.example.lan')

    async def refs(*a, **kw):
        return {'git': True, 'digest': 'new'}

    async def job(*a, **kw):
        return claimed
    monkeypatch.setattr(supervisor.broker, 'call', refs)
    supervisor.broker.hub.links['roamer'] = SimpleNamespace(node_id=NODE, closed=False, host_capable=False,
                                                            info={'capabilities': ['git-backup-v1']}, git_backup=job)
    try:
        status = await supervisor.backup_project(pid, force=True)
    finally:
        supervisor.broker.hub.links.pop('roamer', None)
    assert status['error'] == 'Node backup result did not match the authenticated token path'
    assert 'last_at' not in status and 'via' not in status
    assert not supervisor.backup_grants._grants


def test_installer_capture_probe_is_read_only_and_refuses_odd_leases(tmp_path):
    import fcntl
    import importlib.util
    from pathlib import Path
    script = Path(__file__).resolve().parents[1] / 'deploy/lib/public-aliases.py'
    spec = importlib.util.spec_from_file_location('public_aliases', script)
    aliases = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(aliases)
    data = tmp_path / 'srv/acc/data'
    (data / 'backup-service').mkdir(parents=True)
    assert aliases.capture_running(tmp_path) is False
    assert not (data / '.capture-update.lock').exists()  # never created by the probe
    worker = data / '.capture-update.lock'
    worker.touch()
    with open(worker) as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        assert aliases.capture_running(tmp_path) is True
    assert aliases.capture_running(tmp_path) is False
    worker.unlink()
    os.mkfifo(worker)  # never opened for a blocking read
    with pytest.raises(ValueError, match='unsafe backup capture lease'):
        aliases.capture_running(tmp_path)
    worker.unlink()
    worker.symlink_to(tmp_path / 'elsewhere')
    with pytest.raises(OSError):
        aliases.capture_running(tmp_path)  # a linked lease is never read as "no capture"


async def test_target_check_keeps_unknown_receiver_details_out_of_the_api(api, monkeypatch):
    jobs, target = await node_target(api)

    class Link:
        node_id = 'a' * 32
        _loop = asyncio.get_running_loop()

        async def open_backup(self, *args):
            raise ConnectionError('receiver refused: private-canary at /private/receiver')

    monkeypatch.setattr(api.app.state.ctx.broker.hub, 'get', lambda name: Link())
    used = []
    monkeypatch.setattr(jobs.service, '_restic', lambda *a, **kw: used.append(a), raising=False)
    response = await api.post(f"/api/host/backups/{target['id']}/check")
    assert response.status_code == 409 and response.json()['detail'] == RECEIVER_GENERIC
    assert used == [] and check_audits(jobs, target['id']) == [{'ok': False}]
    assert not jobs.service.target(target['id']).get('setup_verified_at')
