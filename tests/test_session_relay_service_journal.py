"""Late SDK result settles only its exact claimed service operation."""
import hashlib
from types import SimpleNamespace

import pytest
from test_session_relay_broker import operation

from acc.broker.relay_ownership import ExclusiveRuntime
from acc.broker.session_relay import AdmissionJournal, PayloadStaging
from acc.session_relay import protocol as p


@pytest.mark.parametrize('change', ['none', 'digest', 'native', 'run'])
def test_only_correlated_sdk_admission_settles_existing_service_claim(tmp_path, change):
    op = operation(actor=p.Actor(p.ActorKind.SERVICE, '@api', p.Channel.API))
    journal = AdmissionJournal(tmp_path / 'journal', clock=lambda: 1000)
    staging = PayloadStaging(tmp_path / 'payloads')
    try:
        journal.enroll(op.fence, 'link')
        journal.claim(op, 'link')
        staging.stage(op.operation_id, p.canonical_json(p.to_wire(op.params)))
        runtime = SimpleNamespace(service_operations={op.operation_id: op}, journal=journal, staging=staging,
            adapter=SimpleNamespace(attestation=SimpleNamespace(process_identity='native-incarnation', run_id=op.run_id)))
        event = {'operationId': op.operation_id, 'payloadDigest': op.payload_digest,
                 'nativeRef': 'native-incarnation:' + op.operation_id}
        if change == 'digest':
            event['payloadDigest'] = p.sha256_digest(b'changed')
        elif change == 'native':
            event['nativeRef'] = 'other-incarnation:' + op.operation_id
        elif change == 'run':
            runtime.adapter.attestation.run_id = 'other-run'
        if change == 'none':
            ExclusiveRuntime.service_admitted(runtime, event)
            assert journal.lookup(op.operation_id)[0] == 'accepted'
            assert not runtime.service_operations
            assert not list(staging.root.glob('*.payload'))
        else:
            with pytest.raises(p.RelayError) as failure:
                ExclusiveRuntime.service_admitted(runtime, event)
            assert failure.value.code is p.ErrorCode.INTEGRITY_FAILURE
            assert journal.lookup(op.operation_id)[0] == 'uncertain'
            assert runtime.service_operations[op.operation_id] == op
            assert (staging.root / (hashlib.sha256(op.operation_id.encode()).hexdigest() + '.payload')).is_file()
    finally:
        staging.close()
        journal.close()
