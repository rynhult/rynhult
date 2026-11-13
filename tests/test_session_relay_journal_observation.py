"""Only exact v1 registered metadata can observe a conclusive journal record."""
import json
from dataclasses import replace

import pytest
from test_session_relay_codex import operation

from acc.broker.session_relay import AdmissionJournal
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import AdmissionResult, Outcome


def identity(op):
    return {key: value for key, value in p.to_wire(op).items() if key not in ('params', 'deadlineMs')}


def test_observation_reads_without_claiming_or_replacing_fingerprints(tmp_path):
    journal = AdmissionJournal(tmp_path / 'journal', clock=lambda: 1000)
    op = operation('turn.interrupt', {'turnId': 'original'})
    journal.enroll(op.fence, 'link')
    journal.claim(op, 'link')
    result = AdmissionResult(Outcome.ACCEPTED, p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id, 'native-turn'))
    journal.settle(op, result)
    fingerprint = journal.db.execute('SELECT fingerprint FROM admission').fetchone()[0]
    assert journal.observe(identity(op))[0] == 'accepted'
    assert journal.db.execute('SELECT fingerprint FROM admission').fetchone()[0] == fingerprint
    for changed in (replace(op, session_id='other'), replace(op, run_id='other'),
                    replace(op, payload_digest=p.sha256_digest(b'changed')), replace(op, actor=replace(op.actor, principal='other'))):
        with pytest.raises(p.RelayError):
            journal.observe(identity(changed))
    # A legacy v1 record retains its state, but missing proof metadata is unknown.
    journal.db.execute('DELETE FROM observation')
    assert journal.lookup(op.operation_id)[0] == 'accepted'
    assert journal.observe(identity(op)) is None
    journal.close()


def test_observation_metadata_contains_no_prompt_and_does_not_execute(tmp_path):
    journal = AdmissionJournal(tmp_path / 'journal', clock=lambda: 1000)
    op = operation(params={'text': 'Artificial prompt must not enter observation metadata'})
    journal.enroll(op.fence, 'link')
    journal.claim(op, 'link')
    value = journal.db.execute('SELECT identity FROM observation').fetchone()[0]
    assert 'Artificial prompt' not in value and 'params' not in json.loads(value)
    assert journal.observe(identity(op)) == ('uncertain', None)
    journal.close()
