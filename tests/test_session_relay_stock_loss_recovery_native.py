"""Native only: actual owned stock process/stdio loss after decoded key and main CAS."""
import importlib.util
import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('mode',['owner','transport','revoke','owner_after','transport_after','cancel','expiry','deadline','retired'])
def test_stock_post_cas_loss_requires_explicit_fresh_session(record_property,mode):
    path=Path(__file__).resolve().parents[1]/'scripts/session_relay_qualification/installed_security_probe.py'
    spec=importlib.util.spec_from_file_location('stock_loss_recovery',path)
    probe=importlib.util.module_from_spec(spec);spec.loader.exec_module(probe)
    proof=probe.capture_installed_security('codex',scenario='stock_race_'+mode+'_recover_new')
    assert proof['actualWriteLockHeldAfterMainCas']
    refusal=proof.get('preWriteRefusalProof')
    if refusal is not None:
        assert proof['receipt']['state']=='failed' and proof['receipt']['reason']=='rejected'
        assert proof['decision']['status']=='invalidated' and refusal['outcome']=='rejected'
        assert refusal['evidence']['kind']=='broker_refusal' and proof['providerResponseWrites']==[]
        assert refusal['evidence']['operation_id']==proof['receipt']['operationId']
        assert refusal['evidence']['run_id']==proof['receipt']['runId']
    else:assert proof['receipt']['state']=='uncertain'
    assert len(proof['providerResponseWrites'])==int(mode.endswith('_after'))
    assert proof['actualProviderWriteDrainedBeforeLoss'] is mode.endswith('_after')
    assert proof['nativeProcessed']==proof['mainProcessed']==2
    assert proof['postCasCancellationRefused'] is (mode=='cancel')
    if mode=='cancel':
        cancellation=proof['postCasCancellationProof']
        assert cancellation['code']=='REVISION_CONFLICT' and cancellation['seconds']<5
        assert cancellation['refusedBeforeDispatchTimeoutOrLoss'] and cancellation['ownerPhase']=='owned'
        assert cancellation['originalReceiptUnchanged'] and cancellation['reservedDecisionUnchanged']
        assert cancellation['providerResponseWritesAtRefusal']==0 and cancellation['responseAttemptsAtRefusal']==1
    if mode in ('expiry','deadline'):

        expiry=proof['queuedExpiryProof']
        assert expiry['releasedMs']>expiry['expiresMs']>expiry['createdMs']
    if mode=='retired':assert proof['mainInputGrantRetired']
    assert proof['oldOwnerStopped'] and proof['retiredAliasRefused'] and proof['noReplay']
    recovery=proof['publicFreshSessionRecovery']
    assert recovery['oldSessionId']!=recovery['newSessionId']
    assert recovery['oldNativeId']!=recovery['newNativeId'] and recovery['freshModelCalls']==1
    assert recovery['oldSnapshotEventsReceiptsDecisionsUnchanged'] and recovery['sourceOldOwnerAlreadyExited']
    record_property('stock_post_cas_recovery',json.dumps(proof,sort_keys=True))
