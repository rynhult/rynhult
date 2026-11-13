"""One permission reviewer per frozen owner view; other hooks/rules preserved."""
import copy
import json
import stat
from pathlib import Path

import pytest
from test_session_relay_claude_public import SID, public_claude  # noqa: F401
from test_session_relay_ownership import ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import Session
from acc.server.session_relay.claude_settings import split
from acc.session_relay import protocol as p


def test_only_known_legacy_permission_socket_is_replaced():
    hook = {'hooks': [{'type': 'command', 'command': '/tmp/example/acc-hook claude'}]}
    original = {'disableAllHooks': False, 'permissions': {'ask': ['Bash']},
        'hooks': {'PermissionRequest': [hook], 'Stop': [hook], 'PreToolUse': [{'hooks': []}]}}
    before = copy.deepcopy(original)
    tui, sdk = split(original, '/tmp/example/acc-hook claude')
    assert original == before
    assert 'PermissionRequest' not in tui['hooks'] and 'PermissionRequest' not in sdk['hooks']
    assert tui['hooks']['Stop'] == sdk['hooks']['Stop'] == original['hooks']['Stop']
    assert sdk['permissions'] == original['permissions']
    original['hooks']['PermissionRequest'].append({'hooks': [{'type': 'command', 'command': 'unknown-custom'}]})
    with pytest.raises(p.RelayError):
        split(original, '/tmp/example/acc-hook claude')


async def test_sdk_view_has_its_own_immutable_guarded_settings(public_claude):  # noqa: F811
    c = public_claude
    with c.db() as db:
        directory = Path(db.get(Session, SID).run_dir)
    tui = json.loads((directory / 'settings.json').read_text())
    sdk_path = directory / 'relay-sdk-settings.json'
    sdk = json.loads(sdk_path.read_text())
    assert len(tui['hooks']['PermissionRequest']) == 1
    assert 'PermissionRequest' not in sdk['hooks']
    assert sdk['hooks']['PreToolUse'] == tui['hooks']['PreToolUse']
    assert sdk['hooks']['Stop'] == tui['hooks']['Stop']
    assert not stat.S_IMODE(sdk_path.stat().st_mode) & 0o222
    assert 'relay-sdk-settings.json' in c.owned_factory.guards
    assert c.owned_factory.current.client.options.settings == str(sdk_path)
