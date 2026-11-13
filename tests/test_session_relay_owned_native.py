"""Focused no-account native read-only boundary; enforced offline namespace."""
import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_stock_tui_reads_owned_private_server_without_new_writer_or_correlation():
    if not shutil.which('bwrap') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed bubblewrap and pinned Codex required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('owned_tui_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.capture_owned_tui()
    except module.QualificationPinMismatch as error:
        pytest.skip(str(error))
    assert result['owner_alive']
    assert result['nativeId'] in result['loaded']['data']
    assert 'thread/resume' in result['read_methods']
    assert result['native_input_attempted']
    native_turns = [item for item in result['requests'] if item['method'] == 'turn/start']
    assert len(native_turns) == 1
    assert native_turns[0]['params']['input'][0]['text'] == 'artificial unauthorized input'
    assert any(reply.get('id') == native_turns[0]['id'] and 'error' in reply for reply in result['replies'])
    assert not result['read_errors']
    assert result['live_update_attempted']
    assert result['fixture_request_count'] == 2
    assert result['events'].count('turn/started') == 2
    assert result['events'].count('turn/completed') == 2
    assert 'Isolated fixture response 1.' in result['terminal_startup']
    assert 'Isolated fixture response 2.' in result['terminal_startup']
    assert 'Failed to start turn' in result['terminal_startup']
    assert result['terminal_output_bytes'] > 100
    assert result['terminalMediated'] is False




@pytest.mark.parametrize('tampered', [False, True])
def test_stock_codex_runs_guard_and_consumes_denial_instead_of_executing_tool(tampered):
    if not shutil.which('bwrap') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed bubblewrap and pinned Codex required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('owned_guard_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.capture_owned_tui(guard=True, guard_tampered=tampered)
    except module.QualificationPinMismatch as error:
        pytest.skip(str(error))
    guard = result['guard']
    hooks = guard['hooks']['data'][0]
    assert not hooks['warnings'] and not hooks['errors']
    assert len(hooks['hooks']) == 8
    for hook in hooks['hooks']:
        assert hook['trustStatus'] == 'trusted' and hook['enabled']
        assert hook['currentHash'] == guard['expected_hook_hashes'][hook['key']]
    assert len(guard['records']) == 1
    record = guard['records'][0]
    if tampered:
        assert record['guard']['rule'] == 'tampered'
        assert 'were changed after launch' in record['guard']['reason']
    else:
        assert record['guard'] == {'rule': 'custom', 'reason': 'Native fixture denies this command.'}
    assert record['hook']['session_id'] == result['nativeId']
    assert record['hook']['tool_use_id'] == 'call_guard_fixture'
    assert record['hook']['permission_mode'] == 'bypassPermissions'
    assert guard['seen_by_model']
    assert guard['forbidden_file_exists'] is False
    assert result['fixture_request_count'] == 3  # denied tool -> follow-up, then live Chat update
    assert result['events'].count('turn/started') == result['events'].count('turn/completed') == 2
    assert result['owner_alive']
    assert result['terminalMediated'] is False


def test_stock_tui_writer_lease_transfer_fences_old_route_and_keeps_live_reads():
    if not shutil.which('bwrap') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed bubblewrap and pinned Codex required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('owned_writer_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.capture_owned_tui(writer=True)
    except module.QualificationPinMismatch as error:
        pytest.skip(str(error))
    writer = result['writer']
    assert writer['peer_pids'] == [writer['tui_pid']]
    assert writer['takeover'] and writer['stale_typed']
    assert [(entry['actor']['principal'], entry['params']['expectedControlRevision'])
            for entry in writer['admissions']] == [('fixture-terminal-person', 2), ('fixture-chat-person', 3)]
    assert writer['admissions'][0]['params']['text'] == 'artificial authorized input'
    assert writer['snapshot']['writer'] == 'chat' and writer['snapshot']['epoch'] == 3
    assert not writer['snapshot']['recoveryRequired']
    assert writer['retirements'] == [{'epoch': 1, 'idle': True, 'view': 'chat'},
                                    {'epoch': 2, 'idle': True, 'view': 'terminal'}]
    requests = [request for request in result['requests'] if request['method'] == 'turn/start']
    assert len(requests) == 2
    replies = {reply['id']: reply for reply in result['replies'] if 'id' in reply}
    assert 'result' in replies[requests[0]['id']]
    assert 'error' in replies[requests[1]['id']]
    assert requests[1]['params']['input'][0]['text'] == 'stale native input'
    assert result['fixture_request_count'] == result['events'].count('turn/started') == 3
    assert result['events'].count('turn/completed') == 3
    assert result['owner_alive'] and result['nativeId'] in result['loaded']['data']
    assert 'Isolated fixture response 3.' in result['terminal_startup']
    assert result['terminalMediated'] is False  # scripted draft fixture is not TerminalHost


def test_stock_native_decoder_processing_and_retirement_over_unchanged_tmux_bridge():
    if not shutil.which('bwrap') or not shutil.which('tmux') or not Path('/usr/bin/codex').exists():
        pytest.skip('Installed bubblewrap, tmux and pinned Codex required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('native_host_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        result = module.capture_owned_tui(host=True)
    except module.QualificationPinMismatch as error:
        pytest.skip(str(error))
    assert result['rejected_controls'] == ['model', 'shell', 'approval', 'escape', 'paste', 'unicode', 'multiline']
    assert result['owned_placement'] is True
    assert result['view_exit_proven'] is True
    assert all(result['view_isolation'].values())
    assert result['owner_alive'] and len(result['peers']) == 1
    assert result['refused_drain_before_decoder'] and result['refused_old_input']
    assert result['typed']['watermark'] == {'epoch': 1, 'admitted': 1, 'processed': 0}
    assert result['entered']['watermark'] == {'epoch': 1, 'admitted': 2, 'processed': 0}
    assert result['drained'] == {'epoch': 1, 'admitted': 2, 'processed': 2}
    assert result['processing'] == [{'epoch': 1, 'sequence': 2, 'state': 'accepted'}]
    assert result['writer']['writer'] == 'chat' and result['writer']['epoch'] == 2
    assert result['owned_observer_live_chat'] and result['owned_capture_live_chat']
    assert result['framed_live_chat'] and result['observer_live_chat']
    assert result['framed_output_bytes'] > 100 and result['observer_output_bytes'] > 100
    assert result['fixture_requests'] == result['turns_started'] == result['turns_completed'] == 3
    assert [(entry['actor']['principal'], entry['params']['expectedControlRevision']) for entry in result['admissions']] == [
        ('fixture-terminal', 1), ('fixture-chat', 2)]
    assert result['terminalMediated'] is False
