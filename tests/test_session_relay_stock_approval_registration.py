"""The read-only lifecycle watch must tolerate the pre-byte registration phase."""
# ruff: noqa: F811
import asyncio
import threading

from test_session_relay_public_writer import public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.server.db import write_in_thread
from acc.session_relay import protocol as p


async def test_watch_does_not_retire_a_captured_frame_before_registration_finishes(approval_case,monkeypatch):
    c=approval_case;control=c.launch.terminal_grants;record=control.record
    entered,release=threading.Event(),threading.Event();register=c.approvals.register
    def paused():
        entered.set()
        assert release.wait(5)
        return register()
    monkeypatch.setattr(c.approvals,'register',paused)
    task=asyncio.create_task(write_in_thread(lambda:control.frame(record.resolve,record.value.grant,2,1,b'\r')))
    try:
        async with asyncio.timeout(3):
            while not entered.is_set():await asyncio.sleep(.01)
        assert c.approvals.submission.operation_id is None
        assert await write_in_thread(lambda:c.approvals.still_valid(c.approvals.offer))
    finally:
        release.set()
        await asyncio.wait_for(task,5)
    assert c.approvals.submission.operation_id is not None


async def test_withdrawal_during_registration_never_leaves_a_live_queued_key(approval_case,monkeypatch):
    import pytest
    from sqlalchemy import select

    from acc.server.models import RelayOperation
    c=approval_case;control=c.launch.terminal_grants;record=control.record
    entered,release=threading.Event(),threading.Event();register=c.approvals.register
    def paused():
        entered.set();assert release.wait(5);return register()
    monkeypatch.setattr(c.approvals,'register',paused)
    task=asyncio.create_task(write_in_thread(lambda:control.frame(record.resolve,record.value.grant,2,1,b'\r')))
    async with asyncio.timeout(3):
        while not entered.is_set():await asyncio.sleep(.01)
    try:await c.approvals.resolved(c.offer['decisionId'],None)
    finally:release.set()
    with pytest.raises(p.RelayError):await asyncio.wait_for(task,5)
    with c.db() as db:
        assert not [op for op in db.scalars(select(RelayOperation)) if op.registration_key.startswith('stock-frame-')]
    assert not c.replies and not c.host.writes


async def test_withdrawal_after_registration_commit_fails_only_that_queued_operation(approval_case,monkeypatch):
    import pytest

    from acc.server.models import RelayOperation
    from acc.server.session_relay.hub import Hub
    c=approval_case;control=c.launch.terminal_grants;record=control.record
    entered,release=threading.Event(),threading.Event();register=Hub.register;created=[]
    def paused(self,*args,**kwargs):
        value=register(self,*args,**kwargs);created.append(value.operation_id)
        entered.set();assert release.wait(5);return value
    monkeypatch.setattr(Hub,'register',paused)
    task=asyncio.create_task(write_in_thread(lambda:control.frame(record.resolve,record.value.grant,2,1,b'\r')))
    async with asyncio.timeout(3):
        while not entered.is_set():await asyncio.sleep(.01)
    try:await c.approvals.resolved(c.offer['decisionId'],None)
    finally:release.set()
    with pytest.raises(p.RelayError):await asyncio.wait_for(task,5)
    with c.db() as db:assert db.get(RelayOperation,created[0]).receipt['state']=='failed'
    assert not c.replies and not c.host.writes
