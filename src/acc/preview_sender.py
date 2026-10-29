"""A bounded low-priority allocation within a machine link's sender."""
import asyncio
import itertools

from .preview_stream import KINDS, TIMEOUT


class PrioritySender:
    def __init__(self, send):
        self.send_raw = send
        self.queue = asyncio.PriorityQueue()
        self.sequence = itertools.count()
        self.buffered = 0
        self.preview_buffered = 0
        self.task = asyncio.create_task(self.run())
        self.closed = False

    async def send(self, data):
        if self.closed:
            raise ConnectionError('sender closed')
        cost = len(data) + 256
        priority = 1 if isinstance(data, bytes) and data[:1] in KINDS else 0
        if self.buffered + cost > 64 << 20 or (priority and self.preview_buffered + cost > 2 << 20):
            raise ConnectionError('preview sender budget exceeded')
        done = asyncio.get_running_loop().create_future()
        self.preview_buffered += cost if priority else 0
        self.buffered += cost
        self.queue.put_nowait((priority, next(self.sequence), data, done, cost))
        await asyncio.wait_for(done, TIMEOUT)

    async def run(self):
        current = None
        try:
            while True:
                priority, _, data, current, cost = await self.queue.get()
                if not current.cancelled():
                    await asyncio.wait_for(self.send_raw(data), TIMEOUT)
                self.buffered -= cost
                self.preview_buffered -= cost if priority else 0
                if not current.done():
                    current.set_result(None)
                current = None
        except Exception:
            pass  # fail every pending send below; the link owner handles its failed send
        finally:
            self.closed = True
            if current is not None and not current.done():
                current.set_exception(ConnectionError('machine sender closed'))
            self.buffered = self.preview_buffered = 0
            while not self.queue.empty():
                _, _, _, done, _ = self.queue.get_nowait()
                if not done.done():
                    done.set_exception(ConnectionError('machine sender closed'))

    def close(self):
        if not self.closed:
            self.closed = True
            self.task.cancel()

    async def aclose(self):
        self.close()
        await asyncio.gather(self.task, return_exceptions=True)
