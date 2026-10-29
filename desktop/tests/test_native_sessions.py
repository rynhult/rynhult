"""Real Tauri/WebKitGTK window with real broker/PTY and fake provider CLIs.

Account UID is simulated only inside a test-owned companion script; production
continues to require the separate account and protected socket.
"""
import json
import os
import shlex
import subprocess
import time

import httpx
import pytest
from test_native import ARTIFACTS, ROOT, SYSTEM_PYTHON, display, launch, nodes, tree, wait_for

pytestmark = pytest.mark.skipif(os.environ.get('DESKTOP_NATIVE_CHECK') != '1', reason='Opt-in native fake-provider session test')


@pytest.mark.parametrize('provider', ['claude', 'codex'])
def test_native_fake_provider_terminal(tmp_path, provider):
    fixture = tmp_path / 'fixture'
    fixture.mkdir()
    wrapper = tmp_path / 'test-python'
    wrapper.write_text('#!/bin/sh\nexec ' + shlex.join(['/srv/acc/app/.venv/bin/python',
        str(ROOT / 'desktop/tests/session_companion.py'), str(fixture)]) + '\n')
    wrapper.chmod(0o700)
    with display() as (runtime, address, driver), launch(runtime, address, tmp_path, python=wrapper) as (host, _, log):
        wait_for(address, lambda n: n['role'] == 'heading' and n['name'] == 'Projects')
        info = json.loads((fixture / 'info.json').read_text())
        with httpx.Client(base_url=info['origin'], cookies={'rynhult_desktop': info['token'], 'acc_session': info['session']},
                          headers={'x-acc-csrf': info['csrf'], 'Origin': info['origin']}) as client:
            project = client.post('/api/projects/new', json={'name': 'Native demo'})
            assert project.status_code == 201, project.text
            pid = project.json()['id']
            assert client.post(f'/api/desktop/projects/{pid}/native', json={'accept': True}).status_code == 200
            session = client.post('/api/sessions', json={'project_id': pid, 'name': 'Native work', 'provider': provider})
            assert session.status_code == 201, session.text
            link, _ = wait_for(address, lambda n: n['role'] == 'link' and 'Native work' in n['name'])
            subprocess.run([SYSTEM_PYTHON, '-I', str(ROOT / 'desktop/tests/atspi.py'), address,
                            '--action', link['bus'], link['path']], check=True, timeout=15)
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if (fixture / 'layout.json').exists():
                    report = json.loads((fixture / 'layout.json').read_text())
                    if f'fake-{provider} ready' in report['terminal']:
                        break
                time.sleep(.2)
            else:
                (ARTIFACTS / f'desktop-native-{provider}-session-report.json').write_text(json.dumps(report, indent=2))
                raise AssertionError(f'Native terminal did not render fake CLI output: {report}')
            assert report['scale'] > 0 and report['viewport'] > 800, report
            for command in ('click 700 650', 'type native-input-check', 'press Enter'):
                driver.stdin.write(command + '\n')
                driver.stdin.flush()
                assert driver.stdout.readline().strip() == 'ok'
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                report = json.loads((fixture / 'layout.json').read_text())
                if 'native-input-check' in report['terminal']:
                    break
                time.sleep(.2)
            assert 'native-input-check' in report['terminal'], report
            # A native terminal attach is recorded by the shared WebSocket/PTY path.
            audit = client.get('/api/audit').json()
            assert any(row['action'] == 'terminal.attach' and row['target'] == session.json()['id'] for row in audit)
            (ARTIFACTS / f'desktop-native-{provider}-session-report.json').write_text(json.dumps(report, indent=2))
            driver.stdin.write(f'shot {ARTIFACTS / f"desktop-native-{provider}-session.png"}\n')
            driver.stdin.flush()
            assert driver.stdout.readline().strip() == 'ok'
            assert client.post(f"/api/sessions/{session.json()['id']}/stop").status_code == 200
            close = next(n for n in nodes(tree(address)) if n['role'] == 'push button' and n['name'] == 'Close')
            subprocess.run([SYSTEM_PYTHON, '-I', str(ROOT / 'desktop/tests/atspi.py'), address,
                            '--action', close['bus'], close['path']], check=True, timeout=15)
            assert host.wait(15) == 0, log.read_text()[-2000:]
