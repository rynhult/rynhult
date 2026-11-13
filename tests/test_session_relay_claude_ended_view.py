"""Ended-view retirement cannot remove live/replaced/unproved tmux views."""
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acc.broker.claude_owner import TmuxProcess
from acc.broker.relay_enclosure import incarnation
from acc.session_relay import protocol as p


@pytest.mark.parametrize('failure', ['tree', 'server', 'changed', 'transport', 'reaping', 'replaced-after-reap-wait', None])
async def test_only_exact_dead_view_with_kernel_exit_can_be_retired(monkeypatch, failure):
    process=object.__new__(TmuxProcess)
    process.view_retired=False
    process.enclosure=object()
    process.tree_exited=AsyncMock(return_value=failure!='tree')
    process.server_pid=os.getpid()
    process.server_started='stale' if failure=='server' else incarnation(os.getpid())
    process.session_id,process.pane_id,process.pid,process.name='$17','%23',12345,'acc-owned-fixture'
    process.config=SimpleNamespace()
    calls=[]
    def tmux(config,*args):
        calls.append(args)
        if args[0] == 'display-message':
            reads = sum(call[0] == 'display-message' for call in calls)
            pending = failure in ('reaping', 'replaced-after-reap-wait') and reads == 1
            return SimpleNamespace(returncode=0, stdout=(
                f'$17 %23 12345 0 {os.getpid()} acc-owned-fixture' if pending else 'changed-pane'))
        assert args[:4]==('if-shell','-F','-t','$17')
        # The command-server guard must test all pinned identities and deadness.
        for field,value in [('session_id','$17'),('pane_id','%23'),('pane_pid','12345'),
                ('pane_dead','1'),('pid',str(os.getpid())),('session_name','acc-owned-fixture')]:
            assert '#{==:#{'+field+'},'+value+'}' in args[4]
        assert args[5]=="kill-pane -t '%23'"
        attempts = sum(call[0] == 'if-shell' for call in calls)
        refused = failure in ('changed', 'replaced-after-reap-wait') or failure == 'reaping' and attempts == 1
        return SimpleNamespace(returncode=1 if failure=='transport' else 0,
            stdout='refused-ended-view' if refused else '')
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux',tmux)
    if failure and failure != 'reaping':
        with pytest.raises(p.RelayError):await process.retire_view()
        assert not process.view_retired
        assert bool(calls)==(failure not in ('tree','server'))
    else:
        await process.retire_view()
        await process.retire_view()
        assert process.view_retired
        assert sum(call[0] == 'if-shell' for call in calls) == (2 if failure == 'reaping' else 1)


@pytest.mark.parametrize('reuse_live_pane',[False,True])
async def test_real_private_tmux_retirement_preserves_neighbour_and_reused_live_pane(tmp_path,monkeypatch,reuse_live_pane):
    import asyncio
    import subprocess
    import sys

    from acc.broker.relay_enclosure import Enclosure

    # An owned disposable server, no host tmux configuration or account env.
    def tmux(config,*args,check=False):
        result=subprocess.run(['/usr/bin/tmux','-f','/dev/null','-S',str(tmp_path/'tmux.sock'),*args],
            capture_output=True,text=True,env={'PATH':'/usr/bin:/bin','HOME':str(tmp_path)})
        if check and result.returncode:raise RuntimeError(result.stderr)
        return result
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux',tmux)
    config=SimpleNamespace()
    script=tmp_path/'owned-fake-cli'
    script.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(.8)\n')
    script.chmod(0o500)
    enclosure=Enclosure(tmp_path/'private',SimpleNamespace(run_id='owned'),script)
    process=None
    try:
        tmux(config,'start-server',';','set-option','-g','exit-empty','off',';',
            'set-option','-g','remain-on-exit','on',check=True)
        tmux(config,'new-session','-d','-s','acc-owned-fixture',str(enclosure.cli),check=True)
        process=TmuxProcess(config,'acc-owned-fixture',enclosure)
        neighbour=tmux(config,'split-window','-d','-P','-F','#{pane_id}','-t',process.session_id,
            '/usr/bin/sleep','30',check=True).stdout.strip()
        await asyncio.wait_for(process.wait(),5)
        async with asyncio.timeout(3):
            while tmux(config,'display-message','-p','-t',process.pane_id,'#{pane_dead}').stdout.strip()!='1':
                await asyncio.sleep(.01)
        assert await process.tree_exited(process)
        if reuse_live_pane:
            tmux(config,'respawn-pane','-t',process.pane_id,'/usr/bin/sleep','30',check=True)
            with pytest.raises(p.RelayError):await process.retire_view()
            assert tmux(config,'display-message','-p','-t',process.pane_id,'#{pane_dead}').stdout.strip()=='0'
        else:
            await process.retire_view()
            panes=tmux(config,'list-panes','-t',process.session_id,'-F','#{pane_id}',check=True).stdout.split()
            assert process.pane_id not in panes and neighbour in panes
        assert tmux(config,'display-message','-p','-t',neighbour,'#{pane_dead}').stdout.strip()=='0'
    finally:
        tmux(config,'kill-server')  # only this test's private tmp socket
        if process:process.close()
        enclosure.close()


async def test_concurrent_retirement_of_same_dead_view_runs_one_atomic_guard(monkeypatch):
    import asyncio
    process = object.__new__(TmuxProcess)
    process.view_retired = False
    process.enclosure = object()
    async def exited(_):
        await asyncio.sleep(0)  # both old callers can pass the initial retired check
        return True
    process.tree_exited = exited
    process.server_pid = os.getpid()
    process.server_started = incarnation(os.getpid())
    process.session_id, process.pane_id, process.pid, process.name = '$17', '%23', 12345, 'acc-owned-fixture'
    process.config = SimpleNamespace()
    calls = []
    def tmux(_config, *args):
        assert args[:4] == ('if-shell', '-F', '-t', '$17')
        assert args[5] == "kill-pane -t '%23'"
        calls.append(args)
        # The first exact atomic deletion succeeded; the second targets nothing.
        return SimpleNamespace(returncode=0 if len(calls) == 1 else 1, stdout='')
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux', tmux)
    await asyncio.gather(process.retire_view(), process.retire_view())
    assert process.view_retired and len(calls) == 1


def test_proved_tmux_process_handle_close_is_idempotent(monkeypatch):
    process = object.__new__(TmuxProcess)
    process.fd = os.open('/dev/null', os.O_RDONLY)
    process.children = []
    process.returncode = 0
    process.config, process.name = SimpleNamespace(), 'acc-owned-fixture'
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux',
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1, stdout=''))
    process.close()
    process.close()
    assert process.fd is None and process.children == []
