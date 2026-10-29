"""Independent node jobs with one cancellation/join boundary and one exception observer."""
from __future__ import annotations

import asyncio

from .supervisor_common import log


class NodeTasks:
    def __init__(self, name):
        self.name = name
        self.tasks = set()
        self.cleanups = set()
        self.closed = False

    def track(self, task, *, report=True, cleanup=False):
        self.tasks.add(task)
        if cleanup:
            self.cleanups.add(task)
        elif self.closed:
            task.cancel()

        def finished(done):
            self.tasks.discard(done)
            self.cleanups.discard(done)
            if not done.cancelled():
                error = done.exception()
                if error is not None and report:
                    log.error('node %s: task %s failed', self.name, done.get_name(),
                              exc_info=(type(error), error, error.__traceback__))
        task.add_done_callback(finished)
        return task

    def start(self, work, label, *, report=True, cleanup=False):
        return self.track(asyncio.create_task(work, name=f'node:{self.name}:{label}'),
                          report=report, cleanup=cleanup)

    def cancel(self):
        self.closed = True
        current = asyncio.current_task()
        for task in list(self.tasks - self.cleanups):
            if task is not current and not task.cancelling():
                task.cancel()

    async def join(self):
        current = asyncio.current_task()
        while pending := {task for task in self.tasks if task is not current and not task.done()}:
            await asyncio.gather(*pending, return_exceptions=True)


async def joined_thread(work, *args):
    """Cancellation cannot stop a thread: retain and join it before its node scope drains."""
    worker = asyncio.create_task(asyncio.to_thread(work, *args))
    try:
        return await asyncio.shield(worker)
    except asyncio.CancelledError:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled() and worker.exception() is not None:
            error = worker.exception()
            log.error('node worker failed during cancellation', exc_info=(type(error), error, error.__traceback__))
        raise
