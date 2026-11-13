"""OS-process proof: a bound Unix pathname does not certify a listener."""
import asyncio
import errno
import importlib.util
import json
import os
import select
import socket
import subprocess
import sys
from pathlib import Path

import pytest

CHILD='''import json,os,socket,sys
server=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM)
server.bind(sys.argv[1])
print('bound',flush=True)
assert sys.stdin.readline()=='listen\\n'
server.listen(1)
print('listening',flush=True)
peer,_=server.accept()
assert json.loads(peer.makefile('rb').readline())['method']=='state'
peer.sendall(json.dumps({'pid':os.getpid(),'active':0,'recoveryOnly':True}).encode()+b'\\n')
peer.close();server.close()
'''


def line(child):
    ready,_,_=select.select([child.stdout],[],[],5)
    assert ready,'Fixture child did not reach bounded barrier'
    return child.stdout.readline()


def test_fresh_child_bound_path_refuses_until_listen_then_reports_its_own_pid(tmp_path):
    path=tmp_path/'worker.sock'
    child=subprocess.Popen([sys.executable,'-u','-c',CHILD,str(path)],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.PIPE,env={'PATH':os.defpath,'HOME':str(tmp_path)})
    try:
        assert line(child)==b'bound\n' and child.poll() is None and path.exists()
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            assert client.connect_ex(str(path))==errno.ECONNREFUSED
        child.stdin.write(b'listen\n');child.stdin.flush()
        assert line(child)==b'listening\n'
        with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as client:
            client.settimeout(5);client.connect(str(path));client.sendall(b'{"method":"state"}\n')
            assert json.loads(client.makefile('rb').readline())=={'pid':child.pid,'active':0,'recoveryOnly':True}
        assert child.wait(timeout=5)==0
        assert path.exists()  # an exited incarnation can leave the same name too
    finally:
        if child.poll() is None:child.terminate();child.wait(timeout=5)


def readiness():
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/restart_readiness.py'
    spec = importlib.util.spec_from_file_location('restart_readiness', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.wait_for_worker_state


@pytest.mark.parametrize('ready_marker', [False, True])
@pytest.mark.parametrize('wrong_pid', [False, True])
async def test_readiness_waits_for_real_listener_and_refuses_another_incarnation(tmp_path, wrong_pid, ready_marker):
    path = tmp_path / 'ready.sock'
    child = await asyncio.create_subprocess_exec(sys.executable, '-u', '-c', CHILD, str(path),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env={'PATH': os.defpath, 'HOME': str(tmp_path)})
    methods = []
    async def rpc(endpoint, method):
        methods.append(method)
        reader, writer = await asyncio.open_unix_connection(endpoint)
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            return json.loads(await reader.readline())
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b'bound\n'
        if ready_marker:
            # Even a marker naming this live PID cannot replace the actual RPC.
            (tmp_path / 'inspection-ready.json').write_text(json.dumps({'pid': child.pid}))
        task = asyncio.create_task(readiness()(rpc, str(path), child.pid + int(wrong_pid)))
        await asyncio.sleep(.03)
        assert not task.done() and child.returncode is None
        child.stdin.write(b'listen\n')
        await child.stdin.drain()
        assert await asyncio.wait_for(child.stdout.readline(), 5) == b'listening\n'
        if wrong_pid:
            with pytest.raises(AssertionError, match='incarnation'):
                await task
        else:
            assert await task == {'pid': child.pid, 'active': 0, 'recoveryOnly': True}
        assert methods and set(methods) == {'state'}
        assert await asyncio.wait_for(child.wait(), 5) == 0
    finally:
        if child.returncode is None:
            child.terminate()
            await child.wait()


async def test_readiness_is_bounded_and_never_retries_application_refusal():
    async def absent(*_):
        raise ConnectionRefusedError
    with pytest.raises(TimeoutError):
        await readiness()(absent, '/unused', 123, timeout=.03)
    calls = []
    async def refused(*args):
        calls.append(args)
        raise RuntimeError('Application refused')
    with pytest.raises(RuntimeError, match='Application refused'):
        await readiness()(refused, '/unused', 123)
    assert calls == [('/unused', 'state')]
