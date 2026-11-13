"""One-time native consent changes no tool inputs or permission rules."""
import io
import json
from types import SimpleNamespace

from acc.session_relay import claude_hook


def test_native_allow_does_not_rewrite_input_or_permission_rules(monkeypatch, capsys):
    payload = {'hook_event_name': 'PermissionRequest', 'tool_name': 'Bash',
        'tool_input': {'command': 'echo artificial'}, 'session_id': 'fixture'}
    monkeypatch.setattr(claude_hook.sys, 'stdin', SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(claude_hook.sys, 'argv', ['fixture', '/tmp/example/owner-channel.json'])
    monkeypatch.setattr(claude_hook, 'exchange', lambda *_: {'behavior': 'allow'})
    assert claude_hook.main() == 0
    assert json.loads(capsys.readouterr().out) == {'hookSpecificOutput': {
        'hookEventName': 'PermissionRequest', 'decision': {'behavior': 'allow'}}}
