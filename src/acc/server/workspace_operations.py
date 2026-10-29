"""Serialize folder lifecycle operations in the authoritative server process."""
import asyncio
import posixpath
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Literal

ANCESTOR_DELETE_WAIT_SECONDS = 10
OperationKind = Literal['delete', 'convert', 'launch', 'resume', 'other']


@dataclass
class _Entry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0
    kinds: dict[OperationKind, int] = field(default_factory=dict)


class WorkspaceOperations:
    def __init__(self):
        self._entries = {}
        self._changed = asyncio.Event()

    @staticmethod
    def key(path, host='local'):
        host = host or 'local'
        normalized = posixpath.normpath(str(path))
        # Keys describe account-owned paths; resolving symlinks here would probe
        # the server account's filesystem, which may be a different machine.
        return host, normalized

    def busy(self, path, host='local', *, kind=None):
        """Synchronous admission checks must not interleave with a folder operation.

        Publisher acceptance has no awaits: checking this lease and committing its
        request happen in one event-loop turn, before a queued deletion can run.
        """
        entry = self._entries.get(self.key(path, host))
        return bool(entry and (entry.users if kind is None else entry.kinds.get(kind, 0)))

    def other_busy(self, path, host='local'):
        """Deletion reservations on other paths of the same machine, including queued ones."""
        key = self.key(path, host)
        return any(other != key and other[0] == key[0] and entry.kinds.get('delete', 0)
                   for other, entry in self._entries.items())

    def busy_ancestor(self, path, host='local'):
        """Only deletions reserve descendants; all kinds still serialize on exact keys."""
        machine, normalized = self.key(path, host)
        return any(other_machine == machine and entry.kinds.get('delete', 0) and normalized.startswith(parent.rstrip('/') + '/')
                   for (other_machine, parent), entry in self._entries.items())

    async def wait_for_deletions(self, path, host='local', *, include_self=False):
        """Wait for destructive ancestors only; callers revalidate before publication."""
        def blocked():
            return self.busy_ancestor(path, host) or (include_self and self.busy(path, host, kind='delete'))

        try:
            async with asyncio.timeout(ANCESTOR_DELETE_WAIT_SECONDS):
                while blocked():
                    self._changed.clear()  # no await between predicate and clear
                    await self._changed.wait()
        except TimeoutError:
            return not blocked()
        return True

    def pending(self, path, host='local'):
        entry = self._entries.get(self.key(path, host))
        return max(0, entry.users - int(entry.lock.locked())) if entry else 0

    @asynccontextmanager
    async def hold(self, path, host='local', *, wait=True, kind: OperationKind = 'other'):
        key = self.key(path, host)
        entry = self._entries.setdefault(key, _Entry())
        if not wait and entry.users:
            yield False
            return
        entry.users += 1
        entry.kinds[kind] = entry.kinds.get(kind, 0) + 1
        self._changed.set()
        acquired = False
        try:
            await entry.lock.acquire()
            acquired = True
            yield True
        finally:
            if acquired:
                entry.lock.release()
            entry.users -= 1
            entry.kinds[kind] -= 1
            if not entry.kinds[kind]:
                del entry.kinds[kind]
            self._changed.set()
            if not entry.users:
                del self._entries[key]

    async def run(self, path, operation, host='local', *, kind: OperationKind = 'other'):
        started = False

        async def owned():
            nonlocal started
            async with self.hold(path, host, kind=kind):
                started = True
                return await operation()

        task = asyncio.create_task(owned())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if not started:
                task.cancel()
            # A disconnect must not release the operation lock while a broker
            # request continues. Join its outcome; cancelled waiters also drain.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not task.cancelled():
                task.exception()  # retrieve failure without replacing caller cancellation
            raise
