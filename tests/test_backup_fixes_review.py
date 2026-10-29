"""Independent review of backup-fixes: one regression per finding."""
import pytest
from helpers import pause_supervisor, write_db

from acc import backup_server as backup
from acc.server.models import Node


@pytest.mark.parametrize('removed_status', ['revoked', 'missing'])
async def test_a_revoked_machine_does_not_stop_other_targets_schedule(api, removed_status):
    # An enabled target whose machine is revoked used to raise Refused inside schedule(); run() swallows ValueError,
    # so every tick ended before work() and remind() for all targets.
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    removed = jobs.service.add_node_target('a'*32, 'old-box')
    healthy = jobs.service.add_node_target('b'*32, 'new-box')
    with write_db(jobs.db) as db:
        if removed_status != 'missing':
            db.add(Node(id='a'*32, name='old-box', status=removed_status))
        db.add(Node(id='b'*32, name='new-box', status='active'))
        db.commit()
    targets = [{**t, 'enabled': True, 'setup_verified_at': 1.0} for t in jobs.service.targets()]
    backup.private_write(jobs.service.root / 'targets.json', backup.canonical(targets))
    jobs.schedule()
    queued = {j['target_id'] for j in jobs.pending()}
    assert queued == {healthy['id']}, queued
    assert removed['id'] not in queued


def test_setup_binds_only_after_the_disk_scan_and_says_how_to_resume():
    # A refused disk (lost+found, unreadable entries) used to leave a binding without identity; rerunning then refused
    # as "already configured" although the trap said to rerun.
    import subprocess

    from acc.backup_targets import setup_commands
    script = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')
    lines = script.split('\nACC_GUARD\n', 1)[1].splitlines()  # the embedded guard only names the path
    record = next(i for i, line in enumerate(lines) if line.startswith('runuser -u acc-backup') and '--record' in line)
    writes = [i for i, line in enumerate(lines) if '/etc/acc/backup-target.json' in line
              and not line.lstrip().startswith(('#', 'if ', 'echo ', 'ConditionPathExists'))]
    assert writes and min(writes) > record, (record, writes)
    assert 'binding.update' in script and '"$scratch/binding.json" /etc/acc/backup-target.json' in script
    configured = script.index('Receiver already configured')
    assert script.index("cat > /usr/local/libexec/acc-backup-receiver-check") < configured
    assert '--rerecord' in script[configured:configured + 400]
    head = script[:script.index('\numask')]
    for prefix, expected in (('', 'Nothing is bound yet'), ("resume='The receiver is bound: restart it.'\n", 'The receiver is bound')):
        shell = head + '\n' + prefix + 'false\nACC_SETUP\nprintf "LOGIN_SURVIVED\\n"\n'
        result = subprocess.run(['bash'], input=shell, capture_output=True, text=True)
        assert 'LOGIN_SURVIVED' in result.stdout and expected in result.stderr, result.stderr


def guard(tmp_path, monkeypatch, binding):
    import json

    from acc import backup_receiver_check as check
    data, path = tmp_path / 'data', tmp_path / 'backup-target.json'
    data.mkdir(mode=0o700)
    path.write_text(json.dumps(binding))
    path.chmod(0o644)
    monkeypatch.setattr(check, 'DATA', data)
    monkeypatch.setattr(check, 'BINDING', path)
    monkeypatch.setattr(check, 'filesystem_root', lambda p: False)
    monkeypatch.setattr(check.os, 'geteuid', lambda: 0)
    real_fstat = check.os.fstat
    monkeypatch.setattr(check.os, 'fstat', lambda fd: type('S', (), {
        'st_mode': real_fstat(fd).st_mode, 'st_uid': 0, 'st_size': real_fstat(fd).st_size})())
    return check, path


def test_rerecord_replaces_only_the_disk_identity(tmp_path, monkeypatch):
    # The owner path after a reviewed volume change, and for bindings from before disk checks (which now fail closed).
    import json
    import stat
    from types import SimpleNamespace
    legacy = {'target_id': 'a'*32, 'installation_id': 'b'*32, 'port': 18762}
    check, path = guard(tmp_path, monkeypatch, legacy)
    calls = []
    def runner(argv, **kw):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout=json.dumps({'device': 7, 'fsid': 9}), stderr='')
    check.rerecord(runner=runner)
    assert calls[0][:4] == ['runuser', '-u', 'acc-backup', '--'] and calls[0][-1] == '--record'
    assert json.loads(path.read_text()) == {**legacy, 'device': 7, 'fsid': 9}
    assert stat.S_IMODE(path.stat().st_mode) == 0o644
    assert not list(tmp_path.glob('.backup-target.json.new'))


def test_rerecord_keeps_the_binding_when_the_scan_refuses(tmp_path, monkeypatch):
    import json
    from types import SimpleNamespace

    import pytest
    old = {'target_id': 'a'*32, 'installation_id': 'b'*32, 'port': 18762, 'device': 1, 'fsid': 2}
    check, path = guard(tmp_path, monkeypatch, old)
    refused = lambda argv, **kw: SimpleNamespace(returncode=1, stdout='', stderr=check.ADVICE)
    with pytest.raises(ValueError, match='disk check failed'):
        check.rerecord(runner=refused)
    assert json.loads(path.read_text()) == old
    monkeypatch.setattr(check.os, 'geteuid', lambda: 1000)
    with pytest.raises(ValueError):
        check.rerecord(runner=refused)


def test_dry_run_without_root_still_plans_when_the_data_folder_is_private(tmp_path):
    # /srv/acc/data is acc's 0700 folder: an unprivileged dry run can't probe the leases and must say so, not fail.
    import os
    import subprocess
    from pathlib import Path

    import pytest
    if os.geteuid() == 0:
        pytest.skip('root reads the private folder')
    data = tmp_path / 'srv/acc/data'
    (data / 'backup-service').mkdir(parents=True)
    (data / '.capture-update.lock').touch()
    def plan():
        return subprocess.run(['/usr/bin/python3', '-I', str(Path(__file__).resolve().parents[1] / 'deploy/lib/public-aliases.py'),
                               '--role', 'main', '--dry-run', '--alias-root', str(tmp_path)], capture_output=True, text=True)
    readable = plan()  # this bare root has no aliases to plan; only the probe's effect matters
    data.chmod(0)
    try:
        private = plan()
    finally:
        data.chmod(0o700)
    note, _, rest = private.stderr.partition('\n')
    assert "can't check for a running backup capture" in note, private.stderr
    assert (private.returncode, private.stdout, rest) == (readable.returncode, readable.stdout, readable.stderr)


async def test_remove_under_contention_is_409_and_releases_its_first_lease(api, monkeypatch):
    from acc.maintenance import lease
    from acc.server import backup_jobs
    await pause_supervisor(api)
    jobs = api.app.state.ctx.backup_jobs
    target = jobs.service.add_node_target('d'*32, 'old-box')
    with write_db(jobs.db) as db:
        db.add(Node(id='d'*32, name='old-box', status='revoked'))
        db.commit()
    monkeypatch.setattr(backup_jobs, 'lease', lambda path, **kw: lease(path, timeout=0))
    with lease(jobs.service.root / '.coordinator'):
        response = await api.post(f"/api/host/backups/{target['id']}/remove", json={'confirmed': True})
    assert response.status_code == 409
    assert response.json() == {'detail': 'A backup is running; try again shortly.'}
    with lease(jobs.service.root / '.intent', timeout=0):
        pass  # the .intent lease taken before the busy .coordinator was released
    assert [t['id'] for t in jobs.service.targets()] == [target['id']]


def test_receiver_start_allows_the_guards_full_walk():
    from acc.backup_targets import setup_commands
    unit = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE').split("<<'ACC_UNIT'\n", 1)[1].split('\nACC_UNIT', 1)[0]
    lines = unit.splitlines()
    assert 'TimeoutStartSec=30min' in lines
    assert lines.index('TimeoutStartSec=30min') < lines.index('[Install]')


def test_setup_prerequisites_match_what_the_block_runs():
    from pathlib import Path

    from acc.backup_targets import setup_commands
    root = Path(__file__).resolve().parents[1]
    script = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')
    assert '/usr/bin/python3 -I' in script and 'runuser -u acc-backup' in script
    for text in (script.splitlines()[1], (root / 'web/src/pages/BackupTargetSetup.tsx').read_text(),
                 (root / 'docs/wiki/User-Guide/Backups.md').read_text()):
        assert 'Python 3' in text and 'runuser' in text


def test_setup_time_guard_failures_never_point_at_rerecord(tmp_path, monkeypatch, capsys):
    # During setup nothing is bound yet, so --rerecord can't work; the block's own trap says to rerun it.
    import sys
    check, path = guard(tmp_path, monkeypatch, {})
    path.unlink()
    monkeypatch.setattr(check, 'filesystem_root', lambda p: True)
    monkeypatch.setattr(sys, 'argv', ['guard', '--preflight'])
    assert check.main() == 1
    error = capsys.readouterr().err
    assert 'disk check failed' in error and '--rerecord' not in error
    monkeypatch.setattr(check.pwd, 'getpwnam', lambda name: type('P', (), {'pw_uid': 0})())
    monkeypatch.setattr(sys, 'argv', ['guard', '--record'])
    assert check.main() == 1
    error = capsys.readouterr().err
    assert 'disk check failed' in error and '--rerecord' not in error


def test_rerecord_without_a_binding_says_to_run_setup_first(tmp_path, monkeypatch):
    check, path = guard(tmp_path, monkeypatch, {})
    path.unlink()
    with pytest.raises(ValueError, match='No receiver binding yet') as caught:
        check.rerecord(runner=lambda *a, **kw: pytest.fail('scanned without a binding'))
    assert 'setup' in str(caught.value)
    monkeypatch.setattr(check.os, 'geteuid', lambda: 1000)
    with pytest.raises(ValueError, match='as root'):
        check.rerecord()


def test_receiver_start_failure_still_points_at_rerecord(tmp_path, monkeypatch):
    check, _ = guard(tmp_path, monkeypatch, {'device': 1, 'fsid': 2})
    with pytest.raises(ValueError, match='--rerecord'):
        check.verify(check.DATA, {'device': 1, 'fsid': 2}, check.os.getuid())


def test_an_existing_receiver_gets_the_pre_start_guard_too():
    # The Guide promises every receiver start runs the disk guard; a receiver set up before disk checks only gets it
    # from this block, which stops at the existing binding. The drop-in must preserve existing pre-start safety checks.
    import subprocess

    from acc.backup_targets import setup_commands
    script = setup_commands({'id': 'a'*32}, 'b'*32, '{SHA}EXAMPLE')
    branch = script[script.index('if [ -e /etc/acc/backup-target.json ]'):]
    branch = branch[:branch.index('\nfi\n')]
    assert 'acc-backup-receiver.service.d/disk-guard.conf' in branch
    dropin = branch.split("<<'ACC_DROPIN'\n", 1)[1].split('\nACC_DROPIN', 1)[0].splitlines()
    assert dropin == ['[Service]', 'TimeoutStartSec=30min',
                      'ExecStartPre=/usr/bin/python3 -I /usr/local/libexec/acc-backup-receiver-check']
    assert branch.index('systemctl daemon-reload') < branch.index('exit 1')
    assert 'restart' not in branch.split('ACC_DROPIN')[-1].split('echo')[0]  # takes effect at the next start, not now
    assert subprocess.run(['bash', '-n'], input=script, text=True).returncode == 0
