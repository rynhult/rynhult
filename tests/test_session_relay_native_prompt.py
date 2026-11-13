"""A native overwrite screen grants no input/answer authority."""
import asyncio
from types import SimpleNamespace

import pytest
from test_session_relay_ownership import SID, ownership_case, select_owner  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.broker.claude_native_prompt import close, overwrite_modal, watch
from acc.server.models import AttentionItem, Session
from acc.server.session_relay import native_prompt
from acc.session_relay import protocol as p

MODAL='''Overwrite file
fixture.txt
Do you want to overwrite fixture.txt?
❯ 1. Yes
  2. Yes, and switch to accept edits
  3. No
Esc to cancel · Tab to amend
'''


@pytest.mark.parametrize('screen,expected',[(MODAL,True),('● '+MODAL,False),
    (MODAL+'New provider response\n',False),(MODAL.replace('3. No','3. Something'),False),
    (MODAL.replace('Overwrite file','Read file'),False),('ordinary native output',False)])
def test_native_modal_shape_is_a_handoff_hint_only(screen,expected):
    assert overwrite_modal(screen)==expected


async def test_readonly_watcher_suppresses_hook_cards_and_reclaims_its_task():
    publications=[];step=0
    owner=SimpleNamespace(host=SimpleNamespace(run='owned-run'))
    hooks=SimpleNamespace(pending={'question':object()})
    runtime=SimpleNamespace(spec='reviewed',native_prompt_active=False)
    async def publish(value):publications.append(value)
    runtime.publish=publish
    factory=SimpleNamespace(closed=False,current=owner,hooks=hooks,runtime=runtime)
    async def capture(run,*,history):
        nonlocal step
        assert run=='owned-run' and history is False
        step+=1
        if step==1:return {'text':MODAL}
        if step==2:hooks.pending.clear();return {'text':MODAL}
        if step==3:return {'text':MODAL}
        factory.closed=True
        return {'text':'Native response complete'}
    observer=SimpleNamespace(capture=capture)
    factory.native_prompt_task=asyncio.create_task(watch(factory,owner,observer,hooks,'reviewed'))
    await asyncio.wait_for(factory.native_prompt_task,5)
    assert publications==[{'kind':'native.prompt','active':True},{'kind':'native.prompt','active':False}]
    assert runtime.native_prompt_active is False
    await close(factory);assert factory.native_prompt_task is None


async def test_native_prompt_main_notice_and_remote_allow_refusal(ownership_case):  # noqa: F811
    c=ownership_case;await select_owner(c,'terminal','native-modal')
    run=c.launch
    active={'kind':'native.prompt','active':True,'runId':str(run.run_id)}
    assert await run.handle('publication',active) is True
    with c.db() as db:
        session=db.get(Session,SID);fact=native_prompt.current(db,SID)
        assert session.observed_state=='waiting_input'
        assert fact['message']=='Claude is asking in the terminal; answer it there'
        assert fact['remoteAllow'] is False and fact['terminalUrl']==f'/sessions/{SID}?tab=terminal'
        item=db.query(AttentionItem).filter_by(session_id=SID,kind='needs_input').one()
        assert item.detail==fact['message'] and item.source=='claude_terminal' and not item.verified
    with pytest.raises(p.RelayError) as error:await run.answer(c.resolve,{'choiceId':'accept'})
    assert error.value.code is p.ErrorCode.POLICY_REFUSED
    assert await run.handle('publication',active) is True
    with c.db() as db:assert db.query(AttentionItem).filter_by(session_id=SID,kind='needs_input').count()==1
    assert await run.handle('publication',{**active,'active':False}) is True
    with c.db() as db:
        assert native_prompt.current(db,SID) is None
        assert db.get(Session,SID).observed_state=='active'
        assert db.query(AttentionItem).filter_by(session_id=SID,kind='needs_input').one().resolved_at is not None


async def test_stale_prompt_scope_never_relabels_a_session(ownership_case):  # noqa: F811
    c=ownership_case
    for params in [{'kind':'native.prompt','active':True,'runId':str(c.launch.run_id)},
            {'kind':'native.prompt','active':True,'runId':'stale'}]:
        with pytest.raises(p.RelayError):await c.launch.handle('publication',params)
    with c.db() as db:assert db.query(AttentionItem).filter_by(session_id=SID).count()==0


async def test_retiring_screen_watcher_drains_shared_authority_peer(tmp_path):
    from acc.session_relay.runtime_peer import RuntimePeer
    entered=asyncio.Event();release=asyncio.Event();peers=[]
    async def authority(method,params):entered.set();await release.wait();return True
    async def accept(reader,writer):peers.append(RuntimePeer(reader,writer,authority))
    server=await asyncio.start_unix_server(accept,str(tmp_path/'a.sock'))
    reader,writer=await asyncio.open_unix_connection(str(tmp_path/'a.sock'))
    peer=RuntimePeer(reader,writer,authority)
    owner=SimpleNamespace(host=SimpleNamespace(run='run'));hooks=SimpleNamespace(pending={})
    runtime=SimpleNamespace(spec='spec')
    async def publish(_):raise AssertionError('Retired watcher published')
    runtime.publish=publish
    factory=SimpleNamespace(closed=False,current=owner,hooks=hooks,runtime=runtime,native_prompt_stop=asyncio.Event())
    async def capture(*_args,**_kwargs):
        assert await peer.call('authority.owner',{}) is True
        return {'text':MODAL}
    factory.native_prompt_task=asyncio.create_task(watch(factory,owner,SimpleNamespace(capture=capture),hooks,'spec'))
    try:
        await asyncio.wait_for(entered.wait(),3)
        closing=asyncio.create_task(close(factory));await asyncio.sleep(.02)
        assert not peer.closed and not closing.done()
        release.set();await asyncio.wait_for(closing,3)
        assert not peer.closed and await peer.call('authority.owner',{}) is True
    finally:
        release.set();await close(factory);await peer.close()
        for remote in peers:await remote.close()
        server.close();await server.wait_closed()
