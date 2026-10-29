"""Root provisioning plans with fakes only: never create an account or invoke host tools."""
import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('setup_build_account', ROOT / 'deploy/containers/setup_build_account.py')
setup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = setup
spec.loader.exec_module(setup)


def test_allocate_checks_both_files_and_uses_a_shared_free_range():
    assert setup.allocate('alex:100000:65536\n', 'sam:165536:65536\n') == (231072, 65536)


@pytest.mark.parametrize('entries', ['bad', 'alex:0:0', 'alex:-1:65536', 'alex:4294967290:65536'])
def test_malformed_ranges_refuse_instead_of_allocating(entries):
    with pytest.raises(setup.Refused):
        setup.allocate(entries, '')


def test_existing_ranges_must_be_matching_exclusive_and_large_enough():
    assert setup.allocate('rynhult-build:100000:65536', 'rynhult-build:100000:65536', 987) == (100000, 65536)
    for uid, gid in [('rynhult-build:100000:65536', ''),
                     ('rynhult-build:100000:65536\nalex:120000:65536', 'rynhult-build:100000:65536'),
                     ('987:100000:100', '987:100000:100'),
                     ('rynhult-build:100000:65536', 'rynhult-build:200000:65536')]:
        with pytest.raises(setup.Refused):
            setup.allocate(uid, gid, 987)


def test_real_user_and_group_ids_are_never_inside_the_range():
    """Kit review BA-L1: directory-joined hosts can have real IDs at 100000 and above."""
    assert setup.allocate('alex:100000:65536\n', '', ids={1000, 120000}) == (165536, 65536)
    assert setup.allocate('', '', ids={100000}) == (100001, 65536)
    assert setup.allocate('', '', ids={165535, 40000}) == (165536, 65536)
    with pytest.raises(setup.Refused, match='covers a real user or group ID'):
        setup.allocate('rynhult-build:100000:65536', 'rynhult-build:100000:65536', 987, ids={987, 150000})
    commands, selected = setup.plan(snapshot(peers=[SimpleNamespace(pw_name='ad-user', pw_uid=100500, pw_gid=100)],
                                             groups=[SimpleNamespace(gr_name='ad-group', gr_gid=170000, gr_mem=[])]))
    assert selected == (100501, 65536)  # just past the real user; the real group (170000) is beyond its end


def snapshot(**changes):
    fields = dict(account=None, groups=[], password=None, home=None, timers=False,
                  cron=False, manager=False, linger=False, subuid='', subgid='', system_max=999, peers=[])
    fields.update(changes)
    return SimpleNamespace(**fields)


def existing(**changes):
    fields = dict(account=SimpleNamespace(pw_name='rynhult-build', pw_uid=987, pw_gid=987,
                                         pw_dir='/var/lib/rynhult-build', pw_shell='/usr/sbin/nologin'),
                  groups=[SimpleNamespace(gr_name='rynhult-build', gr_gid=987, gr_mem=[])],
                  password='!', home=(987, 987, 0o700), subuid='rynhult-build:100000:65536',
                  subgid='rynhult-build:100000:65536')
    fields.update(changes)
    return snapshot(**fields)


def test_new_account_plan_is_locked_private_and_allocates_explicit_ranges():
    commands, selected = setup.plan(snapshot())
    assert selected == (100000, 65536)
    assert commands[0] == ['useradd', '--system', '--user-group', '--no-create-home',
                           '--home-dir', '/var/lib/rynhult-build', '--shell', '/usr/sbin/nologin',
                           '--password', '!', '-K', 'SUB_UID_COUNT=0', '-K', 'SUB_GID_COUNT=0', 'rynhult-build']
    assert ['usermod', '--add-subuids', '100000-165535', '--add-subgids', '100000-165535', 'rynhult-build'] in commands
    assert ['install', '-d', '-m', '0700', '-o', 'rynhult-build', '-g', 'rynhult-build', '/var/lib/rynhult-build'] in commands
    assert commands[-1] == ['loginctl', 'disable-linger', 'rynhult-build']


def test_matching_existing_account_is_idempotent():
    assert setup.plan(existing()) == ([], (100000, 65536))
    commands, _ = setup.plan(existing(linger=True))
    assert commands == [['loginctl', 'disable-linger', 'rynhult-build']]


@pytest.mark.parametrize('field,value', [('password', ''), ('password', '!hash'), ('home', (987, 987, 0o755)),
                                        ('timers', True), ('cron', True), ('manager', True)])
def test_existing_conflicts_refuse_before_changes(field, value):
    with pytest.raises(setup.Refused):
        setup.plan(existing(**{field: value}))


@pytest.mark.parametrize('field,value', [('pw_uid', 0), ('pw_uid', 1000), ('pw_shell', '/bin/bash'),
                                        ('pw_dir', '/home/alex'), ('pw_gid', 0)])
def test_existing_identity_cannot_be_adopted(field, value):
    state = existing()
    setattr(state.account, field, value)
    with pytest.raises(setup.Refused):
        setup.plan(state)


def test_agent_or_other_supplementary_group_refuses():
    state = existing()
    state.groups.append(SimpleNamespace(gr_name='acc-agents', gr_gid=2000, gr_mem=['rynhult-build']))
    with pytest.raises(setup.Refused):
        setup.plan(state)


def test_orphaned_home_or_group_or_range_refuses():
    for state in [snapshot(home=(987, 987, 0o700)), snapshot(groups=existing().groups),
                  snapshot(subuid='rynhult-build:100000:65536')]:
        with pytest.raises(setup.Refused):
            setup.plan(state)


def test_dry_run_prints_all_changes_and_executes_none(monkeypatch, capsys):
    monkeypatch.setattr(setup, 'inspect', lambda: snapshot())
    monkeypatch.setattr(setup, 'prerequisites', lambda commands: None)
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'execute', lambda command: pytest.fail('dry-run executed a mutation'))
    assert setup.main(['--dry-run']) == 0
    output = capsys.readouterr().out
    assert '100000-165535' in output and 'useradd' in output and 'install' in output and 'disable-linger' in output


def test_apply_rechecks_properties_and_stops_at_first_error(monkeypatch):
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'prerequisites', lambda commands: None)
    states = iter([snapshot(), existing()])
    monkeypatch.setattr(setup, 'inspect', lambda: next(states))
    calls = []
    monkeypatch.setattr(setup, 'execute', calls.append)
    assert setup.main([]) == 0
    assert len(calls) == 4
    monkeypatch.setattr(setup, 'inspect', lambda: snapshot())
    def fail(command):
        raise subprocess.CalledProcessError(1, command)
    monkeypatch.setattr(setup, 'execute', fail)
    assert setup.main([]) == 1


def test_apply_requires_root_before_mutations(monkeypatch):
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 1000)
    monkeypatch.setattr(setup, 'inspect', lambda: pytest.fail('read state before root refusal'))
    assert setup.main([]) == 1


def test_shared_uid_or_primary_group_is_not_a_dedicated_account():
    for uid, gid in [(987, 2000), (2000, 987)]:
        state = existing()
        state.peers = [SimpleNamespace(pw_name='alex-agent', pw_uid=uid, pw_gid=gid)]
        with pytest.raises(setup.Refused):
            setup.plan(state)


def test_dry_run_refuses_conflict_without_printing_apply_commands(monkeypatch, capsys):
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'inspect', lambda: existing(cron=True))
    monkeypatch.setattr(setup, 'execute', lambda command: pytest.fail('mutation'))
    assert setup.main(['--dry-run']) == 1
    output = capsys.readouterr()
    assert 'crontab' in output.err and 'APPLY:' not in output.out


@pytest.fixture
def inspected(tmp_path, monkeypatch):
    home, etc, linger = [tmp_path / name for name in ('home', 'etc', 'linger')]
    for path in (home, etc, linger):
        path.mkdir(mode=0o700)
    for name, content in {'login.defs': 'UID_MIN 1000\nSYS_UID_MAX 999\n',
                          'shadow': 'rynhult-build:!:1:0:99999:7:::\n',
                          'subuid': 'rynhult-build:100000:65536\n',
                          'subgid': 'rynhult-build:100000:65536\n'}.items():
        (etc / name).write_text(content)
    monkeypatch.setattr(setup, 'HOME', home)
    monkeypatch.setattr(setup, 'ETC', etc)
    monkeypatch.setattr(setup, 'LINGER', linger)
    monkeypatch.setattr(setup.pwd, 'getpwnam', lambda name: existing().account)
    monkeypatch.setattr(setup.pwd, 'getpwall', lambda: [existing().account])
    monkeypatch.setattr(setup.grp, 'getgrall', lambda: existing().groups)
    monkeypatch.setattr(setup.shutil, 'which', lambda name, path: '/fake/' + name)
    results = {'crontab': (1, '', 'no crontab for rynhult-build\n'),
               'atq': (0, '', ''), 'systemctl': (3, 'inactive\n', '')}
    def query(command):
        assert command in [['crontab', '-u', 'rynhult-build', '-l'], ['atq'],
                            ['systemctl', 'is-active', 'user@987.service']]
        return subprocess.CompletedProcess(command, *results[command[0]])
    monkeypatch.setattr(setup, 'query', query)
    return home, linger, results


def test_inspection_recognizes_verified_absent_schedulers(inspected):
    state = setup.inspect()
    assert not any([state.cron, state.timers, state.manager, state.linger])
    assert state.password == '!'


@pytest.mark.parametrize('tool,result', [('crontab', (1, '', 'permission denied')),
                                         ('crontab', (2, '', '')), ('atq', (1, '', '')),
                                         ('systemctl', (1, '', 'bus unavailable'))])
def test_scheduler_probe_failure_is_not_treated_as_absence(inspected, tool, result):
    inspected[2][tool] = result
    with pytest.raises(setup.Refused):
        setup.inspect()


def test_inspection_detects_jobs_empty_crontab_active_manager_timers_and_linger(inspected):
    home, linger, results = inspected
    results['crontab'] = (0, '', '')
    results['systemctl'] = (0, 'active\n', '')
    units = home / '.config/systemd/user'
    units.mkdir(parents=True)
    (units / 'build.timer').symlink_to('/nonexistent')
    (linger / 'rynhult-build').symlink_to('/nonexistent')
    state = setup.inspect()
    assert state.cron and state.timers and state.manager and state.linger
    results['crontab'] = (1, '', 'no crontab for rynhult-build')
    results['atq'] = (0, '1 Mon Oct 26 13:00:00 2026 a rynhult-build\n', '')
    assert setup.inspect().cron


def test_linked_unit_directory_cannot_hide_timers(inspected):
    home, _, _ = inspected
    (home / '.config').symlink_to('/nonexistent')
    with pytest.raises(setup.Refused, match='linked'):
        setup.inspect()


def test_wrapper_isolates_python_even_with_a_planted_module(tmp_path):
    (tmp_path / 'argparse.py').write_text('raise RuntimeError("planted module executed")\n')
    result = subprocess.run([ROOT / 'deploy/containers/setup-build-account', '--help'],
                            cwd=tmp_path, env={'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(tmp_path)},
                            capture_output=True, text=True)
    assert result.returncode == 0 and '--dry-run' in result.stdout
    assert 'planted module' not in result.stderr


def test_group_alias_cannot_hide_agent_group_membership():
    state = existing()
    state.groups.append(SimpleNamespace(gr_name='acc-agents', gr_gid=987, gr_mem=[]))
    with pytest.raises(setup.Refused):
        setup.plan(state)


def test_failed_postcheck_reports_partial_setup_without_rollback(monkeypatch, capsys):
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'prerequisites', lambda commands: None)
    states = iter([snapshot(), existing(cron=True)])
    monkeypatch.setattr(setup, 'inspect', lambda: next(states))
    calls = []
    monkeypatch.setattr(setup, 'execute', calls.append)
    assert setup.main([]) == 1
    assert len(calls) == 4
    assert 'No automatic rollback' in capsys.readouterr().err


def test_linked_home_is_refused_before_any_command(inspected):
    home, _, _ = inspected
    home.rmdir()
    home.symlink_to('/nonexistent')
    with pytest.raises(setup.Refused, match='home'):
        setup.inspect()


def test_missing_prerequisite_refuses_before_account_creation(monkeypatch, capsys):
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'inspect', lambda: snapshot())
    monkeypatch.setattr(setup.shutil, 'which', lambda name, path: None)
    monkeypatch.setattr(setup, 'execute', lambda command: pytest.fail('mutation before prerequisites'))
    assert setup.main([]) == 1
    assert 'required tool missing' in capsys.readouterr().err
