"""Killable, single-flight account reads (shared FS-1 worker mechanism). No broker control lock spans filesystem IO."""
from __future__ import annotations

import asyncio
import json
import sys
from collections import deque
from pathlib import Path

from .config import BrokerConfig
from .ops import OpError

DEADLINE = 2.0
QUEUE_WAIT = .5  # plus execution deadline remains below the fresh client's 3s timeout
QUEUE_LIMIT = 4


class ReadBusy(OpError):
    """Expected bounded admission pressure, not a filesystem or transport failure."""


def argv():
    # Import this broker's code, independently of cwd, user site and PYTHONPATH.
    source = str(Path(__file__).resolve().parents[2])
    return [sys.executable, '-I', '-c',
            f'import sys; sys.path.insert(0, {source!r}); from acc.broker.search_worker import main; main()']


class Searches:
    def __init__(self):
        self.active = False
        self.operation = None
        self.chat_cache = {}
        self.waiters = deque()

    def _release(self):
        # Keep the reservation during handoff; never let a newcomer jump the FIFO.
        while self.waiters:
            waiter = self.waiters.popleft()
            if not waiter.done():
                waiter.set_result(None)
                return
        self.active = False

    async def _acquire(self, operation=None):
        if not self.active:
            self.active = True
            return
        if operation == 'files_search' and self.operation == 'files_search':
            raise ReadBusy('Account read is busy: another filename search is running. Retry shortly.')
        if len(self.waiters) >= QUEUE_LIMIT:
            raise ReadBusy('Account read is busy: another read is running and the queue is full.')
        waiter = asyncio.get_running_loop().create_future()
        self.waiters.append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), QUEUE_WAIT)
        except BaseException as error:
            if waiter.done() and not waiter.cancelled():
                self._release()  # Cancellation/timeout raced a transferred reservation.
            else:
                self.waiters.remove(waiter)
                waiter.cancel()
            if isinstance(error, TimeoutError):
                raise ReadBusy('Account read is busy: another read is running. Retry shortly.') from None
            raise

    async def run(self, config, params, *, operation="files_search"):
        await self._acquire(operation)
        self.operation = operation
        task = None
        process = None
        try:
            fields = ('run_dir', 'allowed_roots', 'folder_revision', 'folder_policy_required',
                      'allowed_folders', 'folder_homes', 'folder_protected')
            values = {key: getattr(config, key) for key in fields}
            payload = json.dumps({'config': values, 'params': params, 'operation': operation,
                'chat_cache': self.chat_cache if operation == 'chat_read' else {}}, default=lambda p: str(p)).encode()
            spawn = asyncio.create_task(asyncio.create_subprocess_exec(*argv(), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL))
            try:
                process = await asyncio.shield(spawn)
            except asyncio.CancelledError:
                # Own a spawn even if cancellation lands before it returns its PID.
                process = await spawn
                task = asyncio.create_task(process.communicate())
                raise
            task = asyncio.create_task(process.communicate(payload))
            try:
                output, _ = await asyncio.wait_for(asyncio.shield(task), DEADLINE)
            except TimeoutError:
                raise OpError('Account read is unavailable: its deadline expired.') from None
            if process.returncode:
                raise OpError('Account read is unavailable for this location.')
            result = json.loads(output)
            if operation == "chat_read":
                self.chat_cache = result["cache"]
                return result["result"]
            return result
        finally:
            if process is not None and process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            if task is not None and not task.done():
                # A stuck kernel IO must neither block controls nor permit more workers.
                def reaped(done):
                    self._release()
                    if not done.cancelled():
                        done.exception()
                task.add_done_callback(reaped)
            else:
                self._release()


def main():
    request = json.load(sys.stdin)
    values = request['config']
    values['run_dir'] = Path(values['run_dir'])
    values['allowed_roots'] = tuple(Path(p) for p in values['allowed_roots'])
    config = BrokerConfig(**values)
    if request.get('operation', 'files_search') == 'chat_read':
        from .chat import export_cache, import_cache, read_chat
        import_cache(request.get('chat_cache', {}))
        result = read_chat(Path.home(), **request['params'])
        print(json.dumps({'result': result, 'cache': export_cache()}))
    else:
        from .file_search import search
        print(json.dumps(search(config, **request['params'])))
