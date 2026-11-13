"""Pinned native Claude print/enclosure proof; no live account or external egress."""
import importlib.util
import shutil
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


def test_real_pinned_claude_print_resume_and_guard_use_empty_tree_enclosures(record_property):
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Installed bubblewrap and pinned Claude required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('owned_claude_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.capture_claude_resume(enclosure=True)
    assert len(result['results']) == 2
    assert all(item['exit'] == 0 and item['empty_tree'] is True for item in result['results'])
    assert {item['output']['session_id'] for item in result['results']} == {'11111111-1111-4111-8111-111111111111'}
    assert not result['forbidden_file_exists']
    assert any(event.get('guard', {}).get('rule') == 'custom' for event in result['guard_events'])
    import json
    history = json.dumps(result['requests'][-1]['messages'])
    assert 'First isolated fixture turn' in history and 'Second isolated fixture turn' in history
    assert 'Native fixture denies this command.' in history
    record_property('native_empty_tree_receipts', 2)
    record_property('native_guard_source_digest', result['guard_source_digest'])
    record_property('scope', 'real pinned Claude print/resume; offline synthetic model; no account; not native TUI')


@pytest.mark.parametrize('sdk', [False, True])
def test_real_claude_print_tui_print_resume_preserves_native_id_hooks_and_empty_trees(record_property, sdk):
    import json
    if not shutil.which('bwrap') or not Path('/usr/bin/claude').exists():
        pytest.skip('Installed bubblewrap and pinned Claude required; no fallback')
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('native_claude_tui_probe', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if sdk:
        pytest.importorskip('claude_agent_sdk', reason='Pinned installed Claude SDK required; no alternate CLI or installation')
    result = module.capture_claude_resume(enclosure=True, tui=True, sdk=sdk)
    native = '11111111-1111-4111-8111-111111111111'
    tui = result['tui']
    assert tui['completed'] and tui['entered'] and tui['empty_tree']
    assert tui['exit'] == 0
    assert tui['fixture_preferences_seeded'] and tui['output_bytes'] > 100
    assert 'First isolated fixture turn' in tui['terminal_text']
    assert 'Native TUI isolated fixture turn' in tui['terminal_text']
    assert all(item['session_id'] == native for item in tui['hooks'])
    events = [item['hook_event_name'] for item in tui['hooks']]
    assert events.index('SessionStart') < events.index('UserPromptSubmit') < events.index('Stop')
    assert any(item['fixture_prompt'] for item in tui['hooks'])
    assert len(result['results']) == 2
    assert all(item['exit'] == 0 and item['empty_tree'] for item in result['results'])
    assert {item['output']['session_id'] for item in result['results']} == {native}
    assert all(item['native_exit_status'] == 0 for item in result['results'])
    if sdk:
        assert result['sdk_version'] == '0.1.50'
        assert all(item['output']['is_error'] is False for item in result['results'])
    history = json.dumps(result['requests'][-1]['messages'])
    for marker in ('First isolated fixture turn', 'Native TUI isolated fixture turn', 'Second isolated fixture turn',
                   'Native fixture denies this command.'):
        assert marker in history
    assert not result['forbidden_file_exists']
    record_property('native_empty_tree_receipts', 3)
    record_property('scope', ('real SDK/TUI/SDK' if sdk else 'real print/TUI/print') + '; synthetic API/startup preferences; not production hub handover')
