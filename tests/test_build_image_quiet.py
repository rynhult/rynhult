"""deploy/containers/build-image refuses to build while the building account has other processes (security review 8,
SR8-3, option B): deploy/containers/build_census.py with a fake /proc, and the helper with a fake Podman and census."""
import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('build_census', ROOT / 'deploy/containers/build_census.py')
census = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(census)

UID = 1004
BUILD = 5000


def process(proc, pid, parent, uid=UID, cgroup='/user.slice/user-1004.slice/session-3.scope', state='S (sleeping)',
            exe=None, cmdline=b'sh\0', started=None):
    folder = proc / str(pid)
    folder.mkdir(parents=True)
    (folder / 'status').write_text(f'Name:\tx\nState:\t{state}\nPPid:\t{parent}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n'
                                   'Threads:\t1\n')
    started = pid if started is None else started  # clock ticks since boot; later PIDs started later here
    (folder / 'stat').write_text(f'{pid} (x y) S {parent} ' + ' '.join(['0'] * 17) + f' {started} 0\n')
    (folder / 'cgroup').write_text(f'0::{cgroup}\n')
    (folder / 'cmdline').write_bytes(cmdline)
    if exe:
        (folder / 'exe').symlink_to(exe)


@pytest.fixture
def proc(tmp_path):
    proc = tmp_path / 'proc'
    process(proc, 1, 0, uid=0, cgroup='/init.scope')
    process(proc, 4000, 1, uid=0, cgroup='/user.slice/user-0.slice/session-1.scope')  # the owner's sudo
    process(proc, BUILD, 4000)                                    # build-image
    process(proc, 5001, BUILD)                                    # podman build
    process(proc, 5002, 5001, uid=UID)                            # a RUN step, root in the build's namespace
    process(proc, 5003, 5002, uid=558752 + 999)                   # a RUN step as a container user (subordinate UID)
    process(proc, 5004, BUILD)                                    # this census
    return proc


def test_only_the_builds_own_tree_is_quiet(proc):
    assert census.foreign_processes(BUILD, UID, [range(558752, 558752 + 65536)], proc) == []


def test_every_other_process_of_the_account_is_named(proc):
    """Another agent session, the broker, the user manager, a detached child of the build, and a process running as
    one of the account's subordinate UIDs (inside a user namespace it made) can all write the build's files."""
    process(proc, 6000, 1, cgroup='/user.slice/user-1004.slice/session-7.scope')
    process(proc, 6001, 1, cgroup='/system.slice/system-acc\\x2dbroker.slice/acc-broker@alex-agent.service')
    process(proc, 6002, 1, cgroup='/user.slice/user-1004.slice/user@1004.service/init.scope')
    process(proc, 6003, 1)                                        # a build child that daemonized: no longer the tree's
    process(proc, 6004, 1, uid=558752 + 5, cgroup='/user.slice/user-1004.slice/user@1004.service/app.slice/x.service')
    process(proc, 6005, 1, uid=1005)                              # another account: not this check's
    process(proc, 6006, 1, state='Z (zombie)')                    # can't act
    found = census.foreign_processes(BUILD, UID, [range(558752, 558752 + 65536)], proc)
    assert [(pid, where) for pid, where, _ in found] == [
        (6000, 'login session'), (6001, 'system service'), (6002, 'user manager'), (6003, 'login session'),
        (6004, 'user manager unit')]
    assert found[1][2] == '/system.slice/system-acc\\x5cx2dbroker.slice/acc-broker@alex-agent.service'  # escaped


def test_the_shell_that_started_the_build_is_named_as_such(proc):
    """`sudo -iu` runs the account's own login shell (and its startup files) above the build: outside its tree."""
    (proc / str(BUILD) / 'status').write_text((proc / str(BUILD) / 'status').read_text().replace('PPid:\t4000',
                                                                                                'PPid:\t4500'))
    process(proc, 4500, 4000)
    [(pid, where, _)] = census.foreign_processes(BUILD, UID, [], proc)
    assert pid == 4500 and where.startswith('starts the build (run build-image with sudo -u or runuser')


@pytest.mark.parametrize('exe,cmdline,allowed', [
    ('/usr/bin/catatonit', b'catatonit\0-P\0', True),
    ('/usr/libexec/podman/catatonit', b'catatonit\0-P\0', True),
    ('/usr/bin/catatonit', b'catatonit\0--\0/bin/sh\0', False),   # catatonit as an init for something else
    ('/home/alex-agent/catatonit', b'catatonit\0-P\0', False),    # a lookalike: the exe link isn't the system's
    (None, b'catatonit\0-P\0', False),                             # exe unreadable
])
def test_podmans_pause_process_is_allowed_only_by_its_executable(proc, exe, cmdline, allowed):
    process(proc, 6100, 1, cgroup='/user.slice/user-1004.slice/session-3.scope', exe=exe, cmdline=cmdline)
    found = census.foreign_processes(BUILD, UID, [], proc)
    assert (found == []) is allowed


def test_an_unobservable_process_table_refuses(tmp_path, proc):
    assert census.foreign_processes(BUILD, UID, [], tmp_path / 'missing') is None
    assert census.foreign_processes(4242, UID, [], proc) is None  # the build's own shell isn't visible


def test_subordinate_ranges_by_name_or_uid(tmp_path):
    subuid = tmp_path / 'subuid'
    subuid.write_text('alex-agent:558752:65536\n1004:700000:10\nother:1:5\nbroken\n')
    assert census.subordinate('alex-agent', 1004, subuid) == [range(558752, 624288), range(700000, 700010)]
    assert census.subordinate('alex-agent', 1004, tmp_path / 'none') == []


def fake_run(outputs):
    def run(command, **kwargs):
        code, text = outputs[Path(command[0]).name]
        return subprocess.CompletedProcess(command, code, text, '')
    return run


def test_settings_that_start_processes_unattended_are_refused(tmp_path):
    home, linger, tools = tmp_path / 'home', tmp_path / 'linger', tmp_path / 'bin'
    for folder in (home, linger, tools):
        folder.mkdir()
    quiet = fake_run({'crontab': (1, ''), 'atq': (0, '')})
    assert census.settings_problems('alex-agent', home, linger=linger, tools=tools, run=quiet) == []
    (tools / 'crontab').touch()
    (tools / 'atq').touch()
    assert census.settings_problems('alex-agent', home, linger=linger, tools=tools,
                                    run=fake_run({'crontab': (1, ''), 'atq': (0, '')})) == []  # "no crontab for ..."
    (linger / 'alex-agent').touch()
    timers = home / '.config/systemd/user/timers.target.wants'
    timers.mkdir(parents=True)
    (timers / 'evil\x1b[2J.timer').symlink_to('/nonexistent')
    busy = fake_run({'crontab': (0, '# m h dom mon dow\n*/5 * * * * /home/alex-agent/x\n'), 'atq': (0, '3\tTue x a\n')})
    problems = census.settings_problems('alex-agent', home, linger=linger, tools=tools, run=busy)
    assert problems[0].startswith('lingering is enabled (as root: loginctl disable-linger alex-agent)')
    assert 'evil\\x1b\\x5b2J.timer' in problems[1] and '\x1b' not in problems[1]
    assert 'a crontab' in problems[2] and 'queued at jobs' in problems[3]
    comments = fake_run({'crontab': (0, '# only comments\n'), 'atq': (0, '')})
    (linger / 'alex-agent').unlink()
    (timers / 'evil\x1b[2J.timer').unlink()
    assert census.settings_problems('alex-agent', home, linger=linger, tools=tools, run=comments) == []


def test_only_a_pause_process_started_after_the_build_is_its_own(proc):
    """Kit review H-1: the build gives Podman its own tmp dir, so its pause process is always new. An older catatonit
    passes the exe and command line tests but may run injected code (a tracing parent can write it after exec)."""
    ranges = [range(558752, 558752 + 65536)]
    process(proc, 7000, 1, exe='/usr/bin/catatonit', cmdline=b'catatonit\0-P\0', started=BUILD + 1)  # the build's
    assert census.foreign_processes(BUILD, UID, ranges, proc) == []
    process(proc, 7001, 1, exe='/usr/bin/catatonit', cmdline=b'catatonit\0-P\0', started=BUILD - 1)  # older
    [(pid, where, _)] = census.foreign_processes(BUILD, UID, ranges, proc)
    assert pid == 7001 and where.startswith('a Podman pause process from before this build (left by an earlier build: kill 7001')
    (proc / '7000' / 'stat').unlink()  # start time unknown: not exempt either
    assert [pid for pid, _, _ in census.foreign_processes(BUILD, UID, ranges, proc)] == [7000, 7001]


def cgroups(tmp_path, counts):
    root = tmp_path / 'cgroup'
    for cgroup, count in counts.items():
        (root / cgroup.lstrip('/')).mkdir(parents=True, exist_ok=True)
        (root / cgroup.lstrip('/') / 'pids.current').write_text(f'{count}\n')
    return root


USER_SLICE = '/user.slice/user-1004.slice'
BROKER = '/system.slice/system-acc\\x2dbroker.slice/acc-broker@alex-agent.service'


def test_tasks_the_process_list_never_showed_are_refused(proc, tmp_path, monkeypatch):
    """Kit review H-2: a process that keeps forking and exiting is never at one PID long enough to be read from /proc
    (a real one was missed by 20 of 20 scans). The kernel's per-cgroup task count can't be dodged that way."""
    entry = census.pwd.struct_passwd(('alex-agent', 'x', UID, UID, '', '/home/alex-agent', '/bin/sh'))
    monkeypatch.setattr(census.pwd, 'getpwuid', lambda uid: entry)
    monkeypatch.setattr(census.os, 'getuid', lambda: UID)
    monkeypatch.setattr(census, 'SCAN_GAP', 0)
    for pid in list(proc.iterdir()):  # this build runs from the owner's session, outside the account's cgroups
        if pid.name in ('5000', '5001', '5002', '5003', '5004'):
            text = (pid / 'cgroup').read_text().replace('/user.slice/user-1004.slice/session-3.scope',
                                                        '/user.slice/user-0.slice/session-1.scope')
            (pid / 'cgroup').write_text(text)
    quiet = cgroups(tmp_path, {USER_SLICE: 0, BROKER: 0})
    check = lambda root: census.check('before saving the image', BUILD, proc=proc, subuid=proc / 'none', cgroup_root=root)
    assert check(quiet) is None
    assert check(tmp_path / 'no-cgroups') is None  # neither exists: nothing can run there
    hopping = cgroups(tmp_path / 'hop', {USER_SLICE: 1, BROKER: 2})
    text = check(hopping)
    assert 'never showed (a process that keeps changing its PID)' in text
    assert '1 in /user.slice/user-1004.slice' in text and '2 in /system.slice/system-acc\\x5cx2dbroker.slice' in text
    # A process the scan does see there is reported as itself, not twice.
    process(proc, 6000, 1, cgroup=BROKER)
    seen = cgroups(tmp_path / 'seen', {USER_SLICE: 0, BROKER: 1})
    text = check(seen)
    assert 'PID 6000: system service' in text and 'never showed' not in text
    unreadable = cgroups(tmp_path / 'bad', {USER_SLICE: 'x', BROKER: 0})
    assert 'could not be read' in check(unreadable)


def test_the_refusal_names_the_step_the_account_and_each_process(proc, monkeypatch):
    process(proc, 6000, 1, cgroup='/user.slice/user-1004.slice/session-7.scope')
    entry = census.pwd.struct_passwd(('alex-agent', 'x', UID, UID, '', '/home/alex-agent', '/bin/sh'))
    monkeypatch.setattr(census.pwd, 'getpwuid', lambda uid: entry)
    monkeypatch.setattr(census.os, 'getuid', lambda: UID)
    text = census.check('before saving the image', BUILD, proc=proc, subuid=proc / 'none', cgroup_root=proc / 'no-cgroups')
    assert text.startswith('build-image: refused before saving the image: the building account alex-agent could change')
    assert 'it has 1 other process(es)' in text
    assert 'PID 6000: login session, cgroup /user.slice/user-1004.slice/session-7.scope' in text
    assert 'acc-broker@alex-agent.service' in text
    (proc / '6000' / 'status').unlink()  # it ended
    assert census.check('before saving the image', BUILD, proc=proc, subuid=proc / 'none', cgroup_root=proc / 'no-cgroups') is None


# ---- build-image with a fake Podman and a fake census -----------------------------------------------------------------

FAKE_PODMAN = (ROOT / 'tests/test_container_image_layout.py').read_text().split("FAKE_PODMAN = r'''", 1)[1].split("'''", 1)[0]

FAKE_CENSUS = '''#!/bin/sh
printf '%s %s\\n' "$1" "$2" >> "$CENSUS_LOG"
[ "$1" != "$CENSUS_REFUSE" ] || { echo "build-image: refused at $1 (fake census)" >&2; exit 1; }
exit 0
'''


def build(tmp_path, refuse=None):
    if os.getuid() == 0:
        pytest.skip('the helper refuses root by design')
    podman, fake = tmp_path / 'podman', tmp_path / 'census'
    podman.write_text(FAKE_PODMAN)
    fake.write_text(FAKE_CENSUS)
    for path in (podman, fake):
        path.chmod(0o755)
    out = tmp_path / 'out'
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path / 'home'), 'ACC_BUILD_PODMAN': str(podman),
           'FAKE_LOG': str(tmp_path / 'calls'), 'ACC_BUILD_CENSUS': str(fake), 'CENSUS_LOG': str(tmp_path / 'census.log'),
           'CENSUS_REFUSE': refuse or ''}
    result = subprocess.Popen([ROOT / 'deploy/containers/build-image', out], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    stdout, stderr = result.communicate(timeout=60)
    log = tmp_path / 'census.log'
    checks = [line.split() for line in log.read_text().splitlines()] if log.exists() else []
    assert all(pid == str(result.pid) for _, pid in checks)  # always the helper's own shell
    calls = tmp_path / 'calls'
    podman_calls = [json.loads(line)['args'] for line in calls.read_text().splitlines()] if calls.exists() else []
    return result.returncode, stdout, stderr, [step for step, _ in checks], podman_calls, out


def verbs(calls):
    return [args[args.index('overlay') + 1] for args in calls]


def test_a_quiet_build_is_checked_at_start_before_saving_and_before_the_checksum(tmp_path):
    code, stdout, stderr, checks, calls, out = build(tmp_path)
    assert code == 0, stderr
    assert checks == ['start', 'save', 'checksum']
    assert verbs(calls)[:3] == ['build', 'image', 'save']
    assert (out / 'acc-session.oci.tar.sha256').exists() and 'archive: ' in stdout


def test_a_busy_account_never_starts_a_build(tmp_path):
    code, stdout, stderr, checks, calls, out = build(tmp_path, refuse='start')
    assert code != 0 and 'refused at start' in stderr
    assert checks == ['start'] and calls == [] and not out.exists()  # nothing made, not even the output folder


def test_a_process_found_before_saving_stops_the_build_before_podman_save(tmp_path):
    code, stdout, stderr, checks, calls, out = build(tmp_path, refuse='save')
    assert code != 0 and 'refused at save' in stderr and 'the build FAILED' in stderr
    assert 'save' not in verbs(calls) and not (out / 'acc-session.oci.tar').exists()
    assert verbs(calls)[-1] == 'unshare'  # the throwaway store is still removed


def test_a_process_found_before_the_checksum_leaves_nothing_to_pin(tmp_path):
    code, stdout, stderr, checks, calls, out = build(tmp_path, refuse='checksum')
    assert code != 0 and 'refused at checksum' in stderr and 'the build FAILED' in stderr
    assert checks == ['start', 'save', 'checksum'] and 'save' in verbs(calls)
    assert not (out / 'acc-session.oci.tar').exists() and not (out / 'acc-session.oci.tar.sha256').exists()
    assert not (out / 'image.txt').exists() and 'archive: ' not in stdout and 'image: ' not in stdout


def test_the_real_census_runs_from_the_helper_and_refuses_a_busy_account(tmp_path):
    """The installed helper runs build_census.py directly: it must be executable, and on this test machine (the
    runner's account has other processes: pytest's parent shell at least) it refuses before anything is made."""
    if os.getuid() == 0:
        pytest.skip('the helper refuses root by design')
    assert os.access(ROOT / 'deploy/containers/build_census.py', os.X_OK)
    out = tmp_path / 'out'
    result = subprocess.run([ROOT / 'deploy/containers/build-image', out], capture_output=True, text=True, timeout=60,
                            env={'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path), 'ACC_BUILD_PODMAN': '/bin/false'})
    assert result.returncode != 0 and not out.exists()
    assert result.stderr.startswith('build-image: refused before starting: ')
    assert 'other process(es)' in result.stderr  # named even when a setting is refused too


def test_shown_is_the_same_as_the_qualification_drivers():
    """build_census.py runs standalone from deploy/containers (as the build account), so it keeps its own copy of the
    driver's cgroup escaping; the copies must not drift. (Their label() rules differ; aligning them is still open.)"""
    import sys
    driver_dir = ROOT / 'scripts/container_qualification'
    spec = importlib.util.spec_from_file_location('qualification_driver', driver_dir / 'driver.py')
    driver = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(driver_dir))
    try:
        spec.loader.exec_module(driver)
    finally:
        sys.path.pop(0)
    assert census.SHOWN == driver.SHOWN
    for text in ['/user.slice/user-1004.slice/session-3.scope', 'a\\x41', 'bad\nname\x1b[31m', 'ü' * 50, '/' * 200,
                 'x' * 160, 'x' * 161, '\udcff surrogate']:
        assert census.shown(text) == driver.shown(text), text
        assert census.shown(text, limit=20) == driver.shown(text, limit=20), text
