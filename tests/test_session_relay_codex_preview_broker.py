"""Production declaration and dispatch over the real broker with a fake CLI."""
# ruff: noqa: F811
from dataclasses import FrozenInstanceError

import pytest
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelaySession
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p


async def test_production_codex_adapter_declares_only_text_and_terminal(runtime_case):
    c=runtime_case
    await c.launch.launch('abc123def456')
    record=next(iter(c.factory.active.values())).adapter.declaration()
    assert {key for key in p.CAPABILITIES if record.support(key) is p.Support.QUALIFIED}=={'text','terminal.input'}
    assert record.provider=='codex' and record.cli_version=='0.167.1' and record.qualification_id
    with pytest.raises((FrozenInstanceError,AttributeError)):record.qualification_id='forged'
    with pytest.raises(TypeError):record.operations['decision.tool_approval']=record.operations['text']


async def test_production_codex_adapter_accepts_text_after_main_admission(runtime_case):
    c=runtime_case;sid='abc123def456'
    await c.launch.launch(sid)
    @write_intent
    def admitted_capabilities():
        with c.db() as db:
            row=db.get(RelaySession,sid);row.capabilities={'text':'qualified'}
            db.commit();return row.control_revision
    revision=admitted_capabilities()  # isolates broker support from server grant item
    params={'sessionId':sid,'expectedRunId':str(c.launch.run_id),'text':'Fixture-only preview text',
        'expectedConfigurationRevision':0,'expectedControlRevision':revision,'ifBusy':'reject'}
    operation=Hub(c.ctx).register(c.resolve,sid,str(c.launch.run_id),'preview-text','turn.submit',p.operation_digest('turn.submit',params))
    receipt=await c.manager.submit(c.resolve,sid,{**params,'operationId':operation.operation_id})
    assert receipt.state is p.ReceiptState.ACCEPTED
    assert len([call for call in c.owners[0].calls if call[0]=='turn/start'])==1
