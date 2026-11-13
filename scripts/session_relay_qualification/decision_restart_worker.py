"""Synthetic evidence only; g56's unchanged controller kills this real broker."""
async def worker():
    import asyncio
    import json
    import os
    from pathlib import Path

    from acc.broker.claude_allow_evidence import NativeAllow
    from acc.broker.claude_decision_journal import DecisionEvidenceJournal, checkpoint
    from acc.broker.claude_reconciliation import native_root
    from acc.broker.claude_tool_binding import Completion, prompt_digest
    from acc.broker.relay_runtime import RuntimeTransportFactory
    from acc.broker.server import Broker
    from acc.session_relay import protocol as p

    cfg=globals()['config']()  # g56's unchanged account config, injected by probe
    factory=RuntimeTransportFactory(cfg)
    broker=Broker(cfg,relay_transport=factory)
    cfg.socket_path.unlink(missing_ok=True)
    Path('/evidence/worker.sock').unlink(missing_ok=True)
    server=await asyncio.start_unix_server(broker.handle,str(cfg.socket_path))
    journal=None
    async def inspect(reader,writer):
        nonlocal journal
        try:
            method=json.loads(await reader.readline())['method']
            if method=='state':
                value={'pid':os.getpid(),'active':len(factory.active),'runs':[]}
            elif method=='qualify':
                data=json.loads(Path('/evidence/config.json').read_text())
                root=native_root();project=root/'fixture';project.mkdir(mode=0o700,parents=True,exist_ok=True)
                native=data['scope']['native_conversation'];path=project/(native+'.jsonl')
                common={'sessionId':native,'isSidechain':False}
                key=data['record']['event']['operationId'];tool='tool-'+key;prompt='query-'+key
                rows=[{**common,'type':'user','uuid':'prompt-'+key,'parentUuid':None,'promptId':prompt,
                    'turnPosition':{'promptIndex':0,'turnIndex':1},'message':{'role':'user','content':'fixture'}},
                    {**common,'type':'assistant','uuid':'source-'+key,'parentUuid':'prompt-'+key,'message':{'role':'assistant','content':[
                        {'type':'tool_use','id':tool,'name':'Bash','input':{'command':'fixture'}}]}}]
                with path.open('a') as stream:stream.write(''.join(json.dumps(row)+'\n' for row in rows))
                path.chmod(0o600)
                source=NativeAllow(root,path,native);source.begin()
                digest=prompt_digest({'session_id':native,'prompt_id':prompt,'tool_name':'Bash','tool_input':{'command':'fixture'}})
                binding=source.bind({'boundToolId':tool,'promptId':prompt,'toolName':'Bash','promptDigest':digest})
                record={**data['record'],'reader':checkpoint(source),'binding':binding};source.close()
                directory=Path(data['run_dir'])/data['session_id']/'relay'/'decision-evidence'
                if journal is None:journal=DecisionEvidenceJournal(directory)
                journal.prepare(data['scope'],record)
                if data['barrier']!='prepared':
                    journal.sent(data['scope'],tool,digest)
                if data['barrier'] in ('completion','lost_ack'):
                    journal.complete(data['scope'],Completion(native,tool,digest,'PostToolUse',p.sha256_digest(b'synthetic authenticated completion')))
                value={'pid':os.getpid(),'record':record,'pending':len(journal.pending(data['scope'],prepared=True))}
            else:raise ValueError(method)
        except Exception as error:value={'error':repr(error)}
        writer.write(json.dumps(value).encode()+b'\n');await writer.drain();writer.close();await writer.wait_closed()
    inspector=await asyncio.start_unix_server(inspect,'/evidence/worker.sock')
    try:await asyncio.Event().wait()
    finally:
        server.close();inspector.close()
        await server.wait_closed();await inspector.wait_closed()
        if journal:journal.close()
