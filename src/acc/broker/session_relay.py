"""Account-local relay supervision and durable, at-most-once admission.

No provider is launched by reads or recovery. The integration owner must supply
current authenticated main authority; possession of a run directory grants none.
SQLite is a private broker journal, not a second main database/migration lineage.
"""
from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import os
import signal
import sqlite3
import stat
import time
import uuid
from dataclasses import asdict
from pathlib import Path
from typing import Awaitable, Callable

from acc import peercred
from acc.session_relay.adapters.base import AdapterOperation, AdmissionResult, Outcome, ReviewedSpec
from acc.session_relay.protocol import ErrorCode, EvidenceKind, Fence, RelayError

SCHEMA_VERSION = 1


def private_dir(path: Path) -> int:
    """Walk from / with no-follow descriptors; pin the final owned 0700 directory."""
    path = path.absolute()
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            except FileNotFoundError:
                os.mkdir(part, 0o700, dir_fd=fd)
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        return fd
    except BaseException:
        os.close(fd)
        raise


class AdmissionJournal:
    """Atomic claim before send. Unresolved claims survive indefinitely, never auto-resend.

    Terminal results may expire; expiring one advances a durable deadline floor so
    an old grant cannot resurrect an evicted operation. Capacity never evicts an
    unresolved outcome. All files reside in a pinned private account directory.
    """
    def __init__(self, root: Path, *, capacity: int = 4096, retention_ms: int = 86400_000,
                 clock: Callable[[], int] = lambda: int(time.time() * 1000)):
        self.root, self.capacity, self.retention_ms, self.clock = root, capacity, retention_ms, clock
        self.fd = private_dir(root)
        self.identity = os.fstat(self.fd)
        fd = os.open('admission.sqlite3', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
            os.close(fd)
            os.close(self.fd)
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        os.close(fd)
        self.check_path()
        self.db = sqlite3.connect(root / 'admission.sqlite3', isolation_level=None, timeout=5)
        self.db.execute('PRAGMA synchronous=FULL')
        # DELETE avoids long-lived shared-memory files; the parent is owner-private.
        self.db.execute('PRAGMA journal_mode=DELETE')
        version = self.db.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, SCHEMA_VERSION):
            self.close()
            raise RelayError(ErrorCode.VERSION_MISMATCH)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS admission (
                operation TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, deadline INTEGER NOT NULL,
                state TEXT NOT NULL, result TEXT, updated INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS observation (
                operation TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, identity TEXT NOT NULL);
        ''')
        self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def check_path(self):
        info = self.root.lstat()
        if (info.st_dev, info.st_ino) != (self.identity.st_dev, self.identity.st_ino) or stat.S_IMODE(info.st_mode) != 0o700:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    def _meta(self, key, default=''):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return row[0] if row else default

    def _set(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?, ?)', (key, str(value)))

    def enroll(self, fence: Fence, link_id: str):
        """Called only after main authentication. Same-epoch competing hubs are refused."""
        self.check_path()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            old = json.loads(self._meta('authority', 'null'))
            current = [fence.authority_epoch, fence.broker_incarnation, fence.hub_incarnation, link_id]
            if old and (current[0] < old[0] or (current[0] == old[0] and current != old)):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            self._set('authority', json.dumps(current))
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def claim(self, operation: AdapterOperation, link_id: str):
        self.check_path()
        now = self.clock()
        from acc.session_relay.protocol import to_wire
        # Preserve the v1 journal fingerprint format without deepcopying the
        # operation's immutable mappings or omitting its private payload.
        identity = {**vars(operation), 'actor': asdict(operation.actor), 'fence': asdict(operation.fence),
                    'params': to_wire(operation.params)}
        fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        self.db.execute('BEGIN IMMEDIATE')
        try:
            expected = [operation.fence.authority_epoch, operation.fence.broker_incarnation,
                        operation.fence.hub_incarnation, link_id]
            if json.loads(self._meta('authority', 'null')) != expected:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            row = self.db.execute('SELECT fingerprint, state, result FROM admission WHERE operation=?',
                                  (operation.operation_id,)).fetchone()
            if row:
                if row[0] != fingerprint:
                    raise RelayError(ErrorCode.INTEGRITY_FAILURE)
                self.db.execute('COMMIT')
                return False, row[1], json.loads(row[2]) if row[2] else None
            if operation.deadline_ms <= max(now, int(self._meta('deadline_floor', '0'))):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            # Garbage collect only concluded outcomes whose original grants have expired.
            expired = self.db.execute("SELECT MAX(deadline) FROM admission WHERE state IN ('accepted','rejected') AND updated<? AND deadline<=?",
                                      (now - self.retention_ms, now)).fetchone()[0]
            if expired is not None:
                self._set('deadline_floor', max(expired, int(self._meta('deadline_floor', '0'))))
                self.db.execute("DELETE FROM admission WHERE state IN ('accepted','rejected') AND updated<? AND deadline<=?",
                                (now - self.retention_ms, now))
            self.db.execute('DELETE FROM observation WHERE operation NOT IN (SELECT operation FROM admission)')
            if self.db.execute('SELECT COUNT(*) FROM admission').fetchone()[0] >= self.capacity:
                raise RelayError(ErrorCode.LIMIT)
            self.db.execute('INSERT INTO admission VALUES (?, ?, ?, ?, NULL, ?)',
                            (operation.operation_id, fingerprint, operation.deadline_ms, 'uncertain', now))
            descriptor = {key: value for key, value in to_wire(operation).items() if key not in ('params', 'deadlineMs')}
            self.db.execute('INSERT INTO observation VALUES (?, ?, ?)',
                (operation.operation_id, fingerprint, json.dumps(descriptor, sort_keys=True)))
            self.db.execute('COMMIT')
            return True, 'uncertain', None
        except BaseException:
            if self.db.in_transaction:
                self.db.execute('ROLLBACK')
            raise

    def settle(self, operation: AdapterOperation, result: AdmissionResult):
        evidence = result.evidence
        if result.outcome is Outcome.UNKNOWN:
            return
        if evidence is None or (evidence.operation_id, evidence.run_id) != (operation.operation_id, operation.run_id):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        value = {'outcome': result.outcome.value, 'turn_id': result.turn_id,
                 'evidence': {'kind': evidence.kind.value, 'operation_id': evidence.operation_id,
                              'run_id': evidence.run_id, 'native_ref': evidence.native_ref}}
        self.db.execute("UPDATE admission SET state=?, result=?, updated=? WHERE operation=? AND state='uncertain'",
                        (result.outcome.value, json.dumps(value), self.clock(), operation.operation_id))

    def lookup(self, operation_id):
        row = self.db.execute('SELECT state, result FROM admission WHERE operation=?', (operation_id,)).fetchone()
        return (row[0], json.loads(row[1]) if row[1] else None) if row else None

    def observe(self, identity):
        """Read exact recorded metadata without claim, GC, dispatch or retry.

        Older v1 entries without observation metadata remain unproven here.
        """
        self.check_path()
        row = self.db.execute('SELECT a.state, a.result, a.fingerprint, o.fingerprint, o.identity '
            'FROM admission a JOIN observation o ON o.operation=a.operation WHERE a.operation=?',
            (identity['operationId'],)).fetchone()
        self.check_path()
        if row is None:
            return None
        if row[2] != row[3] or json.loads(row[4]) != identity:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        return row[0], json.loads(row[1]) if row[1] else None

    def close(self):
        self.db.close()
        os.close(self.fd)


class AdmissionBroker:
    """The final serialized barrier shared by dispatch, Stop and invalidation.

    verify is main's authenticated single-operation verifier, not a native client
    callback. It must check binding/account/node, current policy and grant digest.
    """
    def __init__(self, journal: AdmissionJournal, verify: Callable[[AdapterOperation], Awaitable[bool]]):
        self.journal, self.verify = journal, verify
        self.incarnation = uuid.uuid4().hex
        self.link: tuple[Fence, str] | None = None
        self.lock = asyncio.Lock()

    async def enroll(self, fence: Fence, link_id: str):
        async with self.lock:
            if fence.broker_incarnation != self.incarnation:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            self.journal.enroll(fence, link_id)
            self.link = fence, link_id

    async def disconnect(self, link_id: str):
        async with self.lock:
            if self.link and self.link[1] == link_id:
                self.link = None

    async def dispatch(self, operation: AdapterOperation, send):
        # Authentication may await; capture the link first, then check again at admission.
        link = self.link
        if link is None or not await self.verify(operation):
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        async with self.lock:
            # Waiting for another dispatch/Stop may outlive the first authority
            # snapshot. Verify again inside the final serialized admission barrier.
            if not await self.verify(operation):
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            if self.link != link or operation.fence != link[0] or operation.deadline_ms <= self.journal.clock():
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            fresh, state, result = self.journal.claim(operation, link[1])
            if not fresh:
                if result:
                    from acc.session_relay.protocol import Evidence
                    return AdmissionResult(Outcome(result['outcome']), Evidence(**{
                        **result['evidence'], 'kind': EvidenceKind(result['evidence']['kind'])}), result['turn_id'])
                return AdmissionResult(Outcome.UNKNOWN)
            # Holding this barrier through dispatch prevents revocation/take/Stop overtaking send.
            try:
                result = await send(operation)
            except (OSError, ConnectionError, asyncio.TimeoutError):
                result = AdmissionResult(Outcome.UNKNOWN)
            self.journal.settle(operation, result)
            return result


class PrivateRuntime:
    """One locked private app-server, supervised over stdio (no provider listener).

    argv/env come from the reviewed launch compiler, including the frozen guard
    configuration. No inherited session environment or shared daemon is used.
    Crash recovery never respawns; a held lock is not proof of process death.
    """
    def __init__(self, root: Path, spec: ReviewedSpec):
        name = hashlib.sha256((spec.binding.provider + '\0' + (spec.binding.native_conversation or spec.binding.binding_id)).encode()).hexdigest()[:24]
        self.root, self.spec = root / name, spec
        self.fd = private_dir(self.root)
        self.identity = os.fstat(self.fd)
        self.lock_fd = os.open('owner.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(self.lock_fd)
            os.close(self.fd)
            raise RelayError(ErrorCode.BUSY) from None
        self.process = None
        self.kernel_lifetime = None
        self.enclosure = None
        self.exit_proven = False
        self.closed = False
        self.token = uuid.uuid4().hex

    def validate(self):
        if self.kernel_lifetime is not None and not self.kernel_lifetime.alive():
            raise RelayError(ErrorCode.STALE_AUTHORITY)
        info = self.root.lstat()
        if (info.st_dev, info.st_ino) != (self.identity.st_dev, self.identity.st_ino) or stat.S_IMODE(info.st_mode) != 0o700:
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    async def start(self, executable: str, env: dict[str, str]):
        self.validate()
        if self.process is not None or self.spec.binding.provider != 'codex':
            raise RelayError(ErrorCode.BUSY)
        # The reviewed compiler must attest frozen policy; no generic arbitrary argv/RPC tunnel.
        if self.spec.spec.get('guard_digest') != self.spec.binding.launch_policy_digest:
            raise RelayError(ErrorCode.POLICY_REFUSED)
        args = self.spec.spec.get('app_server_args', [])
        if not isinstance(args, (list, tuple)) or len(args) % 2 or any(
                args[index] != '-c' or type(args[index + 1]) is not str for index in range(0, len(args), 2)):
            raise RelayError(ErrorCode.UNSUPPORTED)
        # Only the provider launch compiler supplies overrides, never a native client.
        args = [*args, '-c', 'features.hooks=true',
            '-c','sandbox_workspace_write.exclude_slash_tmp=true',
            '-c','sandbox_workspace_write.exclude_tmpdir_env_var=true']
        marker = os.open('started', os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        os.write(marker, self.spec.run_id.encode())
        os.fsync(marker)
        os.close(marker)
        os.fsync(self.fd)
        from .relay_enclosure import Enclosure
        self.enclosure = Enclosure(self.root, self.spec, executable)
        self.process = await asyncio.create_subprocess_exec(
            str(self.enclosure.cli), 'app-server', '--listen', 'stdio://', *args, cwd=self.spec.binding.workspace,
            env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, start_new_session=True, pass_fds=(self.lock_fd,), limit=(1 << 20) + 1)
        from acc.session_relay.kernel_lifetime import KernelLifetime
        self.kernel_lifetime = KernelLifetime(self.process.pid)
        started = self.kernel_lifetime.started
        self.token = f'{self.process.pid}:{started}:{self.token}'
        return self.process

    def native_exit_status(self):
        if hasattr(self, '_native_status'):
            return self._native_status
        return self.enclosure.native_status(self.token) if self.enclosure is not None else None

    async def stop(self):
        if self.closed:
            return self.exit_proven
        if self.process is not None and self.process.returncode is None:
            try:
                self.process.send_signal(signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                try:
                    self.process.kill()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 1)
                except asyncio.TimeoutError:
                    pass  # inherited pipes/tree still live: no exit receipt
        # Root death alone never proves an empty adopted descendant tree.
        if self.enclosure is not None:
            self.exit_proven = self.enclosure.proof('exit.json', self.token)
            self._native_status = self.enclosure.native_status(self.token)
            self.enclosure.close()
        # A child may retain the inherited lock: a later start must acquire it anew.
        os.close(self.lock_fd)
        os.close(self.fd)
        if self.kernel_lifetime is not None:
            self.kernel_lifetime.close()
        self.closed = True
        return self.exit_proven


def verify_socket(path: Path, parent_fd: int, writer):
    """Only broker-generated socket names beneath the pinned run directory."""
    info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
    if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
        raise RelayError(ErrorCode.INTEGRITY_FAILURE)
    if peercred.peer_uid(writer.get_extra_info('socket')) != os.getuid():
        raise RelayError(ErrorCode.NOT_FOUND_OR_DENIED)


class RuntimeTransport:
    """Private broker channel; A supplies authenticated enrollment/grant resolution.

    No callback is inferred from native messages or paths. Until main wires these
    callbacks the broker refuses this channel. EOF invalidates the enrollment;
    old channels cannot inherit a new enrollment. No transport mutation retry.
    """
    def __init__(self, broker: AdmissionBroker, enroll_authority, resolve_grant, dispatch, read_receipt):
        self.broker = broker
        self.enroll_authority, self.resolve_grant = enroll_authority, resolve_grant
        self.dispatch_operation, self.read_receipt = dispatch, read_receipt

    async def serve(self, request_id, params, reader, writer):
        from acc.broker_protocol import runtime_request_from
        from acc.session_relay.protocol import to_wire
        link_id = uuid.uuid4().hex
        enrolled = False
        try:
            # The main's enrollment verifier checks installation, account, node,
            # session and broker incarnation against current enrolled authority.
            fence = await self.enroll_authority(params, self.broker.incarnation, link_id)
            await self.broker.enroll(fence, link_id)
            enrolled = True
            writer.write(json.dumps({'id': request_id, 'ok': True, 'result': {'version': 1}}).encode() + b'\n')
            await writer.drain()
            # One reader drains the channel independently of grant verification.
            # EOF invalidates queued, not-yet-admitted work even while a callback
            # awaits main. Buffered old frames never inherit a new enrollment.
            queue = asyncio.Queue(maxsize=16)
            async def receive():
                try:
                    while line := await reader.readline():
                        await queue.put(runtime_request_from(line))
                finally:
                    if self.broker.link == (fence, link_id):
                        self.broker.link = None
                    try:
                        queue.put_nowait(None)
                    except asyncio.QueueFull:
                        pass  # a consumer observes the invalidated link before admission
            receiver = asyncio.create_task(receive())
            try:
                while (request := await queue.get()) is not None:
                    if self.broker.link != (fence, link_id):
                        raise RelayError(ErrorCode.STALE_AUTHORITY)
                    if request['method'] == 'dispatch':
                        operation = await self.resolve_grant(request['params']['grant'], fence, link_id)
                        result = await self.broker.dispatch(operation, self.dispatch_operation)
                    else:
                        result = await self.read_receipt(request['params']['operation_id'], fence, link_id)
                    if self.broker.link != (fence, link_id):
                        raise RelayError(ErrorCode.STALE_AUTHORITY)
                    writer.write(json.dumps({'id': request['id'], 'ok': True, 'result': to_wire(result)}).encode() + b'\n')
                    await writer.drain()
            finally:
                receiver.cancel()
                await asyncio.gather(receiver, return_exceptions=True)
        except (RelayError, ValueError):
            writer.write(json.dumps({'id': request_id, 'ok': False, 'error': 'Session runtime refused.'}).encode() + b'\n')
            await writer.drain()
        finally:
            if enrolled:
                await self.broker.disconnect(link_id)
            writer.close()


class CodexRuntimeOwner:
    """Adapter-facing stdio owner created only by the broker's reviewed factory."""
    def __init__(self, runtime: PrivateRuntime, rpc):
        from acc.session_relay.adapters.base import RuntimeAttestation
        self.runtime, self.rpc = runtime, rpc
        self.turn_attempted = False
        self.attestation = RuntimeAttestation(runtime.spec.binding.binding_id, runtime.spec.run_id,
                                             runtime.spec.binding.native_conversation, runtime.token)

    @property
    def alive(self):
        return self.runtime.process is not None and self.runtime.process.returncode is None and not self.runtime.closed

    async def request(self, method, params):
        self.runtime.validate()
        if method == 'turn/start':
            self.turn_attempted = True
        return await self.rpc.request(method, params, before_write=self.validate_before_write)

    async def request_guarded(self, method, params, before_write):
        async def current():
            self.runtime.validate()
            result = await before_write()
            self.runtime.validate()
            return result
        self.runtime.validate()
        if method == 'turn/start':
            self.turn_attempted = True
        return await self.rpc.request(method, params, before_write=current)

    async def respond_guarded(self, request_id, result, before_write):
        async def current():
            self.runtime.validate()
            allowed = await before_write()
            self.runtime.validate()
            return allowed
        self.runtime.validate()
        await self.respond(request_id, result, before_write=current)

    async def notify(self, method, params):
        self.runtime.validate()
        await self.rpc.notify(method, params, before_write=self.validate_before_write)

    async def validate_before_write(self):
        self.runtime.validate()
        return True

    async def respond(self, request_id, result, *, before_write=None):
        self.runtime.validate()
        async def guarded_write():
            if before_write is not None and await before_write() is not True:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            self.runtime.validate()
            if not self.alive:
                raise RelayError(ErrorCode.STALE_AUTHORITY)
            return True
        await self.rpc.respond(request_id, result, before_write=guarded_write)

    async def stop(self):
        await self.rpc.close()
        await self.runtime.stop()

    @classmethod
    async def start(cls, root, spec, executable, env, observe, *, lost=lambda: None):
        from acc.session_relay.adapters.codex import StdioRPC
        runtime = PrivateRuntime(root, spec)
        try:
            process = await runtime.start(executable, env)
        except BaseException:
            await runtime.stop()
            raise
        return cls(runtime, StdioRPC(process.stdout, process.stdin, observe, lost=lost))


class PayloadStaging:
    """Private exact outbound bytes, scoped to a run and hub operation digest.

    No user path, content logging, URL fetch or display redaction. The hub stores
    only the generated reference; deletion requires a settled receipt. Unresolved
    payloads count against capacity and cannot be evicted to admit another turn.
    """
    def __init__(self, root: Path, *, capacity_bytes=256 << 20):
        self.root, self.capacity_bytes = root, capacity_bytes
        self.fd = private_dir(root)
        self.identity = os.fstat(self.fd)

    def _check(self):
        info = self.root.lstat()
        if (info.st_dev, info.st_ino) != (self.identity.st_dev, self.identity.st_ino):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)

    def stage(self, operation_id: str, data: bytes):
        from acc.session_relay.protocol import MAX_PROMPT_BYTES, sha256_digest
        self._check()
        if type(data) is not bytes or not data or len(data) > MAX_PROMPT_BYTES:
            raise RelayError(ErrorCode.LIMIT)
        # One lock across all writers; no quota check/write race between brokers.
        lock = os.open('staging.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX)
            total = sum(os.stat(name, dir_fd=self.fd, follow_symlinks=False).st_size
                        for name in os.listdir(self.fd) if name.endswith('.payload'))
            if total + len(data) > self.capacity_bytes:
                raise RelayError(ErrorCode.LIMIT)
            name = hashlib.sha256(operation_id.encode()).hexdigest() + '.payload'
            fd = os.open(name, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=self.fd)
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(fd, remaining)
                    if not written:
                        raise OSError('payload write failed')
                    remaining = remaining[written:]
                os.fsync(fd)
            except BaseException:
                os.unlink(name, dir_fd=self.fd)
                raise
            finally:
                os.close(fd)
            os.fsync(self.fd)
            return name, sha256_digest(data)
        finally:
            os.close(lock)

    def read(self, reference: str, digest: str):
        import re

        from acc.session_relay.protocol import MAX_PROMPT_BYTES, sha256_digest
        self._check()
        if not re.fullmatch(r'[0-9a-f]{64}\.payload', reference):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        fd = os.open(reference, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=self.fd)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1:
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            data = os.read(fd, MAX_PROMPT_BYTES + 1)
            if len(data) > MAX_PROMPT_BYTES or sha256_digest(data) != digest:
                raise RelayError(ErrorCode.INTEGRITY_FAILURE)
            return data
        finally:
            os.close(fd)

    def remove(self, reference: str, *, settled: bool):
        if not settled:
            raise RelayError(ErrorCode.BUSY)  # deletion cannot stand in for recovery
        import re
        self._check()
        if not re.fullmatch(r'[0-9a-f]{64}\.payload', reference):
            raise RelayError(ErrorCode.INTEGRITY_FAILURE)
        os.unlink(reference, dir_fd=self.fd)
        os.fsync(self.fd)

    def close(self):
        os.close(self.fd)
