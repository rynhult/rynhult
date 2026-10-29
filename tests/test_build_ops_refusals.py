"""Image build helpers: refusal and fail-closed paths the main tests don't reach (build account setup, the quiet-account
census, root's archive custody). Fakes and temporary files only: no account, process or root operation."""
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_build_image_quiet import BUILD, UID, census, proc, process  # noqa: F401 (fixture)
from test_setup_build_account import existing, setup, snapshot
from test_take_archive import layout, take_archive  # noqa: F401 (fixture)

# ---- setup_build_account ----------------------------------------------------------------------------------------------


def test_the_build_home_parent_must_be_root_owned_all_the_way_up(tmp_path, monkeypatch):
    # Root creates and chowns the home: a parent the agent account can write could redirect it.
    (tmp_path / 'lib').mkdir()
    monkeypatch.setattr(setup, 'HOME', tmp_path / 'lib/rynhult-build')
    if os.geteuid() != 0:
        with pytest.raises(setup.Refused, match='unsafe build home parent'):
            setup.protected_parent()
    (tmp_path / 'linked').symlink_to(tmp_path / 'lib')
    monkeypatch.setattr(setup, 'HOME', tmp_path / 'linked/rynhult-build')
    with pytest.raises(setup.Refused, match=f'unsafe build home parent: {tmp_path / "linked"}'):
        setup.protected_parent()
    system = Path('/var/lib')
    if all(p.lstat().st_uid == 0 and not p.lstat().st_mode & 0o022 for p in (system, *system.parents)):
        monkeypatch.setattr(setup, 'HOME', system / 'rynhult-build')
        setup.protected_parent()


def test_comments_and_blank_lines_in_subordinate_files_are_skipped():
    assert setup.ranges('# reviewed 2026-10-01\n\nalex:100000:65536\n') == [('alex', 100000, 65536)]


def test_an_existing_account_without_ranges_or_a_full_id_space_refuses():
    with pytest.raises(setup.Refused, match='lacks subordinate ranges'):
        setup.allocate('', '', 987)
    with pytest.raises(setup.Refused, match='no free subordinate-ID range'):
        setup.allocate('alex:100000:4294860000\n', '')  # ends 7295 IDs short of the top


def test_missing_tools_or_login_shell_refuse(monkeypatch, tmp_path):
    monkeypatch.setattr(setup, 'PATH', str(tmp_path))
    with pytest.raises(setup.Refused, match='required tool missing: useradd'):
        setup.tool('useradd')
    monkeypatch.setattr(setup, 'tool', lambda name: '/fake/' + name)
    monkeypatch.setattr(setup, 'SHELL', str(tmp_path / 'nologin'))
    with pytest.raises(setup.Refused, match='nologin shell missing'):
        setup.prerequisites([['useradd']])


def test_apply_refuses_when_the_post_check_still_plans_changes(monkeypatch, capsys):
    monkeypatch.setattr(setup.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(setup, 'protected_parent', lambda: None)
    monkeypatch.setattr(setup, 'prerequisites', lambda commands: None)
    states = iter([snapshot(), existing(linger=True)])  # lingering came back between apply and check
    monkeypatch.setattr(setup, 'inspect', lambda: next(states))
    monkeypatch.setattr(setup, 'execute', lambda command: None)
    assert setup.main([]) == 1
    assert 'post-check still requires changes' in capsys.readouterr().err


def test_inspection_without_the_account_reads_no_scheduler(tmp_path, monkeypatch):
    etc = tmp_path / 'etc'
    etc.mkdir()
    for name, content in {'login.defs': '# none\nUID_MIN 1000\n', 'subuid': '', 'subgid': ''}.items():
        (etc / name).write_text(content)
    monkeypatch.setattr(setup, 'HOME', tmp_path / 'absent-home')
    monkeypatch.setattr(setup, 'ETC', etc)
    monkeypatch.setattr(setup, 'LINGER', tmp_path / 'linger')
    monkeypatch.setattr(setup.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    monkeypatch.setattr(setup, 'query', lambda command: pytest.fail('probed schedulers of an absent account'))
    state = setup.inspect()
    assert (state.account, state.home, state.password, state.system_max, state.linger) == (None, None, None, 999, False)


# ---- build_census -----------------------------------------------------------------------------------------------------


@pytest.fixture
def account(monkeypatch, tmp_path):
    entry = census.pwd.struct_passwd(('alex-agent', 'x', UID, UID, '', str(tmp_path / 'agent-home'), '/bin/sh'))
    monkeypatch.setattr(census.pwd, 'getpwuid', lambda uid: entry)
    monkeypatch.setattr(census.os, 'getuid', lambda: UID)
    monkeypatch.setattr(census, 'SCAN_GAP', 0)
    return entry


def test_an_unlistable_process_table_refuses_the_build(tmp_path, account):
    text = census.check('before saving the image', BUILD, proc=tmp_path / 'no-proc', subuid=tmp_path / 'none',
                        cgroup_root=tmp_path / 'no-cgroups')
    assert text.startswith('build-image: refused before saving the image')
    assert "its processes could not be listed (/proc), so the build can't be shown to be alone" in text


def test_start_also_refuses_settings_that_start_processes_unattended(proc, tmp_path, account):  # noqa: F811
    linger = tmp_path / 'linger'
    linger.mkdir()
    (linger / 'alex-agent').touch()
    text = census.check('before starting', BUILD, proc=proc, subuid=proc / 'none', cgroup_root=tmp_path / 'none',
                        settings=True, linger=linger, tools=tmp_path / 'no-tools')
    assert 'it can start processes unattended:\n  lingering is enabled' in text


def test_a_scheduler_check_that_cannot_run_is_reported_not_skipped(tmp_path):
    tools = tmp_path / 'bin'
    tools.mkdir()
    (tools / 'crontab').touch()
    def broken(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 15)
    problems = census.settings_problems('alex-agent', tmp_path / 'home', linger=tmp_path, tools=tools, run=broken)
    assert problems == [f'{tools / "crontab"} could not be run to check for a crontab']


def test_many_foreign_processes_are_summarised(proc, account, tmp_path):  # noqa: F811
    for pid in range(6000, 6000 + census.LIMIT + 3):
        process(proc, pid, 1, cgroup='/user.slice/user-1004.slice/user@1004.service/app.slice/x.service')
    text = census.check('before starting', BUILD, proc=proc, subuid=proc / 'none', cgroup_root=tmp_path / 'none')
    assert f'it has {census.LIMIT + 3} other process(es)' in text and '  and 3 more' in text
    assert text.count('user manager unit') == census.LIMIT


@pytest.mark.parametrize('cgroup, where', [
    ('/user.slice/user-1004.slice/user@1004.service/app.slice/dbus.service', 'session bus'),
    ('/user.slice/user-1004.slice/user@1004.service/init.scope', 'user manager'),
    ('/user.slice/user-1004.slice/user@1004.service/podman-pause-1.scope', 'Podman pause scope'),
    ('/user.slice/user-1004.slice/user@1004.service/podman-pause-1.service', 'user manager unit'),
    ('/machine.slice/libpod-1.scope', 'other'),
])
def test_foreign_processes_get_fixed_labels(cgroup, where):
    assert census.label(cgroup, UID, False) == where


def test_the_command_line_takes_only_a_step_and_a_pid_and_never_runs_as_root(monkeypatch, capsys):
    monkeypatch.setattr(census, 'check', lambda step, pid, settings: None)
    monkeypatch.setattr(census.os, 'getuid', lambda: UID)
    for argv in ([], ['start'], ['stop', '1'], ['start', '-1'], ['start', '1x'], ['start', '1', '2']):
        assert census.main(argv) == 2
    assert census.main(['save', str(BUILD)]) == 0
    monkeypatch.setattr(census, 'check', lambda step, pid, settings: f'refused {step} {settings}')
    assert census.main(['start', str(BUILD)]) == 1
    assert 'refused before starting True' in capsys.readouterr().err
    monkeypatch.setattr(census.os, 'getuid', lambda: 0)
    assert census.main(['checksum', str(BUILD)]) == 2


# ---- take_archive -----------------------------------------------------------------------------------------------------


def test_custody_command_line_refuses_without_the_builder_or_with_wrong_arguments(layout, monkeypatch, capsys):  # noqa: F811
    home, archive, custody = layout
    assert take_archive.main([str(archive)]) == 2
    monkeypatch.setattr(take_archive.pwd, 'getpwnam', lambda name: (_ for _ in ()).throw(KeyError(name)))
    assert take_archive.main([str(archive), str(custody / 'a.tar')]) == 1
    assert 'take_archive: refused' in capsys.readouterr().err
    assert not (custody / 'a.tar').exists()
    monkeypatch.setattr(take_archive.pwd, 'getpwnam', lambda name: SimpleNamespace(pw_uid=os.getuid()))
    assert take_archive.main([str(archive), str(custody / 'b.tar')]) == 1  # not inside /var/lib/rynhult-build
    taken = []
    monkeypatch.setattr(take_archive, 'take', lambda *args: taken.append(args))
    assert take_archive.main([str(archive), str(custody / 'c.tar')]) == 0
    assert taken == [(str(archive), str(custody / 'c.tar'))]
    assert 'verify it against the recorded pins' in capsys.readouterr().out


def test_a_relative_source_and_the_home_itself_are_refused(layout):  # noqa: F811
    home, archive, custody = layout
    for source in (Path('build-1/acc-session.oci.tar'), home):
        with pytest.raises(take_archive.Refused, match='plain path inside'):
            take_archive.take(source, custody / 'x.tar', home, os.getuid())


def test_a_large_archive_is_copied_whole_and_privately(layout):  # noqa: F811
    home, archive, custody = layout
    data = os.urandom(take_archive.CHUNK * 2 + 17)
    archive.write_bytes(data)
    take_archive.take(archive, custody / 'big.tar', home, os.getuid())
    copy = custody / 'big.tar'
    assert copy.read_bytes() == data and stat.S_IMODE(copy.stat().st_mode) == 0o600
