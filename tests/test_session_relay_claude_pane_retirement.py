"""Retire only an exited owner's exact dead tmux pane, never its replacement."""
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from acc.broker.claude_owner import TmuxProcess


@pytest.mark.parametrize('pane', ['owned-dead', 'replacement', 'owned-live', 'absent'])
def test_close_retires_only_exact_dead_owned_pane(monkeypatch, pane):
    child = subprocess.Popen([sys.executable, '-c', 'pass'])
    fd = os.pidfd_open(child.pid)
    child.wait(timeout=5)
    process = object.__new__(TmuxProcess)
    process.fd, process.children = fd, []
    process.pid, process.returncode = child.pid, child.returncode
    process.config, process.name = SimpleNamespace(), 'fixture'
    calls = []
    def tmux(_config, *args, **kwargs):
        calls.append(args)
        if args[0] == 'display-message':
            text = {'owned-dead': f'%17:{child.pid}:1', 'replacement': f'%23:{child.pid + 1}:1',
                    'owned-live': f'%17:{child.pid}:0', 'absent': ''}[pane]
            return SimpleNamespace(stdout=text, returncode=int(pane == 'absent'))
        assert args == ('kill-pane', '-t', '%17')
        assert kwargs == {'check': True}
        return SimpleNamespace(stdout='', returncode=0)
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux', tmux)
    process.close()
    process.close()
    assert process.fd is None
    assert sum(x[0] == 'kill-pane' for x in calls) == int(pane == 'owned-dead')
