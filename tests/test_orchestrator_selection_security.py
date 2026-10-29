"""Selection safety transitions, with only the fixture's private brokers/spools."""
import pytest
from sqlalchemy import select
from test_orchestrator_selection_api import assigned_start, choose, human, proposal, selection_runtime  # noqa: F401
from test_team import request

from acc.server.models import AuditLog, Team
from acc.server.orchestrator import selection as sel


@pytest.mark.parametrize('change',['signed_out','catalog','paused','budget','grant','account','live_pause'])
async def test_start_rechecks_current_authority_and_records_refusal(api,env,project,monkeypatch,change):
    caller,row=await proposal(api,env,project)
    if change in ('signed_out','catalog'):
        async def no_fit(*args):
            return {('local','claude'):{'logged_in':change!='signed_out',
                    'models':[] if change=='catalog' else [{'id':'model-a'}]}}
        monkeypatch.setattr(sel,'readiness',no_fit)
    if change in ('paused','budget'):
        monkeypatch.setattr(sel,'snapshot_budget',lambda *args:{'claude':{'paused':change=='paused',
            'remaining':0 if change=='budget' else 10,'windows':[],'unit':'usage_percent'}})
    if change=='grant':
        with api.app.state.db() as db:
            team=db.get(Team,caller['id'])
            limits={**team.limits,'agents':[{**team.limits['agents'][0],
                'inputs':{**team.limits['agents'][0]['inputs'],'permission_mode':'bypassPermissions'}}]}
            team.limits=limits;db.commit()
    if change=='account':
        from acc.server.models import Session
        with api.app.state.db() as db:
            db.get(Session,caller['id']).run_as='other-account';db.commit()
    if change=='live_pause':
        monkeypatch.setattr(sel.budgets,'paused',lambda *args:True)
    result=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='No')
    assert not result['ok']
    with api.app.state.db() as db:
        assert db.get(Team,caller['id']).started==0
        assert db.get(sel.Selection,row['id']).state=='refused'
        assert db.scalar(select(AuditLog).where(AuditLog.action=='selection.refused'))


async def test_override_is_person_only_and_never_accepts_permissions_or_freeform_choices(api,env,project):
    headers=await human(api,env)
    caller,row=await proposal(api,env,project)
    path=f"/api/teams/{caller['id']}/selections/{row['id']}/override"
    for body in ({'choice':True,'reason':'bad'},{'choice':999,'reason':'bad'},
                 {'choice':1,'reason':'bad','permission_mode':'bypassPermissions'}):
        assert (await api.post(path,headers=headers,json=body)).status_code==422
    refused=await request(api,env,caller,'selection_override',selection=row['id'],choice=1,reason='agent')
    assert not refused['ok']
    before=(await api.get('/api/settings/orchestrator/selection',headers=headers)).json()
    assert (await api.post(path,headers=headers,json={'choice':1,'reason':'Human decision'})).status_code==200
    assert (await api.get('/api/settings/orchestrator/selection',headers=headers)).json()==before


async def test_sibling_cannot_read_or_consume_selection_and_parent_cannot_impersonate_requester(api,env,project):
    caller,row=await proposal(api,env,project)
    a=(await assigned_start(api,env,caller,name='A',task='work'))['result']
    b=(await assigned_start(api,env,caller,name='B',task='work'))['result']
    pick=await choose(api,env,a,task='child work',need={'task_type':'implementation'})
    assert pick['ok'],pick
    sid=pick['result']['id']
    assert not (await request(api,env,b,'selection_status',selection=sid))['ok']
    assert (await request(api,env,caller,'selection_status',selection=sid))['ok']
    assert not (await request(api,env,caller,'start',assignment=pick['result']['assignment_id'],selection=sid,name='Impersonated'))['ok']


async def test_selection_text_is_vault_redacted_and_task_stays_a_digest(api,env,project,monkeypatch):
    import uuid

    from test_team import lead

    from acc.server.vault import Redactor
    headers=await human(api,env)
    caller=await lead(api,project,env)
    monkeypatch.setattr(api.app.state.supervisor.vault,'redactor',lambda *args:Redactor([('selection-canary','fixture')]))
    # Deliberately unredacted historical assignment: exercise the independent
    # ledger boundary, not the earlier spool/assignment redaction.
    from acc.server.orchestrator.assignments import Assignment
    assignment = uuid.uuid4().hex
    with api.app.state.db() as db:
        db.add(Assignment(id=assignment, team_id=caller['id'], created_by=caller['id'],
            task_type='implementation', goal='selection-canary task', state='open',
            need={'task_type':'implementation','role':'general','classification_reason':'selection-canary reason'}))
        db.commit()
    row=await api.app.state.supervisor.team_action(caller['id'],{
        'id':uuid.uuid4().hex,'action':'select','assignment':assignment})
    assert 'selection-canary' not in row['classification_reason']
    assert len(row['task_digest'])==64
    assert 'selection-canary' not in str(row)
    ledger = await api.get(f"/api/teams/{caller['id']}/selections", headers=headers)
    assert ledger.status_code == 200
    assert 'selection-canary' not in ledger.text


async def test_wrong_team_review_author_is_refused_without_a_selection(api,env,project):
    caller,row=await proposal(api,env,project)
    reply=await choose(api,env,caller,task='review',need={'task_type':'review','author_session':'f'*12})
    assert not reply['ok'] and 'outside the team' in reply['error']


@pytest.mark.parametrize('identifier',[None,{},[],True,'not-an-id'])
async def test_status_refuses_malformed_identifiers(api,env,project,identifier):
    caller,_=await proposal(api,env,project)
    reply=await request(api,env,caller,'selection_status',selection=identifier)
    assert not reply['ok'] and reply['error']=='invalid selection identifier'
