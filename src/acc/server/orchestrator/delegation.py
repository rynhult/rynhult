"""Bound lead decisions over the authenticated team spool, never payload identities."""

TEAM_FIELDS = {
    'delegated_port': {'request_key', 'range'},
    'delegated_status': {'operation'},
    'delegated_image': {'request_key', 'provider', 'model'},
    'delegated_install': {'request_key', 'recipe_id', 'recipe_digest'},
}


async def attempt(supervisor, caller_id, action, work):
    from .. import approvals, delegated_execution
    from ..db import write_in_thread
    try:
        return await write_in_thread(work)
    except approvals.Refused as error:
        await record_refusal(supervisor, caller_id, action, delegated_execution.failure_reason(error))
        raise


async def record_refusal(supervisor, caller_id, action, reason):
    from .. import delegated_execution
    from ..db import write_in_thread
    def work():
        with supervisor.db() as db:
            delegated_execution.audit_refusal(db, caller_id, action, reason)
            db.commit()
    await write_in_thread(work)


async def reserve(supervisor, caller_id, payload):
    from .. import delegated_execution
    def work():
        with supervisor.db() as db:
            row = delegated_execution.reserve_port(db, caller_id, payload.get('request_key'), payload.get('range'))
            db.commit()
            return delegated_execution.out(row)
    return await attempt(supervisor, caller_id, 'port.reserve', work)


async def status(supervisor, caller_id, payload):
    from .. import approvals, delegated_execution
    from ..models import DelegatedAction
    with supervisor.db() as db:
        row = db.get(DelegatedAction, payload.get('operation'))
        if row is None or row.lead_id != caller_id:
            raise approvals.Refused('no such delegated operation', 404)
        return delegated_execution.out(row)


async def image(supervisor, caller_id, payload):
    from datetime import timedelta

    from fastapi import HTTPException

    from .. import approvals, delegated_execution
    from ..api.operations import ImageCheck, run_image_check
    from ..db import write_in_thread
    from ..models import ApprovalConsent, DelegatedAction, Session, utcnow
    def admission():
        with supervisor.db() as db:
            row, fresh = delegated_execution.admit(db, caller_id, payload.get('request_key'), 'image.check',
                {k: payload.get(k) for k in ('provider', 'model')})
            operation_id = row.id
            caller = db.get(Session, caller_id)
            body = ImageCheck(project_id=caller.project_id, provider=payload['provider'], model=payload['model'])
            owner = caller.owner
            consent = db.get(ApprovalConsent, row.consent_id)
            identity = {k: consent.constraints[k] for k in ('uid', 'runtime_id')}
            db.commit()
            return operation_id, body, owner, fresh, delegated_execution.out(row), identity
    operation_id, body, owner, fresh, previous, identity = await attempt(supervisor, caller_id, 'image.check', admission)
    if not fresh:
        return previous

    def authorize(account, machine):
        with supervisor.db() as db:
            approvals.security.lock_revision(db)
            operation = db.get(DelegatedAction, operation_id)
            consent = approvals.current(db, db.get(ApprovalConsent, operation.consent_id), check_remaining=False)
            snapshot = approvals.authority(db, owner, body.project_id, caller_id)
            caller = db.get(Session, caller_id)
            if (utcnow() >= operation.created_at + timedelta(seconds=30)
                    or operation.authority != snapshot or consent.revision != operation.consent_revision
                    or caller.run_as != account or caller.host != machine):
                raise approvals.Refused('execution scope changed before image probe')
            db.commit()
    try:
        result = await run_image_check(supervisor, body, actor='session:' + caller_id,
                                      budget_owner=owner, authorize=authorize, expected_identity=identity)
        state = 'completed' if result['image_input'] is not None else 'uncertain'
    except Exception as error:
        # Retain the failed admission, including quota consumption. Never turn
        # an executor refusal into a verified result or authorize a blind retry.
        result = {'image_input': None, 'reason': delegated_execution.failure_reason(error)}
        state = 'failed'
        if isinstance(error, (approvals.Refused, HTTPException)):
            await record_refusal(supervisor, caller_id, 'image.check', result['reason'])
    def finish():
        with supervisor.db() as db:
            row = db.get(DelegatedAction, operation_id)
            row.state, row.result = state, result
            approvals.audit(db, 'session:' + caller_id, 'result', row.id, {'state': state, 'result': result})
            db.commit()
            return delegated_execution.out(row)
    return await write_in_thread(finish)


TEAM_ACTIONS = {'delegated_port': reserve, 'delegated_status': status, 'delegated_image': image}


async def install(supervisor, caller_id, payload):
    """Accd drives all phases; every intent and observation has its own commit."""
    import asyncio
    import base64

    from ... import install_artifact as artifact
    from .. import account_installs, approvals
    from ..broker_client import BrokerUnavailable
    from ..db import write_in_thread
    from ..delegated_execution import out
    from ..models import Session

    def transaction(fn, *args):
        def work():
            with supervisor.db() as db:
                value = fn(db, *args)
                db.commit()
                return value
        return write_in_thread(work)

    def admission(db):
        row, fresh = account_installs.admit(db, caller_id, payload.get('request_key'),
            {k: payload.get(k) for k in ('recipe_id', 'recipe_digest')})
        caller = db.get(Session, caller_id)
        return row.id, fresh, out(row), caller.run_as

    operation_id, fresh, previous, account = await attempt(supervisor, caller_id, 'tool.install',
        lambda: _admit_install(supervisor, admission))
    if not fresh:
        return previous

    async def call(**params):
        result = await supervisor.broker.call('account_install', account=account, host='local', **params)
        if isinstance(result, dict) and result.get('reason') in artifact.REASONS:
            raise artifact.InvalidArtifact(result['reason'])
        return result

    try:
        envelope, manifest = await transaction(account_installs.preview, operation_id)
        report = await call(phase='footprint', envelope=envelope, manifest=manifest)
        noop = await transaction(account_installs.footprint, operation_id, report)
        if noop:
            def retained(db):
                from ..models import DelegatedAction
                return out(db.get(DelegatedAction, operation_id))
            return await transaction(lambda db: retained(db))
        envelope, blob = await transaction(account_installs.dispatch, operation_id)
        async with asyncio.timeout(envelope['limits']['execution_seconds']):
            await call(phase='prepare', envelope=envelope)
            for offset in range(0, len(blob), 1024 * 1024):
                await call(phase='chunk', operation_id=operation_id, offset=offset,
                           data=base64.b64encode(blob[offset:offset + 1024 * 1024]).decode('ascii'))
            report = await call(phase='stage', operation_id=operation_id)
        await transaction(account_installs.staged, operation_id, report)
        manifest_digest = await transaction(account_installs.start_publish, operation_id)
        async with asyncio.timeout(envelope['limits']['verification_seconds']):
            for index in range(len(report['plan'])):
                await transaction(account_installs.intent, operation_id, index)
                observation = await call(phase='publish', operation_id=operation_id,
                                         manifest_digest=manifest_digest, index=index)
                await transaction(account_installs.observed, operation_id, index, observation)
            report = await call(phase='finish', operation_id=operation_id)
            def completed(db, oid, observed):
                return out(account_installs.finish(db, oid, observed))
            return await transaction(completed, operation_id, report)
    except Exception as error:
        reason = (error.reason if isinstance(error, artifact.InvalidArtifact) else
                  'transport_lost' if isinstance(error, (BrokerUnavailable, TimeoutError)) else
                  (str(error) if str(error) in artifact.REASONS else 'authority_changed')
                  if isinstance(error, approvals.Refused) else 'internal_error')
        def failed(db, oid, why):
            return out(account_installs.fail(db, oid, why))
        result = await transaction(failed, operation_id, reason)
        if result['state'] == 'uncertain' and reason != 'transport_lost':
            try:
                async with asyncio.timeout(30):
                    while cleanup := await transaction(account_installs.cleanup_intent, operation_id):
                        report = await call(phase='compensate', operation_id=operation_id, **cleanup)
                        await transaction(account_installs.cleanup_observed, operation_id, cleanup['index'], report)
                result = await transaction(failed, operation_id, reason)
            except Exception:
                pass  # Unproved restoration remains uncertain and blocks installs.
        return result
    finally:
        try:
            await call(phase='abort', operation_id=operation_id)
        except Exception:
            pass  # Accd retains uncertainty; broker memory is never authority.


def _admit_install(supervisor, admission):
    with supervisor.db() as db:
        result = admission(db)
        db.commit()
        return result


TEAM_ACTIONS['delegated_install'] = install
