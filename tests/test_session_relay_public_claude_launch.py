"""Public person launch selects existing exclusive enrollment; native owner is stubbed."""
from types import SimpleNamespace

import pytest
from test_auth import cookie_client, do_setup

from acc.server.models import RelaySession, Session
from acc.server.session_relay.exclusive_runtime import ExclusiveLaunch
from acc.session_relay import protocol as p


async def test_claude_launch_stays_refused_until_separate_qualification(api,project,monkeypatch):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY','true')
    body={'project_id':project['id'],'provider':'claude','claude_auth':'api_key','name':'Fresh','session_relay':True,'worktree':False}
    for endpoint in ('/api/sessions/preview','/api/sessions'):
        reply=await api.post(endpoint,json=body)
        assert reply.status_code==409 and 'not qualified' in reply.text
    assert (await api.get('/api/sessions')).json()==[]


async def test_person_public_claude_launch_reserves_new_exclusive_sessions_without_initial_replay(api,project,env,monkeypatch):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY','true')
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED',True)
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
    await do_setup(api,env);client=cookie_client(api)
    headers={'Origin':'http://acc','X-ACC-CSRF':client.cookies.get('acc_csrf')}
    client.headers.update(headers)
    from test_vault import add
    credential = await add(client, 'Reviewed fixture Anthropic', 'ANTHROPIC_API_KEY')
    granted = await client.post(f"/api/vault/keys/{credential['id']}/give", headers=headers,
        json={'project_id': project['id']})
    assert granted.status_code == 200, granted.text
    supervisor=api.app.state.supervisor;original=supervisor.broker.call;probes=[];launched=[]
    async def call(method,**params):
        if method=='relay_probe':
            probes.append(params);assert params['provider']=='claude'
            from acc.broker.claude_owner import CLI_DIGEST
            return {'available':True,'provider':'claude','cliDigest':CLI_DIGEST}
        return await original(method,**params)
    monkeypatch.setattr(supervisor.broker,'call',call)
    async def launch(self,identifier):
        assert self.provider=='claude' and self.session_id==identifier
        launched.append(identifier)  # explicit fake owner; no subscription CLI input
    monkeypatch.setattr(ExclusiveLaunch,'launch',launch)
    body={'project_id':project['id'],'provider':'claude','claude_auth':'api_key','name':'Fresh review','session_relay':True,
        'worktree':False,'initial_prompt':''}
    created=[]
    for _ in range(2):
        reply=await client.post('/api/sessions',json=body,headers=headers)
        assert reply.status_code==201,reply.text
        created.append(reply.json()['id'])
    assert len(set(created))==2 and launched==created and len(probes)==2
    with api.app.state.db() as db:
        sessions=[db.get(Session,key) for key in created]
        assert all(db.get(RelaySession,key) is not None for key in created)
        assert sessions[0].native_id and sessions[0].native_id!=sessions[1].native_id
        assert all(not session.launch_spec.inputs.get('initial_prompt') for session in sessions)
    denied=await client.post('/api/sessions',json={**body,'initial_prompt':'Do not replay'},headers=headers)
    assert denied.status_code==409 and launched==created


@pytest.mark.parametrize('fault',['none','digest','sdk','tool','unknown','pidfd'])
def test_claude_probe_is_readonly_pinned_and_provider_specific(tmp_path,monkeypatch,fault):
    import importlib.metadata

    from acc.broker import claude_owner, relay_runtime
    path=tmp_path/'claude';path.write_text('fake CLI bytes')
    monkeypatch.setattr(claude_owner,'CLI_DIGEST',p.sha256_digest(path.read_bytes()))
    monkeypatch.setattr(relay_runtime.shutil,'which',lambda name:None if fault=='tool' and name=='socat' else str(path))
    monkeypatch.setattr(importlib.metadata,'version',lambda _: 'wrong' if fault=='sdk' else '0.1.50')
    if fault == 'pidfd':monkeypatch.delattr(claude_owner.os, 'pidfd_open')
    factory=relay_runtime.RuntimeTransportFactory(SimpleNamespace(providers={'claude':str(path)}),owned_view_factory=None)
    if fault=='digest':path.write_text('changed')
    value=factory.probe('unknown' if fault=='unknown' else 'claude')
    assert value['available'] is (fault=='none')
    if fault=='none':assert value['provider']=='claude' and value['cliDigest']==claude_owner.CLI_DIGEST
    assert not factory.active


@pytest.mark.parametrize('readiness', [
    {'available':True},
    {'available':True,'provider':'codex','cliDigest':'wrong'},
    {'available':True,'provider':'claude','cliDigest':'wrong'},
    {'available':False,'provider':'claude'},
])
async def test_public_claude_launch_refuses_unbound_broker_readiness(api,project,env,monkeypatch,readiness):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY','true')
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED',True)
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
    await do_setup(api,env);client=cookie_client(api)
    headers={'Origin':'http://acc','X-ACC-CSRF':client.cookies.get('acc_csrf')}
    original=api.app.state.supervisor.broker.call;probes=[]
    async def call(method,**params):
        if method=='relay_probe':
            probes.append(params);return readiness
        return await original(method,**params)
    monkeypatch.setattr(api.app.state.supervisor.broker,'call',call)
    response=await client.post('/api/sessions',headers=headers,json={'project_id':project['id'],'provider':'claude','claude_auth':'api_key',
        'name':'Do not launch','session_relay':True,'worktree':False})
    assert response.status_code==409,response.text
    assert len(probes)==1 and probes[0]['provider']=='claude'
    assert (await api.get('/api/sessions')).json()==[]


@pytest.mark.parametrize('public,launch', [(False, False), (False, True), (True, False)])
async def test_each_claude_qualification_gate_precedes_probe_and_reservation(api, project, env, monkeypatch, public, launch):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY', 'true')
    monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', public)
    monkeypatch.setattr('acc.server.session_relay.exclusive_public.CLAUDE_LAUNCH_QUALIFIED', launch)
    await do_setup(api, env)
    client = cookie_client(api)
    headers = {'Origin': 'http://acc', 'X-ACC-CSRF': client.cookies.get('acc_csrf')}
    supervisor = api.app.state.supervisor
    original = supervisor.broker.call
    async def call(method, **params):
        assert method != 'relay_probe', 'Partial qualification must refuse before a probe'
        return await original(method, **params)
    monkeypatch.setattr(supervisor.broker, 'call', call)
    response = await client.post('/api/sessions', headers=headers, json={
        'project_id': project['id'], 'provider': 'claude', 'claude_auth': 'api_key', 'name': 'Unqualified',
        'session_relay': True, 'worktree': False})
    assert response.status_code == 409, response.text
    assert (await api.get('/api/sessions')).json() == []
    with api.app.state.db() as db:
        assert db.query(RelaySession).count() == 0


def test_codex_probe_keeps_its_digest_after_claude_probe(tmp_path, monkeypatch):
    from acc.broker import claude_owner, relay_runtime

    codex = tmp_path / 'codex-fixture'
    claude = tmp_path / 'claude-fixture'
    codex.write_bytes(b'artificial Codex executable')
    claude.write_bytes(b'artificial Claude executable')
    codex_digest = p.sha256_digest(codex.read_bytes())
    claude_digest = p.sha256_digest(claude.read_bytes())
    monkeypatch.setattr(relay_runtime, 'CLI_DIGEST', codex_digest)
    monkeypatch.setattr(claude_owner, 'CLI_DIGEST', claude_digest)
    monkeypatch.setattr(relay_runtime.shutil, 'which', lambda name: name)
    monkeypatch.setattr(claude_owner, 'installation_probe', lambda config: {'available': True})
    factory = relay_runtime.RuntimeTransportFactory(
        SimpleNamespace(providers={'codex': str(codex), 'claude': str(claude)}), owned_view_factory=None)
    assert factory.probe('claude') == {'available': True, 'provider': 'claude', 'cliDigest': claude_digest}
    assert factory.probe() == factory.probe('codex') == {'available': True}
    codex.write_bytes(b'changed executable')
    assert factory.probe() == factory.probe('codex') == {'available': False}
    assert factory.probe('claude')['cliDigest'] == claude_digest
    assert not factory.active
