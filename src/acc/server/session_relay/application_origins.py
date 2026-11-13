"""Original permission-answer identities persisted before any native answer.

These records recover evidence correlation only. They contain no credentials or
reply authority; a cache miss must never reconstruct/resend a native answer.
"""
from acc.session_relay import protocol as p

from ..db import write_intent
from ..models import RelayDecision, RelayOperation, RelayRun
from . import store

KEY = 'applicationOrigins'
MAX_ORIGINS = 4096  # same bound as per-session operation registrations
PARAMS = frozenset(('sessionId', 'expectedRunId', 'operationId', 'decisionId', 'choiceId',
                    'expectedRevision', 'actionDigest'))


def identity_digest(record):
    return p.sha256_digest(p.canonical_json({key: value for key, value in record.identity.items()
                                           if key not in (KEY, 'conclusiveStop')}))


def checked(db, run, params, *, retired=False):
    if (not isinstance(params, dict) or params.keys() != PARAMS
            or type(params['expectedRevision']) is not int or len(p.encode_frame(params)) > 4096):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    row = store.lock(db, run.session_id)
    record = db.get(RelayRun, run.run_id)
    op = db.get(RelayOperation, params['operationId'])
    decision = db.get(RelayDecision, (run.session_id, params['decisionId']))
    if (not record or not op or not decision or record.session_id != run.session_id
            or record.binding_id != run.binding_id or not retired and row.binding_id != run.binding_id
            or not retired and row.current_run_id != run.run_id or op.session_id != run.session_id
            or op.run_id != run.run_id or decision.run_id != run.run_id
            or params['sessionId'] != run.session_id or params['expectedRunId'] != str(run.run_id)):
        raise p.RelayError(p.ErrorCode.STALE_RUN)
    value = store.decision_from(decision.decision)
    digest = p.operation_digest('decision.reply', {key: item for key, item in params.items() if key != 'operationId'})
    if (op.receipt['method'] != 'decision.reply' or op.receipt['payloadDigest'] != digest
            or value.choice_id != params['choiceId'] or value.action_digest != params['actionDigest']
            or p.to_wire(value.winner) != op.receipt['actor']
            or value.status not in (p.DecisionStatus.RESERVED, p.DecisionStatus.OUTCOME_UNKNOWN,
                                    p.DecisionStatus.ANSWERED, p.DecisionStatus.DENIED)):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return record, {'params': dict(params), 'actor': op.receipt['actor'], 'payloadDigest': digest,
                    'bindingId': record.binding_id, 'identityDigest': identity_digest(record)}


@write_intent
def remember(run, params):
    with run.ctx.db() as db:
        record, value = checked(db, run, params)
        origins = record.identity.get(KEY, {})
        if not isinstance(origins, dict):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        previous = origins.get(params['operationId'])
        if previous is not None and previous != value:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if previous is None and len(origins) >= MAX_ORIGINS:
            raise p.RelayError(p.ErrorCode.LIMIT)
        record.identity = {**record.identity, KEY: {**origins, params['operationId']: value}}
        db.commit()


@write_intent
def load(run, identifier, *, retired=False):
    with run.ctx.db() as db:
        return load_db(db, run, identifier, retired=retired)


def load_db(db, run, identifier, *, retired=False):
    record = db.get(RelayRun, run.run_id)
    origins = record.identity.get(KEY, {}) if record else {}
    saved = origins.get(identifier) if isinstance(origins, dict) else None
    if not isinstance(saved, dict) or not isinstance(saved.get('params'), dict):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    _, expected = checked(db, run, saved['params'], retired=retired)
    if saved != expected or saved['params']['operationId'] != identifier:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    return dict(saved['params'])
