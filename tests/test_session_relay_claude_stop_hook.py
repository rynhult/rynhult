"""Lost observation must not turn a native Stop into a model continuation."""
import io
from types import SimpleNamespace

import pytest

from acc.session_relay import claude_hook


@pytest.mark.parametrize('event,exit_code', [('Stop', 0), ('SessionEnd', 0), ('UserPromptSubmit', 2), ('PreToolUse', 2)])
def test_lost_native_hook_channel_stops_observation_but_denies_new_actions(monkeypatch, capsys, event, exit_code):
    import json
    monkeypatch.setattr(claude_hook.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps({'hook_event_name': event}).encode())))
    monkeypatch.setattr(claude_hook.sys, 'argv', ['fixture', '/fixture/owner-channel.json'])
    def lost(*args):
        raise ConnectionError('artificial lost channel')
    monkeypatch.setattr(claude_hook, 'exchange', lost)
    assert claude_hook.main() == exit_code
    output = capsys.readouterr()
    assert not output.out  # no fabricated observed/admitted/allowed reply
