"""Cancellation refuses claimed work before waiting behind its provider dispatch."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from relay_helpers import DIGEST, setup

from acc.server.models import RelayOperation
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.runtime import RuntimeManager
from acc.session_relay import protocol as p


@pytest.fixture
def cancel_case(tmp_path):
    c=setup(tmp_path);hub=Hub(c.ctx)
    registered=hub.register(c.resolve,'relay','1','cancel-under-dispatch','turn.submit',DIGEST)
    queued=hub.queue(c.resolve,'relay',registered.operation_id,expected_run='1',digest=DIGEST,if_busy='queue')
    c.hub=hub;c.identifier=registered.operation_id;c.queued=queued
    c.manager=RuntimeManager.__new__(RuntimeManager);c.manager.ctx=c.ctx
    c.run=SimpleNamespace(submit_lock=asyncio.Lock(),grants={})
    c.manager.runs={'relay':c.run};c.discarded=[]
    async def discard(_run,token):c.discarded.append(token)
    c.manager.discard=discard
    yield c
    c.engine.dispose()


async def test_claimed_cancel_refuses_before_blocked_dispatch_releases_submit_lock(cancel_case):
    c=cancel_case;before=p.to_wire(c.hub.claim(c.resolve,'relay',c.identifier))
    c.run.grants['original']=SimpleNamespace(operation=SimpleNamespace(operation_id=c.identifier))
    await c.run.submit_lock.acquire()
    try:
        with pytest.raises(p.RelayError) as error:
            await asyncio.wait_for(c.manager.cancel(c.resolve,'relay',{
                'operationId':c.identifier,'expectedRevision':before['revision']}),1)
        assert error.value.code is p.ErrorCode.REVISION_CONFLICT
        assert c.run.submit_lock.locked() and not c.discarded and 'original' in c.run.grants
        with c.db() as db:assert db.get(RelayOperation,c.identifier).receipt==before
    finally:c.run.submit_lock.release()


@pytest.mark.parametrize('revoked',[False,True])
async def test_queued_cancel_waits_and_reauthenticates_before_resource_effects(cancel_case,revoked):
    c=cancel_case;before=p.to_wire(c.queued)
    c.run.grants['original']=SimpleNamespace(operation=SimpleNamespace(operation_id=c.identifier))
    await c.run.submit_lock.acquire()
    pending=asyncio.create_task(c.manager.cancel(c.resolve,'relay',{
        'operationId':c.identifier,'expectedRevision':before['revision']}))
    try:
        await asyncio.sleep(.05)
        assert not pending.done() and not c.discarded
        with c.db() as db:assert db.get(RelayOperation,c.identifier).receipt==before
        c.state['revoked']=revoked;c.run.submit_lock.release()
        if revoked:
            with pytest.raises(HTTPException) as error:await asyncio.wait_for(pending,2)
            assert error.value.status_code==401 and not c.discarded
            with c.db() as db:assert db.get(RelayOperation,c.identifier).receipt==before
        else:
            receipt=await asyncio.wait_for(pending,2)
            assert receipt.state is p.ReceiptState.FAILED and receipt.reason is p.FailureReason.CANCELLED
            assert c.discarded==['original']
    finally:
        if c.run.submit_lock.locked():c.run.submit_lock.release()
        pending.cancel();await asyncio.gather(pending,return_exceptions=True)
