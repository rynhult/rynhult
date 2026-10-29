"""Public edge identity: per-request checks, open-connection closure, and machine identity lifecycle.

The root edge helper sets X-Acc-Edge-Client to the verified client leaf's SHA-256 on every public request (it
overwrites any client-supplied value). Browser/machine assertions only restrict access. Native possession
additionally requires a separately provisioned secret assertion, Unix-proxy ingress and the public hostname; a TCP header grants no proof. Each public request and stream is checked
against the registry, and open streams are closed when their identity stops being valid (revocation, node
removal, expiry, retirement after rotation) — at once on those actions, else by the 15-second sweep.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import secrets
import stat
import threading
import time
from datetime import timedelta

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from sqlalchemy import or_, select, update

from . import auth, devices
from .db import MaintenanceBusy, write_intent
from .models import AuditLog, Device, Node, Project, Setting, User

log = logging.getLogger(__name__)
HEADER = b'x-acc-edge-client'
FINGERPRINT = re.compile(r'[0-9a-f]{64}')
MACHINE_OU = 'acc-machine'
MACHINE_DAYS = 30
ROTATE_BEFORE = timedelta(days=10)  # request a successor once a third of the life remains
OVERLAP = timedelta(hours=24)  # the superseded leaf is retired once the node presents its successor, or after this
AUTHORITY_SECONDS = 60  # how long accd trusts its last answer from the signer about the current authority
ROLLOVER_PER_HOUR = 10  # machines asked to leave a previous authority per hour: half the signer's shared 20,
                        # so people can still pair and renew devices during a rollover
ROLLOVER_RETRY = 300  # seconds before a connected machine held back by that share is considered again
GIT = re.compile(r'^/api/git/([0-9]{1,12})\.git(/.*)?$')
# Machine identities reach only these; browser identities never do. The edge enforces the same split.
MACHINE_PATHS = ('/api/nodes/connect', '/api/nodes/bundle')
PUBLIC_DENIED = ('/pair-device', '/api/device-pairing/', '/api/devices/pairings', '/api/nodes', '/api/git',
                 '/api/preview', '/preview', '/install.sh', '/bundle', '/pair-mobile',
                 '/api/mobile/v1/pairings', '/api/mobile/v1/claim', '/api/mobile/v1/credential')


def native_edge_key(ctx):
    """Separate edge proof: private proxy paths may forward a client's forged fingerprint/Host.

    Root provisions the same random key in its edge policy; never return it through review or APIs.
    No-follow, bounded, regular and private: a missing/unreadable/unsafe key always refuses native proof.
    """
    path = ctx.settings.data_dir / 'secrets' / 'mobile-edge-key'
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or info.st_size not in (64, 65)):
                return None
            raw = os.read(fd, 66).strip()
            return raw if re.fullmatch(b'[0-9a-f]{64}', raw) else None
        finally:
            os.close(fd)
    except OSError:
        return None


def enabled(s) -> bool:
    row = s.get(Setting, 'remote_access')
    return bool(row and (row.value or {}).get('machine_identities'))


def public_on(s) -> bool:
    """The owner wants public routing. Turn off clears this before the edge confirms, and Caddy 2.6 keeps streaming
    a response that began before OFF (it only closes upgraded WebSockets), so accd ends public use itself."""
    row = s.get(Setting, 'remote_access_runtime')
    return bool(row and (row.value or {}).get('desired_enabled') is True)


def previous_authority(row: Device, authority: str | None) -> bool:
    """Issued before a rollover: not signed by the signer's current authority. Unknown authority: never."""
    if not authority:
        return False
    try:
        x509.load_pem_x509_certificate(row.certificate_pem.encode()).verify_directly_issued_by(
            x509.load_pem_x509_certificate(authority.encode()))
        return False
    except Exception:
        return True


def usable(row: Device | None, now=None) -> bool:
    now = now or auth.now()
    return (row is not None and row.status == 'active' and row.expires_at > now
            and (row.retire_after is None or row.retire_after > now))


def active_leaves(s, mobile_enabled=False):
    """Every leaf the public gate admits: current people's browser devices and, while machine identities are on,
    active nodes' machine identities."""
    now = auth.now()
    kinds = (Device.kind == 'browser') & Device.person.in_(select(User.username))
    if mobile_enabled:
        kinds = or_(kinds, (Device.kind == 'native') & Device.person.in_(select(User.username)))
    if enabled(s):
        kinds = or_(kinds, (Device.kind == 'node') & Device.node_id.in_(select(Node.id).where(Node.status == 'active')))
    return list(s.scalars(select(Device).where(
        Device.status == 'active', Device.expires_at > now,
        or_(Device.retire_after.is_(None), Device.retire_after > now), kinds).order_by(Device.id)))


def admitted(s, fingerprint: str, path: str, mobile_enabled=False) -> Device | None:
    """The registry row a public request may proceed as, or None."""
    if not FINGERPRINT.fullmatch(fingerprint) or not public_on(s):
        return None
    row = s.scalar(select(Device).where(Device.fingerprint == fingerprint))
    if not usable(row):
        return None
    if row.kind == 'node':
        node = s.get(Node, row.node_id)
        if node is None or node.status != 'active' or not enabled(s):
            return None
        git = GIT.match(path)
        if git:
            # A machine pushes backups only for projects that live on it.
            project = s.get(Project, int(git.group(1)))
            return row if (node.public_git_backup is True and project is not None
                           and project.git_backup and project.host == node.name) else None
        return row if path in MACHINE_PATHS else None
    if row.kind == 'native' and (not mobile_enabled or path.startswith('/api/auth/oidc/')):
        return None
    if row.kind not in ('browser', 'native') or s.scalar(select(User.id).where(User.username == row.person)) is None:
        return None
    if path.startswith('/api/mobile/v1/devices/') and path.endswith('/rotate'):
        return None
    return None if any(path == p or path.startswith(p) for p in PUBLIC_DENIED) else row


class Connection:
    def __init__(self, fingerprint: str, path: str | None = None):
        self.path = path
        self.fingerprint = fingerprint
        self.closed = asyncio.Event()
        self.native_key_hash = None


class Edge:
    """Open public connections by client leaf, so revocation can end established streams."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.open: set[Connection] = set()
        # Turn off pressed in this process: refuse and close public use without waiting for the remote access lock,
        # which a reconciliation can hold on a slow helper for minutes. Cleared by the owner's next enable.
        self.halted = False
        self._authority: str | None = None
        self._authority_at = 0.0
        self._authority_lock = threading.Lock()
        self._rollover_lock = threading.Lock()

    def authority(self) -> str | None:
        """Current public authority, or None. Blocking: use a thread, without an open DB transaction.

        Refresh records an audit through writer admission, so a caller's read transaction would deadlock it.
        """
        with self._authority_lock:
            now = time.monotonic()
            if self._authority_at and now - self._authority_at < AUTHORITY_SECONDS:
                return self._authority
            try:
                answer = devices.sign({'operation': 'authority'})
                pem = answer['certificate']
                x509.load_pem_x509_certificate(pem.encode())
            except Exception:
                answer, pem = None, None
            if answer is not None:
                try:
                    write_intent(self._record_authority)(answer)
                except Exception:
                    log.warning('Could not record the device authority in the audit log', exc_info=True)
            self._authority, self._authority_at = pem, now
            return pem

    def _record_authority(self, answer: dict):
        """Audit what the signer reports changing: its authority, the previous key's deletion at promote, and the
        previous authority's retirement at finish. The last answer is kept, so a restart doesn't repeat entries."""
        current = devices.fingerprint(x509.load_pem_x509_certificate(answer['certificate'].encode()))
        previous = answer.get('previous') if FINGERPRINT.fullmatch(str(answer.get('previous'))) else None
        key_deleted = previous is not None and answer.get('previous_key') is False
        with self.ctx.db() as s:
            row = s.get(Setting, 'device_ca_authority')
            known = dict(row.value) if row else None
            seen = {'fingerprint': current, 'previous': previous, 'previous_key_deleted': key_deleted}
            if known == seen:
                return
            if known is not None:
                if known.get('fingerprint') != current:
                    s.add(AuditLog(actor='system', action='device_ca.authority_changed', target='device_ca',
                                   detail={'from': known.get('fingerprint'), 'to': current,
                                           'planned': previous is not None and previous == known.get('fingerprint')}))
                if key_deleted and not (known.get('previous') == previous and known.get('previous_key_deleted')):
                    s.add(AuditLog(actor='system', action='device_ca.previous_key_deleted', target='device_ca',
                                   detail={'previous': previous}))
                if known.get('previous') and previous is None and known.get('fingerprint') == current:
                    s.add(AuditLog(actor='system', action='device_ca.previous_retired', target='device_ca',
                                   detail={'previous': known['previous']}))
            s.merge(Setting(key='device_ca_authority', value=seen, updated_by='system'))
            s.commit()

    def step(self, node_id: str, presented: str | None) -> tuple[str | None, str | None]:
        """What to tell a connected machine: request a successor, switch to one, wait for this hour's rollover share,
        or nothing. Blocking: use a thread."""
        authority = self.authority()
        with self.ctx.db() as s:
            reason = due(s, node_id, presented, authority)
            leaves = leaves_of(s, node_id)
            newest = leaves[-1].fingerprint if leaves else None
        if reason == 'rollover' and not write_intent(self.rollover_ask)(node_id):
            return 'wait', None
        if reason:
            return 'request', None
        return ('switch', newest) if newest and newest != presented else (None, None)

    def rollover_ask(self, node_id: str) -> bool:
        """Take one of this hour's rollover requests for node_id (recorded, so restarts count it). False when the
        hour's share is used. Asking the same machine again within the hour reuses its share."""
        with self._rollover_lock, self.ctx.db() as s:
            asked = rollover_requests(s)
            if node_id in asked:
                return True
            if len(asked) >= ROLLOVER_PER_HOUR:
                return False
            node = s.get(Node, node_id)
            s.add(AuditLog(actor='system', action='machine_identity.rollover_request', target=node.name if node else node_id,
                           detail={'node_id': node_id}))
            s.commit()
            return True

    def forget_authority(self):
        self._authority_at = 0.0

    def rollover(self, s, authority: str | None) -> dict:
        """Format owner status from a pre-fetched authority; never refresh/audit inside the read snapshot."""
        fingerprint_ = devices.fingerprint(x509.load_pem_x509_certificate(authority.encode())) if authority else None
        return {'authority': fingerprint_, 'previous_identities': sum(
            previous_authority(row, authority) for row in active_leaves(s, self.ctx.settings.mobile_enabled))}

    def check(self, fingerprint: str, path: str) -> dict | None:
        from .admin_features import enabled as feature_enabled
        if self.halted or not feature_enabled(self.ctx.settings, 'remote_access'):
            return None
        with self.ctx.db() as s:
            row = admitted(s, fingerprint, path, self.ctx.settings.mobile_enabled)
            return {'kind': row.kind, 'public_hostname': devices.settings(s)['public_hostname']} if row else None

    async def close_node_git(self, node_id: str) -> int:
        """Consent withdrawal closes git streams only, never this machine's control/update link."""
        def fingerprints():
            with self.ctx.db() as s:
                return set(s.scalars(select(Device.fingerprint).where(Device.node_id == node_id)))
        leaves = await asyncio.to_thread(fingerprints)
        closing = [c for c in self.open if c.fingerprint in leaves and c.path and GIT.match(c.path)]
        for connection in closing:
            connection.closed.set()
        return len(closing)

    async def sweep(self) -> int:
        """Close every open public connection whose identity is no longer usable. Returns how many were closed."""
        connections = list(self.open)
        if not connections:
            return 0
        fingerprints = {c.fingerprint for c in connections}

        def valid():
            from .admin_features import enabled as feature_enabled
            if self.halted or not feature_enabled(self.ctx.settings, 'remote_access'):
                return set()
            with self.ctx.db() as s:
                if not public_on(s):
                    return set()
                rows = s.scalars(select(Device).where(Device.fingerprint.in_(fingerprints)))
                good = set()
                for row in rows:
                    if not usable(row):
                        continue
                    if row.kind == 'node':
                        node = s.get(Node, row.node_id)
                        if node is None or node.status != 'active' or not enabled(s):
                            continue
                    elif row.kind == 'native' and not self.ctx.settings.mobile_enabled:
                        continue
                    elif s.scalar(select(User.id).where(User.username == row.person)) is None:
                        continue
                    good.add(row.fingerprint)
                return {c for c in connections if c.fingerprint in good and
                        (c.path is None or admitted(s, c.fingerprint, c.path, self.ctx.settings.mobile_enabled) is not None)}
        good = await asyncio.to_thread(valid)
        key = await asyncio.to_thread(native_edge_key, self.ctx) if any(c.native_key_hash for c in connections) else None
        current_key_hash = auth.digest(key.decode()) if key else None
        closing = [c for c in connections if c not in good
                   or (c.native_key_hash and c.native_key_hash != current_key_hash)]
        for connection in closing:
            connection.closed.set()
        return len(closing)


class EdgeGuard:
    """Pure ASGI: public requests are admitted per request, and their streams end when the identity does."""

    def __init__(self, app, ctx):
        self.app, self.ctx = app, ctx

    async def __call__(self, scope, receive, send):
        if scope['type'] not in ('http', 'websocket'):
            return await self.app(scope, receive, send)
        values = [v for k, v in scope.get('headers', ()) if k == HEADER]
        if not values:
            return await self.app(scope, receive, send)
        fingerprint = values[0].decode('latin-1').strip().lower() if len(values) == 1 else ''
        edge = self.ctx.edge
        connection = Connection(fingerprint, scope['path'])
        # Registered before the check: a revocation whose sweep runs while the check is in flight still reaches it.
        edge.open.add(connection)
        try:
            await self.guarded(scope, receive, send, edge, connection)
        finally:
            edge.open.discard(connection)

    async def guarded(self, scope, receive, send, edge, connection):
        fingerprint = connection.fingerprint
        try:
            admission = await asyncio.to_thread(edge.check, fingerprint, scope['path'])
        except MaintenanceBusy:
            # Admission runs outside FastAPI's exception handlers. Never turn
            # temporary inability to verify identity into admission or a 500.
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 1013})
            from starlette.responses import JSONResponse
            return await JSONResponse({'detail': 'Rynhult is busy with a backup or maintenance task. Try again shortly.'},
                                      status_code=503, headers={'Retry-After': '1'})(scope, receive, send)
        if not admission or connection.closed.is_set():
            if scope['type'] == 'websocket':
                return await send({'type': 'websocket.close', 'code': 4403})
            body = b'{"detail":"This device or machine is not allowed here."}'
            await send({'type': 'http.response.start', 'status': 403,
                        'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
            return await send({'type': 'http.response.body', 'body': body})
        is_native, public_host = admission['kind'] == 'native', admission['public_hostname']
        if is_native:
            key = await asyncio.to_thread(native_edge_key, self.ctx)
            assertions = [v for k, v in scope.get('headers', ()) if k == b'x-acc-native-edge']
            trusted = key is not None and len(assertions) == 1 and secrets.compare_digest(assertions[0], key)
            hosts = [v.decode('latin-1').lower().rstrip('.') for k, v in scope.get('headers', ()) if k == b'host']
            if not trusted or scope.get('client') is not None or hosts != [public_host]:
                if scope['type'] == 'websocket':
                    return await send({'type': 'websocket.close', 'code': 4403})
                body = b'{"detail":"Native identity requires the trusted public edge."}'
                await send({'type': 'http.response.start', 'status': 403,
                            'headers': [(b'content-type', b'application/json'), (b'content-length', str(len(body)).encode())]})
                return await send({'type': 'http.response.body', 'body': body})
            connection.native_key_hash = auth.digest(key.decode())
        # The assertion is consumed here, never forwarded to a project/node upstream or exposed to a handler.
        scope = {**scope, 'headers': [(k, v) for k, v in scope.get('headers', ()) if k != b'x-acc-native-edge']}
        scope.setdefault('state', {})['edge_native'] = is_native
        scope.setdefault('state', {})['edge_fingerprint'] = fingerprint
        closed_sent = False

        async def guarded_receive():
            nonlocal closed_sent
            if not connection.closed.is_set():
                message = asyncio.ensure_future(receive())
                waiter = asyncio.ensure_future(connection.closed.wait())
                try:
                    done, _ = await asyncio.wait({message, waiter}, return_when=asyncio.FIRST_COMPLETED)
                    # Both tasks may complete in one loop turn. Once invalidation
                    # finished, never deliver a newly ready body/frame to the handler.
                    if message in done and not connection.closed.is_set():
                        return message.result()
                finally:
                    for child in (message, waiter):
                        if not child.done():
                            child.cancel()
                    await asyncio.gather(message, waiter, return_exceptions=True)
            if scope['type'] == 'websocket':
                if not closed_sent:
                    closed_sent = True
                    try:
                        await send({'type': 'websocket.close', 'code': 4403})
                    except Exception:
                        pass
                return {'type': 'websocket.disconnect', 'code': 4403}
            return {'type': 'http.disconnect'}

        async def guarded_send(message):
            if connection.closed.is_set():
                raise OSError('edge identity is no longer valid')
            await send(message)

        await self.app(scope, guarded_receive, guarded_send)


# Machine identity lifecycle over the node's signed link.

def leaves_of(s, node_id):
    return [r for r in s.scalars(select(Device).where(Device.kind == 'node', Device.node_id == node_id,
                                                       Device.status == 'active').order_by(Device.created_at))
            if usable(r)]


def rollover_requests(s) -> set[str]:
    """Machines asked within the last hour to leave a previous authority."""
    rows = s.scalars(select(AuditLog).where(AuditLog.action == 'machine_identity.rollover_request',
                                            AuditLog.ts > auth.now() - timedelta(hours=1)))
    return {(row.detail or {}).get('node_id') for row in rows}


def needs_identity(s, node_id, presented: str | None, authority: str | None = None) -> bool:
    """A successor is due: none yet, a third of its life left, or issued before an authority rollover."""
    return due(s, node_id, presented, authority) is not None


def due(s, node_id, presented: str | None, authority: str | None = None) -> str | None:
    """Why a successor is due ('expiry' covers a first leaf and a lost successor too; 'rollover' is capped per
    hour by Edge.rollover_ask), or None."""
    if not enabled(s):
        return None
    leaves = leaves_of(s, node_id)
    if not leaves:
        return 'expiry'
    newest = leaves[-1]
    if newest.fingerprint != presented:
        # A successor is issued but not yet in use: wait for its switch, unless the node no longer holds it.
        lost = presented is not None and newest.created_at < auth.now() - timedelta(hours=1)
        return 'expiry' if lost else None
    if newest.expires_at - auth.now() < ROTATE_BEFORE:
        return 'expiry'
    return 'rollover' if previous_authority(newest, authority) else None


def check_machine(pem: str, node_id: str, csr_pem: str):
    """Validate privileged signer output before it enters the registry."""
    cert = x509.load_pem_x509_certificate(pem.encode())
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    spki = serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    if (cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value != node_id
            or [a.value for a in cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)] != [MACHINE_OU]
            or cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
            or set(cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value) != {ExtendedKeyUsageOID.CLIENT_AUTH}
            or cert.public_key().public_bytes(*spki) != csr.public_key().public_bytes(*spki)
            or cert.not_valid_after_utc > auth.now() + timedelta(days=MACHINE_DAYS, minutes=1)
            or cert.not_valid_after_utc <= auth.now()):
        raise ValueError('invalid machine identity')
    return cert


def issue(ctx, node_id: str, csr_pem: str, presented: str | None) -> str:
    """Sign a node-held key (blocking: call in a thread). Earlier leaves get a bounded retirement deadline.

    Only when the main would ask for one: an unsolicited CSR (or one per reconnect) never spends the signer's
    shared hourly budget, so a node gets at most one new leaf an hour while its successor is unused."""
    authority = ctx.edge.authority()
    with ctx.db() as s:
        node = s.get(Node, node_id)
        reason = due(s, node_id, presented, authority) if node is not None and node.status == 'active' else None
        # A rollover successor only for a machine asked within this hour's share: an unsolicited CSR can't spend it.
        if reason is None or (reason == 'rollover' and node_id not in rollover_requests(s)):
            raise ValueError('machine identities are off, not due, or the node is not enrolled')
    result = devices.sign({'operation': 'issue_machine', 'node_id': node_id, 'csr': csr_pem, 'days': MACHINE_DAYS})
    cert = check_machine(result['certificate'], node_id, csr_pem)
    return _register_machine(ctx, node_id, result['certificate'], cert, reason)


@write_intent
def _register_machine(ctx, node_id, pem, cert, reason):
    # Reserve only the registry transaction, never the privileged signer call.
    with ctx.db() as s:
        node = s.get(Node, node_id)
        if node is None or node.status != 'active':  # removal wins a race with issuance
            raise ValueError('the node was removed')
        deadline = auth.now() + OVERLAP
        for old in leaves_of(s, node_id):
            old.retire_after = min(old.retire_after or deadline, deadline)
        row = Device(id=secrets.token_hex(16), person='', label=node.name, kind='node', node_id=node_id, status='active',
                     certificate_pem=pem, fingerprint=devices.fingerprint(cert),
                     serial=str(cert.serial_number), expires_at=cert.not_valid_after_utc)
        s.add(row)
        s.add(AuditLog(actor=f'node:{node.name}', action='machine_identity.issue', target=node.name,
                       detail={'fingerprint': row.fingerprint, 'expires_at': row.expires_at.isoformat(), 'reason': reason}))
        s.commit()
        return pem


@write_intent
def switched(ctx, node_id: str, presented: str | None) -> bool:
    """The node now presents `presented`: retire its older leaves. True when the registry changed."""
    with ctx.db() as s:
        leaves = leaves_of(s, node_id)
        current = next((r for r in leaves if r.fingerprint == presented), None)
        if current is None:
            return False
        older = [r for r in leaves if r.created_at < current.created_at]
        for row in older:
            row.status = 'retired'
        current.last_seen_at = auth.now()
        if older:
            s.add(AuditLog(actor=f'node:{current.label}', action='machine_identity.rotate', target=current.label,
                           detail={'fingerprint': current.fingerprint, 'retired': [r.fingerprint for r in older]}))
        s.commit()
        return bool(older)


def retire_overdue(s) -> int:
    rows = list(s.scalars(select(Device).where(Device.kind == 'node', Device.status == 'active',
                                               Device.retire_after.is_not(None), Device.retire_after <= auth.now())))
    for row in rows:
        row.status = 'retired'
    return len(rows)


def revoke_node(s, node_id: str) -> int:
    return s.execute(update(Device).where(Device.kind == 'node', Device.node_id == node_id, Device.status == 'active')
                     .values(status='revoked')).rowcount


def ready(ctx, fingerprint: str) -> bool:
    """Whether the node may switch to this leaf: public access is off, or the verified policy already admits it."""
    from acc.remote_access_helper import revision

    from .remote_access import state
    with ctx.db() as s:
        value = state(s)
    if not value['desired_enabled'] and not value['enabled']:
        return True
    payload = ctx.remote_access.payload()
    with ctx.db() as s:
        row = s.scalar(select(Device).where(Device.fingerprint == fingerprint))
        pem = row.certificate_pem if usable(row) else None
    return (pem is not None and pem in payload['leaves'] and value['enabled'] is True and not value['pending']
            and value['registry_revision'] == revision(payload))


def summary(s, node_id: str, authority: str | None = None) -> dict:
    """What the Machines page shows: whether identities are on, and this machine's newest usable leaf."""
    leaves = leaves_of(s, node_id)
    if not leaves:
        return {'enabled': enabled(s), 'issued': False}
    newest = leaves[-1]
    return {'enabled': enabled(s), 'issued': True, 'fingerprint': newest.fingerprint,
            'previous_authority': previous_authority(newest, authority),
            'expires_at': newest.expires_at.isoformat(), 'rotating': len(leaves) > 1,
            'last_presented_at': newest.last_seen_at.isoformat() if newest.last_seen_at else None}


def message(kind: str, **fields) -> str:
    return json.dumps({'t': kind, **fields})
