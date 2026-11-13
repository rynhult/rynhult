"""Unexpected capability failures remain fail-closed with safe log diagnostics."""
import pytest
from helpers import write_db
from test_session_capabilities import family

from acc.server import session_caps
from acc.server.models import Session
from acc.server.vault import Redactor


@pytest.mark.parametrize('redaction_fails', [False, True])
async def test_capability_failure_log_is_bounded_redacted_and_still_stops(
        api, project, monkeypatch, caplog, redaction_fails):
    _, child = family(api, project, {})
    secret = 'private-value-for-capability-log'
    supervisor = api.app.state.supervisor

    def broken(*args):
        raise RuntimeError(f'cap resolver broke {secret} token=another-private-token\n' + 'x' * 1000)

    def redactor(*args):
        if redaction_fails:
            raise RuntimeError(secret)
        return Redactor([(secret, 'test key')])

    monkeypatch.setattr(session_caps, 'check_execution', broken)
    monkeypatch.setattr(supervisor.vault, 'redactor', redactor)
    with write_db(api.app.state.db) as db:
        worker = db.get(Session, child)
        await supervisor._reconcile_one(db, worker, None)
        assert worker.desired_state == 'stopped'
        assert 'execution refused' in worker.last_error
    records = [record for record in caplog.records if 'capability check failed; stopping execution:' in record.message]
    assert records
    for record in records:
        reason = record.message.split('stopping execution: ', 1)[1]
        assert len(reason) <= 300 and '\n' not in reason
        assert secret not in reason and 'another-private-token' not in reason
        assert record.exc_info is None
        assert ('redaction failed' if redaction_fails else 'RuntimeError: cap resolver broke') in reason
