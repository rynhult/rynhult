"""Pinned SDK custom transport: exact permission identity, never application proof.

The SDK reader spawns a task per native control request. A ContextVar carried by
that task preserves its exact native IDs even for identical concurrent inputs.
No prompt matching, byte rewriting, response replay or second owner is involved.
"""
import json
from contextvars import ContextVar
from dataclasses import dataclass, field

from .protocol import canonical_json, sha256_digest

_current = ContextVar('claude_native_permission', default=None)


def identifier(value):
    return type(value) is str and 0 < len(value.encode()) <= 256 and value.isascii()


@dataclass(frozen=True)
class PermissionIdentity:
    request_id: str
    tool_use_id: str
    tool: str
    input_digest: str
    owner: object = field(repr=False,compare=False)


class PermissionWire:
    """Transparent single-owner SDK transport with bounded observation ledgers."""
    capacity=256

    def __init__(self,base):
        self.base=base
        self.requests={}
        self.tools={}
        self.closed=False
        self.used=set()
        self.ambiguous=set()
        self.responses={}
        self.results=[]
        self.interrupts={}

    @property
    def _process(self):return self.base._process

    async def connect(self):await self.base.connect()
    async def close(self):
        self.closed=True
        await self.base.close()
    async def end_input(self):await self.base.end_input()
    def is_ready(self):return self.base.is_ready()

    def current(self,value):
        return (not self.closed and isinstance(value,PermissionIdentity) and value.owner is self
            and self.requests.get(value.request_id) is value and value.request_id not in self.ambiguous)

    def claim(self,tool,inputs):
        value=_current.get()
        if (not self.current(value) or value.request_id in self.used or value.tool!=tool
                or value.input_digest!=sha256_digest(canonical_json(inputs))):
            return None
        self.used.add(value.request_id)
        return value

    async def write(self,data):
        value=json.loads(data)
        if (value.get('type') == 'control_request'
                and value.get('request', {}).get('subtype') == 'interrupt'):
            from .claude_sdk_interrupt import _scope, write
            from .protocol import ErrorCode, RelayError
            scope = _scope.get()
            key = value.get('request_id')
            if (scope is None or scope['wire'] is not self or scope['record'] is not None
                    or not identifier(key) or key in self.interrupts or len(self.interrupts) >= self.capacity
                    or value.keys() != {'type', 'request_id', 'request'} or value['request'] != {'subtype': 'interrupt'}):
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            record = {'requestId': key, 'writeCompleted': False, 'acknowledged': False, 'ambiguous': False}
            self.interrupts[key] = scope['record'] = record
            await write(self, data)
            record['writeCompleted'] = True
            return
        response=value.get('response',{}) if isinstance(value,dict) else {}
        request_id=response.get('request_id')
        observed=None
        if value.get('type')=='control_response' and request_id in self.responses:
            self.ambiguous.add(request_id)  # includes partial-write/error-response ambiguity
        if (value.get('type')=='control_response' and request_id in self.requests
                and request_id not in self.ambiguous and len(self.responses)<self.capacity):
            body=response.get('response',{})
            observed={'requestId':request_id,'behavior':body.get('behavior'),'writeCompleted':False}
            self.responses[request_id]=observed
        await self.base.write(data)  # exact bytes, exactly once; never resend
        if observed is not None:observed['writeCompleted']=True  # not an ACK

    async def read_messages(self):
        async for message in self.base.read_messages():
            response = message.get('response', {})
            if message.get('type') == 'control_response' and response.get('request_id') in self.interrupts:
                record = self.interrupts[response['request_id']]
                if record['acknowledged'] or response.get('subtype') != 'success' or not isinstance(response.get('response'), dict):
                    record['ambiguous'] = True
                record['acknowledged'] = True
            identity=None
            body=message.get('request',{})
            if message.get('type')=='control_request' and body.get('subtype')=='can_use_tool':
                request_id,tool_id=message.get('request_id'),body.get('tool_use_id')
                if identifier(request_id) and request_id in self.requests:
                    self.ambiguous.add(request_id)
                elif (identifier(request_id) and identifier(tool_id) and isinstance(body.get('tool_name'),str)
                        and isinstance(body.get('input'),dict) and len(self.requests)<self.capacity):
                    identity=PermissionIdentity(request_id,tool_id,body['tool_name'],
                        sha256_digest(canonical_json(body['input'])),self)
                    self.requests[request_id]=identity
                    previous=self.tools.get(tool_id)
                    if previous is not None:
                        self.ambiguous.update((previous,request_id))
                    else:self.tools[tool_id]=request_id
            if message.get('type')=='control_cancel_request' and message.get('request_id') in self.requests:
                self.ambiguous.add(message['request_id'])
            if message.get('type')=='result' and len(self.results)<self.capacity:
                denials=message.get('permission_denials')
                self.results.append({'sessionId':message.get('session_id'),'uuid':message.get('uuid'),
                    'subtype':message.get('subtype'),'isError':message.get('is_error'),
                    'permissionDenials':tuple((v.get('tool_name'),v.get('tool_use_id')) for v in denials
                        if isinstance(v,dict)) if isinstance(denials,list) and len(denials)<=self.capacity else None})
            _current.set(identity)
            try:yield message  # SDK start_soon copies this exact request context
            finally:_current.set(None)
