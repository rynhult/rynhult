"""File-framed browser bridge to the real offline Hub and retained native owner."""
import asyncio
import json
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from installed_security import prepare as prepare_security
from sqlalchemy import select

from acc.server.api.session_relay import router
from acc.server.models import RelayOperation


class Case:
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def exercise(self, c, state, launch, manager, config):
        directory = Path('/evidence/bridge')
        sid = state.spec.spec['session_id']
        identity = state.owner.attestation.process_identity
        native = state.binding.native_conversation
        assert state.owned_view is not None and state.owned_view.peer is not None
        def principal(request: Request):
            request.state.cookie_authenticated = True
            request.state.session_hash = 'offline-selector-login'
            return c.resolve()[0]
        c.ctx.principal = principal
        app = FastAPI()
        app.include_router(router(c.ctx))
        methods, client_ids, tokens = [], [], {}
        last_initialize = None
        resets = 0
        allowed = {f'/api/sessions/{sid}/relay','/api/session-relay/preference'}
        (directory/'ready.json').write_text(json.dumps({'sessionId':sid,'nativeId':native}))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),base_url='http://fixture') as client:
            sequence = 1
            async with asyncio.timeout(80):
                while not (directory/'stop').exists():
                    path = directory/f'request-{sequence}.json'
                    if not path.exists():
                        await asyncio.sleep(.01)
                        continue
                    assert path.stat().st_size <= 1 << 20
                    request = json.loads(path.read_text())
                    assert request.keys() == {'method','path','body','headers'}
                    assert request['path'] in allowed and request['method'] in ('GET','POST','PUT')
                    command = request['body'] if request['path'].endswith('/relay') else None
                    if command:
                        methods.append(command['method'])
                        assert command['method'] in {'ry/initialize','ry/session.attach','ry/session.detach',
                            'ry/session.snapshot','ry/events.read','ry/history.page'}
                        if command['method']=='ry/initialize':
                            last_initialize = command
                            client_ids.append(command['params']['clientId'])
                        if command['method']=='ry/session.snapshot' and (directory/'reset').exists():
                            # Reconnect invalidates the actual old Hub token.
                            # The browser receives a real stale-binding error.
                            assert last_initialize is not None
                            changed = await client.post(request['path'],json=last_initialize)
                            assert 'result' in changed.json()
                            resets += 1
                            (directory/'reset').unlink()
                    response = await client.request(request['method'],request['path'],json=request['body'],
                        headers={key:value for key,value in request['headers'].items() if key.lower()=='x-acc-relay-connection'})
                    body = response.json()
                    if command and command['method']=='ry/initialize' and 'result' in body:
                        key = command['params']['clientId']
                        old = tokens.get(key)
                        new = body['result']['connectionToken']
                        assert old != new
                        tokens[key] = new
                    assert state.owner.alive and state.owner.attestation.process_identity==identity
                    assert len(state.factory.active)==1 and self.base.calls==0
                    reply = directory/f'reply-{sequence}.json'
                    temporary = directory/f'reply-{sequence}.tmp'
                    temporary.write_text(json.dumps({'status':response.status_code,'body':body}))
                    temporary.rename(reply)
                    path.unlink()
                    sequence += 1
        assert resets==1 and len(client_ids)>=3, (resets,client_ids)
        assert len(set(client_ids)) < len(client_ids)  # reconnect reuses its client
        with c.db() as db:
            assert not list(db.scalars(select(RelayOperation)))
        proof = {'nativeId':native,'retainedExecutionIdentity':identity,'methods':methods,
            'clientIds':client_ids,'realReadTokenResets':resets,'oneRetainedOwner':True,
            'noInputClaimOrOperations':True,'noNativeModelCalls':True,'productionHub':True,
            'ownedTUIRetained':True,'terminalMediated':False,'declaredCapabilities':{}}
        (directory/'native-proof.json').write_text(json.dumps(proof))
        Path('/evidence/result.json').write_text(json.dumps(proof))


async def prepare(root, work, c):
    return Case(await prepare_security(root,work,c))
