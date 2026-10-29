"""Review 2026-10-26 (L-2): the person's answers, plan edits and NOW edits wake an idle lead through slice 4's queue."""
import pytest
from helpers import pause_supervisor, wait_for
from sqlalchemy import select
from test_team import lead, request

from acc.server import orchestrator
from acc.server.orchestrator import delivery, planning, worklist


@pytest.fixture
def flag(monkeypatch):
    """The preview with the plan, working list and delivery on, as accd starts with all three switches."""
    monkeypatch.setenv('ACC_ORCHESTRATOR', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2', '1')
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    tools = {**worklist.TOOL_FIELDS, **planning.TOOL_FIELDS}
    handlers = {**{n: worklist._tool for n in worklist.TOOL_FIELDS}, **{n: planning._tool for n in planning.TOOL_FIELDS}}
    monkeypatch.setattr(orchestrator, 'TEAM_FIELDS', {**orchestrator.TEAM_FIELDS, **tools})
    monkeypatch.setattr(orchestrator, 'TEAM_ACTIONS', {**orchestrator.TEAM_ACTIONS, **handlers})


@pytest.fixture
async def on(flag, api, monkeypatch):
    woken = []
    monkeypatch.setattr(delivery, 'wake', woken.append)  # the test runs the queue itself, one step at a time
    api.woken = woken
    yield api
    await delivery.stop_worker(api.app.state.supervisor)


def nudges(api, lead_id):
    with api.app.state.supervisor.db() as db:
        return [(row.state, row.body) for row in db.scalars(select(delivery.MessageDelivery).where(
            delivery.MessageDelivery.session_id == lead_id, delivery.MessageDelivery.actor == 'supervisor')
            .order_by(delivery.MessageDelivery.created_at))]


async def idle(api, sid):
    return (await api.get(f'/api/sessions/{sid}')).json()['observed_state'] == 'idle'


async def test_answers_and_edits_wake_the_lead_once_per_turn(on, project, env):
    api = on
    x = await lead(api, project, env)
    url = f"/api/teams/{x['id']}"
    decision = (await request(api, env, x, 'worklist_add', kind='decision', title='Which database?',
                              options=[{'id': 'a', 'label': 'SQLite', 'consequence': 'one machine'},
                                       {'id': 'b', 'label': 'PostgreSQL', 'consequence': 'a service'}]))['result']
    assert (await request(api, env, x, 'now', text='Planning the importer'))['ok']
    assert (await request(api, env, x, 'plan_update', idea='Import a catalog', reason='Initial idea'))['ok']
    assert nudges(api, x['id']) == []  # the lead's own writes never wake it

    answered = await api.patch(f"{url}/worklist/items/{decision['id']}", json={'revision': decision['revision'], 'answer': 'b'})
    assert answered.status_code == 200, answered.text
    listed = (await api.get(f'{url}/worklist')).json()
    assert (await api.put(f'{url}/worklist/now', json={'text': 'Waiting on the database', 'revision': listed['revision']})).status_code == 200
    version = (await api.get(f'{url}/plan')).json()['version']
    assert (await api.patch(f'{url}/plan', json={'version': version, 'idea': 'Import customer lists'})).status_code == 200
    # Three changes while the lead is idle: one line, in Rynhult's voice, naming all three and none of the person's text.
    [(state, body)] = nudges(api, x['id'])
    assert state == 'queued' and len(api.woken) == 3
    assert body == ('[From Rynhult, not from your person or an agent]: your person answered a card on the working list, '
                    'edited the plan and changed NOW. Read the working list and the plan (worklist, plan) before '
                    'you go on.')

    await wait_for(lambda: idle(api, x['id']))
    sup = api.app.state.supervisor
    async def acknowledged():
        await delivery.process_once(sup)
        return nudges(api, x['id'])[0][0] == 'acknowledged'
    await wait_for(acknowledged, timeout=30)
    assert (env['run_dir'] / x['id'] / 'fake.received').read_text().startswith('[From Rynhult, not from your person')

    # The next change is a new turn's nudge.
    listed = (await api.get(f'{url}/worklist')).json()
    assert (await api.put(f'{url}/worklist/now', json={'text': 'Importer next', 'revision': listed['revision']})).status_code == 200
    assert [state for state, _ in nudges(api, x['id'])] == ['acknowledged', 'queued']
    assert nudges(api, x['id'])[1][1].startswith(delivery.NUDGE_ORIGIN + 'your person changed NOW.')


async def test_no_nudge_without_the_delivery_switch_or_for_a_stopped_lead(on, project, env, monkeypatch):
    api = on
    x = await lead(api, project, env)
    url = f"/api/teams/{x['id']}"
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '0')
    listed = (await api.get(f'{url}/worklist')).json()
    assert (await api.put(f'{url}/worklist/now', json={'text': 'One', 'revision': listed['revision']})).status_code == 200
    assert nudges(api, x['id']) == []
    monkeypatch.setenv('ACC_ORCHESTRATOR_V2_DELIVERY', '1')
    assert (await api.post(f"/api/sessions/{x['id']}/stop")).status_code in (200, 202, 204)
    listed = (await api.get(f'{url}/worklist')).json()
    assert (await api.put(f'{url}/worklist/now', json={'text': 'Two', 'revision': listed['revision']})).status_code == 200
    assert nudges(api, x['id']) == []


async def test_the_route_starts_the_delivery_worker_itself(flag, api, project, env):
    """No test stepping here: the person's request starts the worker (from the route's thread) and the line arrives."""
    x = await lead(api, project, env)
    await wait_for(lambda: idle(api, x['id']))
    try:
        listed = (await api.get(f"/api/teams/{x['id']}/worklist")).json()
        assert (await api.put(f"/api/teams/{x['id']}/worklist/now",
                              json={'text': 'Waiting on you', 'revision': listed['revision']})).status_code == 200
        async def arrived():
            return [state for state, _ in nudges(api, x['id'])] == ['acknowledged']
        await wait_for(arrived, timeout=30)
    finally:
        await delivery.stop_worker(api.app.state.supervisor)


@pytest.mark.parametrize('in_flight', ['typing', 'submitted'])
async def test_changes_after_a_nudge_is_claimed_keep_one_followup(on, project, env, in_flight):
    """An acknowledgement may lag the lead reading its tools; later edits must not disappear."""
    from acc.server.models import Session

    x = await lead(on, project, env)
    await pause_supervisor(on)
    sup = on.app.state.supervisor
    with sup.db() as db:
        first = delivery.nudge_lead(db, x['id'], 'answer')
        db.commit()
        first_id = first.id
    with sup.db() as db:
        delivery.transition(db, db.get(delivery.MessageDelivery, first_id), in_flight)
        db.get(Session, x['id']).observed_state = 'active'
        db.commit()
    # The worker has claimed its immutable text, and may already have submitted it.
    # Simulate edits after the lead's read, before the delayed acknowledgement.
    with sup.db() as db:
        followup = delivery.nudge_lead(db, x['id'], 'plan')
        assert followup is not None and followup.id != first_id
        followup_id = followup.id
        db.commit()
    with sup.db() as db:
        assert delivery.nudge_lead(db, x['id'], 'now').id == followup_id
        db.commit()
    with sup.db() as db:
        delivery.transition(db, db.get(delivery.MessageDelivery, first_id), 'acknowledged')
        db.commit()
    assert [state for state, _ in nudges(on, x['id'])] == ['acknowledged', 'queued']
    assert 'edited the plan and changed NOW' in nudges(on, x['id'])[1][1]
    await delivery.process_once(sup)  # an active turn must not be interrupted by the follow-up
    assert [state for state, _ in nudges(on, x['id'])] == ['acknowledged', 'queued']


async def test_foreign_and_unauthenticated_people_cannot_queue_a_lead_nudge(on, project, env, member_accounts):
    from test_auth import do_setup
    from test_users import browser, csrf, join

    await do_setup(on, env)
    x = await lead(on, project, env)
    card = (await request(on, env, x, 'worklist_add', kind='question', title='Which format?'))['result']
    url = f"/api/teams/{x['id']}"
    listed = (await on.get(f'{url}/worklist')).json()
    version = (await on.get(f'{url}/plan')).json()['version']
    writes = [(f'{url}/worklist/items/{card["id"]}', {'revision': card['revision'], 'answer': 'CSV'}),
              (f'{url}/plan', {'version': version, 'idea': 'Untrusted edit'}),
              (f'{url}/worklist/now', {'revision': listed['revision'], 'text': 'Untrusted edit'})]
    invite = (await on.post('/api/users/invites', json={'role': 'member', 'account': 'dana-agent'})).json()['link']
    async with browser(on) as foreign:
        assert (await join(foreign, invite, 'dana')).status_code == 200
        for path, body in writes:
            method = foreign.put if path.endswith('/now') else foreign.patch
            assert (await method(path, json=body, headers=csrf(foreign))).status_code == 404
    async with browser(on) as anonymous:
        for path, body in writes:
            method = anonymous.put if path.endswith('/now') else anonymous.patch
            assert (await method(path, json=body)).status_code == 401
    assert nudges(on, x['id']) == []
    assert on.woken == []
    assert (await on.get(f'{url}/plan')).json()['version'] == version
    assert (await on.get(f'{url}/worklist')).json()['revision'] == listed['revision']
