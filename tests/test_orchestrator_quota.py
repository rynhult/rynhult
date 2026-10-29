"""Synthetic usage observations only; no provider CLI or network calls."""
import pytest
from test_orchestrator_selection import choices, inventory

from acc.server.orchestrator import selection as sel


def policy():
    return {**sel.seed_preferences(), 'quota': {'enabled': True, 'max_used_percent': 90, 'max_age_minutes': 10}}


def test_fresh_quota_filters_without_budget_and_headroom_adds_a_reason():
    quota = {('local', 'codex'): {'status': 'fresh', 'used_percent': 95, 'headroom_percent': 5},
             ('local', 'claude'): {'status': 'fresh', 'used_percent': 20, 'headroom_percent': 80}}
    result = sel.rank({'role': 'backend', 'task_type': 'implementation', 'effort': 'implement'}, choices(), ['local'], inventory(),
                      preferences=policy(), quota=quota)
    assert result['candidates'][result['chosen']]['agent'] == 'claude'
    denied = [c for c in result['candidates'] if c['agent'] == 'codex']
    assert all(not c['eligible'] and any('quota' in r for r in c['rejections']) for c in denied)
    assert 'quota' in result['candidates'][0]['reasons'][0].lower()


def test_stale_unknown_and_disabled_quota_keep_existing_selection():
    args = ({'role': 'backend', 'task_type': 'implementation', 'effort': 'implement'}, choices(), ['local'], inventory())
    baseline = sel.rank(*args)
    for status in ('stale', 'unknown'):
        result = sel.rank(*args, preferences=policy(), quota={('local', 'codex'): {'status': status, 'used_percent': 100}})
        assert result['candidates'][0]['agent'] == baseline['candidates'][0]['agent']
        assert result['candidates'][0]['score'] == baseline['candidates'][0]['score']
    assert sel.rank(*args, quota={('local', 'codex'): {'status': 'fresh', 'used_percent': 100}}) == baseline


def test_headroom_affects_ties_but_cannot_override_owner_fallback_order():
    ready = inventory()
    ready.update({('worker', a): row for (_, a), row in inventory().items()})
    result = sel.rank({'role': 'backend', 'task_type': 'implementation', 'effort': 'implement'}, choices(), ['local', 'worker'], ready,
                      preferences=policy(), quota={('local', 'codex'): {'status': 'fresh', 'used_percent': 80, 'headroom_percent': 20},
                      ('worker', 'codex'): {'status': 'fresh', 'used_percent': 10, 'headroom_percent': 90},
                      ('local', 'claude'): {'status': 'fresh', 'used_percent': 0, 'headroom_percent': 100}})
    assert (result['candidates'][0]['agent'], result['candidates'][0]['machine']) == ('codex', 'worker')


@pytest.mark.parametrize('kind,value,used', [
    ('claude_statusline', {'five_hour': {'used_percentage': 20, 'resets_at': 2000},
                          'seven_day': {'used_percentage': 95, 'resets_at': 3000}}, 95),
    ('claude_cache', {'fetched_at': 990, 'five_hour': {'utilization': 40, 'resets_at': '1970-01-01T00:33:20Z'}}, 40),
    ('codex', {'primary': {'used_percent': 30, 'resets_at': 2000},
               'secondary': {'used_percent': 91, 'resets_at': 3000}}, 91),
    ('copilot', {'credits': {'percent_remaining': 15}, 'resets_at': '1970-01-01T00:33:20Z'}, 85),
])
def test_normalize_real_shapes(kind, value, used):
    from acc.server.orchestrator.quota import reading
    q = reading(kind, value, 995, now=1000, max_age_seconds=600)
    assert q['status'] == 'fresh' and q['used_percent'] == used and q['headroom_percent'] == 100 - used


@pytest.mark.parametrize('value,at', [
    ({'primary': {'used_percent': 99, 'resets_at': 2000}}, 300),  # old observation
    ({'primary': {'used_percent': 99, 'resets_at': 999}}, 995),  # window reset
    ({'primary': {'used_percent': 99, 'resets_at': 2000}}, 1001),  # future clock
    ({'primary': {'used_percent': float('nan'), 'resets_at': 2000}}, 995),
    ({'primary': {'used_percent': True, 'resets_at': 2000}}, 995),
    ({'primary': {'used_percent': 10**1000, 'resets_at': 2000}}, 995),
    ({'primary': 'bad'}, 995), ({'error': 'DO-NOT-COPY-CREDENTIALS'}, 995),
])
def test_bad_stale_or_reset_readings_never_become_filters(value, at):
    from acc.server.orchestrator.quota import reading
    q = reading('codex', value, at, now=1000, max_age_seconds=600)
    assert q['status'] != 'fresh' and 'DO-NOT-COPY-CREDENTIALS' not in str(q)


def test_repolling_old_claude_cache_does_not_make_it_fresh():
    from acc.server.orchestrator.quota import reading
    q = reading('claude_cache', {'fetched_at': 100, 'five_hour': {'utilization': 99, 'resets_at': 2000}},
                999, now=1000, max_age_seconds=600)
    assert q['status'] == 'stale'


@pytest.mark.parametrize('field', ['observed_at', 'fetched_at'])
@pytest.mark.parametrize('invalid', [1001, 1e20, None, 'bad', True, float('nan'), float('inf')])
def test_invalid_cache_timestamp_cannot_be_hidden_by_the_other_timestamp(field,invalid):
    from acc.server.orchestrator.quota import reading

    times={'observed_at':995,'fetched_at':995,field:invalid}
    q=reading('claude_cache',{'fetched_at':times['fetched_at'],
              'five_hour':{'utilization':99,'resets_at':2000}},
              times['observed_at'],now=1000,max_age_seconds=600)
    assert q=={'status':'unknown'}
    args=({'role': 'backend', 'task_type':'design','effort':'implement'},choices()[:1],['local'],inventory())
    baseline=sel.rank(*args)
    result=sel.rank(*args,preferences=policy(),quota={('local','claude'):q})
    assert result['state']=='proposed'
    assert [(c['model'],c['eligible'],c['score']) for c in result['candidates']]==[
        (c['model'],c['eligible'],c['score']) for c in baseline['candidates']]


def test_provider_without_usage_source_retains_existing_selection(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from acc.server.models import API_PRINCIPAL, Base
    from acc.server.orchestrator.quota import snapshot

    engine=create_engine('sqlite:///'+str(tmp_path/'no-source.sqlite'))
    Base.metadata.create_all(engine)
    prefs={'version':1,'rules':[{'profile':'*','task_type':'implementation',
            'choices':[{'agent':'cursor','models':['*']}]}]}
    allowed=[{'agent':'cursor','profile':None,'inputs':{'options':{'mode':'agent'}}}]
    ready={('local','cursor'):{'logged_in':True,'models':[{'id':'known'}]}}
    with Session(engine) as db:
        q=snapshot(db,'agent',['local'],{'cursor'},'agent',{**prefs,'quota':policy()['quota']},1000,
                   owner=API_PRINCIPAL)
    engine.dispose()
    assert q=={('local','cursor'):{'status':'unknown'}}
    args=({'role': 'general', 'task_type':'implementation','effort':'implement'},allowed,['local'],ready)
    baseline=sel.rank(*args,preferences=prefs)
    result=sel.rank(*args,preferences={**prefs,'quota':policy()['quota']},quota=q)
    chosen=result['candidates'][result['chosen']]
    assert result['state']=='proposed' and (chosen['agent'],chosen['model'])==('cursor','known')
    assert chosen['score']==baseline['candidates'][baseline['chosen']]['score']


@pytest.mark.parametrize('denial', ['permission','offline','allowlist'])
def test_fresh_headroom_cannot_widen_machine_provider_model_or_permission_grants(denial):
    allowed=choices()[:1]
    allowed[0]['inputs']['model']='small'
    ready=inventory()
    ready['ungranted','codex']={'logged_in':True,'models':[{'id':'ungranted'}]}
    prefs=policy()
    if denial=='permission':
        allowed[0]['inputs']['permission_mode']='plan'
    elif denial=='offline':
        ready.pop(('local','claude'))
    else:
        prefs['rules']=[{'profile':'*','task_type':'implementation',
                        'choices':[{'agent':'codex','models':['*']}]}]
    q={key:{'status':'fresh','used_percent':0,'headroom_percent':100} for key in ready}
    result=sel.rank({'role': 'backend', 'task_type':'implementation','effort':'implement'},allowed,['local'],ready,
                    preferences=prefs,quota=q)
    assert result['state']=='refused' and result['chosen'] is None
    assert result['candidates'] and all(not c['eligible'] and c['machine']=='local'
                                      and c['agent']=='claude' and c['model'] in ('small',None)
                                      for c in result['candidates'])


@pytest.mark.parametrize('key,value', [('max_used_percent', 0), ('max_used_percent', float('nan')),
                                    ('max_age_minutes', 0), ('max_age_minutes', 61), ('enabled', 'true')])
def test_owner_quota_policy_is_bounded_and_typed(key, value):
    p = policy(); p['quota'][key] = value
    with pytest.raises(ValueError): sel.validate_preferences(p)


def test_snapshot_does_not_borrow_default_account_or_remote_singletons(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from acc.server.models import API_PRINCIPAL, Base, Observation
    from acc.server.orchestrator.quota import snapshot
    engine = create_engine('sqlite:///'+str(tmp_path/'scope.sqlite'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(Observation(key='codex.rate_limits', source='test', observed_at=995,
                           value={'primary': {'used_percent': 99, 'resets_at': 2000}, '_scope': {'account':'owner-agent','machine':'local'}}))
        db.commit()
        own = snapshot(db, 'owner-agent', ['local', 'worker'], {'codex'}, 'owner-agent', policy(), 1000, owner=API_PRINCIPAL)
        assert own['local', 'codex']['status'] == 'fresh'
        assert own['worker', 'codex']['status'] == 'unknown'
        other = snapshot(db, 'member-agent', ['local'], {'codex'}, 'owner-agent', policy(), 1000, owner='dana')
        assert other['local', 'codex']['status'] == 'unknown'
        obs = db.get(Observation, 'codex.rate_limits')
        obs.value = {'primary': {'used_percent': 99, 'resets_at': 2000}}
        db.commit()
        legacy = snapshot(db, 'owner-agent', ['local'], {'codex'}, 'owner-agent', policy(), 1000, owner=API_PRINCIPAL)
        assert legacy['local', 'codex']['status'] == 'unknown', 'legacy global data has no proven account scope'
    engine.dispose()


def test_claude_statusline_is_joined_to_the_session_person_account_and_machine(tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as DbSession

    from acc.server.models import Base, LaunchSpec, Observation, Project, Session
    from acc.server.orchestrator.quota import snapshot
    engine = create_engine('sqlite:///'+str(tmp_path/'scope.sqlite'))
    Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        project = Project(slug='scope', name='Scope', workspace_path='/tmp/example', account='one-agent', host='local')
        db.add(project)
        for name, account, machine, used in [('a','one-agent','local',95), ('b','two-agent','local',10),
                                           ('c','one-agent','worker',40)]:
            db.add(Session(id=name, provider='claude', name=name, tmux_name=name, run_as=account, owner='alex', team_host=machine, project=project,
                           launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='test')))
            db.add(Observation(key='claude.statusline.'+name, source='statusline', observed_at=995,
                               value={'rate_limits': {'five_hour': {'used_percentage': used, 'resets_at': 2000}}}))
        db.add(Session(id='foreign',provider='claude',name='Foreign',tmux_name='foreign',run_as='one-agent',
                       owner='blair',team_host='worker',project=project,
                       launch_spec=LaunchSpec(provider='claude',inputs={},files={},digest='test')))
        db.add(Observation(key='claude.statusline.foreign',source='statusline',observed_at=999,
                           value={'rate_limits':{'five_hour':{'used_percentage':99,'resets_at':2000}}}))
        db.commit()
        result = snapshot(db,'one-agent',['local','worker'],{'claude'},'default-agent',policy(),1000,owner='alex')
        assert result['local','claude']['used_percent'] == 95
        assert result['worker','claude']['used_percent'] == 40
        assert 'two-agent' not in str(result) and 'via_session' not in str(result)
    engine.dispose()


def test_a_newer_or_future_statusline_cannot_hide_a_higher_reading(tmp_path):
    """Review 2026-10-26: statusline.json, and the file time that becomes observed_at, are written by the agent account.
    A newer low reading, or a future-dated one (unknown), must not shadow a genuine reading over the owner's threshold."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as DbSession

    from acc.server.models import Base, LaunchSpec, Observation, Project, Session
    from acc.server.orchestrator.quota import snapshot, worse
    engine = create_engine('sqlite:///'+str(tmp_path/'scope.sqlite'))
    Base.metadata.create_all(engine)
    with DbSession(engine) as db:
        project = Project(slug='scope', name='Scope', workspace_path='/tmp/example', account='one-agent', host='local')
        db.add(project)
        for name, at, used in [('genuine', 990, 95), ('forged', 999, 1), ('future', 5000, 1)]:
            db.add(Session(id=name, provider='claude', name=name, tmux_name=name, run_as='one-agent', owner='alex',
                           team_host='local', project=project,
                           launch_spec=LaunchSpec(provider='claude', inputs={}, files={}, digest='test')))
            db.add(Observation(key='claude.statusline.'+name, source='statusline', observed_at=at,
                               value={'rate_limits': {'five_hour': {'used_percentage': used, 'resets_at': 9000}}}))
        db.commit()
        result = snapshot(db, 'one-agent', ['local'], {'claude'}, 'default-agent', policy(), 1000, owner='alex')
        assert result['local', 'claude']['status'] == 'fresh' and result['local', 'claude']['used_percent'] == 95
    engine.dispose()
    fresh = {'status': 'fresh', 'used_percent': 40, 'headroom_percent': 60, 'observed_at': 1}
    assert worse({'status': 'unknown'}, {'status': 'stale'}) == {'status': 'stale'}
    assert worse(fresh, {'status': 'stale'}) is fresh and worse({'status': 'unknown'}, fresh) is fresh


@pytest.mark.parametrize('owner,status', [('casey','fresh'), ('dana','unknown'), (None,'unknown')])
def test_default_account_singletons_remain_owner_only_even_when_account_names_match(tmp_path,owner,status):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from acc.server.models import Base, Observation, User
    from acc.server.orchestrator.quota import snapshot

    engine=create_engine('sqlite:///'+str(tmp_path/'scope.sqlite'))
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        for username,role in [('casey','owner'),('dana','member')]:
            db.add(User(username=username,role=role,account='agent',password_hash='test',totp_secret='test'))
        db.add(Observation(key='codex.rate_limits',source='test',observed_at=995,
                           value={'primary':{'used_percent':99,'resets_at':2000},
                                  '_scope':{'account':'agent','machine':'local'}}))
        db.commit()
        result=snapshot(db,'agent',['local'],{'codex'},'agent',policy(),1000,owner=owner)
        assert result['local','codex']['status']==status
    engine.dispose()


@pytest.mark.parametrize('used,eligible', [(89.9, True), (90, False), (100, False)])
def test_threshold_is_inclusive(used, eligible):
    result = sel.rank({'role': 'backend', 'task_type':'implementation','effort':'implement'}, choices(), ['local'], inventory(),
                      preferences=policy(), quota={('local','codex'):{'status':'fresh','used_percent':used,'headroom_percent':100-used}})
    assert all(c['eligible'] == eligible for c in result['candidates'] if c['agent']=='codex')


async def test_poll_records_trusted_account_scope_even_if_provider_payload_claims_another(tmp_path):
    from types import SimpleNamespace

    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session

    from acc.server.models import Base, Observation
    from acc.server.polling import PollingMixin
    engine=create_engine('sqlite:///'+str(tmp_path/'poll.sqlite'))
    Base.metadata.create_all(engine)
    calls=[]
    async def call(method,**params):
        calls.append((method,params))
        if method=='installed_providers': return {'codex':True}
        return {'primary':{'used_percent':95,'resets_at':2000},'_scope':{'account':'forged','machine':'worker'}}
    supervisor=SimpleNamespace(db=lambda:Session(engine),broker=SimpleNamespace(default_account='owner-agent',call=call))
    await PollingMixin.refresh_providers(supervisor)
    with Session(engine) as db:
        obs=db.get(Observation,'codex.rate_limits')
        assert obs.value['_scope']=={'account':'owner-agent','machine':'local'}
    assert calls==[('installed_providers',{'account':'owner-agent'}),('usage',{'account':'owner-agent','provider':'codex'})]
    engine.dispose()
