"""A concurrent native completion must not turn BUSY into a SQLite lock error."""
import sqlite3

import pytest
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


async def test_durable_submitting_turn_refuses_owner_switch_without_sql_lock(ownership_case, monkeypatch):  # noqa: F811
    c = ownership_case
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'fixture-busy',
              'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
    hub = Hub(c.ctx)
    registered = hub.register(c.resolve, SID, str(c.launch.run_id), 'busy-diagnostic',
                              'turn.submit', p.operation_digest('turn.submit', params))
    hub.queue(c.resolve, SID, registered.operation_id, expected_run=str(c.launch.run_id),
              digest=p.operation_digest('turn.submit', params))
    hub.claim(c.resolve, SID, registered.operation_id)
    original_audit = c.launch.owner_hub._audit
    def audit(db, actor, session_id, action, **details):
        if action == 'refused':
            # Another native completion can commit after the refused mutation
            # rolled back but before its separate audit transaction begins.
            with sqlite3.connect(c.engine.url.database, timeout=1) as other:
                other.execute("INSERT INTO setting(key,value,updated_at) VALUES ('g56-audit-race','{}',CURRENT_TIMESTAMP)")
        return original_audit(db, actor, session_id, action, **details)
    monkeypatch.setattr(c.launch.owner_hub, '_audit', audit)
    with pytest.raises(p.RelayError) as error:
        await select_owner(c, 'terminal', 'refuse-submitting')
    assert error.value.code is p.ErrorCode.BUSY
