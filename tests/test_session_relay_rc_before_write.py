from types import SimpleNamespace

import pytest

from acc.broker.session_relay import CodexRuntimeOwner
from acc.session_relay import protocol as p


@pytest.mark.parametrize('result',[False,None,0])
async def test_owner_response_requires_explicit_true_before_any_native_write(result):
    writes=[]
    async def respond(identifier,value,*,before_write):
        await before_write();writes.append((identifier,value))
    owner=CodexRuntimeOwner.__new__(CodexRuntimeOwner)
    owner.runtime=SimpleNamespace(validate=lambda:None,closed=False,process=SimpleNamespace(returncode=None))
    owner.rpc=SimpleNamespace(respond=respond)
    async def denied():return result
    with pytest.raises(p.RelayError):await owner.respond(1,{},before_write=denied)
    assert not writes
