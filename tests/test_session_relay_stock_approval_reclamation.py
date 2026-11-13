"""Irreversible local ticket retirement and observer lifetime boundaries."""
# ruff: noqa: F811
import asyncio

import pytest
from test_session_relay_native_input_host import host_case  # noqa: F401
from test_session_relay_public_writer import public_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401
from test_session_relay_stock_approval_frames import approval_case  # noqa: F401
from test_session_relay_stock_approval_input import stock_host  # noqa: F401
from test_session_relay_terminal_grants import stream_case  # noqa: F401

from acc.session_relay import protocol as p


async def test_invalidated_ticket_never_reopens_input_after_late_application(stock_host):
    c=stock_host
    await c.host.write_frames(c.grant,2,b'\r')
    await c.host.native_approval_response(c.offer,'accept')
    c.host.approval_input.ticket.invalidated=True
    c.applied=True  # accepted original application is not a new input grant
    with pytest.raises(p.RelayError):
        await c.host.approval_input.completed(c.grant.grant,2,'original-operation')
    assert c.host.input_fenced and c.host.processed==1


async def test_empty_offer_cannot_start_a_retained_observer(approval_case):
    c=approval_case
    c.approvals.reset();c.approvals.watch();task=c.approvals.watch_task
    try:assert task is None
    finally:
        if task is not None:
            task.cancel();await asyncio.gather(task,return_exceptions=True)


async def test_reset_accepts_a_completed_observer_from_a_closed_loop(approval_case):
    def completed():
        loop=asyncio.new_event_loop()
        try:
            task=loop.create_task(asyncio.sleep(0))
            loop.run_until_complete(task)
            return task
        finally:loop.close()
    c=approval_case
    c.approvals.watch_task=await asyncio.to_thread(completed)
    c.approvals.reset()
    assert c.approvals.watch_task is None and c.approvals.offer is None
