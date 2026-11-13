"""Real cookie/read authorization for private retained data; fake CLI only."""
import importlib.util
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_ownership import SID, ownership_case  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import InputGrant, TerminalRun
from acc.session_relay.claude_retained_client import RetainedClaudeClient


@pytest.fixture
async def retained_public(ownership_case):  # noqa: F811
    c=ownership_case
    c.ctx.supervisor.vault.redactor=lambda *_:SimpleNamespace(text=lambda value:value.replace('artificial-secret','[redacted]'))
    actor=c.resolve()[1]
    grant=InputGrant(TerminalRun(SID,'old-native-run'),'old-attachment',actor,'must-not-expose-token',4,p.Fence(1,'b','h'))
    c.retained=RetainedClaudeClient.capture(grant,c.launch.binding.native_conversation,3,2,'Keep artificial-secret unsent')
    c.launch.terminal_grants.retained_clients[(c.retained.run_id,c.retained.attachment_id)]=c.retained
    spec=importlib.util.spec_from_file_location('retained_http',Path(__file__).resolve().parents[1]/
        'scripts/session_relay_qualification/public_native_http.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    c.public=await module.Public.create(c,c.manager,SID,features=[])
    try:yield c
    finally:await c.public.close()


async def test_public_retained_draft_is_private_redacted_readonly_data_not_an_input_grant(retained_public):
    c=retained_public
    value=(await c.public.call('session.snapshot',{}))['result']
    assert value['retainedInputs']==[{'sourceRunId':'old-native-run','view':'terminal',
        'text':'Keep [redacted] unsent','readOnly':True}]
    assert value['terminalMediated'] is False and not c.launch.terminal_grants.record
    assert 'must-not-expose-token' not in str(value) and 'draftDigest' not in str(value)
    assert c.retained.draft=='Keep artificial-secret unsent' and c.retained.processed==2


@pytest.mark.parametrize('change',['person','device','native','binding'])
async def test_foreign_or_stale_retained_models_do_not_escape_current_read_scope(retained_public,change):
    c=retained_public;client=c.retained
    if change=='person':client=replace(client,actor=replace(client.actor,principal='blair'))
    elif change=='device':client=replace(client,actor=replace(client.actor,channel=p.Channel.PHONE,device='other-device'))
    elif change=='native':client=replace(client,native_id='different-native')
    elif change=='binding':
        old=c.launch.binding_id;c.launch.binding_id='different-binding'
    c.launch.terminal_grants.retained_clients[(client.run_id,client.attachment_id)]=client
    try:
        value=(await c.public.call('session.snapshot',{}))['result']
        assert not value.get('retainedInputs') and 'Keep artificial-secret' not in str(value)
    finally:
        if change=='binding':c.launch.binding_id=old


async def test_revoked_real_cookie_cannot_read_a_cached_retained_draft(retained_public):
    c=retained_public;c.public.revoke()
    response=await c.public.client.get(f'/api/sessions/{SID}/relay')
    assert response.status_code in (401,403) and 'unsent' not in response.text
