"""Exercise real namespace isolation with Python only; no account or inference."""
import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('relay_native_probe',
    Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py')
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


def test_native_metadata_sandbox_has_no_egress_host_state_or_inherited_credentials(tmp_path, monkeypatch):
    if not shutil.which('bwrap'):
        pytest.skip('Installed bubblewrap is required; no unisolated fallback')
    output = tmp_path / 'evidence'
    output.mkdir(mode=0o700)
    private = tmp_path / 'host-credential-sentinel'
    private.write_text('artificial fixture, never a credential')
    monkeypatch.setenv('RELAY_QUALIFICATION_SENTINEL', 'must-not-be-inherited')
    script = (
        'import errno,json,os,socket; '
        'assert "RELAY_QUALIFICATION_SENTINEL" not in os.environ; '
        f'assert not os.path.exists({str(private)!r}); '
        'assert not os.path.exists("/srv/acc"); '
        'assert not os.path.exists("/run/user"); '
        'assert os.listdir(os.environ["CODEX_HOME"]) == []; '
        'assert os.listdir(os.environ["CLAUDE_CONFIG_DIR"]) == []; '
        'assert {name for _,name in socket.if_nameindex()} == {"lo"}; '
        's=socket.socket(); s.settimeout(1); '
        'assert s.connect_ex(("192.0.2.1",443)) == errno.ENETUNREACH; '
        'print(json.dumps({"network":os.readlink("/proc/self/ns/net")}))'
    )
    result = json.loads(probe.run(output, ['/usr/bin/python3', '-c', script]))
    assert result['network'] != os.readlink('/proc/self/ns/net')
    assert list(output.iterdir()) == []


@pytest.mark.relay_native
def test_pinned_native_tui_uses_private_unix_websocket_without_actor_watermark():
    if not shutil.which('bwrap') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed pinned Codex and bubblewrap required; no fallback')
    result = probe.capture_tui()
    frames = result['first_frames']
    assert frames[0]['upgrade'].startswith('GET /rpc HTTP/1.1')
    initialize = next(frame for frame in frames if frame.get('method') == 'initialize')
    assert initialize['params']['clientInfo']['version'] == '0.167.1'
    assert initialize['params']['capabilities']['requiredHumanApproval']
    assert not any('watermark' in json.dumps(frame).lower() for frame in frames)
    assert any(frame.get('method') == 'turn/start' for frame in frames)
    assert 'wire discovery only' in result['qualification']


@pytest.mark.relay_native
def test_real_claude_guard_denial_and_same_native_id_resume_without_live_account():
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Installed pinned Claude and bubblewrap required; no fallback')
    result = probe.capture_claude_resume()
    assert len(result['results']) == 2
    assert all(item['exit'] == 0 for item in result['results'])
    assert {item['output']['session_id'] for item in result['results']} == {'11111111-1111-4111-8111-111111111111'}
    assert result['forbidden_file_exists'] is False
    assert any(event.get('guard', {}).get('rule') == 'custom' for event in result['guard_events'])
    last = json.dumps(result['requests'][-1]['messages'])
    assert 'First isolated fixture turn' in last and 'Second isolated fixture turn' in last
    assert 'Native fixture denies this command.' in last
