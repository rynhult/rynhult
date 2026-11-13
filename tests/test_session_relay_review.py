"""Review regressions on SQLite and a test-owned PostgreSQL container."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import pytest
from relay_helpers import ACTOR, DIGEST, setup
from sqlalchemy import event, select

from acc.server.api.context import Principal
from acc.server.db import write_intent
from acc.server.models import RelayDecision, RelayEvent, RelayOperation, Session, SessionRun, User, utcnow
from acc.server.session_relay import authority, history, store
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


@pytest.fixture
def case(tmp_path, backend_url):
    case = setup(tmp_path, backend_url)
    case.hub = Hub(case.ctx)
    yield case
    case.engine.dispose()


def decision(identifier='question', session='relay', run='1'):
    return p.Decision(identifier, session, run, p.DecisionKind.TOOL_APPROVAL, DIGEST, 'Review tool',
                      (p.DecisionChoice('reject', 'Reject', p.ChoiceEffect.DENY),
                       p.DecisionChoice('allow', 'Allow', p.ChoiceEffect.ALLOW_ONCE)), 9999999999999, 0)


@write_intent
def add_decision(case):
    with case.db() as db:
        store.add_decision(db, store.lock(db, 'relay'), decision())
        db.commit()


def other_person(case):
    @write_intent
    def promote():
        with case.db() as db:
            db.get(User, 2).role = 'owner'
            db.commit()
    promote()
    return lambda: (Principal('sam', 'owner', 'fixture', 2), p.Actor(p.ActorKind.PERSON, 'sam', p.Channel.CHAT))


def enqueue(case, resolve, key):
    op = case.hub.register(resolve, 'relay', '1', key, 'turn.submit', DIGEST)
    case.hub.queue(resolve, 'relay', op.operation_id, expected_run='1', digest=DIGEST, if_busy='queue')
    return op.operation_id


def test_read_authority_uses_no_row_locks_and_writer_still_locks(case):
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    event.listen(case.engine, 'before_cursor_execute', capture)
    try:
        with case.db() as db:
            authority.current(case.ctx, db, case.resolve, 'relay')
        assert not any('FOR UPDATE' in sql for sql in statements)
        if case.engine.dialect.name == 'postgresql':
            with case.db() as writer, case.db() as reader:
                row = store.lock(writer, 'relay')
                # The production read lock timeout is 1ms. An MVCC reader must
                # succeed while a writer owns this row and after its snapshot ages.
                _, before = authority.current(case.ctx, reader, case.resolve, 'relay')
                store.append(writer, row, 'session.stateChanged', {'state': 'fixture'})
                writer.commit()
                reader.expire_all()
                _, after = authority.current(case.ctx, reader, case.resolve, 'relay')
                assert before == after
            assert any('FOR UPDATE' in sql for sql in statements)
    finally:
        event.remove(case.engine, 'before_cursor_execute', capture)


@pytest.mark.parametrize('source', ['group_board', 'schedule'])
def test_person_shaped_service_principal_cannot_answer(case, source):
    # These are exactly the person-shaped principals existing service paths build.
    who = Principal('alex', 'owner', 'fixture', 1)
    actor = authority.authenticated_actor(who)  # no authenticated human cookie
    assert actor.kind is p.ActorKind.SERVICE
    resolver = lambda: (who, actor)
    with case.db() as db, pytest.raises(p.RelayError) as error:
        authority.current(case.ctx, db, resolver, 'relay', action='decide', attached=True, decision=decision())
    assert error.value.code is p.ErrorCode.POLICY_REFUSED
    add_decision(case)
    with pytest.raises(p.RelayError) as error:
        case.hub.answer(resolver, 'relay', 'question', expected_run='1', attached=True, choice_id='allow',
                        revision=0, digest=DIGEST, operation_id='unregistered-' + source)
    assert error.value.code is p.ErrorCode.POLICY_REFUSED
    with case.db() as db:
        assert db.get(RelayDecision, ('relay', 'question')).decision['status'] == 'pending'


@pytest.mark.parametrize('pass_kind', ['queue', 'claim'])
def test_all_person_expiry_commits_even_when_current_action_refuses(case, pass_kind):
    old = enqueue(case, case.resolve, 'old')
    case.hub.disconnect(case.resolve, 'relay')
    resolver = other_person(case)
    @write_intent
    def age():
        with case.db() as db:
            db.get(RelayOperation, old).deadline = utcnow() - timedelta(seconds=1)
            db.commit()
    age()
    with pytest.raises(p.RelayError):
        if pass_kind == 'queue':
            case.hub.queue(resolver, 'relay', 'missing', expected_run='1', digest=DIGEST)
        else:
            case.hub.claim(resolver, 'relay', 'missing')
    with case.db() as db:
        assert db.get(RelayOperation, old).receipt['reason'] == 'expired'
    new = enqueue(case, resolver, 'new')
    assert case.hub.claim(resolver, 'relay', new).state is p.ReceiptState.SUBMITTING


def test_unconfirmed_entry_is_skipped_without_sending_or_reconfirming_it(case):
    old = enqueue(case, case.resolve, 'old')
    case.hub.disconnect(case.resolve, 'relay')
    resolver = other_person(case)
    new = enqueue(case, resolver, 'new')
    assert case.hub.claim(resolver, 'relay', new).state is p.ReceiptState.SUBMITTING
    with case.db() as db:
        op = db.get(RelayOperation, old)
        assert not op.confirmed and op.receipt['state'] == 'queued'


def test_event_counters_dedup_rollback_prune_and_constant_time_append(case):
    statements = []
    def capture(conn, cursor, statement, parameters, context, many):
        statements.append(statement)
    @write_intent
    def run():
        with case.db() as db:
            row = store.lock(db, 'relay')
            event.listen(case.engine, 'before_cursor_execute', capture)
            try:
                first = store.append(db, row, 'message.upsert', {'text': '世界🙂'}, source_key='source')
                store.append(db, row, 'message.upsert', {'text': '世界🙂'}, source_key='source')
            finally:
                event.remove(case.engine, 'before_cursor_execute', capture)
            assert row.event_count == 1 and row.used_bytes == first.byte_size
            assert row.used_bytes == len(p.canonical_json(first.event))
            assert not any('sum(' in sql.lower() or 'count(' in sql.lower() for sql in statements)
            db.commit()
            with db.begin_nested():
                row = store.lock(db, 'relay')
                store.append(db, row, 'session.stateChanged', {'state': 'discard'})
                db.rollback()
            row = store.lock(db, 'relay')
            assert row.event_count == 1 and row.used_bytes == first.byte_size
            first = db.get(RelayEvent, ('relay', 1))
            first.created_at = utcnow() - timedelta(days=31)
            db.flush()
            store.prune(db, row)
            remaining = db.scalars(select(RelayEvent).where(RelayEvent.session_id == 'relay')).all()
            assert row.event_count == len(remaining) == 1
            assert row.used_bytes == sum(e.byte_size for e in remaining)
            db.commit()
    run()


def test_flag_off_can_register_and_deny_but_cannot_allow(case):
    add_decision(case)
    case.ctx.settings = replace(case.ctx.settings, feature_overrides={'session_relay': False})
    def answer(choice):
        digest = p.operation_digest('decision.reply', {'sessionId': 'relay', 'expectedRunId': '1',
                    'decisionId': 'question', 'choiceId': choice, 'expectedRevision': 0, 'actionDigest': DIGEST})
        op = case.hub.register(case.resolve, 'relay', '1', choice, 'decision.reply', digest)
        return case.hub.answer(case.resolve, 'relay', 'question', expected_run='1', attached=True, choice_id=choice,
                               revision=0, digest=DIGEST, operation_id=op.operation_id)
    with pytest.raises(p.RelayError) as error:
        answer('allow')
    assert error.value.code is p.ErrorCode.UNSUPPORTED
    with case.db() as db:
        assert not list(db.scalars(select(RelayOperation)))  # refused before registration
    result = answer('reject')  # Choice *effect*, not a magical deny choice ID.
    assert result.status is p.DecisionStatus.RESERVED and result.choice_id == 'reject'
    with pytest.raises(p.RelayError):
        case.hub.register(case.resolve, 'relay', '1', 'turn', 'turn.submit', DIGEST)


def test_same_decision_id_in_two_sessions_is_independent(case):
    @write_intent
    def run():
        with case.db() as db:
            db.add(Session(id='second', provider='codex', name='Second', tmux_name='second-relay',
                           launch_spec_id=1, run_as='fixture', owner='alex'))
            db.flush()
            db.add(SessionRun(id=2, session_id='second', reason='launch', verified_at=utcnow()))
            db.flush()
            store.enroll(db, 'second', 2, 'second-binding', identity={}, frozen_policy={}, policy_revision=0)
            for session, run in [('relay', '1'), ('second', '2')]:
                store.add_decision(db, store.lock(db, session), decision(session=session, run=run))
            store.reserve_decision(db, store.lock(db, 'relay'), 'question', ACTOR, choice_id='reject',
                                   expected_revision=0, action_digest=DIGEST)
            db.commit()
            assert db.get(RelayDecision, ('relay', 'question')).decision['status'] == 'reserved'
            assert db.get(RelayDecision, ('second', 'question')).decision['status'] == 'pending'
    run()


def test_snapshot_recent_receipts_are_ordered_by_creation_time(case):
    @write_intent
    def run():
        with case.db() as db:
            row = store.lock(db, 'relay')
            for number in range(101):
                op = store.register(db, row, ACTOR, str(number), 'turn.submit', DIGEST, {})
                db.get(RelayOperation, op.operation_id).created_at = utcnow() + timedelta(seconds=number)
            db.commit()
            snapshot = history.snapshot(db, store.get(db, 'relay'), ACTOR)
            keys = [db.get(RelayOperation, r.operation_id).registration_key for r in snapshot.receipts]
            assert keys == [str(n) for n in range(100, 0, -1)]
    run()


async def test_cursor_replays_after_router_restart_and_auth_kind_uses_request_provenance(case):
    import httpx
    from fastapi import FastAPI, Request

    from acc.server.api.session_relay import router

    mode = {'cookie': True}
    def principal(request: Request):
        request.state.cookie_authenticated = mode['cookie']
        request.state.session_hash = 'fixture-login'
        return case.resolve()[0]
    case.ctx.principal = principal
    case.ctx.supervisor = SimpleNamespace(vault=SimpleNamespace(redactor=lambda db, keys: SimpleNamespace(text=lambda s: s)))
    @write_intent
    def publish():
        with case.db() as db:
            store.append(db, store.lock(db, 'relay'), 'session.stateChanged', {'state': 'fixture'})
            db.commit()
    publish()
    async def page(cursor=None):
        app = FastAPI()
        app.include_router(router(case.ctx))  # fresh router/key loader, as after main restart
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture') as client:
            return await client.get('/api/sessions/relay/relay/events', params={'cursor': cursor} if cursor else {})
    first = await page()
    assert first.status_code == 200
    cursor = first.json()['cursor']
    second = await page(cursor)
    assert second.status_code == 200 and second.json()['events'] == []
    key_path = case.ctx.settings.data_dir / 'secrets/session_relay_cursor.key'
    assert key_path.stat().st_mode & 0o777 == 0o600
    mode['cookie'] = False  # same username and user_id, authenticated as a service
    assert (await page(cursor)).status_code == 409  # different ActorKind/scope, not a person
