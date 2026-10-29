"""Destructive qualification ONLY inside a disposable rootless Podman/systemd guest.

No live instance, host broker or real provider is used. The root driver creates
fixture owners/accounts in the guest; owner phases use the installed desktop main.
"""
from __future__ import annotations

import argparse
import hmac
import json
import os
import pwd
import secrets
import selectors
import shutil
import subprocess
import time
from pathlib import Path

OWNERS = ('desktop-alex', 'desktop-blair')
STATE = Path('/var/lib/desktop-qualification')


def require_disposable(*, root=False):
    try:
        mapping = [list(map(int, row.split())) for row in Path('/proc/self/uid_map').read_text().splitlines()]
        valid = (Path('/proc/1/comm').read_text().strip() == 'systemd'
                 and Path('/run/systemd/container').read_text().strip() == 'podman'
                 and any(a == 0 and b > 0 and n > 0 for a, b, n in mapping)
                 and not any(a == 0 and b == 0 for a, b, n in mapping)
                 and (not root or os.geteuid() == 0))
    except (OSError, ValueError):
        valid = False
    if not valid:
        raise RuntimeError('requires a disposable rootless Podman guest with systemd PID 1; refusing host execution')


def run(*command, check=True, timeout=90):
    return subprocess.run(command, check=check, capture_output=True, text=True, timeout=timeout)


def wait_for(check, seconds=40):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(.2)
    raise AssertionError('qualification condition timed out')


def resources():
    query = ('pacman', '-Qlq') if shutil.which('pacman') else ('dpkg-query', '-L')
    files = run(*query, 'rynhult-linux-spike').stdout.splitlines()
    setup = [Path(p) for p in files if p.endswith('/desktop/setup.py')]
    assert len(setup) == 1, 'installed desktop package is missing'
    return setup[0].parent.parent


def manifest(uid):
    return json.loads(Path(f'/etc/rynhult-desktop/{uid}/execution.json').read_text())


def private_json(path, value):
    path.write_text(json.dumps(value, indent=2) + '\n')
    path.chmod(0o600)


def terminal_stream(client, origin, session, provider):
    from websockets.sync.client import connect

    cookies = '; '.join(f'{key}={value}' for key, value in client.cookies.items())
    url = origin.replace('http://', 'ws://', 1) + f'/api/sessions/{session}/tty?mode=read'
    received = b''
    with connect(url, origin=origin, additional_headers={'Cookie': cookies}, open_timeout=15) as ws:
        for _ in range(20):
            value = ws.recv(timeout=15)
            if isinstance(value, bytes):
                received += value
                if f'fake-{provider} ready'.encode() in received:
                    return
    raise AssertionError('PTY WebSocket did not stream fake-provider terminal bytes')


def owner_phase(phase):
    require_disposable()
    assert os.geteuid() >= 1000
    import httpx

    entry = pwd.getpwuid(os.getuid())
    root = resources()
    profile = Path(entry.pw_dir) / '.desktop-qualification'
    profile.mkdir(mode=0o700, exist_ok=True)
    # Fake owner credentials must never satisfy the agent's auth checks.
    (profile / '.qualification-auth').mkdir(mode=0o700, exist_ok=True)
    (profile / '.qualification-auth/claude').write_text('owner fixture, never agent credentials')
    token = secrets.token_hex(32)
    errors = (profile / f'main-{phase}.stderr').open('w')
    proc = subprocess.Popen([str(root / 'desktop/runtime/python/bin/python3'), str(root / 'desktop/main.py')],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors, text=True, cwd=profile,
        env={'HOME': str(profile), 'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(root / 'src'),
             'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        proc.stdin.write(json.dumps({'token': token, 'data_dir': str(profile),
                                    'static_dir': str(root / 'desktop/web-dist')}) + '\n')
        proc.stdin.flush()
        with selectors.DefaultSelector() as ready:
            ready.register(proc.stdout, selectors.EVENT_READ)
            assert ready.select(40), 'desktop main readiness timed out'
        line = proc.stdout.readline()
        assert line, f'desktop main exited before readiness: {profile / ("main-"+phase+".stderr")}'
        hello = json.loads(line)
        origin = hello['origin']
        assert hmac.compare_digest(hello['proof'], hmac.digest(token.encode(), origin.encode(), 'sha256').hex())
        with httpx.Client(base_url=origin, cookies={'rynhult_desktop': token}, timeout=60) as client:
            client.get('/').raise_for_status()
            client.headers.update({'Origin': origin, 'X-ACC-CSRF': client.cookies['acc_csrf']})

            def api(method, path, **kwargs):
                response = client.request(method, path, **kwargs)
                assert response.is_success, f'{method} {path}: {response.status_code} {response.text[:1000]}'
                return response.json()

            info = manifest(entry.pw_uid)
            status = api('GET', '/api/desktop/status')
            assert status['provisioned'] and status['account'] == info['agent']
            manager = run('systemctl', '--user', 'is-active', 'default.target')
            assert manager.stdout.strip() == 'active'
            state_path = profile / 'qualification-state.json'
            if phase == 'first':
                auth = api('GET', '/api/accounts?refresh=1')
                assert all(not auth[p]['logged_in'] for p in ('claude', 'codex')), auth
                bad = api('POST', '/api/accounts/claude/login')
                failure = api('POST', f"/api/accounts/login/{bad['flow_id']}/code", json={'code': 'wrong-fixture-code'})
                assert failure['done'] and not failure['ok']
                assert not api('GET', '/api/accounts?refresh=1')['claude']['logged_in']
                for provider in ('claude', 'codex'):
                    flow = api('POST', f'/api/accounts/{provider}/login')
                    assert flow['url'].startswith('https://login.example.com/')
                    if provider == 'claude':
                        done = api('POST', f"/api/accounts/login/{flow['flow_id']}/code", json={'code': 'qualification-code'})
                    else:
                        done = wait_for(lambda: (x if (x := api('GET', f"/api/accounts/login/{flow['flow_id']}"))['done'] else None))
                    assert done['done'] and done['ok'], done
                auth = api('GET', '/api/accounts?refresh=1')
                assert all(auth[p]['logged_in'] for p in ('claude', 'codex'))
                project = api('POST', '/api/projects/new', json={'name': 'Qualification demo', 'source': 'empty'})
                refused = client.post('/api/sessions', json={'project_id': project['id'], 'provider': 'claude', 'name': 'Unapproved'})
                assert refused.status_code == 409
                api('POST', f"/api/desktop/projects/{project['id']}/native", json={'accept': True})
                state = {'agent': info['agent'], 'project': project, 'sessions': []}
                for provider in ('claude', 'codex'):
                    session = api('POST', '/api/sessions', json={'project_id': project['id'], 'provider': provider,
                                                              'name': f'Qualification {provider}'})
                    assert session['run_as'] == info['agent']
                    wait_for(lambda: f'fake-{provider} ready' in api('GET', f"/api/sessions/{session['id']}/terminal-output")['text'])
                    process = Path(info['run_dir']) / info['agent'] / session['id'] / 'qualification-process.json'
                    identity = json.loads(wait_for(lambda: process.read_text() if process.exists() else None))
                    assert identity['uid'] == pwd.getpwnam(info['agent']).pw_uid != entry.pw_uid
                    assert identity['home'] == info['home'] and not identity['owner_home_readable']
                    wait_for(lambda: any(e['kind'] == 'SessionStart' and e['source'] == provider + '_hook'
                        for e in api('GET', f"/api/sessions/{session['id']}/events")))
                    assert api('GET', f"/api/sessions/{session['id']}")['native_id']
                    terminal_stream(client, origin, session['id'], provider)
                    state['sessions'].append({'id': session['id'], 'provider': provider, 'identity': identity})
                private_json(state_path, state)
                api('POST', f"/api/files/project/{project['id']}", json={'action': 'create',
                    'path': 'acl-proof.txt', 'content': 'owner can read inherited agent checkout\n'})
                assert Path(project['workspace_path'], 'acl-proof.txt').read_text().startswith('owner can read')
            else:
                state = json.loads(state_path.read_text())
                auth = api('GET', '/api/accounts?refresh=1')
                assert all(auth[p]['logged_in'] for p in ('claude', 'codex'))
                for session in state['sessions']:
                    data = api('GET', f"/api/sessions/{session['id']}")
                    assert data['desired_state'] == 'running'
                    assert f"fake-{session['provider']} ready" in api('GET', f"/api/sessions/{session['id']}/terminal-output")['text']
                    assert Path(f"/proc/{session['identity']['pid']}").is_dir()
                    try:
                        os.kill(session['identity']['pid'], 0)
                    except PermissionError:
                        pass  # the owner must not gain signal permission over the agent UID
                    else:
                        raise AssertionError('desktop owner could signal the separate agent account')
                    terminal_stream(client, origin, session['id'], session['provider'])
                    api('POST', f"/api/sessions/{session['id']}/stop")
                assert Path(state['project']['workspace_path'], 'acl-proof.txt').read_text().startswith('owner can read')
            private_json(profile / f'result-{phase}.json', {'status': 'passed', 'phase': phase,
                'owner_uid': entry.pw_uid, 'account': info['agent'], 'owner_user_manager': 'active',
                'auth': 'fake paste-code and device-code; persisted' if phase == 'first' else 'fake credentials retained',
                'sessions': len(state['sessions']), 'terminal': 'real read-only PTY WebSocket before/after logout',
                'hooks': 'SessionStart ingested for both providers across the real owner/agent ACL',
                'main_pid': proc.pid})
    finally:
        if proc.stdin:
            proc.stdin.close()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()  # only this owned companion PID
            proc.wait(timeout=10)
            raise AssertionError('desktop main did not shut down after EOF') from None
        errors.close()
    assert proc.returncode == 0


def provision():
    require_disposable(root=True)
    root = resources()
    STATE.mkdir(mode=0o700)
    for index, owner in enumerate(OWNERS):
        uid = 1000 + index
        run('useradd', '--create-home', '--uid', str(uid), '--shell', '/bin/bash', owner)
        home = Path(pwd.getpwnam(owner).pw_dir)
        home.chmod(0o755)  # world-readable marker tests ProtectHome independently of Unix home permissions
        (home / 'public-fixture').write_text('must be hidden from the agent service by ProtectHome')
    # Reserve both ordinary identities before setup allocates any agent UID.
    for owner in OWNERS:
        uid = pwd.getpwnam(owner).pw_uid
        run(str(root / 'desktop/runtime/python/bin/python3'), '-I', str(root / 'desktop/setup.py'),
            'install', '--owner', owner, '--apply')
        info = manifest(uid)
        agent = pwd.getpwnam(info['agent'])
        assert agent.pw_uid not in (0, uid) and agent.pw_uid >= 1000
        shadow = run('getent', 'shadow', info['agent']).stdout.strip().split(':')
        assert shadow[1].startswith('!') and shadow[7] == '1', 'agent must remain locked and expired'
        for provider in ('claude', 'codex'):
            target = Path(info['home']) / '.local/bin' / provider
            shutil.copyfile(Path(__file__).with_name('linux_fake_provider.py'), target)
            target.chmod(0o700)
            os.chown(target, agent.pw_uid, agent.pw_gid)
        unit = f'rynhult-desktop-{uid}'
        assert run('systemctl', 'is-active', unit + '.socket').stdout.strip() == 'active'
        meta = Path(info['socket']).stat()
        assert meta.st_uid == uid and meta.st_mode & 0o777 == 0o600
        assert run('systemctl', 'is-active', unit + '.service', check=False).stdout.strip() == 'inactive'
    print('provisioned two desktop owners and two separate agent accounts', flush=True)


def cross_user_checks(info, state):
    other = pwd.getpwnam(OWNERS[1])
    forbidden = [str(Path(info['home'])), str(Path(state['project']['workspace_path']) / 'acl-proof.txt'),
                 f"{pwd.getpwnam(OWNERS[0]).pw_dir}/.desktop-qualification/qualification-state.json",
                 str(Path(info['run_dir']) / info['agent'] / state['sessions'][0]['id'] / 'events.jsonl')]
    program = '''import pathlib,socket,sys
for name in sys.argv[2:]:
    try: pathlib.Path(name).read_bytes()
    except PermissionError: pass
    else: raise SystemExit('cross-user read was permitted: '+name)
s=socket.socket(socket.AF_UNIX)
try: s.connect(sys.argv[1])
except PermissionError: pass
else: raise SystemExit('cross-user socket connect was permitted')
'''
    prefix = ['setpriv', '--reuid', str(other.pw_uid), '--regid', str(other.pw_gid), '--clear-groups', 'python3', '-c']
    run(*prefix, program, info['socket'], *forbidden)
    # Independently exercise broker SO_PEERCRED rejection, with the outer DAC gate deliberately opened in this guest.
    path = Path(info['socket'])
    path.chmod(0o666)
    try:
        code = '''import json,socket,sys
s=socket.socket(socket.AF_UNIX); s.settimeout(5); s.connect(sys.argv[1])
try:
 s.sendall(b'{"id":1,"method":"ping","params":{}}\\n'); answer=s.recv(4096)
except (ConnectionResetError,BrokenPipeError): answer=b''
if answer and json.loads(answer).get('ok'): raise SystemExit('broker accepted wrong uid')
'''
        run(*prefix, code, info['socket'])
    finally:
        path.chmod(0o600)


def qualify():
    require_disposable(root=True)
    provision()
    root = resources()
    python = str(root / 'desktop/runtime/python/bin/python3')
    probe = str(Path(__file__).resolve())
    owner = pwd.getpwnam(OWNERS[0])
    info = manifest(owner.pw_uid)
    report = {'status': 'running', 'container': 'rootless Podman; systemd PID 1',
              'activation': 'system socket unit with User=agent; owner PAM/systemd --user login cycle', 'checks': {}}
    private_json(STATE / 'report.json', report)
    try:
        for phase in ('first', 'reopen'):
            unit = f'qualification-owner-{phase}'
            command = ['systemd-run', '--quiet', '--wait', '--pipe', '--collect', '--unit='+unit,
                       '--uid='+OWNERS[0], '--property=PAMName=login', '--property=Type=exec',
                       '--setenv=XDG_RUNTIME_DIR=/run/user/'+str(owner.pw_uid),
                       '--setenv=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/'+str(owner.pw_uid)+'/bus',
                       '--', python, probe, 'owner-'+phase]
            result = run(*command, check=False, timeout=150)
            if result.returncode:
                (STATE / f'owner-{phase}.stderr').write_text(result.stderr + result.stdout)
                raise AssertionError(f'owner {phase} failed; see guest report/companion stderr')
            profile = Path(owner.pw_dir) / '.desktop-qualification'
            report['checks'][phase] = json.loads((profile / f'result-{phase}.json').read_text())
            if phase == 'first':
                state = json.loads((profile / 'qualification-state.json').read_text())
                broker_pid = int(run('systemctl', 'show', f'rynhult-desktop-{owner.pw_uid}.service', '-p', 'MainPID', '--value').stdout)
                status = Path(f'/proc/{broker_pid}/status').read_text()
                assert f'Uid:\t{pwd.getpwnam(info["agent"]).pw_uid}\t' in status and 'NoNewPrivs:\t1' in status
                cross_user_checks(info, state)
                agent = pwd.getpwnam(info['agent'])
                calls = [json.loads(line) for line in (Path(info['home']) / '.qualification-auth/invocations.jsonl').read_text().splitlines()]
                assert all(row['uid'] == agent.pw_uid and row['home'] == info['home'] for row in calls)
                assert {row['provider'] for row in calls} == {'claude', 'codex'}
                report['checks']['agent_identity'] = {'uid': agent.pw_uid, 'owner_uid': owner.pw_uid,
                    'broker_pid': broker_pid, 'no_new_privileges': True, 'locked_expired': True,
                    'fake_auth_calls_as_agent': len(calls), 'owner_home_hidden_by_service': True}
                # No linger: closing the actual PAM login must stop the owner's user manager.
                wait_for(lambda: run('systemctl', 'is-active', f'user@{owner.pw_uid}.service', check=False).stdout.strip() == 'inactive', 45)
                assert run('systemctl', 'is-active', f'rynhult-desktop-{owner.pw_uid}.service').stdout.strip() == 'active'
                report['checks']['logged_out'] = {'owner_user_manager': 'inactive', 'broker': 'active',
                                                    'cross_user_files_socket_peercred': 'refused'}
                for session in state['sessions']:
                    os.kill(session['identity']['pid'], 0)
        wait_for(lambda: all(not Path(f"/proc/{x['identity']['pid']}").exists() for x in state['sessions']))
        report['checks']['stop'] = 'both recorded fake-provider PIDs gone'
        report['status'] = 'passed'
    except Exception as error:
        report['status'] = 'failed'
        report['error'] = str(error)
        raise
    finally:
        private_json(STATE / 'report.json', report)


def account_pids(uid):
    result = []
    for path in Path('/proc').iterdir():
        if path.name.isdigit():
            try:
                if path.stat().st_uid == uid:
                    result.append(int(path.name))
            except FileNotFoundError:
                pass
    return result


def uninstall_arch():
    """Installed uninstall, with its UID sweep required to be a verified no-op.

    Never invoke a pattern/UID kill from this qualification. Normal API stop has
    already removed both recorded providers; stop only the owned broker units.
    Refuse unexpected surviving agent processes rather than killing them.
    """
    require_disposable(root=True)
    report = json.loads((STATE / 'report.json').read_text())
    assert report['status'] == 'passed'
    assert shutil.which('pacman'), 'this uninstall qualification is Arch-specific'
    root = resources()
    listed = run('pacman', '-Qlq', 'rynhult-linux-spike').stdout.splitlines()
    owned_files = [name for name in listed if not name.endswith('/')]
    wrapper = Path('/opt/qualification/empty-uid-sweep')
    wrapper.mkdir(mode=0o700)
    # The shipped uninstall uses pkill -u. Intercept only that cleanup primitive
    # after proving there is nothing left to kill; every other installed --apply
    # command, unit removal and manifest removal executes unchanged.
    sweep = wrapper / 'pkill'
    sweep.write_text('''#!/usr/bin/python3
import json, pwd, sys
sys.path.insert(0, '/opt/qualification')
from linux_phase1_probe import account_pids, require_disposable, STATE
require_disposable(root=True)
assert len(sys.argv) == 4 and sys.argv[1:3] == ['-KILL', '-u']
assert sys.argv[3] in ('rynd-1000', 'rynd-1001')
assert not account_pids(pwd.getpwnam(sys.argv[3]).pw_uid), 'unexpected live agent process'
with (STATE / 'empty-uid-sweeps.jsonl').open('a') as output:
    output.write(json.dumps({'agent': sys.argv[3], 'processes': []}) + '\\n')
raise SystemExit(1)
''')
    sweep.chmod(0o700)
    credentials = []
    closed_tmux = []
    for name in OWNERS:
        owner = pwd.getpwnam(name)
        info = manifest(owner.pw_uid)
        agent = pwd.getpwnam(info['agent'])
        if name == OWNERS[0]:
            credentials = [Path(info['home']) / '.qualification-auth' / provider for provider in ('claude', 'codex')]
            assert all(path.is_file() for path in credentials)
        unit = f'rynhult-desktop-{owner.pw_uid}'
        run('systemctl', 'stop', unit + '.socket', unit + '.service')
        remaining = account_pids(agent.pw_uid)
        if remaining:
            control = ['setpriv', '--reuid', str(agent.pw_uid), '--regid', str(agent.pw_gid),
                       '--clear-groups', 'tmux', '-L', f'rynhult-desktop-{owner.pw_uid}']
            tmux_pid = int(run(*control, 'display-message', '-p', '#{pid}').stdout.strip())
            assert remaining == [tmux_pid], 'unexpected process beyond recorded dedicated tmux server'
            run(*control, 'kill-server')  # control only this guest's dedicated test socket
            closed_tmux.append(tmux_pid)
        wait_for(lambda: not account_pids(agent.pw_uid))
        result = subprocess.run([str(root / 'desktop/runtime/python/bin/python3'), '-I',
            str(root / 'desktop/setup.py'), 'uninstall', '--owner', name, '--apply'],
            env={'PATH': str(wrapper) + ':/usr/bin:/bin'}, capture_output=True, text=True, timeout=90)
        assert result.returncode == 0, result.stderr
        assert not Path(f'/etc/rynhult-desktop/{owner.pw_uid}/execution.json').exists()
        assert not Path(info['socket']).exists()
        for suffix in ('.socket', '.service'):
            assert not Path('/etc/systemd/system', unit + suffix).exists()
            assert run('systemctl', 'show', unit + suffix, '-p', 'LoadState', '--value', check=False).stdout.strip() == 'not-found'
        assert not account_pids(agent.pw_uid)
        shadow = run('getent', 'shadow', info['agent']).stdout.strip().split(':')
        assert shadow[1].startswith('!') and shadow[7] == '1'
        assert Path(info['home']).is_dir() and Path(info['projects']).is_dir()
    sweeps = [json.loads(line) for line in (STATE / 'empty-uid-sweeps.jsonl').read_text().splitlines()]
    assert {row['agent'] for row in sweeps} == {'rynd-1000', 'rynd-1001'} and len(sweeps) == 2
    owner = pwd.getpwnam(OWNERS[0])
    profile = Path(owner.pw_dir) / '.desktop-qualification'
    state = json.loads((profile / 'qualification-state.json').read_text())
    work = Path(state['project']['workspace_path'], 'acl-proof.txt')
    assert work.read_text().startswith('owner can read')
    run('pacman', '-R', '--noconfirm', 'rynhult-linux-spike')
    assert run('pacman', '-Q', 'rynhult-linux-spike', check=False).returncode != 0
    assert all(not Path(name).exists() for name in owned_files), 'packaged file survived removal'
    assert work.read_text().startswith('owner can read') and (profile / 'qualification-state.json').is_file()
    assert all(path.is_file() for path in credentials)
    report['checks']['uninstall'] = {'status': 'passed', 'package': 'removed with pacman',
        'units_socket_manifests': 'removed for both owners', 'agent_processes': [],
        'closed_owned_tmux_pids': closed_tmux,
        'accounts': 'preserved locked and expired', 'work_profile_credentials': 'preserved',
        'uid_sweep': 'intercepted verified-empty no-op; no UID/pattern kill executed'}
    private_json(STATE / 'report.json', report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('qualify', 'owner-first', 'owner-reopen', 'uninstall-arch'))
    args = parser.parse_args()
    if args.action.startswith('owner-'):
        owner_phase(args.action.removeprefix('owner-'))
    elif args.action == 'uninstall-arch':
        try:
            uninstall_arch()
        except Exception as error:
            require_disposable(root=True)
            report = json.loads((STATE / 'report.json').read_text())
            report.update(status='failed', error='uninstall: ' + str(error))
            private_json(STATE / 'report.json', report)
            raise
    else:
        qualify()


if __name__ == '__main__':
    main()
