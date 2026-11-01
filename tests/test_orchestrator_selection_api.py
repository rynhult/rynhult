"""Slice 3 HTTP/spool integration. Broker launches only test fixture CLIs."""
import json

import pytest
from sqlalchemy import select
from test_auth import do_setup
from test_team import lead, repo, request

from acc.server.models import AuditLog, Team
from acc.server.orchestrator import selection as sel


@pytest.fixture(autouse=True)
def selection_runtime(monkeypatch):
    monkeypatch.setenv('ACC_ORCHESTRATOR','1')
    # These tests own synthetic quota observations; a concurrent fake-provider poll must not replace them.
    from acc.server.polling import PollingMixin
    async def no_usage_poll(self):
        return {}
    monkeypatch.setattr(PollingMixin,'refresh_providers',no_usage_poll)
    async def ready(supervisor,account,machines,agents):
        return {(machine,agent):{'logged_in':True,'models':[{'id':'model-a','price_class':'cheap'},
                    {'id':'model-b','price_class':'standard'}]} for machine in machines for agent in agents}
    monkeypatch.setattr(sel,'readiness',ready)


async def human(api,env):
    await do_setup(api,env)
    return {'Authorization':'','Origin':env['settings'].allowed_origins[0],
            'X-ACC-CSRF':api.cookies.get('acc_csrf')}


async def choose(api, env, caller, *, task, need):
    response = await request(api, env, caller, 'assign', goal=task, task_type=need['task_type'],
                             acceptance=[], requirement_ids=[],
                             need={'role':'general', 'classification_reason':'Test task classification', **need})
    if not response['ok']:
        return response
    return await request(api,env,caller,'select', assignment=response['result']['id'])


async def assigned_start(api, env, caller, *, name, task, **options):
    from unittest.mock import patch

    from acc.server.models import Team
    with api.app.state.db() as db:
        legacy = db.get(Team, caller['team_id'] or caller['id']).protocol_version < 2
    if legacy:
        return await request(api, env, caller, 'start', agent='claude', name=name, task=task, **options)
    async def ready(supervisor, account, machines, agents):
        return {(machine, agent): {'logged_in':True, 'models':[{'id':'model-a'}]}
                for machine in machines for agent in agents}
    # Lifecycle tests use fixture providers, never real account/catalog discovery.
    # Dedicated selection tests exercise refusals with their own readiness snapshot.
    with patch.object(sel, 'readiness', ready):
        proposed = await choose(api, env, caller, task=task, need={'task_type':'implementation'})
        if not proposed['ok'] or proposed['result']['chosen'] is None:
            return proposed
        row = proposed['result']
        return await request(api, env, caller, 'start', assignment=row['assignment_id'], selection=row['id'], name=name, **options)


async def proposal(api,env,project,task='synthetic work',**need):
    caller=await lead(api,project,env)
    reply=await choose(api,env,caller,task=task,need={'task_type':'implementation','effort':'implement',**need})
    assert reply['ok'],reply
    return caller,reply['result']


async def test_selection_records_digest_reasons_and_starts_only_the_frozen_choice(api,env,project):
    caller,row=await proposal(api,env,project,task='SYNTHETIC-TASK-NOT-STORED')
    assert row['state']=='proposed' and row['candidates'][row['chosen']]['agent']=='claude'
    assert 'SYNTHETIC-TASK-NOT-STORED' not in json.dumps(row)
    reply=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Selected')
    assert reply['ok'],reply
    helper=reply['result']
    detail=(await api.get(f"/api/sessions/{helper['id']}")).json()
    assert detail['launch_spec']['inputs']['model']=='model-a'
    replay=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Replay')
    assert replay['ok'] and replay['result']['id'] == helper['id']
    current=await request(api,env,caller,'selection_status',selection=row['id'])
    assert current['result']['session_id']==helper['id']


@pytest.mark.parametrize('codex_signed_in', [False, True])
async def test_default_grant_selects_the_assignments_profile_and_keeps_review_read_only(api, env, project, monkeypatch, codex_signed_in):
    repo(env['workspaces'] / 'demo')
    original = api.app.state.ctx.broker.call

    async def call(method, **params):
        if method == 'auth_health':
            return {'claude': {'logged_in': True}, 'codex': {'logged_in': codex_signed_in}}
        if method == 'installed_providers':
            return {'claude': True, 'codex': True}
        return await original(method, **params)

    monkeypatch.setattr(api.app.state.ctx.broker, 'call', call)
    body = {'project_id': project['id'], 'provider': 'claude',
        'name': 'Default profile team', 'worktree': True, 'orchestrator': {}}
    preview = await api.post('/api/sessions/preview', json=body)
    assert preview.status_code == 200, preview.text
    created = await api.post('/api/sessions', json=body)
    assert created.status_code == 201, created.text
    caller = created.json()
    limits = (await api.get(f"/api/sessions/{caller['id']}/team")).json()['limits']
    expected = [('claude', profile) for profile in ('fullstack', 'frontend', 'backend', 'code-reviewer', 'qa')]
    if codex_signed_in:
        expected += [('codex', profile) for profile in ('fullstack', 'backend')]
    assert [(a['agent'], a['profile']) for a in limits['agents']] == expected
    reviewer = next(a for a in limits['agents'] if a['profile'] == 'code-reviewer')
    assert reviewer['inputs']['permission_mode'] == 'plan'
    registry = (await api.get('/api/providers/registry', params={'project_id': project['id']})).json()
    assert [(a['agent'], a['profile']) for a in registry['team_agents']] == expected
    selected = await choose(api, env, caller, task='Build the settings form',
        need={'task_type': 'implementation', 'role': 'frontend'})
    assert selected['ok'], selected
    row = selected['result']
    assert row['chosen'] is not None, row
    assert row['candidates'][row['chosen']]['profile'] == 'frontend'
    started = await request(api, env, caller, 'start', assignment=row['assignment_id'], selection=row['id'], name='Frontend')
    assert started['ok'], started
    detail = (await api.get(f"/api/sessions/{started['result']['id']}")).json()
    assert detail['launch_spec']['inputs']['profile'] == 'frontend'


async def test_person_override_and_ordered_allowlists_preserve_grants(api,env,project):
    headers=await human(api,env)
    caller,row=await proposal(api,env,project)
    path=f"/api/teams/{caller['id']}/selections/{row['id']}/override"
    assert (await api.post(path,json={'choice':1,'reason':'Prefer this model'})).status_code==403
    changed=await api.post(path,headers=headers,json={'choice':1,'reason':'Prefer this model'})
    assert changed.status_code==200,changed.text
    assert changed.json()['state']=='overridden' and changed.json()['override_by']
    assert (await api.get(f"/api/teams/{caller['id']}/selections",headers=headers)).status_code==200
    assert (await api.post(path,headers={'Authorization':''},json={'choice':0,'reason':'Missing CSRF'})).status_code==403
    rules={'version':1,'rules':[{'profile':'*','task_type':'implementation','choices':[]}]}
    saved=await api.put('/api/settings/orchestrator/selection',headers=headers,json=rules)
    assert saved.status_code==200,saved.text
    refused=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Revoked')
    assert not refused['ok']
    with api.app.state.db() as db:
        actions=[a.action for a in db.scalars(select(AuditLog).where(AuditLog.action.like('selection.%')))]
        assert 'selection.overridden' in actions and 'selection.preferences_changed' in actions
        assert db.get(Team,caller['id']).started==0


async def test_task_tampering_foreign_selection_and_stopped_team_refuse_before_launch(api,env,project):
    caller,row=await proposal(api,env,project)
    bad=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Changed',task='different task')
    assert not bad['ok']
    # A caller cannot mix an agent/profile or machine override into a selected start.
    bad=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Changed',agent='codex')
    assert not bad['ok']
    with api.app.state.db() as db:
        db.get(Team,caller['id']).stopped=True; db.commit()
    bad=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Stopped')
    assert not bad['ok']


async def test_review_launch_records_author_and_uses_a_distinct_session(api,env,project):
    caller=await lead(api,project,env)
    no_author=await choose(api,env,caller,task='review',need={'task_type':'security_review','effort':'implement'})
    assert not no_author['ok']
    selected=await choose(api,env,caller,task='review',need={'task_type':'security_review','effort':'implement',
                                                                      'author_session':caller['id']})
    assert selected['ok'],selected
    started=await request(api,env,caller,'start',assignment=selected['result']['assignment_id'],selection=selected['result']['id'],name='Review')
    assert started['ok'],started
    assert started['result']['id']!=caller['id']
    with api.app.state.db() as db:
        ledger=db.get(sel.Selection,selected['result']['id'])
        assert ledger.need['author_session']==caller['id'] and ledger.session_id!=caller['id']


async def test_selection_api_and_tools_are_off_with_explicit_override(api,env,project,monkeypatch):
    headers=await human(api,env)
    caller=await lead(api,project,env)
    monkeypatch.setenv('ACC_ORCHESTRATOR','0')
    assert (await api.get('/api/settings/orchestrator/selection',headers=headers)).status_code==404
    reply=await choose(api,env,caller,task='work',need={'task_type':'docs'})
    assert reply=={'ok':False,'error':'invalid team action or arguments'}


async def test_same_profile_with_different_permissions_uses_exact_grant_entry(api,env,project):
    caller=await lead(api,project,env,agents=[{'agent':'claude','profile':None,'permission_mode':'acceptEdits'},
                                           {'agent':'claude','profile':None,'permission_mode':'plan'}])
    chosen=await choose(api,env,caller,task='plan only',need={'task_type':'design','effort':'plan'})
    assert chosen['ok'],chosen
    row=chosen['result']; assert row['candidates'][row['chosen']]['grant_index']==1
    started=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Planner')
    assert started['ok'],started
    detail=(await api.get(f"/api/sessions/{started['result']['id']}")).json()
    assert detail['launch_spec']['inputs']['permission_mode']=='plan'


async def test_foreign_people_and_bearers_cannot_see_or_override_selection(api,env,project,member_accounts):
    from test_users import browser, csrf, join
    await human(api,env)
    caller,row=await proposal(api,env,project)
    link=(await api.post('/api/users/invites',json={'role':'member','account':'dana-agent'})).json()['link']
    async with browser(api) as other:
        await join(other,link,'other')
        assert (await other.get(f"/api/teams/{caller['id']}/selections")).status_code==404
        assert (await other.post(f"/api/teams/{caller['id']}/selections/{row['id']}/override",headers=csrf(other),
                                 json={'choice':1,'reason':'foreign'})).status_code==404
        assert (await other.get('/api/settings/orchestrator/selection')).status_code==403
        assert (await other.put('/api/settings/orchestrator/selection',headers=csrf(other),
                                json={**sel.seed_preferences(),'quota':{
                                    'enabled':True,'max_used_percent':100,'max_age_minutes':60}})).status_code==403
    assert (await api.get(f"/api/teams/{caller['id']}/selections")).status_code==403


async def test_quota_reasons_do_not_borrow_foreign_person_readings_for_rejected_candidates(
        api,env,project,member_accounts,monkeypatch):
    import time

    from helpers import write_db
    from test_multihost import enroll
    from test_users import browser, join

    from acc.server.models import LaunchSpec, Observation, Project, Session

    headers=await human(api,env)
    policy={**sel.seed_preferences(),'quota':{'enabled':True,'max_used_percent':90,'max_age_minutes':10}}
    assert (await api.put('/api/settings/orchestrator/selection',headers=headers,json=policy)).status_code==200
    await enroll(api,'worker')
    caller=await lead(api,project,env,machines=['local','worker'])
    invite=await api.post('/api/users/invites',json={'role':'member','account':'dana-agent'})
    assert invite.status_code==201,invite.text
    async with browser(api) as member:
        assert (await join(member,invite.json()['link'],'dana')).status_code==200
        with write_db(api.app.state.db) as db:
            own=db.get(Session,caller['id'])
            own.owner,own.run_as='dana','dana-agent'
            foreign=Project(slug='foreign-quota',name='Foreign quota',owner='blair',account='dana-agent',
                            host='worker',workspace_path='/tmp/foreign-quota')
            db.add(Session(id='foreign-quota',project=foreign,provider='claude',name='Foreign',
                           tmux_name='foreign-quota',owner='blair',run_as='dana-agent',
                           desired_state='stopped',observed_state='stopped',
                           launch_spec=LaunchSpec(provider='claude',inputs={},files={},digest='test')))
            db.add(Observation(key='claude.statusline.foreign-quota',source='statusline',observed_at=time.time(),
                               value={'rate_limits':{'five_hour':{
                                   'used_percentage':91.234,'resets_at':time.time()+3600}}}))
            db.commit()
        async def local_only(supervisor,account,machines,agents):
            return {('local','claude'):{'logged_in':True,'models':[{'id':'model-a'}]}}
        monkeypatch.setattr(sel,'readiness',local_only)
        reply=await choose(api,env,caller,task='private quota',
                            need={'task_type':'implementation','effort':'implement'})
        assert reply['ok'],reply
        agent_candidates=reply['result']['candidates']
        rejected=[c for c in agent_candidates if c['machine']=='worker']
        assert rejected and all(not c['eligible'] and c['quota']=={'status':'unknown'} for c in rejected)
        listed=await member.get(f"/api/teams/{caller['id']}/selections")
        assert listed.status_code==200,listed.text
        person_candidates=listed.json()['selections'][0]['candidates']
        rejected=[c for c in person_candidates if c['machine']=='worker']
        assert rejected and all(c['quota']=={'status':'unknown'} for c in rejected)
        assert '91.234' not in listed.text and '91.2% used' not in listed.text


async def test_launch_failure_is_consumed_and_audited(api,env,project,monkeypatch):
    caller,row=await proposal(api,env,project)
    supervisor=api.app.state.supervisor
    real=supervisor.broker.call
    async def failed(method,**params):
        if method=='make_worktree': raise RuntimeError('synthetic worktree failure')
        return await real(method,**params)
    monkeypatch.setattr(supervisor.broker,'call',failed)
    result=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Fail')
    assert not result['ok']
    with api.app.state.db() as db:
        record=db.get(sel.Selection,row['id'])
        assert record.state=='started' and record.session_id is None
        assert db.get(Team,caller['id']).started==0  # a start that never created its helper gives its count back (C-L3)
        assert db.scalar(select(AuditLog).where(AuditLog.action=='selection.started'))


async def test_another_team_cannot_consume_a_selection(api,env,project):
    caller,row=await proposal(api,env,project)
    # A separate lead with the same person/account still has no authority for this team's selection.
    response=await api.post('/api/sessions',json={'project_id':project['id'],'provider':'claude','name':'Other lead',
        'worktree':True,'orchestrator':{'agents':[{'agent':'claude','profile':None}],'machines':['local']}})
    assert response.status_code==201,response.text
    result=await request(api,env,response.json(),'start',assignment=row['assignment_id'],selection=row['id'],name='Stolen')
    assert not result['ok']
    with api.app.state.db() as db:
        assert db.get(sel.Selection,row['id']).state=='proposed'


async def test_agents_see_only_their_teams_rounded_remaining_budget(api,env,project,monkeypatch):
    """Review 2026-10-25 (OV-8, threat review S3-7): no windows, account, owner or policy for agents; the person's
    selection API keeps the full snapshot."""
    def snapshot(db, owner, agents, project_id=None, machine=None, account=None):
        assert project_id == project['id']
        return {agent: {'remaining': 41.7,
            'windows': [{'id': agent + '.five_hour', 'remaining': 41.7, 'resets_at': None}],
            'paused': False, 'unit': 'usage_percent'} for agent in agents}
    monkeypatch.setattr(sel, 'snapshot_budget', snapshot)
    caller,row=await proposal(api,env,project)
    assert row['budget']=={'providers':{'claude':{'remaining':42,'paused':False}},'unit':'usage_percent',
                           'expected_spend':row['budget']['expected_spend']}
    current=await request(api,env,caller,'selection_status',selection=row['id'])
    assert 'windows' not in json.dumps(current['result']['budget']) and 'owner' not in current['result']['budget']
    headers=await human(api,env)
    listed=(await api.get(f"/api/teams/{caller['id']}/selections",headers=headers)).json()['selections']
    assert listed[0]['budget']['providers']['claude']['windows'] and listed[0]['budget']['owner']


async def test_quota_policy_is_audited_and_fresh_usage_is_rechecked_before_start(api,env,project):
    import time

    from acc.server.models import Observation
    headers=await human(api,env)
    policy={**sel.seed_preferences(),'quota':{'enabled':True,'max_used_percent':90,'max_age_minutes':10}}
    saved=await api.put('/api/settings/orchestrator/selection',headers=headers,json=policy)
    assert saved.status_code==200,saved.text
    def usage(percent):
        with api.app.state.db() as db:
            db.merge(Observation(key='claude.usage_cache',source='test',observed_at=time.time(),value={
                '_scope':{'account':api.app.state.supervisor.broker.default_account,'machine':'local'},
                'fetched_at':time.time(),'five_hour':{'utilization':percent,'resets_at':time.time()+3600}}))
            db.commit()
    usage(20)
    caller,row=await proposal(api,env,project)
    assert row['state']=='proposed'
    chosen=row['candidates'][row['chosen']]
    assert chosen['quota']=={'status':'fresh','headroom_percent':80}
    assert 'observed_at' not in json.dumps(row)
    usage(95)
    denied=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='No quota')
    assert not denied['ok']
    with api.app.state.db() as db:
        ledger=db.get(sel.Selection,row['id'])
        assert ledger.state=='refused'
        assert db.get(Team,caller['id']).started==0
        audit=db.scalar(select(AuditLog).where(AuditLog.action=='selection.preferences_changed'))
        assert audit.detail['quota']==policy['quota']


async def test_override_cannot_bypass_new_quota_and_stale_usage_does_not_block_start(api,env,project):
    import time

    from acc.server.models import Observation
    headers=await human(api,env)
    policy={**sel.seed_preferences(),'quota':{'enabled':True,'max_used_percent':90,'max_age_minutes':10}}
    assert (await api.put('/api/settings/orchestrator/selection',headers=headers,json=policy)).status_code==200
    caller,row=await proposal(api,env,project)
    with api.app.state.db() as db:
        db.merge(Observation(key='claude.usage_cache',source='test',observed_at=time.time(),value={
            '_scope':{'account':api.app.state.supervisor.broker.default_account,'machine':'local'},
                'fetched_at':time.time(),'five_hour':{'utilization':99,'resets_at':time.time()+3600}}))
        db.commit()
    override=await api.post(f"/api/teams/{caller['id']}/selections/{row['id']}/override",headers=headers,
                            json={'choice':row['chosen'],'reason':'Cannot override quota'})
    assert override.status_code==409,override.text
    with api.app.state.db() as db:
        obs=db.get(Observation,'claude.usage_cache')
        obs.value={**obs.value,'fetched_at':time.time()-3600}
        db.commit()
    # A new poll timestamp cannot make the old provider cache fresh. Original budgets/grant still apply.
    started=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Stale is unknown')
    assert started['ok'],started


@pytest.mark.parametrize('invalid_field', ['observed_at','fetched_at'])
async def test_invalid_cache_timestamp_cannot_refuse_selection_override_or_start(api,env,project,invalid_field):
    import time

    from helpers import write_db

    from acc.server.models import Observation

    headers=await human(api,env)
    policy={**sel.seed_preferences(),'quota':{'enabled':True,'max_used_percent':90,'max_age_minutes':10}}
    assert (await api.put('/api/settings/orchestrator/selection',headers=headers,json=policy)).status_code==200
    caller,row=await proposal(api,env,project)
    at=time.time()
    times={'observed_at':at,'fetched_at':at,invalid_field:at+3600}
    with write_db(api.app.state.db) as db:
        db.merge(Observation(key='claude.usage_cache',source='test',observed_at=times['observed_at'],value={
            '_scope':{'account':api.app.state.supervisor.broker.default_account,'machine':'local'},
            'fetched_at':times['fetched_at'],'five_hour':{'utilization':99,'resets_at':at+7200}}))
        db.commit()
    selected=await choose(api,env,caller,task='synthetic work',
                           need={'task_type':'implementation','effort':'implement'})
    assert selected['ok'] and selected['result']['state']=='proposed',selected
    assert all(c['quota']=={'status':'unknown'} for c in selected['result']['candidates'])
    override=await api.post(f"/api/teams/{caller['id']}/selections/{row['id']}/override",headers=headers,
                            json={'choice':row['chosen'],'reason':'Invalid quota is not a filter'})
    assert override.status_code==200,override.text
    started=await request(api,env,caller,'start',assignment=row['assignment_id'],selection=row['id'],name='Unknown quota')
    assert started['ok'],started


async def test_plain_start_never_bypasses_assignment_classification(api,env,project):
    caller = await lead(api,project,env)
    refused = await request(api,env,caller,'start',agent='claude',profile=None,name='Plain',task='synthetic work')
    assert not refused['ok'] and 'assignment' in refused['error']
