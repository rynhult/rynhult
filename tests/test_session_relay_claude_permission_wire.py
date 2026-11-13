"""Exact native identity survives concurrent callbacks; writes are never ACKs."""
import asyncio
import json

import pytest

from acc.session_relay.claude_permission_wire import PermissionWire


class Base:
    _process=object()
    def __init__(self,messages):self.messages=messages;self.writes=[];self.ready=True;self.fail=False
    async def connect(self):pass
    async def close(self):self.ready=False
    async def end_input(self):pass
    def is_ready(self):return self.ready
    async def write(self,data):
        self.writes.append(data)
        if self.fail:raise ConnectionError('fixture lost write')
    async def read_messages(self):
        for message in self.messages:yield message


def question(request_id,tool_id):
    return {'type':'control_request','request_id':request_id,'request':{
        'subtype':'can_use_tool','tool_name':'Bash','tool_use_id':tool_id,'input':{'command':'identical fixture'}}}


async def claims(wire):
    release=asyncio.Event();tasks=[]
    async def delayed():
        await release.wait()
        return wire.claim('Bash',{'command':'identical fixture'})
    async for _ in wire.read_messages():tasks.append(asyncio.create_task(delayed()))
    release.set()
    return await asyncio.gather(*tasks)


async def test_identical_concurrent_callbacks_bind_exact_ids_without_input_matching():
    messages=[question('request-a','tool-a'),question('request-b','tool-b')]
    base=Base(messages);wire=PermissionWire(base)
    values=await claims(wire)
    assert [(v.request_id,v.tool_use_id) for v in values]==[('request-a','tool-a'),('request-b','tool-b')]
    assert values[0].input_digest==values[1].input_digest
    assert all(wire.current(v) for v in values)
    assert wire._process is base._process and wire.is_ready()
    await wire.close()
    assert not any(wire.current(v) for v in values)


@pytest.mark.parametrize('messages',[[question('same','a'),question('same','b')],
    [question('a','same'),question('b','same')],
    [question('a','tool-a'),{'type':'control_cancel_request','request_id':'a'}]])
async def test_duplicate_or_cancelled_native_identity_cannot_be_treated_as_correlated(messages):
    wire=PermissionWire(Base(messages))
    assert all(value is None for value in await claims(wire))


async def test_missing_identity_and_capacity_never_evict_or_guess_a_binding():
    wire=PermissionWire(Base([question('a','tool-a'),question('b','tool-b'),question('c',None)]));wire.capacity=1
    values=await claims(wire)
    assert values[0].request_id=='a' and values[1:]==[None,None] and list(wire.requests)==['a']


async def test_writes_are_byte_exact_single_attempt_observations_not_provider_acknowledgements():
    base=Base([question('a','tool-a')]);wire=PermissionWire(base)
    identity,=await claims(wire)
    data=json.dumps({'type':'control_response','response':{'subtype':'success','request_id':'a','response':{'behavior':'deny'}}})+'\n'
    base.fail=True
    with pytest.raises(ConnectionError):await wire.write(data)
    assert base.writes==[data] and wire.responses['a']['writeCompleted'] is False
    base.fail=False
    await wire.write(data)  # a separate SDK write is observable ambiguity, never silently retried by wrapper
    assert base.writes==[data,data] and not wire.current(identity)
    assert 'outcome' not in wire.responses['a']


async def test_absent_denial_report_is_distinct_from_explicit_empty_native_report():
    wire=PermissionWire(Base([{'type':'result','session_id':'native','subtype':'success','is_error':False},
        {'type':'result','session_id':'native','subtype':'success','is_error':False,'permission_denials':[]}]))
    async for _ in wire.read_messages():pass
    assert wire.results[0]['permissionDenials'] is None and wire.results[1]['permissionDenials']==()
