"""Public launch must record the broker's actual Codex pin, never assume one."""
import pytest
from test_auth import cookie_client, do_setup

from acc.broker.relay_runtime import CLI_DIGEST
from acc.server.models import Session
from acc.server.session_relay.runtime import Launch


@pytest.mark.parametrize('fault',['missing','digest','provider','unavailable','none'])
async def test_codex_public_launch_requires_matching_probe_before_recording_pin(api,project,env,monkeypatch,fault):
    monkeypatch.setenv('ACC_FEATURE_SESSION_RELAY','true')
    await do_setup(api,env);client=cookie_client(api)
    client.headers.update({'Origin':'http://acc','X-ACC-CSRF':client.cookies.get('acc_csrf')})
    original=api.app.state.supervisor.broker.call;launched=[]
    ready={'available':True,'provider':'codex','cliDigest':CLI_DIGEST}
    if fault=='missing':ready.pop('cliDigest')
    if fault=='digest':ready['cliDigest']='sha256:'+'0'*64
    if fault=='provider':ready['provider']='claude'
    if fault=='unavailable':ready['available']=False
    async def broker_call(method,**params):
        if method=='relay_probe':return ready
        return await original(method,**params)
    monkeypatch.setattr(api.app.state.supervisor.broker,'call',broker_call)
    async def launch(self,identifier):launched.append((identifier,self.cli_digest))
    monkeypatch.setattr(Launch,'launch',launch)
    body={'project_id':project['id'],'provider':'codex','name':'Codex preview','session_relay':True,'worktree':False,'initial_prompt':''}
    reply=await client.post('/api/sessions',json=body)
    if fault=='none':
        assert reply.status_code==201,reply.text
        assert launched==[(reply.json()['id'],CLI_DIGEST)]
    else:
        assert reply.status_code==409 and not launched,reply.text
        with api.app.state.db() as db:assert not db.query(Session).count()
