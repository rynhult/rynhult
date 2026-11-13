"""Conclusive Stop survives runtime retirement; absence/replacement never proves exit."""
from unittest.mock import AsyncMock

import pytest
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayRun, RelaySession, Session, SessionRun
from acc.server.session_relay import stop_proof
from acc.server.session_relay.hub import seal_for_stop
from acc.server.session_relay.runtime import RuntimeManager


@write_intent
def stop_intent(c):
    seal_for_stop(c.ctx, SID)
    with c.db() as db:
        db.get(Session, SID).desired_state = 'stopped'
        db.commit()


async def stopped(c):
    await select_owner(c, 'terminal', 'stop-proof-owner')
    stop_intent(c)
    assert await c.manager.stop(SID) is True
    assert SID not in c.manager.runs
    assert c.owned_factory.current.process.returncode is not None


async def test_conclusive_stop_reused_after_runtime_and_manager_retirement(ownership_case):  # noqa: F811
    c = ownership_case
    await stopped(c)
    native_count = len(c.owned_factory.owners)
    fresh = RuntimeManager(c.ctx)
    try:
        for manager in (c.manager, fresh):
            assert await manager.stop(SID) is True
            assert not manager.runs and not manager.launches
        with c.db() as db:
            row = db.get(RelaySession, SID)
            record = db.get(RelayRun, row.current_run_id)
            assert record.identity[stop_proof.KEY] == stop_proof.stamp(row, record)
        assert len(c.owned_factory.owners) == native_count
    finally:
        await fresh.close()


@pytest.mark.parametrize('fault', ['enrollment', 'binding', 'epoch', 'run', 'running', 'unverified',
                                  'unended', 'identity', 'coordinates', 'placement', 'missing', 'malformed', 'provider'])
async def test_old_stop_proof_never_covers_changed_run_or_placement(ownership_case, fault):  # noqa: F811
    c = ownership_case
    await stopped(c)
    @write_intent
    def change():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            record = db.get(RelayRun, row.current_run_id)
            native = db.get(SessionRun, row.current_run_id)
            session = db.get(Session, SID)
            if fault == 'enrollment':
                row.enrollment = 'different-enrollment'
            elif fault == 'binding':
                row.binding_id = 'different-binding'
            elif fault == 'epoch':
                row.authority_epoch += 1
            elif fault == 'run':
                row.current_run_id = None
            elif fault == 'running':
                session.desired_state = 'running'
            elif fault == 'unverified':
                native.verified_at = None
            elif fault == 'unended':
                native.ended_at = None
            elif fault == 'identity':
                record.identity = {**record.identity, 'processIdentity': 'replacement-process'}
            elif fault == 'coordinates':
                session.workdir += '/replacement'
            elif fault == 'placement':
                session.run_dir += '-replacement'
            elif fault == 'missing':
                record.identity = {key: value for key, value in record.identity.items() if key != stop_proof.KEY}
            elif fault == 'malformed':
                record.identity = {**record.identity, stop_proof.KEY: {'version': 1}}
            elif fault == 'provider':
                session.provider = 'codex'
            db.commit()
    change()
    assert await c.manager.stop(SID) is False


@pytest.mark.parametrize('reply', [None, False, {'exited': False}, {'exited': 1}, {'exited': 'true'}, {'exited': True, 'extra': 1}])
async def test_unconcluded_reply_cannot_create_durable_stop_proof(ownership_case, reply):  # noqa: F811
    c = ownership_case
    stop_intent(c)
    original = c.launch.peer.call
    async def response(method, params):
        return reply if method == 'stop' else await original(method, params)
    c.launch.peer.call = response
    assert await c.manager.stop(SID) is False
    assert await c.manager.stop(SID) is False
    with c.db() as db:
        assert stop_proof.KEY not in db.get(RelayRun, c.launch.run_id).identity


async def test_changed_binding_after_exit_reply_cannot_commit_proof(ownership_case):  # noqa: F811
    c = ownership_case
    stop_intent(c)
    original = c.launch.peer.call
    async def response(method, params):
        result = await original(method, params)
        if method == 'stop':
            @write_intent
            def replace():
                with c.db() as db:
                    db.get(RelaySession, SID).binding_id = 'post-await-replacement'
                    db.commit()
            replace()
        return result
    c.launch.peer.call = response
    assert await c.manager.stop(SID) is False
    assert await c.manager.stop(SID) is False


async def test_old_stop_finally_does_not_remove_replacement_runtime(ownership_case):  # noqa: F811
    c = ownership_case
    stop_intent(c)
    replacement = object()
    original = c.launch.peer.close
    async def close():
        await original()
        c.manager.runs[SID] = replacement
    c.launch.peer.close = close
    try:
        assert await c.manager.stop(SID) is True
        assert c.manager.runs[SID] is replacement
    finally:
        c.manager.runs.pop(SID, None)


async def test_absent_runtime_or_unknown_identifier_is_not_exit_evidence(ownership_case):  # noqa: F811
    c = ownership_case
    assert await c.manager.stop('unknown-session') is False
    # A new coordinator must not turn an existing, still-live run into a proof.
    fresh = RuntimeManager(c.ctx)
    try:
        fresh.ctx.broker.call = AsyncMock(side_effect=AssertionError('No generic kill/relaunch fallback'))
        assert await fresh.stop(SID) is False
    finally:
        await fresh.close()


async def test_conclusive_stop_survives_fresh_database_engine(ownership_case):  # noqa: F811
    from copy import copy

    from acc.server.db import make_engine, make_sessionmaker
    c = ownership_case
    await stopped(c)
    engine = make_engine(str(c.engine.url))
    context = copy(c.ctx)
    context.db = make_sessionmaker(engine)
    fresh = RuntimeManager(context)
    try:
        assert await fresh.stop(SID) is True
        assert not fresh.runs and not fresh.launches
        assert len(c.owned_factory.owners) == 2
    finally:
        await fresh.close()
        engine.dispose()


async def test_receipt_metadata_does_not_invalidate_same_producer_exit(ownership_case):  # noqa: F811
    c = ownership_case
    await stopped(c)
    @write_intent
    def receipt_fact():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            record = db.get(RelayRun, row.current_run_id)
            record.identity = {**record.identity, 'applicationOrigins': {}}
            db.commit()
    receipt_fact()
    assert await c.manager.stop(SID) is True


async def test_json_boolean_is_not_a_stop_proof_version(ownership_case):  # noqa: F811
    c = ownership_case
    await stopped(c)
    @write_intent
    def malformed():
        with c.db() as db:
            row = db.get(RelaySession, SID)
            record = db.get(RelayRun, row.current_run_id)
            proof = {**record.identity[stop_proof.KEY], 'version': True}
            record.identity = {**record.identity, stop_proof.KEY: proof}
            # SQLAlchemy's JSON dirty comparison also treats True == 1. Force
            # this malformed persisted value rather than leaving the old int.
            from sqlalchemy.orm.attributes import flag_modified
            flag_modified(record, 'identity')
            db.commit()
    malformed()
    assert await c.manager.stop(SID) is False


async def test_stop_fact_is_not_blocked_by_withdrawn_admission_policy(ownership_case):  # noqa: F811
    from unittest.mock import Mock

    c = ownership_case
    stop_intent(c)
    c.manager.current_placement = Mock(side_effect=AssertionError('Stop must not require input admission'))
    assert await c.manager.stop(SID) is True
    assert await c.manager.stop(SID) is True
    c.manager.current_placement.assert_not_called()
