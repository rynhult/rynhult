"""A disposable broker OS process, with its model stub/controller kept alive."""
import asyncio
import json
import os
import select
import sys
from pathlib import Path

from acc.broker import ops
from acc.broker.config import BrokerConfig


def native_tui(settings, env, observer_file, requests, native):
    before = len(requests)
    result = asyncio.run(serve(env))
    return {**result, 'requests': requests[before:]}


async def serve(env):
    data = json.loads(Path('/evidence/placement-config.json').read_text())
    process = None
    generations = 0
    tracked = []
    finish = asyncio.Event()
    errors = []
    async def exchange(method):
        reader, writer = await asyncio.open_unix_connection('/evidence/owned-inspect.sock')
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            return json.loads(await asyncio.wait_for(reader.readline(), 15))
        finally:
            writer.close()
            await writer.wait_closed()
    async def start():
        nonlocal process, generations
        assert process is None or process.returncode is not None
        for name in ('owned-broker.sock', 'owned-inspect.sock', 'owned-ready.json', 'noop-hook'):
            Path('/evidence', name).unlink(missing_ok=True)
        generations += 1
        with Path(f'/evidence/worker-{generations}.out').open('w') as out, Path(f'/evidence/worker-{generations}.err').open('w') as err:
            process = await asyncio.create_subprocess_exec(sys.executable, '/evidence/claude_broker_worker.py', stdout=out, stderr=err, env=env)
        for _ in range(1000):
            if process.returncode is not None:
                raise AssertionError(Path(f'/evidence/worker-{generations}.err').read_text())
            if Path('/evidence/owned-ready.json').exists():
                return {'pid': process.pid, 'generation': generations}
            await asyncio.sleep(.02)
        raise TimeoutError('Broker startup')
    async def control(reader, writer):
        try:
            method = json.loads(await asyncio.wait_for(reader.readline(), 3))['method']
            if method == 'start':
                result = await start()
            elif method == 'track':
                for file in Path(data['run_dir']).rglob('started.json'):
                    record = json.loads(file.read_text())
                    if not file.with_name('exit.json').exists():
                        tracked.append(os.pidfd_open(record['pid']))
                background = Path('/evidence/background.pid')
                if background.exists():
                    tracked.append(os.pidfd_open(int(background.read_text())))
                assert tracked
                result = {'pinned': len(tracked)}
            elif method == 'kill':
                pid = process.pid
                process.kill()
                result = {'pid': pid, 'returncode': await process.wait()}
            elif method == 'trees':
                started = list(Path(data['run_dir']).rglob('started.json'))
                exits = [file.with_name('exit.json') for file in started]
                def empty_receipt(file):
                    try:
                        return json.loads(file.read_text()).get('empty') is True
                    except (FileNotFoundError, json.JSONDecodeError):
                        # O_EXCL publishes the name before the writer fills it.
                        # An incomplete record proves nothing; keep polling.
                        return False
                result = {'started': len(started), 'empty': all(empty_receipt(file) for file in exits),
                    'tracked_dead': all(select.select([fd], [], [], 0)[0] for fd in tracked)}
            elif method == 'finish':
                if process.returncode is None:
                    await exchange('finish')
                    await asyncio.wait_for(process.wait(), 20)
                finish.set()
                result = {'done': True}
            else:
                raise ValueError(method)
        except Exception as error:
            errors.append(repr(error))
            result = {'error': repr(error)}
        writer.write(json.dumps(result).encode() + b'\n')
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    controller = await asyncio.start_unix_server(control, '/evidence/broker-control.sock')
    result = {}
    try:
        await start()
        await asyncio.wait_for(finish.wait(), 110)
        if process.returncode == 0:
            result = json.loads(Path(f'/evidence/worker-{generations}.out').read_text())
        assert not errors, errors
    finally:
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 5)
            except TimeoutError:
                process.kill()
                await process.wait()
        for fd in tracked:
            os.close(fd)
        controller.close()
        await controller.wait_closed()
        config = BrokerConfig(tmux_socket='relay-placement-offline')
        await asyncio.to_thread(ops.tmux, config, 'kill-server')
    return {**result, 'broker_generations': generations, 'os_process': True}
