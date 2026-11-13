"""A real native Claude background task fences incoming team service input."""

import hashlib
import sqlite3
from pathlib import Path

import pytest
import test_session_relay_claude_churn as churn
from test_session_relay_claude_service_native import run_native_service
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.models import Session
from acc.server.orchestrator import delivery
from acc.server.session_relay import delivery as service
from acc.session_relay import protocol as p

pytestmark = pytest.mark.relay_native


async def test_native_claude_background_refuses_team_delivery_before_admission(runtime_case, monkeypatch, record_property):  # noqa: F811
    original = churn.select_owner
    refused = []

    async def intercept(case, view, key):
        if key == 'background-must-refuse':
            supervisor = case.ctx.supervisor
            with case.db() as db:
                private = Path(db.get(Session, 'abc123def456').run_dir) / 'relay'
            # The person's prior unknown permission turn intentionally retains
            # its stage. Service rejection must preserve it byte-for-byte.
            before = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                      for path in (private / 'payloads').glob('*.payload')}
            assert before
            queued = (await delivery.team_message(supervisor, 'fixture-team-lead',
                {'helper': 'abc123def456', 'text': 'Artificial incoming team message during native background'}))['delivery']
            await delivery._deliver(supervisor, queued['id'])
            with case.db() as db:
                op = service.operation(db, queued['id'])
                receipt = dict(op.receipt)
                assert receipt['state'] == 'failed' and receipt['reason'] == 'rejected'
                assert receipt['actor'] == p.to_wire(p.Actor(p.ActorKind.SERVICE,
                    'session:fixture-team-lead', p.Channel.TEAM))
                private = Path(db.get(Session, 'abc123def456').run_dir) / 'relay'
            with sqlite3.connect('file:' + str(private / 'journal/admission.sqlite3') + '?mode=ro', uri=True) as journal:
                assert journal.execute('SELECT state FROM admission WHERE operation=?',
                    (receipt['operationId'],)).fetchone() is None
            assert not (private / 'payloads' / (hashlib.sha256(receipt['operationId'].encode()).hexdigest() + '.payload')).exists()
            assert {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in (private / 'payloads').glob('*.payload')} == before
            refused.append(receipt)
        return await original(case, view, key)

    monkeypatch.setattr(churn, 'select_owner', intercept)
    await run_native_service(runtime_case, monkeypatch, record_property, team=True)
    assert len(refused) == 1
    record_property('background_service_receipt', p.canonical_json(refused[0]).decode())
