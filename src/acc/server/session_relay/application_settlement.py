"""Conclusive old-run application facts settle receipts, never execution authority."""
from acc.session_relay import protocol as p

from ..db import write_intent
from ..models import RelayDecision, RelayOperation, RelayRun
from . import store


@write_intent
def settle(ctx, session_id, params, result):
    with ctx.db() as db:
        return settle_db(ctx, db, session_id, params, result)


def settle_db(ctx, db, session_id, params, result):
    row = store.lock(db, session_id)
    op = db.get(RelayOperation, params['operationId'])
    record = db.get(RelayDecision, (session_id, params['decisionId']))
    decision = store.decision_from(record.decision)
    application = result.get('application') if isinstance(result, dict) else None
    reference = application.get('nativeRef') if isinstance(application, dict) else None
    source_run = db.get(RelayRun, op.run_id)
    native_id = source_run.identity['binding']['native_conversation'] if source_run else None
    if op.receipt['state'] == 'accepted' and decision.status in (p.DecisionStatus.ANSWERED, p.DecisionStatus.DENIED):
        return {'outcome': 'accepted'}
    applied = (isinstance(result, dict) and result.get('outcome') == 'accepted'
        and isinstance(application, dict) and application.get('effect') in ('deny', 'allow_once')
        and application.get('decisionId') == decision.decision_id
        and application.get('actionDigest') == decision.action_digest == params['actionDigest']
        and isinstance(reference, str) and len(reference.encode()) <= 2048
        and isinstance(native_id, str) and reference.startswith(native_id + ':') and ':' + decision.decision_id + ':' in reference
        and str(record.run_id) == str(op.run_id) == params['expectedRunId']
        and op.receipt['method'] == 'decision.reply'
        and op.receipt['payloadDigest'] == p.operation_digest('decision.reply',
            {key: value for key, value in params.items() if key != 'operationId'})
        and op.receipt['state'] in ('submitting', 'uncertain')
        and decision.choice_id == params['choiceId'] and p.to_wire(decision.winner) == op.receipt['actor']
        and any(choice.id == decision.choice_id and choice.effect.value == application.get('effect') for choice in decision.choices)
        and decision.status in (p.DecisionStatus.RESERVED, p.DecisionStatus.OUTCOME_UNKNOWN))
    if applied:
        evidence = p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION, op.id, str(op.run_id), reference)
        store.transition(db, row, op, p.ReceiptState.ACCEPTED, evidence=evidence)
        record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.DENIED
            if application['effect'] == 'deny' else p.DecisionStatus.ANSWERED))
        from .. import attention
        from .approval_inbox import PREFIX
        attention.resolve(db, PREFIX + session_id + ':' + decision.decision_id)
    else:
        if op.receipt['state'] == 'submitting':
            store.transition(db, row, op, p.ReceiptState.UNCERTAIN)
        if decision.status is p.DecisionStatus.RESERVED:
            record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.OUTCOME_UNKNOWN))
    store.append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=decision.decision_id)
    db.commit()
    return {'outcome': 'accepted' if applied else 'unknown'}
