"""Public Codex answers use the existing hub CAS and opaque staged dispatch.

Writer qualification remains false in production. This path cannot independently
promote native decisions or infer application from a response write.
"""
import time
from dataclasses import dataclass

from acc.session_relay import protocol as p

from ..admin_features import enabled
from ..db import write_in_thread, write_intent
from ..models import RelayDecision, RelayOperation, RelayRun
from . import authority, store, writers
from .hub import Hub


def public_ready(ctx, row):
    runtime = getattr(ctx, 'relay_runtime', None)
    run = runtime.runs.get(row.session_id) if runtime else None
    return (writers.PUBLIC_WRITER_QUALIFIED is True and not row.sealed
            and row.capabilities.get('decision.tool_approval') == 'qualified'
            and run is not None and run.provider in ('codex', 'claude'))


@write_intent
def closed(run,params):
    """Exact native closure never supplies a winner or settles a chosen answer."""
    if run.provider!='codex' or params.get('kind')!='decision.closed' or params.keys()!={'kind','decisionId','actionDigest'}:
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    with run.ctx.db() as db:
        row=store.lock(db,run.session_id)
        record=db.get(RelayDecision,(run.session_id,params['decisionId']))
        original=db.get(RelayRun,run.run_id)
        if record is None or original is None or original.binding_id!=run.binding_id:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        decision=store.decision_from(record.decision)
        if (decision.run_id!=str(run.run_id) or decision.action_digest!=params['actionDigest']
                or decision.kind is not p.DecisionKind.TOOL_APPROVAL):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if decision.status is not p.DecisionStatus.PENDING or decision.winner is not None:return False
        record.decision=p.to_wire(p.advance_decision(decision,p.DecisionStatus.CANCELLED))
        store.append(db,row,'decision.changed',{'decision':record.decision},decision_id=decision.decision_id)
        db.commit()
        return True


@write_intent
def applied(run,params,*,native_run=None,validate=None,original_operation=None):
    """Authenticated broker application evidence for the original chosen answer.

    This is not an operation admission and does not reuse the person's authority.
    Late conclusive evidence may settle its old receipt after revocation/Stop.
    """
    evidence_run=str(run.run_id) if native_run is None else native_run
    if (run.provider not in ('codex','claude') or not p.valid_opaque(evidence_run) or params.get('kind')!='decision.applied'
            or params.keys()!={'kind','decisionId','operationId','choiceId','actionDigest','nativeRef'}
            or not p.valid_digest(params.get('nativeRef'))):
        raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    with run.ctx.db() as db:
        row=store.lock(db,run.session_id)
        if validate is not None:
            validate(db)
        record=db.get(RelayDecision,(run.session_id,params['decisionId']))
        if record is None:raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
        decision=store.decision_from(record.decision)
        if (decision.kind is not p.DecisionKind.TOOL_APPROVAL or decision.run_id!=evidence_run
                or decision.winner is None or decision.choice_id!=params['choiceId']
                or decision.action_digest!=params['actionDigest']):
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        operation=store.operation(db,run.session_id,params['operationId'],decision.winner)
        original=db.get(RelayRun,operation.run_id)
        if (str(operation.run_id)!=evidence_run or original is None or original.binding_id!=run.binding_id
                or operation.receipt['method']!='decision.reply'):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        if original_operation is not None and original_operation != {key: operation.receipt[key]
                for key in ('operationId','runId','method','actor','payloadDigest')}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        references = original.identity.get('decisionEvidence', {})
        if operation.id in references and references[operation.id] != params['nativeRef']:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        if operation.receipt['state']=='accepted' and decision.status in (p.DecisionStatus.ANSWERED,p.DecisionStatus.DENIED):
            return True
        if (operation.receipt['state'] not in ('submitting','uncertain') or decision.status not in
                (p.DecisionStatus.RESERVED,p.DecisionStatus.OUTCOME_UNKNOWN)):
            raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
        revision=decision.revision-(1 if decision.status is p.DecisionStatus.RESERVED else 2)
        payload={'sessionId':run.session_id,'expectedRunId':evidence_run,'decisionId':decision.decision_id,
            'choiceId':decision.choice_id,'expectedRevision':revision,'actionDigest':decision.action_digest}
        p.check_registered(store.receipt_from(operation.receipt),actor=decision.winner,
            session_id=run.session_id,run_id=evidence_run,method='decision.reply',
            payload_digest=p.operation_digest('decision.reply',payload))
        choice=next((value for value in decision.choices if value.id==decision.choice_id),None)
        if (choice is None or choice.id not in (('accept','cancel','decline') if run.provider=='codex' else ('accept','decline')) or choice.effect is not
                (p.ChoiceEffect.ALLOW_ONCE if choice.id=='accept' else p.ChoiceEffect.DENY)):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        store.settle(db,row,operation,accepted=True,evidence=p.Evidence(p.EvidenceKind.PROVIDER_ADMISSION,
            operation.id,str(operation.run_id),params['nativeRef']))
        status=p.DecisionStatus.ANSWERED if choice.effect is p.ChoiceEffect.ALLOW_ONCE else p.DecisionStatus.DENIED
        record.decision=p.to_wire(p.advance_decision(decision,status))
        original.identity = {**original.identity, 'decisionEvidence': {**references, operation.id: params['nativeRef']}}
        store.append(db,row,'decision.changed',{'decision':record.decision},decision_id=decision.decision_id)
        db.commit()
        return True


@dataclass(frozen=True)
class DecisionBinding:
    identifier: str
    choice: str
    revision: int
    digest: str


def check_grant(run, db, grant):
    row = store.get(db, run.session_id)
    binding = grant.native_decision
    if (not public_ready(run.ctx, row) or not isinstance(binding, DecisionBinding)
            or getattr(grant.resolve, 'attachment_id', None) is None):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    record = db.get(RelayDecision, (run.session_id, binding.identifier))
    if record is None:
        raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
    decision = store.decision_from(record.decision)
    if (decision.kind is not p.DecisionKind.TOOL_APPROVAL or decision.run_id != str(run.run_id)
            or decision.policy_revision != run.policy_revision or decision.action_digest != binding.digest):
        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
    _, current = authority.current(run.ctx, db, grant.resolve, run.session_id,
        action='decide', expected_run=run.run_id, attached=True, decision=decision)
    authority.recheck(grant.authority, current)
    if decision.expires_at_ms <= int(time.time() * 1000):
        raise p.RelayError(p.ErrorCode.DECISION_EXPIRED)
    choice = next((value for value in decision.choices if value.id == binding.choice), None)
    if choice is None or (not enabled(run.ctx.settings, 'session_relay') and choice.effect is not p.ChoiceEffect.DENY):
        raise p.RelayError(p.ErrorCode.UNSUPPORTED)
    if decision.status is p.DecisionStatus.PENDING:
        if decision.revision != binding.revision:
            raise p.RelayError(p.ErrorCode.REVISION_CONFLICT)
    elif (decision.status is not p.DecisionStatus.RESERVED or decision.revision != binding.revision + 1
            or decision.winner != grant.operation.actor or decision.choice_id != binding.choice):
        raise p.RelayError(p.ErrorCode.DECISION_RESOLVED)
    return choice.effect


@write_intent
def issue(run, resolve, params):
    with run.ctx.db() as db:
        row, current = run.check_run(db, resolve=resolve)
        if (not public_ready(run.ctx, row) or current.actor.kind is not p.ActorKind.PERSON
                or getattr(resolve, 'attachment_id', None) is None):
            raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        operation = store.operation(db, run.session_id, params['operationId'], current.actor)
        if operation.registration_key.startswith('stock-frame-'):
            from .stock_approval_frames import Resolver as StockResolver
            approvals = run.terminal_grants.approvals
            if (not isinstance(resolve, StockResolver) or resolve.owner is not approvals
                    or resolve.submission is not approvals.submission
                    or resolve.submission.operation_id != operation.id):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if operation.receipt['method'] != 'decision.reply':
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    token = run.issue(resolve, params['operationId'], {key: value for key, value in params.items() if key != 'operationId'})
    if isinstance(token, str):
        grant = run.grants[token]
        grant.native_decision = DecisionBinding(params['decisionId'], params['choiceId'], params['expectedRevision'], params['actionDigest'])
        try:
            with run.ctx.db() as db:
                check_grant(run, db, grant)
        except BaseException:
            run.fail_queued(token)  # exact registered payload was authenticated; no provider IO
            run.grants.pop(token, None)  # no stage or provider IO has occurred
            raise
    return token


@write_intent
def unknown(run, params):
    # Durable observation after possible dispatch; no new execution authority.
    with run.ctx.db() as db:
        row = store.lock(db, run.session_id)
        operation = db.get(RelayOperation, params['operationId'])
        if operation is None or operation.run_id != run.run_id:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        if operation.receipt['state'] == 'failed' and operation.receipt.get('reason') == 'rejected':
            # FAILED after submission requires conclusive broker/provider
            # rejection in advance_receipt; it is not an HTTP failure guess.
            record = db.get(RelayDecision, (run.session_id, params['decisionId']))
            decision = store.decision_from(record.decision)
            if decision.status in (p.DecisionStatus.RESERVED, p.DecisionStatus.OUTCOME_UNKNOWN):
                record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.INVALIDATED))
                store.append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=decision.decision_id)
                db.commit()
            return False
        if operation.receipt['state'] != 'submitting' and operation.receipt['state'] != 'uncertain':
            return False
        if operation.receipt['state'] == 'submitting':
            store.transition(db, row, operation, p.ReceiptState.UNCERTAIN)
        record = db.get(RelayDecision, (run.session_id, params['decisionId']))
        decision = store.decision_from(record.decision)
        if decision.status is p.DecisionStatus.RESERVED:
            record.decision = p.to_wire(p.advance_decision(decision, p.DecisionStatus.OUTCOME_UNKNOWN))
            store.append(db, row, 'decision.changed', {'decision': record.decision}, decision_id=decision.decision_id)
        db.commit()
        return True


async def answer(run, resolve, params):
    async with run.submit_lock:
        token = await write_in_thread(lambda: issue(run, resolve, params))
        if not isinstance(token, str):
            return token  # an existing receipt never authorizes resend
        try:
            await run.peer.call('stage', {'grant': token})
            await write_in_thread(lambda: run.resolve_grant(token))
            await write_in_thread(lambda: Hub(run.ctx).answer(resolve, run.session_id, params['decisionId'],
                expected_run=params['expectedRunId'], attached=True, choice_id=params['choiceId'],
                revision=params['expectedRevision'], digest=params['actionDigest'], operation_id=params['operationId']))
            await run.terminal_grants.approvals.resolved(params['decisionId'], params['operationId'])
            receipt = await run.manager.dispatch(run, token)
            await write_in_thread(lambda: unknown(run, params))
            return receipt
        except BaseException:
            # A cancelled CAS await can have committed in its worker thread.
            # Inspect durable state instead of assuming nothing happened.
            reserved = await write_in_thread(lambda: unknown(run, params))
            if reserved and token in run.grants:
                await write_in_thread(lambda: run.settle(token, {'outcome': 'unknown'}))
            else:
                await write_in_thread(lambda: run.fail_queued(token))
            raise
        finally:
            if token in run.grants:
                await run.manager.discard(run, token)
