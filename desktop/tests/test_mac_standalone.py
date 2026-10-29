"""macOS standalone pieces that Linux can check: the portable-main bundle, the Keychain adapter (with its fake), the
start-at-login LaunchAgent and tmux recovery on macOS paths, all behind the staff-group gate. Nothing here touches
launchd, a Keychain or an account; tmux runs only as a private test server this test starts and stops by PID."""
import base64
import json
import os
import plistlib
import shutil
import signal
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'desktop'))

import build_mac_main  # noqa: E402
import mac_gate  # noqa: E402
import mac_keychain  # noqa: E402
import mac_launchd  # noqa: E402
import mac_setup  # noqa: E402
import mac_tmux  # noqa: E402

ARM64 = b'\xcf\xfa\xed\xfe' + struct.pack('<I', 0x0100000C) + b'\0' * 24
X86_64 = b'\xcf\xfa\xed\xfe' + struct.pack('<I', 0x01000007) + b'\0' * 24


# ------------------------------------------------------------ the staff-group gate


def identity():
    return {'agent': 'rynd-501', 'agent_uid': 50001}


def test_the_gate_refuses_off_macos_and_without_provisioning():
    with pytest.raises(mac_gate.Blocked, match='only on macOS'):
        mac_gate.require_ready(501, platform='linux')
    with pytest.raises(mac_gate.Blocked, match='not provisioned'):
        mac_gate.require_ready(501, platform='darwin', read=lambda uid: None, verify=lambda v: None)
    assert mac_gate.require_ready(501, platform='darwin', read=lambda uid: identity(), verify=lambda v: None) == identity()


@pytest.mark.parametrize('answer, reason', [
    ('user is a member of the group\n', 'must never belong to staff'),
    ('', 'membership unknown'),  # dsmemberutil said nothing usable: fail closed
    ('Unable to find user\n', 'membership unknown'),
])
def test_the_gate_is_the_staff_membership_check(monkeypatch, answer, reason):
    """The same dsmemberutil check setup and every app start use; staff is asked first."""
    asked = []

    def dsmemberutil(args, **kwargs):
        asked.append(args)
        return SimpleNamespace(stdout=answer, returncode=0)
    monkeypatch.setattr(mac_setup.subprocess, 'run', dsmemberutil)
    with pytest.raises(mac_gate.Blocked, match=reason):
        mac_gate.require_ready(501, platform='darwin', read=lambda uid: identity(),
                               verify=lambda v: mac_setup.check_groups(v['agent_uid']))
    assert asked[0] == ['/usr/bin/dsmemberutil', 'checkmembership', '-u', '50001', '-g', '20']


def test_every_system_entry_point_is_gated(monkeypatch):
    with pytest.raises(mac_gate.Blocked):  # off macOS, or (on the Mac before setup) not provisioned
        mac_keychain.owner_store(os.getuid())
    with pytest.raises(mac_gate.Blocked, match='staff'):
        mac_keychain.owner_store(os.getuid(), platform='darwin', read=lambda uid: identity(),
                                 verify=lambda v: (_ for _ in ()).throw(ValueError('Dedicated agent must never belong to staff')))
    monkeypatch.setattr(mac_launchd.sys, 'platform', 'linux')
    owner = SimpleNamespace(pw_uid=os.getuid(), pw_dir='/Users/alex', pw_name='alex')
    with pytest.raises(ValueError, match='only on macOS'):
        mac_launchd.apply(owner, 'uninstall')


# ------------------------------------------------------------ Keychain adapter and fake


def test_the_fake_behaves_like_the_store():
    store = mac_keychain.MemoryStore()
    assert store.get('vault-key') is None
    store.put('vault-key', 'first')
    store.put('vault-key', 'second')  # replaces
    assert store.get('vault-key') == 'second'
    assert store.delete('vault-key') is True and store.delete('vault-key') is False
    for bad in ('', 'Vault', 'a b', 'x\n-a other', 'x;y', 'a' * 65, '-w'):
        with pytest.raises(ValueError):
            store.put(bad, 'x')
    with pytest.raises(ValueError):
        store.put('ok', '')
    with pytest.raises(ValueError):
        store.put('ok', 'x' * (mac_keychain.MAX_SECRET + 1))


class Security:
    """A recording stand-in for /usr/bin/security with a real item table."""

    def __init__(self):
        self.items, self.calls = {}, []

    def __call__(self, args, input=None, **kwargs):
        self.calls.append((args, input, kwargs['env']))
        command = args[1:]
        if command == ['-i']:
            words = input.split()
            assert words[0] == 'add-generic-password' and '-U' in words
            account, secret = words[words.index('-a') + 1], words[words.index('-w') + 1]
            self.items[account] = secret
            return SimpleNamespace(returncode=0, stdout='', stderr='')
        account = command[command.index('-a') + 1]
        if command[0] == 'find-generic-password':
            if account not in self.items:
                return SimpleNamespace(returncode=44, stdout='', stderr='')
            return SimpleNamespace(returncode=0, stdout=self.items[account] + '\n', stderr='')
        if command[0] == 'delete-generic-password':
            return SimpleNamespace(returncode=0 if self.items.pop(account, None) else 44, stdout='', stderr='')
        raise AssertionError(command)


def test_keychain_secrets_never_reach_a_command_line():
    security = Security()
    store = mac_keychain.KeychainStore(os.getuid(), run=security)
    secret = 'pa ss "word" \n; -w evil'
    store.put('vault-key', secret)
    assert store.get('vault-key') == secret
    for args, stdin, env in security.calls:
        assert secret not in ' '.join(args) and base64.b64encode(secret.encode()).decode() not in ' '.join(args)
        assert args[0] == '/usr/bin/security' and env['PATH'] == '/usr/bin:/bin'
    written = [stdin for args, stdin, _ in security.calls if args[1:] == ['-i']][0]
    assert written.count('\n') == 1 and secret not in written  # one command, base64 only
    assert f'-s com.rynhult.desktop -a {os.getuid()}.vault-key' in written
    assert store.delete('vault-key') is True and store.get('vault-key') is None
    assert store.delete('vault-key') is False


def test_keychain_failures_are_errors_not_silence(monkeypatch):
    security = Security()
    store = mac_keychain.KeychainStore(os.getuid(), run=lambda args, **kw: SimpleNamespace(returncode=51, stdout='', stderr=''))
    with pytest.raises(mac_keychain.CredentialError, match='exit 51'):
        store.get('vault-key')
    lossy = mac_keychain.KeychainStore(os.getuid(), run=lambda args, input=None, **kw: (
        SimpleNamespace(returncode=0, stdout='', stderr='') if args[1:] == ['-i'] else SimpleNamespace(returncode=44, stdout='', stderr='')))
    with pytest.raises(mac_keychain.CredentialError, match='read back'):
        lossy.put('vault-key', 'secret')  # -i reported success but nothing was stored
    security.items[f'{os.getuid()}.vault-key'] = 'not base64!'
    with pytest.raises(mac_keychain.CredentialError, match='not one Rynhult wrote'):
        mac_keychain.KeychainStore(os.getuid(), run=security).get('vault-key')
    monkeypatch.setattr(mac_keychain.os, 'geteuid', lambda: 0)
    with pytest.raises(mac_keychain.CredentialError, match='never as root'):
        mac_keychain.KeychainStore(os.getuid(), run=security).get('vault-key')
    with pytest.raises(ValueError):
        mac_keychain.KeychainStore(0)


# ------------------------------------------------------------ start-at-login LaunchAgent


def owner(tmp_path):
    return SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_name='alex', pw_dir=str(tmp_path / 'home'))


APP = Path('/Applications/Rynhult Desktop.app/Contents/MacOS/rynhult-desktop')


def test_launch_agent_opens_the_app_once_at_a_graphical_login(tmp_path):
    value = mac_launchd.agent(owner(tmp_path), APP)
    assert value['ProgramArguments'] == [str(APP), '--started-at-login']
    assert value['RunAtLoad'] is True and value['KeepAlive'] is False
    assert value['LimitLoadToSessionType'] == ['Aqua'] and value['ProcessType'] == 'Interactive'
    assert value['StandardErrorPath'] == str(tmp_path / 'home/Library/Logs/Rynhult Desktop/app.log')
    assert set(value['EnvironmentVariables']) == {'PATH', 'LANG'}  # nothing inherited from a shell
    assert plistlib.loads(mac_launchd.render(value)) == value
    plan = mac_launchd.plan(owner(tmp_path), 'install', APP)
    assert plan['plist'] == str(tmp_path / 'home/Library/LaunchAgents/com.rynhult.desktop.app.plist')
    assert plan['commands'][-1] == ['/bin/launchctl', 'bootstrap', f'gui/{os.getuid()}', plan['plist']]
    assert mac_launchd.plan(owner(tmp_path), 'uninstall')['commands'] == [
        ['/bin/launchctl', 'bootout', f'gui/{os.getuid()}/com.rynhult.desktop.app']]


@pytest.mark.parametrize('path', ['/tmp/Rynhult.app/Contents/MacOS/x', '/Applications/../tmp/x.app/Contents/MacOS/x',
                                  '/Applications/Rynhult.app/Contents/Resources/x', '/Applications/a/b.app/Contents/MacOS/x',
                                  'Applications/Rynhult.app/Contents/MacOS/x'])
def test_launch_agent_opens_only_an_installed_app(tmp_path, path):
    with pytest.raises(ValueError):
        mac_launchd.agent(owner(tmp_path), Path(path))


def test_the_app_executable_comes_from_info_plist_and_stays_inside(tmp_path):
    app = tmp_path / 'Rynhult Desktop.app'
    (app / 'Contents').mkdir(parents=True)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': '../../evil'}))
    with pytest.raises(ValueError, match='CFBundleExecutable'):
        mac_launchd.executable_for(app)
    (app / 'Contents/Info.plist').write_bytes(plistlib.dumps({'CFBundleExecutable': 'rynhult-desktop'}))
    with pytest.raises(ValueError, match='/Applications'):  # a build folder is not an installed app
        mac_launchd.executable_for(app)


def test_installing_and_removing_the_agent_on_macos(tmp_path, monkeypatch):
    person = owner(tmp_path)
    calls, gates = [], []
    monkeypatch.setattr(mac_launchd.sys, 'platform', 'darwin')

    def run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0)
    with pytest.raises(mac_gate.Blocked):
        mac_launchd.apply(person, 'install', APP, run=run,
                          gate=lambda uid: (_ for _ in ()).throw(mac_gate.Blocked('staff membership unknown')))
    assert not calls and not Path(mac_launchd.plist_path(person)).exists()  # blocked: nothing written or loaded
    mac_launchd.apply(person, 'install', APP, run=run, gate=gates.append)
    path = mac_launchd.plist_path(person)
    assert gates == [os.getuid()] and oct(path.stat().st_mode & 0o777) == '0o644'
    assert plistlib.loads(path.read_bytes())['Label'] == 'com.rynhult.desktop.app'
    assert (mac_launchd.logs(person)).stat().st_mode & 0o777 == 0o700
    assert calls == mac_launchd.commands(person, 'install')
    path.chmod(0o666)
    with pytest.raises(ValueError, match='writable by others'):
        mac_launchd.check_installed(path, person)
    path.chmod(0o644)
    mac_launchd.apply(person, 'uninstall', run=run, gate=gates.append)
    assert not path.exists() and calls[-1][1] == 'bootout'
    monkeypatch.setattr(mac_launchd.os, 'geteuid', lambda: 0)
    with pytest.raises(ValueError, match='never as root'):
        mac_launchd.apply(person, 'install', APP, run=run, gate=gates.append)


def test_plan_runs_anywhere_as_a_dry_run(capsys):
    assert mac_launchd.main(['plan']) == 1  # needs --app
    assert '--app' in capsys.readouterr().err


# ------------------------------------------------------------ tmux on macOS paths


@pytest.fixture
def agent_run():
    """A short private run directory (tmux sockets have a path limit) and cleanup of any test server by PID."""
    base = Path(tempfile.mkdtemp(prefix='mt', dir='/tmp'))
    value = {'run_dir': str(base / 'run'), 'agent': 'rynd-501', 'agent_uid': os.getuid()}
    (base / 'run/rynd-501').mkdir(parents=True)
    pids = []
    yield value, pids
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    shutil.rmtree(base, ignore_errors=True)


def tmux(value, *args):
    env = {'PATH': os.environ['PATH'], 'TMUX_TMPDIR': str(mac_tmux.tmpdir(value)), 'HOME': '/tmp'}
    return subprocess.run(['tmux', '-L', mac_tmux.LABEL, '-f', '/dev/null', *args], env=env, capture_output=True,
                          text=True, check=True)


@pytest.mark.skipif(shutil.which('tmux') is None, reason='tmux not installed')
def test_recovery_keeps_a_live_server_and_clears_a_dead_one(agent_run):
    value, pids = agent_run
    first = mac_tmux.recover(value, mac_tmux.LABEL)
    assert first['state'] == 'absent' and first['tmpdir'].endswith('/run/rynd-501/tmux')
    assert oct(Path(first['tmpdir']).stat().st_mode & 0o777) == '0o700'
    # The broker's tmux -L lands where recovery looks.
    tmux(value, 'new-session', '-d', '-s', 'acc-0123456789ab', 'sleep 300')
    pids.append(int(tmux(value, 'display-message', '-p', '#{pid}').stdout))
    alive = mac_tmux.recover(value, mac_tmux.LABEL)
    assert alive['state'] == 'alive' and alive['removed_stale'] is False
    assert [s['name'] for s in alive['sessions']] == ['acc-0123456789ab']
    assert alive['socket'] == str(mac_tmux.socket_path(value, mac_tmux.LABEL))
    # The server dies hard (as after a crash): its socket is left behind and nothing answers.
    os.kill(pids[0], signal.SIGKILL)
    deadline = time.time() + 5
    while mac_tmux.probe(Path(alive['socket'])) != 'stale' and time.time() < deadline:
        time.sleep(.05)
    recovered = mac_tmux.recover(value, mac_tmux.LABEL)
    assert recovered['state'] == 'absent' and recovered['removed_stale'] is True
    assert not Path(alive['socket']).exists()
    tmux(value, 'new-session', '-d', '-s', 'acc-fedcba987654', 'sleep 300')  # tmux starts cleanly again
    pids.append(int(tmux(value, 'display-message', '-p', '#{pid}').stdout))
    assert [s['name'] for s in mac_tmux.recover(value, mac_tmux.LABEL)['sessions']] == ['acc-fedcba987654']


def test_recovery_never_removes_what_is_not_the_agents_socket(agent_run):
    value, _ = agent_run
    path = mac_tmux.socket_path(value, mac_tmux.LABEL)
    mac_tmux.prepare(value)
    path.parent.mkdir(mode=0o700)
    path.write_text('not a socket')
    with pytest.raises(ValueError, match='not a tmux socket'):
        mac_tmux.recover(value, mac_tmux.LABEL)
    assert path.exists()
    Path(mac_tmux.tmpdir(value)).chmod(0o755)
    with pytest.raises(ValueError, match='0700'):
        mac_tmux.prepare(value)


def test_macos_socket_paths_fit_sun_path():
    for uid in (501, 2147483647):
        value = {**mac_setup.locations(uid), 'agent_uid': 59999}
        path = mac_tmux.socket_path(value, mac_tmux.LABEL)
        assert str(path).startswith('/Library/Application Support/Rynhult Desktop/Owners/')
        assert len(os.fsencode(path)) < mac_tmux.SUN_PATH
    with pytest.raises(ValueError, match='too long'):
        mac_tmux.socket_path({**mac_setup.locations(501), 'agent_uid': 59999}, 'x' * 30)
    assert mac_tmux.legacy_path('rd', 50001, 'darwin') == Path('/private/tmp/tmux-50001/rd')


# ------------------------------------------------------------ portable-main bundle


def fixture_runtime(base: Path, binary=ARM64) -> Path:
    runtime = base / 'runtime'
    (runtime / 'python/bin').mkdir(parents=True)
    (runtime / 'python/bin/python3').write_bytes(binary)
    (runtime / 'python/bin/python3').chmod(0o755)
    (runtime / 'python/lib/python3.12/site-packages/fixture').mkdir(parents=True)
    (runtime / 'python/lib/python3.12/site-packages/fixture/_native.cpython-312-darwin.so').write_bytes(binary)
    (runtime / 'python/lib/libpython3.12.dylib').write_bytes(binary)
    return runtime


def fixture_web(base: Path) -> Path:
    web = base / 'web-dist'
    (web / 'assets').mkdir(parents=True)
    (web / 'index.html').write_text('<!doctype html><title>Rynhult</title>')
    (web / 'assets/app.js').write_text('console.log(1)')
    return web


@pytest.fixture(scope='module')
def built(tmp_path_factory):
    base = tmp_path_factory.mktemp('mac-main')
    out = base / 'My Builds'  # a space, as in "Application Support"
    bundle = build_mac_main.build(out, fixture_runtime(base), fixture_web(base), revision='test', host_compile=True)
    return bundle


def test_bundle_layout_is_complete_private_and_verified(built):
    manifest = build_mac_main.verify(built)
    assert manifest['arch'] == 'arm64' and manifest['revision'] == 'test'
    files = manifest['files']
    for required in ('bin/rynhult-main', 'desktop/main.py', 'desktop/mac_tmux.py', 'desktop/mac_gate.py',
                     'desktop/web-dist/index.html', 'desktop/runtime/python/bin/python3', 'src/acc/server/app.py',
                     'bin/acc-hook', 'bin/acc-env.sh'):
        assert required in files, required
    assert not any('__pycache__' in name and name.startswith('desktop/runtime') for name in files)
    assert not any(name.startswith('src/acc/static/') for name in files)  # the desktop UI is web-dist
    assert all(int(entry['mode'], 8) & 0o022 == 0 for entry in files.values() if 'mode' in entry)
    assert files['bin/rynhult-main']['mode'] == '0o755'
    assert any(name.endswith('.pyc') for name in files)  # precompiled: a read-only install writes no bytecode


def test_launcher_finds_the_bundle_from_a_path_with_spaces(built, tmp_path):
    copy = tmp_path / 'Application Support' / build_mac_main.NAME
    shutil.copytree(built, copy, symlinks=True)
    python = copy / 'desktop/runtime/python/bin/python3'
    python.unlink()
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$PYTHONPATH" "$1" "$2" "$PYTHONNOUSERSITE" "$PATH"\n')
    python.chmod(0o755)
    result = subprocess.run([str(copy / 'bin/rynhult-main'), 'arg with space'], capture_output=True, text=True,
                            env={'HOME': str(tmp_path), 'SECRET_FROM_PARENT': 'x'}, check=True)
    lines = result.stdout.splitlines()
    assert lines[0] == str((copy / 'src').resolve()) and lines[1] == str((copy / 'desktop/main.py').resolve())
    assert lines[2] == 'arg with space' and lines[3] == '1' and lines[4] == '/usr/bin:/bin'


def test_bundle_refuses_a_runtime_that_is_not_apple_silicon(tmp_path):
    for name, binary in (('linux', b'\x7fELF' + b'\0' * 28), ('intel', X86_64)):
        with pytest.raises(ValueError, match='arm64'):
            build_mac_main.build(tmp_path / name, fixture_runtime(tmp_path / name, binary), fixture_web(tmp_path / name))
        assert not (tmp_path / name / build_mac_main.NAME).exists()
    leaky = tmp_path / 'leaky'
    runtime = fixture_runtime(leaky)
    (runtime / 'python/lib/python3.12/site-packages/fixture/__init__.py').write_text(f'HOME = {str(runtime)!r}\n')
    with pytest.raises(ValueError, match='Not relocatable'):  # names the runtime it was built from
        build_mac_main.build(leaky / 'out', runtime, fixture_web(leaky), host_compile=True)
    assert not (leaky / 'out' / build_mac_main.NAME).exists()
    with pytest.raises(ValueError, match='web UI'):
        build_mac_main.build(tmp_path / 'noweb', fixture_runtime(tmp_path / 'noweb'), tmp_path / 'missing')


def tampered(built, tmp_path):
    copy = tmp_path / build_mac_main.NAME
    shutil.copytree(built, copy, symlinks=True)
    return copy


def resign(bundle):
    """Rewrite the manifest after a change, to reach the checks behind the hash comparison."""
    manifest = json.loads((bundle / 'MANIFEST.json').read_text())
    manifest['files'] = build_mac_main.inventory(bundle)
    (bundle / 'MANIFEST.json').write_text(json.dumps(manifest))


def test_verify_catches_changes_and_unsafe_contents(built, tmp_path):
    bundle = tampered(built, tmp_path)
    (bundle / 'desktop/main.py').write_text('# changed')
    with pytest.raises(ValueError, match='differs from its manifest: desktop/main.py'):
        build_mac_main.verify(bundle)
    (bundle / 'desktop/main.py').write_text(f'PATH = {str(tmp_path)!r}\n')
    resign(bundle)
    with pytest.raises(ValueError, match='Not relocatable'):
        build_mac_main.verify(bundle)
    shutil.copy2(built / 'desktop/main.py', bundle / 'desktop/main.py')
    (bundle / 'desktop/runtime/python/lib/intel.so').write_bytes(X86_64)
    resign(bundle)
    with pytest.raises(ValueError, match='not arm64'):
        build_mac_main.verify(bundle)
    (bundle / 'desktop/runtime/python/lib/intel.so').unlink()
    (bundle / 'desktop/escape').symlink_to('/etc/passwd')
    resign(bundle)
    with pytest.raises(ValueError, match='leaves the bundle'):
        build_mac_main.verify(bundle)
    (bundle / 'desktop/escape').unlink()
    (bundle / 'catalog').chmod(0o775)
    (bundle / 'desktop/main.py').chmod(0o664)
    resign(bundle)
    with pytest.raises(ValueError, match='Writable by group'):
        build_mac_main.verify(bundle)


def test_build_never_replaces_an_existing_bundle(built):
    with pytest.raises(ValueError, match='exists'):
        build_mac_main.build(built.parent, built / 'desktop/runtime', built / 'desktop/web-dist')
    assert build_mac_main.verify(built)
