"""Short-lived observer authority for one bounded existing-owned Terminal read.

Read/reconnect never claims input, changes ownership, or launches a native client.
"""
import asyncio
import secrets
import time
from dataclasses import dataclass

from acc.session_relay import protocol as p

from ..models import Session
from . import authority, history, store


@dataclass
class ReadGrant:
    resolve: object
    stamp: object
    deadline: float


class TerminalReads:
    def __init__(self, run):
        self.run, self.grants, self.inflight = run, {}, set()

    def current(self, resolve):
        run = self.run
        leader = getattr(run.ctx.broker, 'authority', None)
        if leader:
            leader.check()
        with run.ctx.db() as db:
            row, current = authority.current(run.ctx, db, resolve, run.session_id, expected_run=run.run_id)
            session = db.get(Session, run.session_id)
            if (row.sealed or row.binding_id != run.binding_id or row.authority_epoch != run.fence.authority_epoch
                    or store.coordinates(session) != run.coordinates
                    or run.manager.current_placement(db, session, provider=run.provider) != run.placement):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            mask = run.ctx.supervisor.vault.redactor(db, session.vault_keys or []).text
            return current, mask

    def current_grant(self, token):
        grant = self.grants.get(token)
        if grant is None or grant.deadline <= time.monotonic():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        current, mask = self.current(grant.resolve)
        authority.recheck(grant.stamp, current)
        if self.grants.get(token) is not grant or grant.deadline <= time.monotonic():
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        return current, mask

    def verify(self, token):
        current, _ = self.current_grant(token)
        return {'sessionId': self.run.session_id, 'runId': str(self.run.run_id),
            'epoch': current.epoch, 'bindingId': current.binding_id}

    async def read(self, resolve, expected_run):
        if expected_run != str(self.run.run_id):
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        if len(self.grants) >= 16 or len(self.inflight) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        current, _ = await asyncio.to_thread(self.current, resolve)
        if len(self.grants) >= 16 or len(self.inflight) >= 16:
            raise p.RelayError(p.ErrorCode.LIMIT)
        token = secrets.token_hex(32)
        self.grants[token] = ReadGrant(resolve, current, time.monotonic() + 10)
        try:
            task = asyncio.create_task(self.run.peer.call('terminal.read', {'grant': token}))
            self.inflight.add(task)
            def finished(done):
                self.inflight.discard(done)
                if not done.cancelled():
                    done.exception()
            task.add_done_callback(finished)
            # A viewer leaving cannot cancel/fence the shared execution peer.
            # Retire its grant immediately; the bounded IO gets no final read
            # authority and drains its own response without any replay.
            result = await asyncio.shield(task)
            await asyncio.to_thread(self.verify, token)
            # Refresh redaction under the same unexpired, unretired grant. A
            # separately authorized read may have a different run/epoch stamp.
            _, mask = await asyncio.to_thread(self.current_grant, token)
            if (not isinstance(result, dict) or result.keys() != {'runId', 'text', 'truncated'}
                    or result['runId'] != expected_run or not isinstance(result['text'], str)
                    or len(result['text'].encode()) > 128 << 10 or type(result['truncated']) is not bool):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            return history.safe_display(result, mask)
        except (ConnectionError, OSError, TimeoutError):
            raise p.RelayError(p.ErrorCode.OFFLINE) from None
        finally:
            self.grants.pop(token, None)


def service(run):
    if not hasattr(run, 'terminal_reads'):
        run.terminal_reads = TerminalReads(run)
    return run.terminal_reads
