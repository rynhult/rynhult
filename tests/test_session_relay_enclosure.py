"""Owned disposable child processes only; never the installed account CLI."""
import asyncio
import os
import sys
from types import SimpleNamespace

import pytest

from acc.broker.relay_enclosure import Enclosure, incarnation


async def until(condition):
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(.01)
    raise AssertionError('owned child did not reach its boundary')


@pytest.mark.parametrize('stop', [False, True])
async def test_subreaper_reaps_setsid_double_fork_and_writes_only_empty_tree_proof(tmp_path, stop):
    script = tmp_path / 'fake-cli'
    # Detached grandchild ignores TERM; its pid must be adopted/reaped by the
    # supervisor, rather than disappearing from a root's /proc snapshot.
    script.write_text(f'#!{sys.executable}\n' + '''import os, signal, time, sys
if os.fork() == 0:
 os.setsid()
 if os.fork() == 0:
  signal.signal(signal.SIGTERM, signal.SIG_IGN)
  open(sys.argv[1], 'w').write(str(os.getpid()))
  while True: time.sleep(.01)
 os._exit(0)
if sys.argv[2] == 'True':
 while True: time.sleep(.01)
time.sleep(.2)
''')
    script.chmod(0o500)
    spec = SimpleNamespace(run_id='run1')
    enclosure = Enclosure(tmp_path / 'private', spec, script)
    pidfile = tmp_path / 'grandchild'
    process = await asyncio.create_subprocess_exec(str(enclosure.cli), str(pidfile), str(stop),
        env={'PATH': '/usr/bin:/bin'}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    identity = f'{process.pid}:{incarnation(process.pid)}'
    try:
        await until(lambda: pidfile.exists() and pidfile.stat().st_size > 0)
        grandchild = int(pidfile.read_text())
        assert enclosure.proof('started.json', identity)
        assert not enclosure.proof('exit.json', identity)
        if stop:
            process.terminate()
        await asyncio.wait_for(process.wait(), 5)
        assert enclosure.proof('exit.json', identity), (await process.stderr.read()).decode()
        assert not enclosure.proof('exit.json', f'{process.pid}:wrong-incarnation')
        with pytest.raises(ProcessLookupError):
            os.kill(grandchild, 0)
    finally:
        if process.returncode is None:
            process.terminate()
            await asyncio.wait_for(process.wait(), 5)
        enclosure.close()


async def test_killed_supervisor_never_produces_an_exit_attestation(tmp_path):
    script = tmp_path / 'fake-cli'
    script.write_text(f'#!{sys.executable}\nimport time\ntime.sleep(.4)\n')
    script.chmod(0o500)
    enclosure = Enclosure(tmp_path / 'private', SimpleNamespace(run_id='run1'), script)
    process = await asyncio.create_subprocess_exec(str(enclosure.cli), env={'PATH': '/usr/bin:/bin'})
    identity = f'{process.pid}:{incarnation(process.pid)}'
    try:
        await until(lambda: enclosure.proof('started.json', identity))
        process.kill()
        await process.wait()
        assert not enclosure.proof('exit.json', identity)
        # The test's finite child exits naturally; no host PID signalling.
        await asyncio.sleep(.5)
    finally:
        enclosure.close()


async def test_pinned_sdk_owner_requires_enclosure_receipt_before_same_id_resume(tmp_path):
    from dataclasses import replace

    from test_session_relay_claude_agent import CLI, spec

    from acc.session_relay.adapters.base import CloseMode
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter

    pytest.importorskip('claude_agent_sdk')
    reviewed = spec(tmp_path)
    enclosures = []
    adapters = []
    async def publish(event):
        pass
    try:
        for index in range(2):
            current = replace(reviewed, run_id=f'run{index}')
            enclosure = Enclosure(tmp_path / 'private', current, CLI.resolve())
            enclosures.append(enclosure)
            async def proof(observed, owned=enclosure):
                return owned.proof('exit.json', observed.process_identity)
            adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish, exit_proof=proof)
            adapters.append(adapter)
            observed = await adapter.start(replace(current, spec={**current.spec, 'supervised_cli_path': str(enclosure.cli), 'launch_env': {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)}}))
            assert enclosure.proof('started.json', observed.process_identity)
            assert not enclosure.proof('exit.json', observed.process_identity)
            evidence = await adapter.stop_owner()
            assert evidence.exit.value == 'exited'
            assert adapter.exit_verified and enclosure.proof('exit.json', observed.process_identity)
    finally:
        for adapter in adapters:
            await adapter.close(CloseMode.STOP)
        for enclosure in enclosures:
            enclosure.close()


async def test_terminal_enclosure_exits_before_sdk_resumes_same_native_id(tmp_path, monkeypatch):
    import json
    from dataclasses import replace

    from test_session_relay_claude_agent import CLI, spec

    from acc.broker.claude_owner import TmuxProcess
    from acc.session_relay.adapters.base import CloseMode
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter

    pytest.importorskip('claude_agent_sdk')
    reviewed = replace(spec(tmp_path), run_id='terminal1')
    terminal = Enclosure(tmp_path / 'private', reviewed, CLI.resolve())
    child = await asyncio.create_subprocess_exec(str(terminal.cli), '--resume', 'fixture-conversation', '--settings', str(tmp_path / 'settings.json'),
        cwd=tmp_path, env={'PATH': '/usr/bin:/bin'}, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    assert json.loads(await child.stdout.readline())['type'] == 'terminal_ready'
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux', lambda config, *args, **kwargs:
        SimpleNamespace(returncode=0, stdout=(f'$1 %1 {child.pid} {__import__("os").getpid()}' if args[-1] == '#{session_id} #{pane_id} #{pane_pid} #{pid}' else str(child.pid)) if args[0] == 'display-message' else 'final fixture screen'))
    pane = TmuxProcess(SimpleNamespace(), 'fixture', terminal)
    adapter = None
    chat = None
    try:
        pane.terminate()
        await asyncio.wait_for(pane.wait(), 5)
        await child.wait()
        assert await pane.tree_exited(pane)
        assert pane.snapshot == 'final fixture screen'
        resumed = replace(reviewed, run_id='chat2')
        chat = Enclosure(tmp_path / 'private', resumed, CLI.resolve())
        async def publish(event):
            pass
        async def proof(observed):
            return chat.proof('exit.json', observed.process_identity)
        adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=publish, exit_proof=proof)
        await adapter.start(replace(resumed, spec={**resumed.spec, 'supervised_cli_path': str(chat.cli), 'launch_env': {'PATH': '/usr/bin:/bin', 'HOME': str(tmp_path)}}))
        assert adapter.client.options.resume == 'fixture-conversation'
        assert (await adapter.stop_owner()).exit.value == 'exited'
    finally:
        if adapter:
            await adapter.close(CloseMode.STOP)
        if child.returncode is None:
            pane.terminate()
            await child.wait()
        pane.close()
        terminal.close()
        if chat:
            chat.close()


async def test_sdk_metadata_probe_inherits_neither_worker_credentials_nor_account_home(tmp_path, monkeypatch):
    import json
    script = tmp_path / 'fake-cli'
    script.write_text(f'#!{sys.executable}\n' + "import os,json; print(json.dumps({'sentinel': os.getenv('RELAY_TEST_SENTINEL'), 'worker_key_present': 'ANTHROPIC_API_KEY' in os.environ, 'home': os.getenv('HOME')}))\n")
    script.chmod(0o500)
    enclosure = Enclosure(tmp_path / 'private', SimpleNamespace(run_id='run1'), script)
    monkeypatch.setenv('RELAY_TEST_SENTINEL', 'artificial-not-inherited')
    process = await asyncio.create_subprocess_exec(str(enclosure.cli), '-v', stdout=asyncio.subprocess.PIPE)
    try:
        output, _ = await process.communicate()
        result = json.loads(output)
        assert result['sentinel'] is None and result['worker_key_present'] is False
        assert result['home'] == str(enclosure.path / 'metadata-home')
        assert not (enclosure.path / 'started.json').exists()
    finally:
        enclosure.close()


async def test_factory_reclaims_completed_enclosure_and_pidfds_before_next_owner(tmp_path, monkeypatch):
    from acc.broker.claude_owner import ClaudeOwnerFactory, TmuxProcess
    script = tmp_path / 'fake-cli'
    script.write_text(f'#!{sys.executable}\nimport time; time.sleep(.2)\n')
    script.chmod(0o500)
    directory = tmp_path / 'private'
    old_spec = SimpleNamespace(run_id='old', binding=SimpleNamespace(native_conversation='fixture'))
    old = Enclosure(directory, old_spec, script)
    child = await asyncio.create_subprocess_exec(str(old.cli), env={'PATH': '/usr/bin:/bin'})
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux', lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=(f'$1 %1 {child.pid} {os.getpid()}' if args[-1] == '#{session_id} #{pane_id} #{pane_pid} #{pid}' else '')))
    pane = TmuxProcess(SimpleNamespace(), 'owned-fixture', old)
    await pane.wait()
    await child.wait()
    owner = object.__new__(ClaudeOwnerFactory)
    owner.closed, owner.exit_qualified, owner.close_complete = False, True, False
    owner.current = SimpleNamespace(process=pane)
    owner.processes, owner.enclosures = [pane], {'old': old}
    owner.hooks, owner.enclosure, owner.observer, owner.input_attachment = None, old, None, None
    owner.directory, owner.executable = directory, script
    owner.files = {'settings.json': '{"permissions":{"ask":[]}}'}
    async def authorized(*args):
        return True
    async def noop(*args):
        pass
    owner.authorize = authorized
    owner.runtime = SimpleNamespace(publish=noop, observe=noop)
    try:
        await owner.prepare(SimpleNamespace(run_id='new', binding=old_spec.binding))
        assert old.fd is None and pane.fd is None
        assert owner.processes == [] and set(owner.enclosures) == {'new'}
    finally:
        if owner.hooks:
            await owner.hooks.close()
        for enclosure in owner.enclosures.values():
            enclosure.close()
        pane.close()
        old.close()
