"""Authentication reuse is confined to one resolver and synchronous transaction."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from relay_helpers import setup

from acc.server.session_relay.runtime import RequestResolver
from acc.server.session_relay.terminal_grants import frame_identity


def test_frame_authentication_rechecks_after_transaction_and_never_caches_failure(tmp_path):
    c = setup(tmp_path)
    calls, attached = [], []
    def principal(db, request):
        calls.append(request)
        return c.resolve()[0]
    c.ctx.relay_principal = principal
    request = SimpleNamespace(state=SimpleNamespace())
    resolver = RequestResolver(c.ctx, request, lambda db, actor: attached.append(actor))
    other = RequestResolver(c.ctx, SimpleNamespace(state=SimpleNamespace()))
    try:
        with c.db() as db:
            # Start a real transaction before resolving a principal.
            db.connection()
            with frame_identity(db):
                first = resolver.in_transaction(db)
                assert resolver.in_transaction(db) == first
                assert len(calls) == 1 and len(attached) == 2
                other.in_transaction(db)
                assert len(calls) == 2  # a different request cannot borrow it
                db.commit()
                resolver.in_transaction(db)
                assert len(calls) == 3  # transaction ended, same frame context
            resolver.in_transaction(db)
            assert len(calls) == 4  # context ended, no reused principal
            assert 'relay_frame_principals' not in db.info
        c.state['revoked'] = True
        with c.db() as db, frame_identity(db):
            for _ in range(2):
                with pytest.raises(HTTPException):
                    resolver.in_transaction(db)
            assert len(calls) == 6
    finally:
        c.engine.dispose()
