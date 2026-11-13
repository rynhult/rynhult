"""Pinned owned SDK permission-wire inventory; identifiers only, no ACK claim."""
import json
from pathlib import Path

from enrolled_decisions import prepare as prepare_decisions


class Case:
    def __init__(self,base):self.base=base
    def __getattr__(self,name):return getattr(self.base,name)
    async def exercise(self,c,state,launch,manager,config):
        query=state.owners.current.client._query
        original=query._handle_control_request
        samples=[]
        results=[]
        original_loads=json.loads
        def inspect_json(*args,**kwargs):
            value=original_loads(*args,**kwargs)
            if isinstance(value,dict) and value.get("type")=="result" and value.get("session_id")==state.binding.native_conversation:
                denials=value.get("permission_denials")
                results.append({"keys":sorted(value),"subtype":value.get("subtype"),"isError":value.get("is_error"),
                    "permissionDenials":([{k:v for k,v in item.items() if k in ("tool_use_id","tool_name")} for item in denials] if isinstance(denials,list) else None)})
            return value
        async def inventory(request):
            body=request.get('request',{})
            if body.get('subtype')=='can_use_tool':
                samples.append({'envelopeKeys':sorted(request),'requestKeys':sorted(body),
                    'requestId':request.get('request_id'),'nativeToolId':body.get('tool_use_id'),
                    'toolName':body.get('tool_name')})
            return await original(request)
        query._handle_control_request=inventory
        json.loads=inspect_json
        # Explicit evidence-loss inventory: wire identifiers/write completion
        # remain real, but the separate primary application ledger receives no
        # provider result. A response write alone must still remain unknown.
        applications=state.owners.current.applications
        original_observe=applications.observe
        applications.observe=lambda *_:None
        try:await self.base.exercise(c,state,launch,manager,config)
        finally:
            json.loads=original_loads
            applications.observe=original_observe
        assert samples
        path=Path('/evidence/result.json');proof=json.loads(path.read_text())
        proof['permissionWireInventory']=samples
        proof['wireInventoryOnly']=True
        proof['applicationEvidenceWithheld']=True
        proof['nativeResultInventory']=results
        wire=state.owners.current.client._transport
        bindings=state.owners.current.native_decisions
        raw={(item['requestId'],item['nativeToolId']) for item in samples}
        assert len(bindings)==len(samples) and all((v.request_id,v.tool_use_id) in raw for v in bindings.values())
        assert all(wire.responses[v.request_id]['writeCompleted'] for v in bindings.values())
        proof['exactSDKCallbackBindings']=[{'decisionId':key,'requestId':v.request_id,'nativeToolId':v.tool_use_id,
            'currentAfterResult':wire.current(v),'responseObservation':wire.responses[v.request_id]} for key,v in bindings.items()]
        proof['exactSDKRequestBinding']=True
        proof['responseWriteIsNotAcknowledgement']=True
        path.write_text(json.dumps(proof))


async def prepare(root,work,c):
    (root/'home').mkdir(mode=0o700)
    return Case(await prepare_decisions(root,work,c))
