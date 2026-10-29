"""Real local main, exclusively in test-owned temporary directories/listeners."""
import json
import os
import secrets
import select
import subprocess
import time
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
PYTHON = '/srv/acc/app/.venv/bin/python'


@contextmanager
def companion(tmp_path):
    if getattr(os, "geteuid", lambda: None)() == 0:
        pytest.skip("Real desktop main refuses elevation; run process integration as an ordinary owner")
    tmp_path.mkdir(parents=True, exist_ok=True)
    profile = tmp_path / 'profile'
    profile.mkdir(mode=0o700, exist_ok=True)
    home = tmp_path / 'home'
    home.mkdir(exist_ok=True)
    token = secrets.token_hex(32)
    log = (tmp_path / 'main.log').open('w+')
    started = time.monotonic()
    process = subprocess.Popen([PYTHON, str(ROOT / 'desktop/main.py')], stdin=subprocess.PIPE,
                               stdout=subprocess.PIPE, stderr=log, text=True, cwd=ROOT,
                               env={'HOME': str(home), 'PATH': '/usr/bin:/bin', 'PYTHONPATH': str(ROOT / 'src')})
    try:
        process.stdin.write(json.dumps({'token': token, 'data_dir': str(profile),
                                        'static_dir': str(ROOT / 'desktop/web-dist')}) + '\n')
        process.stdin.flush()
        assert select.select([process.stdout], [], [], 30)[0], 'main readiness timed out'
        line = process.stdout.readline()
        assert line, (tmp_path / 'main.log').read_text()
        origin = json.loads(line)['origin']
        assert origin.startswith('http://127.0.0.1:')
        with httpx.Client(base_url=origin, cookies={'rynhult_desktop': token}) as client:
            yield process, client, profile, time.monotonic() - started, token
        process.stdin.close()
        assert process.wait(timeout=12) == 0, (tmp_path / 'main.log').read_text()
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        log.close()


def test_real_main_auth_persistence_and_eof_shutdown(tmp_path):
    for launch in range(2):
        with companion(tmp_path) as (process, client, profile, elapsed, token):
            assert client.get('/api/health').status_code == 200
            with httpx.Client(base_url=str(client.base_url)) as untrusted:
                for path in ['/', '/api/health', '/api/events', '/api/projects']:
                    assert untrusted.get(path).status_code == 401
            assert client.get('/api/projects').status_code == 401  # capability does not bypass person auth
            api_token = (profile / 'data_dir/secrets/api_token').read_text().strip()
            headers = {'Authorization': f'Bearer {api_token}'}
            if launch == 0:
                result = client.post('/api/projects', headers=headers,
                                     json={'name': 'Desktop demo', 'slug': 'desktop-demo',
                                           'workspace_path': str(profile / 'workspaces_root/demo')})
                assert result.status_code == 201, result.text
            projects = client.get('/api/projects', headers=headers)
            assert projects.status_code == 200
            assert 'desktop-demo' in projects.text
            assert token not in (tmp_path / 'main.log').read_text()
            assert token not in Path(f'/proc/{process.pid}/cmdline').read_text()
            assert token not in Path(f'/proc/{process.pid}/environ').read_text()


def test_shared_ui_keyboard_and_accessibility_tree(tmp_path):
    import pytest
    if os.environ.get('DESKTOP_BROWSER_CHECK') != '1':
        pytest.skip('Set DESKTOP_BROWSER_CHECK=1 with the provided read-only node_modules link')
    with companion(tmp_path) as (_, client, profile, _, token):
        # Native first navigation issues ordinary login/CSRF cookies, behind the capability.
        client.get('/')
        state = client.get('/api/auth/state')
        assert state.json()['authenticated'] and not state.json()['setup_required']
        artifacts = ROOT / '.acc/artifacts'
        artifacts.mkdir(parents=True, exist_ok=True)
        config = {'origin': str(client.base_url).rstrip('/'),
                  'screenshot': str(artifacts / 'desktop-chromium-projects.png'),
                  'cookies': [{'name': name, 'value': value, 'path': '/',
                               'httpOnly': name in {'rynhult_desktop', 'acc_session'}, 'sameSite': 'Strict'}
                              for name, value in client.cookies.items()]}
        result = subprocess.run(['node', str(ROOT / 'desktop/tests/browser.mjs')], input=json.dumps(config),
                                text=True, capture_output=True, timeout=60, env={**os.environ,
                                'PLAYWRIGHT_BROWSERS_PATH': '/srv/acc/tools/ms-playwright'})
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout)
        (artifacts / 'desktop-chromium-accessibility.json').write_text(json.dumps(report, indent=2))


def test_main_measurements(tmp_path):
    import pytest
    if os.environ.get('DESKTOP_MEASURE') != '1':
        pytest.skip('Opt-in five-minute main-only measurement')
    import platform

    import psutil
    starts = []
    for run in range(3):
        with companion(tmp_path / str(run)) as (process, client, _, elapsed, _):
            starts.append(elapsed * 1000)
            assert client.get('/api/health').status_code == 200
            if run != 2:
                continue
            main = psutil.Process(process.pid)
            def snapshot():
                tree = [main, *main.children(recursive=True)]
                return (sum(p.cpu_times().user + p.cpu_times().system for p in tree),
                        sum(p.memory_info().rss for p in tree))
            cpu_before, _ = snapshot()
            began = time.monotonic()
            rss = []
            for _ in range(300):
                time.sleep(1)
                rss.append(snapshot()[1])
            cpu_after, _ = snapshot()
            seconds = time.monotonic() - began
            report = {'scope': 'Python companion tree ONLY; no shell/WebKit/agents/VM',
                      'interpreter': PYTHON, 'platform': platform.platform(),
                      'cpu_count': psutil.cpu_count(), 'startup_to_ready_ms': starts,
                      'startup_type': 'Three fresh profiles, warm filesystem caches; not UI interactive',
                      'idle_seconds': seconds, 'cpu_percent_one_core': (cpu_after-cpu_before)/seconds*100,
                      'rss_mean_mib': sum(rss)/len(rss)/2**20, 'rss_peak_mib': max(rss)/2**20}
            (ROOT / '.acc/artifacts/desktop-main-measurements.json').write_text(json.dumps(report, indent=2))


def test_native_host_without_display_reaps_companion(tmp_path):
    if getattr(os, "geteuid", lambda: None)() == 0:
        pytest.skip("Real desktop main refuses elevation; run native integration as an ordinary owner")
    if os.environ.get('DESKTOP_NATIVE_CHECK') != '1':
        pytest.skip('Opt-in headless native host failure-path check')
    import re

    import psutil
    binary = ROOT / 'desktop/src-tauri/target/release/rynhult-desktop-spike'
    result = subprocess.run([str(binary)], cwd=ROOT, text=True, capture_output=True, timeout=45,
                            env={'HOME': str(tmp_path), 'XDG_CONFIG_HOME': str(tmp_path / 'config'),
                                 'PATH': '/usr/bin:/bin', 'RYNHULT_SPIKE_PYTHON': PYTHON})
    assert result.returncode != 0  # An embedded WebKitGTK window needs a real display.
    match = re.search(r'desktop_main_ready_ms=(\d+) child_pid=(\d+)', result.stderr)
    assert match, result.stderr
    child_pid = int(match[2])
    for _ in range(100):
        if not psutil.pid_exists(child_pid):
            break
        time.sleep(.1)
    assert not psutil.pid_exists(child_pid), 'Companion survived failed host startup'
    assert (tmp_path / 'config/rynhult-desktop-spike/data_dir/acc.db').exists()
    (ROOT / '.acc/artifacts/desktop-headless-native.txt').write_text(result.stderr)


def test_profile_singleton_preserves_first_window_login(tmp_path):
    import pytest
    with companion(tmp_path) as (_, client, _, _, _):
        client.get('/')
        assert client.get('/api/auth/state').json()['authenticated']
        with pytest.raises(AssertionError, match='BlockingIOError'):
            with companion(tmp_path):
                raise AssertionError('second main unexpectedly started')
        assert client.get('/api/auth/state').json()['authenticated']
