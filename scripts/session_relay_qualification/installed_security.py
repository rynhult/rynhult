"""Production installed guard exercise, inside the offline enrolled-node fixture."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from websockets.asyncio.client import unix_connect

from acc.server.compiler import LaunchInputs, compile_files
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


class Security:
    def __init__(self, root, work, options):
        self.root, self.work, self.options = root, work, options
        self.provider = options['provider']
        self.hook = Path('/opt/relay-install/native-hook')
        self.env = {}
        self.requests = []
        self.guard_seen = False
        self.tool_name = None
        self.target = str(Path('/home/probe/.codex/config.toml')) if options['owned_view'] else str(work / 'installed-security-forbidden')
        self.command = 'printf fixture > ' + self.target
        self.calls = 0

    async def model(self, reader, writer):
        try:
            header = await reader.readuntil(b'\r\n\r\n')
            length = next((int(line.split(b':',1)[1]) for line in header.split(b'\r\n')
                           if line.lower().startswith(b'content-length:')),0)
            assert 0 <= length <= 1 << 20
            raw = await reader.readexactly(length)
            payload = json.loads(raw)
            self.guard_seen |= any(marker in raw.decode() for marker in (
                'Installed guard denies this command.', 'The session hub could not verify this native action.',
                'were changed after launch', 'holds this session', "agents can’t change", "agents can't change"))
            self.requests.append({'path':header.split(b' ',2)[1].decode(), 'guard_seen':self.guard_seen})
            if 'count_tokens' in self.requests[-1]['path']:
                body = b'{"input_tokens":10}'
                content_type = 'application/json'
            else:
                self.calls += 1
                events = self.codex_events(payload) if self.provider == 'codex' else self.claude_events(payload)
                body = ''.join('event: '+name+'\ndata: '+json.dumps(value)+'\n\n' for name,value in events).encode()
                content_type = 'text/event-stream'
            writer.write(b'HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Type: '+content_type.encode()
                + b'\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    def codex_events(self, payload):
        output = {'id':'msg_security_'+str(self.calls), 'type':'message', 'status':'completed','role':'assistant',
                  'content':[{'type':'output_text','text':'Offline installed guard response.','annotations':[]}]}
        if self.calls == 1:
            tools = [tool.get('name') for tool in payload.get('tools',[]) if tool.get('type')=='function']
            self.tool_name = next(name for name in tools if name in ('exec_command','shell_command','shell'))
            args = {'cmd':self.command} if self.tool_name=='exec_command' else (
                {'command':self.command} if self.tool_name=='shell_command' else {'command':['/bin/sh','-c',self.command]})
            output = {'id':'fc_installed_security','type':'function_call','call_id':'installed_security_tool',
                      'name':self.tool_name,'arguments':json.dumps(args),'status':'completed'}
        response = {'id':'resp_security_'+str(self.calls),'object':'response','created_at':1,'status':'completed',
            'model':'fixture-model','output':[output],'usage':{'input_tokens':1,'output_tokens':1,'total_tokens':2}}
        if output['type']=='function_call':
            events = [
                ('response.created',{'response':{**response,'status':'in_progress','output':[]}}),
                ('response.output_item.added',{'output_index':0,'item':{**output,'arguments':'','status':'in_progress'}}),
                ('response.function_call_arguments.delta',{'output_index':0,'item_id':output['id'],'delta':output['arguments']}),
                ('response.function_call_arguments.done',{'output_index':0,'item_id':output['id'],'arguments':output['arguments']}),
                ('response.output_item.done',{'output_index':0,'item':output}),
                ('response.completed',{'response':response})]
        else:
            part = output['content'][0]
            events = [
                ('response.created',{'response':{**response,'status':'in_progress','output':[]}}),
                ('response.output_item.added',{'output_index':0,'item':{**output,'content':[],'status':'in_progress'}}),
                ('response.content_part.added',{'output_index':0,'content_index':0,'item_id':output['id'],'part':{**part,'text':''}}),
                ('response.output_text.delta',{'output_index':0,'content_index':0,'item_id':output['id'],'delta':part['text']}),
                ('response.output_text.done',{'output_index':0,'content_index':0,'item_id':output['id'],'text':part['text']}),
                ('response.content_part.done',{'output_index':0,'content_index':0,'item_id':output['id'],'part':part}),
                ('response.output_item.done',{'output_index':0,'item':output}),('response.completed',{'response':response})]
        return [(name,{'type':name,**value,'sequence_number':i}) for i,(name,value) in enumerate(events)]

    def claude_events(self, payload):
        message = {'id':'msg_installed_security_'+str(self.calls),'type':'message','role':'assistant',
            'model':payload.get('model','fixture'),'content':[],'stop_reason':None,'stop_sequence':None,
            'usage':{'input_tokens':10,'output_tokens':2}}
        block = {'type':'text','text':''}
        delta = {'type':'text_delta','text':'Offline installed guard response.'}
        stop = 'end_turn'
        if self.calls == 1:
            self.tool_name = 'Bash'
            block = {'type':'tool_use','id':'installed_security_tool','name':'Bash','input':{}}
            delta = {'type':'input_json_delta','partial_json':json.dumps({'command':self.command,'description':'Guard fixture write'})}
            stop = 'tool_use'
        return [('message_start',{'type':'message_start','message':message}),
            ('content_block_start',{'type':'content_block_start','index':0,'content_block':block}),
            ('content_block_delta',{'type':'content_block_delta','index':0,'delta':delta}),
            ('content_block_stop',{'type':'content_block_stop','index':0}),
            ('message_delta',{'type':'message_delta','delta':{'stop_reason':stop,'stop_sequence':None},'usage':{'output_tokens':2}}),
            ('message_stop',{'type':'message_stop'})]

    async def exercise(self, c, state, launch, manager, config):
        native = state.binding.native_conversation
        baseline_target = Path(self.target).read_bytes() if Path(self.target).exists() else None
        assert state.binding.provider == self.provider
        run = Path(state.spec.spec['run_dir'])
        assert (run / 'guardrails.json').read_text() == self.compiled.files['guardrails.json']
        state.guard_check()
        controls = []
        hooks = None
        if self.provider == 'codex':
            hooks = await state.owner.request('hooks/list',{'cwds':[str(self.work)]})
            group = hooks['data'][0]
            assert not group['warnings'] and not group['errors']
            assert len(group['hooks']) == 8
            assert all(hook['trustStatus']=='trusted' and hook['enabled'] for hook in group['hooks'])
            assert any('/opt/relay-install/acc-guard' in json.dumps(hook) for hook in group['hooks'])
            async with unix_connect(str(state.native_socket.path),compression=None) as client:
                await client.send(json.dumps({'id':0,'method':'initialize','params':{'clientInfo':{'name':'codex-tui','version':'0.167.1'}}}))
                assert 'result' in json.loads(await client.recv())
                for n,(method,params) in enumerate([
                    ('config/value/write',{'keyPath':'features.hooks','value':False}),
                    ('account/login/start',{'type':'apiKey','apiKey':'artificial-unapproved'}),
                    ('account/logout',{}),
                    ('thread/resume',{'threadId':native,'config':{'features.hooks':False}}),
                    ('turn/start',{'threadId':native,'model':'other','input':[{'type':'text','text':'not delivered'}]}),
                    ('turn/start',{'threadId':native,'effort':'high','input':[{'type':'text','text':'not delivered'}]})],1):
                    await client.send(json.dumps({'id':n,'method':method,'params':params}))
                    assert 'error' in json.loads(await client.recv())
                    controls.append(method)
            if self.options['owned_view']:
                view = state.owned_view
                assert view is not None and view.peer is not None
                pid = view.peer[0]
                mounts = Path(f'/proc/{pid}/mountinfo').read_text().splitlines()
                assert any(line.split()[4]=='/home/relay/.codex/config.toml' and 'ro' in line.split()[5].split(',') for line in mounts)
                assert not Path(f'/proc/{pid}/root/home/probe/.codex').exists()
        if self.options['tampered'] and self.provider == 'codex':
            policy = run / 'guardrails.json'
            policy.chmod(0o600)
            policy.write_text('{}')
            try:
                state.guard_check()
            except p.RelayError as error:
                assert error.code is p.ErrorCode.INTEGRITY_FAILURE
            else:
                raise AssertionError('Tampered production guard retained admission')
        if self.provider == 'codex':
            await state.owner.request('turn/start',{'threadId':native,'input':[{'type':'text','text':'Offline installed guard fixture','text_elements':[]}]})
            async with asyncio.timeout(20):
                while self.calls < 2 or (await state.owner.request('thread/read',{'threadId':native,'includeTurns':True}))['thread']['status']['type']!='idle':
                    await asyncio.sleep(.05)
        else:
            params = {'sessionId':state.spec.spec['session_id'],'expectedRunId':str(launch.run_id),
                'text':'Offline installed guard fixture','expectedConfigurationRevision':0,'expectedControlRevision':0,'ifBusy':'reject'}
            registered = Hub(c.ctx).register(c.resolve,params['sessionId'],params['expectedRunId'],'installed-guard','turn.submit',p.operation_digest('turn.submit',params))
            await manager.submit(c.resolve,params['sessionId'],{**params,'operationId':registered.operation_id})
            async with asyncio.timeout(20):
                while self.calls < 2 or state.adapter.active:
                    await asyncio.sleep(.05)
        records = [json.loads(line) for line in (run / 'events.jsonl').read_text().splitlines()]
        guards = [item for item in records if item.get('event')=='PreToolUse' and item.get('guard')]
        assert guards, records
        expected = 'tampered' if self.options['tampered'] and self.provider == 'codex' else 'own_files' if self.options['owned_view'] else 'custom'
        assert any(row['guard']['rule']==expected for row in guards), guards
        assert self.guard_seen and self.calls == 2
        assert (Path(self.target).read_bytes() if Path(self.target).exists() else None) == baseline_target
        if self.options['tampered'] and self.provider == 'claude':
            policy = run / 'guardrails.json'
            policy.chmod(0o600)
            policy.write_text('{}')
            try:
                state.guard_check()
            except p.RelayError as error:
                assert error.code is p.ErrorCode.INTEGRITY_FAILURE
            else:
                raise AssertionError('Tampered production guard retained admission')
            params['text'] = 'Must not reach native model after policy tamper'
            registered = Hub(c.ctx).register(c.resolve,params['sessionId'],params['expectedRunId'],
                'installed-guard-tamper','turn.submit',p.operation_digest('turn.submit',params))
            try:
                refused = await manager.submit(c.resolve,params['sessionId'],{**params,'operationId':registered.operation_id})
            except (p.RelayError, ConnectionError):
                pass
            else:
                assert refused.state is p.ReceiptState.FAILED, refused
                assert refused.reason is p.FailureReason.REJECTED
                # The production adapter conclusively refused before send.
                # Repeating the same operation observes that refusal.
                duplicate = await manager.submit(c.resolve,params['sessionId'],{**params,'operationId':registered.operation_id})
                assert duplicate == refused
            assert self.calls == 2
        proof = {'provider':self.provider,'nativeId':native,'productionInstalledGuard':True,'productionCompiledPolicy':True,
            'tampered':self.options['tampered'],'tamperedAdmissionRefused':self.options['tampered'],
            'expectedRule':expected,'guardRecords':guards,'nativeGuardDenied':True,
            'guardDenialConsumedByModel':self.guard_seen,'targetUnchanged':True,'modelCalls':self.calls,
            'refusedControls':controls,'ownedView':self.options['owned_view'],'terminalMediated':False,
            'productionNodeClient':True,'hooks':hooks,'declaredCapabilities':{}}
        Path('/evidence/result.json').write_text(json.dumps(proof))
        # Restore only for owned lifecycle cleanup after tamper proof. This never
        # retries the mutation or treats restoration as permission to send.
        if self.options['tampered']:
            (run / 'guardrails.json').write_text(self.compiled.files['guardrails.json'])
            (run / 'guardrails.json').chmod(0o400)

    async def close(self):
        self.server.close()
        await self.server.wait_closed()


async def prepare(root, work, c):
    options = json.loads(Path('/evidence/security.json').read_text())
    security = Security(root,work,options)
    security.server = await asyncio.start_server(security.model,'127.0.0.1',0)
    port = security.server.sockets[0].getsockname()[1]
    from acc.server import guardrails
    from acc.server.db import write_intent
    @write_intent
    def approved_policy():
        with c.db() as db:
            guardrails.save(db, {'custom':[{'pattern':'installed-security-forbidden',
                'reason':'Installed guard denies this command.'}]}, 'alex')
            frozen = guardrails.policy(db, str(work), owner='alex', machine='lab-node', account='relayagent')
            db.commit()
            return frozen
    frozen = approved_policy()
    security.inputs = LaunchInputs(provider=security.provider,model='fixture-model',
        permission_mode='acceptEdits')
    security.compiled = compile_files(security.inputs, str(security.hook), '/usr/bin/true',
        agent_home='/home/probe', guardrails=frozen)
    if security.provider == 'codex':
        home = Path('/home/probe/.codex')
        home.mkdir(mode=0o700)
        (home / 'config.toml').write_text('model_provider="fixture"\n[model_providers.fixture]\nname="Offline fixture"\n'
            +f'base_url="http://127.0.0.1:{port}/v1"\nwire_api="responses"\nrequires_openai_auth=false\n'
            +'request_max_retries=0\nstream_max_retries=0\n[features]\nhooks=true\n')
    else:
        security.env = {'ANTHROPIC_API_KEY':'relay-installed-security-fixture-only',
            'ANTHROPIC_BASE_URL':f'http://127.0.0.1:{port}',
            'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1'}
        prefs = {'hasCompletedOnboarding':True,'theme':'dark','projects':{str(work):{'hasTrustDialogAccepted':True}},
            'customApiKeyResponses':{'approved':['relay-installed-security-fixture-only','relay-installed-security-fixture-only'[-20:]],'rejected':[]}}
        for path in (Path('/home/probe/.claude.json'),):
            path.write_text(json.dumps(prefs))
        local = work / '.claude'
        local.mkdir()
        (local / 'settings.local.json').write_text('{"disableAllHooks":true}')
    return security
