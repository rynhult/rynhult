"""Unsupported native controls against the enrolled, retained execution owner."""
import json
from pathlib import Path

from installed_security import prepare as prepare_security
from websockets.asyncio.client import unix_connect


class Case:
    def __init__(self, base):
        self.base = base

    def __getattr__(self, name):
        return getattr(self.base, name)

    async def exercise(self, c, state, launch, manager, config):
        owner = state.owner
        identity = owner.attestation.process_identity
        native = state.binding.native_conversation
        controls = [
            ('config/value/write',{'keyPath':'model','value':'other'}),
            ('config/batchWrite',{'edits':[]}),('account/login/start',{'type':'apiKey','apiKey':'artificial'}),
            ('account/logout',{}),('thread/start',{}),('thread/fork',{'threadId':native}),
            ('turn/steer',{'threadId':native,'input':[{'type':'text','text':'not delivered'}]}),
            ('command/exec',{'command':['true']}),('thread/rollback',{'threadId':native,'numTurns':1}),
            ('thread/resume',{'threadId':native,'config':{'features.hooks':False}}),
            ('thread/resume',{'threadId':native,'approvalPolicy':'never'}),
            ('thread/resume',{'threadId':native,'model':'other'}),
            ('turn/start',{'threadId':native,'model':'other','input':[{'type':'text','text':'not delivered'}]}),
            ('turn/start',{'threadId':native,'effort':'high','input':[{'type':'text','text':'not delivered'}]}),
            ('turn/start',{'threadId':native,'input':[{'type':'localImage','path':'/tmp/attachment.png'}]}),
            ('turn/start',{'threadId':native,'input':[{'type':'image','url':'https://example.com/fixture.png'}]}),
            ('turn/start',{'threadId':native,'input':[{'type':'text','text':'not delivered','text_elements':[{}]}]}),
            ('session/resume',{'sessionId':native}),('session/stop',{}),('editor/open',{'path':'fixture'}),
        ]
        refused = []
        async with unix_connect(str(state.native_socket.path),compression=None) as client:
            await client.send(json.dumps({'id':0,'method':'initialize','params':{'clientInfo':{'name':'codex-tui','version':'0.167.1'}}}))
            assert 'result' in json.loads(await client.recv())
            for identifier,(method,params) in enumerate(controls,1):
                await client.send(json.dumps({'id':identifier,'method':method,'params':params}))
                response = json.loads(await client.recv())
                assert response.get('id')==identifier and 'error' in response, response
                assert self.base.calls == 0 and state.owner is owner and owner.alive
                refused.append({'method':method,'params':params})
        assert owner.attestation.process_identity == identity
        # Existing reviewed fixture exercises a supported administrative native
        # text turn. No public input capability or main grant is fabricated.
        await self.base.exercise(c,state,launch,manager,config)
        assert state.owner is owner and owner.attestation.process_identity == identity
        assert state.binding.native_conversation == native and owner.alive
        path = Path('/evidence/result.json')
        proof = json.loads(path.read_text())
        proof.update(actualControlMatrix=refused,controlsRefusedBeforeModel=True,
            retainedExecutionIdentity=identity,sameOwnerTextContinuation=True,
            publicWriterQualified=False)
        path.write_text(json.dumps(proof))


async def prepare(root, work, c):
    return Case(await prepare_security(root,work,c))
