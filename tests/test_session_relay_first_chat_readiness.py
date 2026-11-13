"""Readiness presentation uses retained receipt facts, without a provider SDK."""
from types import SimpleNamespace

from relay_helpers import setup

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.server.session_relay import claude_observer, claude_public, store, writers
from acc.session_relay import protocol as p


def test_first_chat_readiness_is_read_only_and_ignores_other_receipts(tmp_path, monkeypatch):
    c = setup(tmp_path)
    actor = c.resolve()[1]
    run = SimpleNamespace(terminal_grants=SimpleNamespace(record=None, last_epoch=0), turn_origins={})
    c.ctx.relay_runtime = SimpleNamespace(runs={'relay': run})
    monkeypatch.setattr(claude_public, 'supported', lambda *_: True)
    monkeypatch.setattr(writers, 'public_ready', lambda *_: True)
    @write_intent
    def seed():
        with c.db() as db:
            db.add(RelayOwnership(session_id='relay', mode='exclusive', view='chat', phase='owned',
                source_run_id=1, authority={'actor': p.to_wire(actor)}))
            row = db.get(RelaySession, 'relay')
            registered = store.register(db, row, actor, 'readiness-other-method', 'switch.prepare',
                p.sha256_digest(b'presentation-only fixture'), {}, methods=frozenset({'switch.prepare'}))
            db.commit()
            return registered.operation_id
    def snapshot():
        with c.db() as db:
            return claude_observer.presentation(c.ctx, db, db.get(RelaySession, 'relay'), actor)
    @write_intent
    def receipt(method, state):
        with c.db() as db:
            op = db.get(RelayOperation, identifier)
            op.receipt = {**op.receipt, 'method': method, 'state': state}
            db.commit()
    try:
        identifier = seed()
        receipt('switch.prepare', 'accepted')
        before = snapshot()
        assert before['execution']['mine'] is True
        assert before['execution']['waitingForFirstChat'] is True
        receipt('turn.submit', 'uncertain')
        assert snapshot()['execution']['waitingForFirstChat'] is True
        receipt('turn.submit', 'accepted')
        assert snapshot()['execution']['waitingForFirstChat'] is False
        with c.db() as db:
            owner = db.get(RelayOwnership, 'relay')
            assert owner.view == 'chat' and owner.phase == 'owned'
            assert db.get(RelaySession, 'relay').current_run_id == 1
        assert c.ctx.relay_runtime.runs == {'relay': run}
    finally:
        c.engine.dispose()
