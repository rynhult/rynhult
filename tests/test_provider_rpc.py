"""Short-lived provider probes must not outlive their total deadline."""
import asyncio
import os
import time

import pytest

from acc.providers.codex import _jsonrpc


def cli(tmp_path, body):
    path = tmp_path / "cli"
    path.write_text("#!/usr/bin/env python3\n" + body)
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize("body", [
    'import time; time.sleep(.6)\n',
    'import sys,time; sys.stdout.write("{"); sys.stdout.flush(); time.sleep(.6)\n',
])
def test_codex_total_deadline_includes_partial_line(tmp_path, body):
    exe = cli(tmp_path, body)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        _jsonrpc(exe, "model/list", timeout=.05)
    assert time.monotonic() - started < .4


@pytest.mark.parametrize("output, match", [
    ('"not-json\\n"', "malformed"),
    ('"[]\\n"', "malformed"),
    ('"{\\"id\\":1,\\"error\\":{\\"code\\":-1,\\"message\\":\\"refused\\"}}\\n"', "RPC error"),
    ('"x"*300000', "too large"),
])
def test_rpc_rejects_malformed_oversized_and_error_frames(tmp_path, output, match):
    exe = cli(tmp_path, f"import sys,time; sys.stdout.write({output}); sys.stdout.flush(); time.sleep(2)\n")
    with pytest.raises(ValueError, match=match):
        _jsonrpc(exe, "model/list", timeout=.5)


async def test_cancellation_joins_and_reaps_a_child_ignoring_termination(tmp_path):
    from acc.providers.rpc import cancellable

    pidfile = tmp_path / "pid"
    exe = cli(tmp_path, f"""import os,signal,time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
open({str(pidfile)!r}, "w").write(str(os.getpid()))
while True: time.sleep(.01)
""")
    task = asyncio.create_task(cancellable(_jsonrpc, exe, "model/list"))
    try:
        async with asyncio.timeout(2):
            while not pidfile.exists():
                await asyncio.sleep(.01)
        pid = int(pidfile.read_text())
        started = time.monotonic()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert time.monotonic() - started < .8
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def test_buffered_frames_and_one_deadline_for_the_whole_probe(tmp_path):
    from acc.providers.rpc import RPC

    exe = cli(tmp_path, """import sys,time
sys.stdout.write('{"method":"notice"}\\n{"id":1,"result":{}}\\n{')
sys.stdout.flush()
time.sleep(.6)
""")
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        with RPC([exe], .05) as rpc:
            assert rpc.receive(1) == {"id": 1, "result": {}}
            rpc.receive(2)
    assert time.monotonic() - started < .4


def test_model_catalog_propagates_probe_cancellation(env, monkeypatch):
    from acc import providers
    from acc.broker import models, ops

    monkeypatch.setattr(ops, "installed_providers", lambda _: {"codex": "codex"})

    def cancelled(*args):
        raise InterruptedError("provider RPC cancelled")

    monkeypatch.setattr(providers.get("codex"), "models", cancelled)
    with pytest.raises(InterruptedError):
        models.model_catalog(env["broker"])


def test_rpc_accepts_null_acknowledgements(tmp_path):
    from acc.providers.rpc import RPC

    exe = cli(tmp_path, """print('{"id":3,"result":null}', flush=True)
""")
    with RPC([exe], 1) as rpc:
        assert rpc.receive(3) == {"id": 3, "result": None}
