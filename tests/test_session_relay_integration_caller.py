"""A revoked writer cannot change another authenticated caller into a401."""
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from acc.server.session_relay import authority
from acc.server.session_relay.terminal_grants import TerminalGrants
from acc.session_relay import protocol as p


def test_foreign_attachment_refuses_before_revoked_original_writer(monkeypatch):
    calls = []
    class Caller:
        attachment_id = 'writer-tab'  # copied grant/attachment, foreign connection
        def authenticate_in_transaction(self, db):
            calls.append('caller-auth')
        def __call__(self):
            calls.append('caller-attachment')
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)

    def original_writer(db, **kwargs):
        calls.append('revoked-writer-auth')
        raise HTTPException(401, 'Original writer login was revoked')

    monkeypatch.setattr(authority, 'current', lambda ctx, db, resolve, sid, **kwargs: resolve())
    run = SimpleNamespace(ctx=SimpleNamespace(), session_id='session', run_id='run', check_run=original_writer)
    grants = TerminalGrants(run)
    grants.record = SimpleNamespace(value=SimpleNamespace(grant='fixture-token', attachment_id='writer-tab'),
        phase='active', expires=time.monotonic() + 60, resolve=object(), authority=object())
    with pytest.raises(p.RelayError) as refused:
        grants._current(SimpleNamespace(info={}), 'fixture-token', resolve=Caller())
    assert refused.value.code is p.ErrorCode.STALE_AUTHORITY
    assert calls == ['caller-auth', 'caller-attachment']
    assert grants.record.phase == 'active'  # a pre-reservation refusal cannot poison it
