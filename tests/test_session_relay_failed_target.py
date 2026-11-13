"""Failed target attempts cannot revive old owners or hide a possible fork."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from acc.broker import claude_owner, ops
from acc.session_relay import protocol as p


def factory():
    owner=object.__new__(claude_owner.ClaudeOwnerFactory)
    owner.closed=owner.close_complete=False
    owner.current=owner.observer=owner.input_attachment=owner.hooks=None
    owner.processes=[];owner.enclosures={};owner.files={};owner.env={};owner.guards={}
    owner.config=SimpleNamespace();owner.enclosure=SimpleNamespace(cli='/tmp/example-owner',proof=lambda *_:True)
    async def noop(*args,**kwargs):pass
    async def authorized(*args,**kwargs):return True
    owner.prepare=noop;owner.authorize=authorized;owner.lease=SimpleNamespace(stop=noop)
    return owner


def spec():
    return SimpleNamespace(binding=SimpleNamespace(workspace='/tmp/example-work'),spec={
        'session_id':'abc123def456','tmux_name':'acc-example','run_dir':'/tmp/example-run','terminal_resume_args':[]})


def test_launch_gate_is_internal_and_refuses_before_actual_tmux_fork(env,monkeypatch):
    commands=[];checks=[]
    original=ops.tmux
    def record(config,*args,**kwargs):
        if args[0]=='new-session':commands.append(args)
        return original(config,*args,**kwargs)
    def refuse():
        checks.append('fresh owner check')
        raise ops.OpError('owner revoked')
    monkeypatch.setattr(ops,'tmux',record)
    params={'acc_id':'abcdef123456','tmux_name':'acc-t-abcdef12','provider':'claude',
        'cwd':str(env['workspaces']/'demo'),'args':[],'files':{},'env':{}}
    with pytest.raises(ops.OpError,match='requires a supervised owner'):
        ops.launch(env['broker'],params,before_owner_start=refuse)
    with pytest.raises(ops.OpError,match='owner revoked'):
        ops.launch(env['broker'],params,inherit_environment=False,
            owner_executable='/usr/bin/false',before_owner_start=refuse)
    assert checks==['fresh owner check'] and commands==[]


@pytest.mark.parametrize('entered',[False,True])
async def test_pre_fork_refusal_closes_but_unknown_fork_remains_fenced(monkeypatch,entered):
    owner=factory()
    def launch(*args,before_owner_start,**kwargs):
        if entered:before_owner_start()
        raise ops.OpError('artificial launch failure')
    monkeypatch.setattr(ops,'launch',launch)
    with pytest.raises(ops.OpError):await owner.terminal(spec())
    assert owner.current is None
    if entered:
        assert not await owner.exited()
        with pytest.raises(p.RelayError) as error:await owner.close()
        assert error.value.code is p.ErrorCode.INTEGRITY_FAILURE
        assert not owner.close_complete and owner.launch_attempt is not None
    else:
        await owner.close()
        assert owner.close_complete and await owner.exited()


async def test_cancelled_worker_is_joined_and_cannot_fork_later(monkeypatch):
    owner=factory();entered=threading.Event();release=threading.Event();forks=[]
    def launch(*args,before_owner_start,**kwargs):
        entered.set()
        assert release.wait(5)
        before_owner_start()
        forks.append('forbidden')
    monkeypatch.setattr(ops,'launch',launch)
    call=asyncio.create_task(owner.terminal(spec()))
    async with asyncio.timeout(5):
        while not entered.is_set():await asyncio.sleep(.01)
    call.cancel()
    with pytest.raises(asyncio.CancelledError):await call
    assert not await owner.exited()
    release.set()
    await owner.close()
    assert not forks and owner.close_complete and await owner.exited()


async def test_final_authority_loss_refuses_before_fork(monkeypatch):
    owner=factory();checks=0;forks=[]
    async def authorized(*args,**kwargs):
        nonlocal checks
        checks+=1
        return checks==1
    owner.authorize=authorized
    def launch(*args,before_owner_start,**kwargs):
        before_owner_start()
        forks.append('forbidden')
    monkeypatch.setattr(ops,'launch',launch)
    with pytest.raises(ops.OpError):await owner.terminal(spec())
    await owner.close()
    assert checks==2 and not forks and owner.close_complete


@pytest.mark.parametrize('empty',[False,True])
async def test_known_partial_target_requires_its_own_empty_tree_receipt(monkeypatch,empty):
    owner=factory();events=[]
    def launch(*args,before_owner_start,**kwargs):before_owner_start()
    class Process:
        pid=123;started='456';snapshot='retained failed target'
        def __init__(self,*args):pass
        def current(self):return True
        def terminate(self):events.append('stop')
        async def wait(self):events.append('wait')
        async def tree_exited(self,*args):return empty
        async def retire_view(self):events.append('retire')
        def close(self):events.append('close')
    def refuse(*args,**kwargs):raise ops.OpError('observer failed after launch')
    monkeypatch.setattr(ops,'launch',launch)
    monkeypatch.setattr(claude_owner,'TmuxProcess',Process)
    monkeypatch.setattr(claude_owner,'OwnedObserver',refuse)
    candidate=spec();candidate.run_id='target'
    with pytest.raises(ops.OpError):await owner.terminal(candidate)
    assert owner.current is None and not await owner.exited()
    if empty:
        await owner.close()
        assert events==['stop','wait','retire','close'] and owner.close_complete
    else:
        with pytest.raises(p.RelayError):await owner.close()
        assert events==['stop','wait'] and not owner.close_complete
