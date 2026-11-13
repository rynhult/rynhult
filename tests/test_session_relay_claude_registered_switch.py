"""Reviewed registered L1 operations keep their identity through the real broker."""
from datetime import timedelta

import pytest
from fastapi import HTTPException
from sqlalchemy import func, select
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession
from acc.server.session_relay import store
from acc.session_relay import protocol as p


def count(c):
    with c.db() as db:
        return db.scalar(select(func.count()).select_from(RelayOperation))


async def prepared(c, key='prepare'):
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'target': 'terminal', 'expectedRevision': revision}
    registered = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), key,
        'switch.prepare', p.operation_digest('switch.prepare', params))
    plan = await c.launch.prepare_owner(c.resolve, target='terminal', revision=revision,
        operation_id=registered.operation_id)
    return params, registered, plan


def commit_registration(c, plan):
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'planId': plan['planId'],
        'planDigest': plan['planDigest'], 'expectedRevision': plan['revision']}
    registered = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), 'commit',
        'switch.commit', p.operation_digest('switch.commit', params))
    return registered


async def test_registered_switch_preserves_two_receipts_and_never_replays_launch(ownership_case):  # noqa: F811
    c = ownership_case
    first = c.owned_factory.current
    params, prepare, plan = await prepared(c)
    same = await c.launch.prepare_owner(c.resolve, target='terminal', revision=params['expectedRevision'],
        operation_id=prepare.operation_id)
    assert same == plan and count(c) == 1
    commit = commit_registration(c, plan)
    assert count(c) == 2
    outcome = await c.launch.commit_owner(c.resolve, plan['planId'], plan['planDigest'],
        revision=plan['revision'], operation_id=commit.operation_id)
    assert outcome['view'] == 'terminal' and first.process.returncode is not None
    with c.db() as db:
        owner = db.get(RelayOwnership, SID)
        assert owner.prepare_operation_id == prepare.operation_id
        assert owner.commit_operation_id == commit.operation_id
        for registered in (prepare, commit):
            receipt = db.get(RelayOperation, registered.operation_id).receipt
            assert receipt['state'] == 'accepted'
            assert receipt['actor']['principal'] == 'alex'
    assert count(c) == 2 and len(c.owned_factory.owners) == 2
    with pytest.raises(p.RelayError):
        await c.launch.commit_owner(c.resolve, plan['planId'], plan['planDigest'],
            revision=plan['revision'], operation_id=commit.operation_id)
    assert count(c) == 2 and len(c.owned_factory.owners) == 2


@pytest.mark.parametrize('fault', ['digest', 'expired', 'actor', 'revoked'])
async def test_registered_commit_refuses_before_stopping_source(ownership_case, fault):  # noqa: F811
    c = ownership_case
    first = c.owned_factory.current
    _, _, plan = await prepared(c)
    commit = commit_registration(c, plan)
    resolve = c.resolve
    if fault == 'actor':
        def resolve():
            who, actor = c.resolve()
            return who, p.Actor(actor.kind, actor.principal, p.Channel.PHONE)
    elif fault == 'revoked':
        c.state['revoked'] = True
    else:
        @write_intent
        def change():
            with c.db() as db:
                op = db.get(RelayOperation, commit.operation_id)
                if fault == 'expired':
                    op.deadline = store.utcnow() - timedelta(seconds=1)
                else:
                    op.receipt = {**op.receipt, 'payloadDigest': 'sha256:' + 'b' * 64}
                db.commit()
        change()
    with pytest.raises((p.RelayError, HTTPException)):
        await c.launch.commit_owner(resolve, plan['planId'], plan['planDigest'],
            revision=plan['revision'], operation_id=commit.operation_id)
    assert first.process.returncode is None and len(c.owned_factory.owners) == 1
    assert count(c) == 2


async def test_registered_prepare_digest_mismatch_creates_no_shadow_operation(ownership_case):  # noqa: F811
    c = ownership_case
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'target': 'terminal', 'expectedRevision': revision}
    operation = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), 'wrong-prepare',
        'switch.prepare', p.operation_digest('switch.prepare', params))
    with pytest.raises(p.RelayError):
        c.launch.owner_hub.prepare(c.resolve, SID, target='chat', revision=revision,
            request_key=operation.operation_id, operation_id=operation.operation_id)
    assert count(c) == 1 and len(c.owned_factory.owners) == 1
    with c.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'owned'


async def registered_switch(c, target, key):
    """Native fixture helper; no HTTP method or qualification override."""
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'target': target, 'expectedRevision': revision}
    prepare = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), key + '-prepare',
        'switch.prepare', p.operation_digest('switch.prepare', params))
    plan = await c.launch.prepare_owner(c.resolve, target=target, revision=revision, operation_id=prepare.operation_id)
    if plan['phase'] == 'owned':
        return plan
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'planId': plan['planId'],
        'planDigest': plan['planDigest'], 'expectedRevision': plan['revision']}
    commit = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), key + '-commit',
        'switch.commit', p.operation_digest('switch.commit', params))
    return await c.launch.commit_owner(c.resolve, plan['planId'], plan['planDigest'],
        revision=plan['revision'], operation_id=commit.operation_id)


async def test_registered_plan_cannot_fall_back_to_an_internally_created_commit(ownership_case):  # noqa: F811
    c = ownership_case
    _, prepare, plan = await prepared(c)
    with pytest.raises(p.RelayError) as failure:
        await c.launch.select_owner(c.resolve, target='terminal', revision=plan['revision'], request_key=prepare.operation_id)
    assert failure.value.code is p.ErrorCode.BUSY
    assert count(c) == 1 and len(c.owned_factory.owners) == 1


async def test_pre_stop_refusal_restores_pre_prepare_authority(ownership_case):  # noqa: F811
    c = ownership_case
    source = c.owned_factory.current
    @write_intent
    def previous():
        with c.db() as db:
            owner = db.get(RelayOwnership, SID)
            owner.authority = {**owner.authority, 'writerAttachment': 'fixture-previous-attachment', 'inputEpoch': 9}
            value = dict(owner.authority)
            db.commit()
            return value
    old = previous()
    _, _, plan = await prepared(c)
    commit = commit_registration(c, plan)
    source.active = True  # real private switch must inspect its owned source
    try:
        with pytest.raises(ConnectionError):
            await c.launch.commit_owner(c.resolve, plan['planId'], plan['planDigest'],
                revision=plan['revision'], operation_id=commit.operation_id)
    finally:
        source.active = False
    with c.db() as db:
        owner = db.get(RelayOwnership, SID)
        assert owner.phase == 'owned' and owner.authority == old
        assert db.get(RelayOperation, commit.operation_id).receipt['state'] == 'failed'
    assert source.process.returncode is None and not c.launch.prepared_plans


async def test_other_attachment_cannot_commit_borrowed_registered_plan(ownership_case):  # noqa: F811
    c = ownership_case
    base = c.resolve
    def attached(identifier):
        def resolve():
            return base()
        resolve.attachment_id = identifier
        return resolve
    c.resolve = attached('first-view')
    _, _, plan = await prepared(c)
    commit = commit_registration(c, plan)
    with pytest.raises(p.RelayError) as failure:
        await c.launch.commit_owner(attached('other-view'), plan['planId'], plan['planDigest'],
            revision=plan['revision'], operation_id=commit.operation_id)
    assert failure.value.code is p.ErrorCode.STALE_AUTHORITY
    assert len(c.owned_factory.owners) == 1
    await c.launch.commit_owner(c.resolve, plan['planId'], plan['planDigest'],
        revision=plan['revision'], operation_id=commit.operation_id)
    assert count(c) == 2 and len(c.owned_factory.owners) == 2


async def test_registered_same_view_is_conclusive_without_a_provider_effect(ownership_case):  # noqa: F811
    c = ownership_case
    with c.db() as db:
        revision = db.get(RelaySession, SID).revision
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'target': 'chat', 'expectedRevision': revision}
    registration = c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), 'already-chat',
        'switch.prepare', p.operation_digest('switch.prepare', params))
    result = await c.launch.prepare_owner(c.resolve, target='chat', revision=revision, operation_id=registration.operation_id)
    assert result['phase'] == 'owned' and result['view'] == 'chat'
    with c.db() as db:
        assert db.get(RelayOperation, registration.operation_id).receipt['state'] == 'accepted'
    assert count(c) == 1 and len(c.owned_factory.owners) == 1 and not c.launch.prepared_plans


def cancel_registration(c, plan):
    params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'planId': plan['planId']}
    return c.launch.owner_hub.register_switch(c.resolve, SID, str(c.launch.run_id), 'cancel',
        'switch.cancel', p.operation_digest('switch.cancel', params))


@pytest.mark.parametrize('expired', [False, True])
async def test_registered_cancel_restores_source_without_stopping_or_new_launch(ownership_case, expired):  # noqa: F811
    c = ownership_case
    first = c.owned_factory.current
    with c.db() as db:
        previous = dict(db.get(RelayOwnership, SID).authority)
    _, _, plan = await prepared(c)
    if expired:
        @write_intent
        def expire():
            with c.db() as db:
                db.get(RelayOwnership, SID).expires_at = store.utcnow() - timedelta(seconds=1)
                db.commit()
        expire()
    registration = cancel_registration(c, plan)
    result = await c.launch.cancel_owner(c.resolve, plan['planId'], operation_id=registration.operation_id)
    assert result['view'] == 'chat' and result['phase'] == 'owned'
    assert first.process.returncode is None and len(c.owned_factory.owners) == 1
    assert not c.launch.prepared_plans and count(c) == 2
    with c.db() as db:
        assert db.get(RelayOwnership, SID).authority == previous
        assert db.get(RelayOperation, registration.operation_id).receipt['state'] == 'accepted'
    with pytest.raises(p.RelayError):
        await c.launch.cancel_owner(c.resolve, plan['planId'], operation_id=registration.operation_id)


async def test_revoked_caller_cannot_cancel_and_restore_authority(ownership_case):  # noqa: F811
    c = ownership_case
    _, _, plan = await prepared(c)
    registration = cancel_registration(c, plan)
    c.state['revoked'] = True
    with pytest.raises((p.RelayError, HTTPException)):
        await c.launch.cancel_owner(c.resolve, plan['planId'], operation_id=registration.operation_id)
    assert len(c.owned_factory.owners) == 1 and c.owned_factory.current.process.returncode is None
    with c.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'prepared'
