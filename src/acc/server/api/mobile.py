"""Versioned, off-by-default native lifecycle; LAN enrollment plus ordinary person sign-in."""
import asyncio
import functools
import inspect
import logging
import secrets
import threading
import time
from datetime import timedelta
from typing import Literal
from urllib.parse import urlsplit

from cryptography.exceptions import InvalidSignature
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from acc.remote_access_helper import revision

from .. import auth, devices, edge, mobile, passkeys
from ..models import AuditLog, AuthSession, Device, DevicePairing, PushSubscription, User
from .context import Context
from .devices import PRIVATE, ConfirmIn, LabelIn, ReceiptIn, TokenIn

PREFIX = '/api/mobile/v1'
log = logging.getLogger(__name__)


class BeginIn(LabelIn):
    person: str = Field(min_length=1, max_length=64)


class Refusals:
    """Refused attempts are audited, but at most 10 per client and 100 in all per ten minutes: probing stays
    visible, a flood can't fill the audit log."""
    def __init__(self, per_client=10, total=100, window=600):
        self.per_client, self.total, self.window = per_client, total, window
        self.lock, self.started, self.counts = threading.Lock(), 0.0, {}

    def allow(self, client):
        with self.lock:
            now = time.monotonic()
            if now - self.started >= self.window:
                self.started, self.counts = now, {}
            if sum(self.counts.values()) >= self.total or self.counts.get(client, 0) >= self.per_client:
                return False
            self.counts[client] = self.counts.get(client, 0) + 1
            return True


class ClaimIn(TokenIn):
    installation_id: str = Field(pattern=r'^[0-9a-f]{32}$')
    platform: Literal['ios', 'android']
    csr: str = Field(min_length=1, max_length=2048)
    proof: str = Field(min_length=64, max_length=128, pattern=r'^[A-Za-z0-9_-]+$')


def router(ctx: Context):
    def enabled(response: Response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        if not ctx.settings.mobile_enabled:
            raise HTTPException(404, 'Not found')

    api = APIRouter(prefix=PREFIX, dependencies=[Depends(enabled)])
    # Separate buckets: an unauthenticated receiver can't use up an owner's attempts, and an owner's successful
    # steps never count against them.
    device_bucket = auth.LoginThrottle(limit=10, window=600)  # every claim; credential with an unknown receipt
    bind_bucket = auth.LoginThrottle(limit=10, window=600)    # session binding: every attempt
    owner_bucket = auth.LoginThrottle(limit=10, window=600)   # start, rotate, confirm: failures only
    throttle_lock = threading.Lock()
    refusals = Refusals()

    def keys(request, person=None):
        return ['ip:' + ctx.client_key(request)] + (['person:' + person] if person else [])

    def limit(request, person=None, *, bucket, count=True):
        with throttle_lock:
            if any(bucket.blocked(k) for k in keys(request, person)):
                raise HTTPException(429, 'Too many mobile app pairing attempts. Wait ten minutes and try again.')
            if count:
                for key in keys(request, person):
                    bucket.fail(key)

    def record_refusal(request, who, operation, error, target=None):
        """One bounded audit row: operation, status and the fixed reason, never tokens, codes or keys."""
        client = ctx.client_key(request)
        if not refusals.allow(client):
            return
        try:
            with ctx.db() as s:
                s.add(AuditLog(actor=who.username if who else 'anonymous', action='mobile.refused', target=target,
                               detail={'operation': operation, 'status': error.status_code,
                                       'reason': str(error.detail)[:120], 'client': client}))
                s.commit()
        except Exception:  # never replace the refusal itself with an audit failure
            log.warning('mobile refusal audit for %s skipped', operation)

    def audited(operation, *, owner_failures=False):
        """Audit every refusal from the route; owner actions also count only their failures toward the limit."""
        def wrap(handler):
            def refused(error, kwargs):
                request, who = kwargs['request'], kwargs.get('who')
                if owner_failures and error.status_code != 429:
                    with throttle_lock:
                        for key in keys(request, who.username if who else None):
                            owner_bucket.fail(key)
                record_refusal(request, who, operation, error, kwargs.get('id'))

            if inspect.iscoroutinefunction(handler):
                @functools.wraps(handler)
                async def run(*args, **kwargs):
                    try:
                        return await handler(*args, **kwargs)
                    except HTTPException as error:
                        await asyncio.to_thread(refused, error, kwargs)
                        raise
            else:
                @functools.wraps(handler)
                def run(*args, **kwargs):
                    try:
                        return handler(*args, **kwargs)
                    except HTTPException as error:
                        refused(error, kwargs)
                        raise
            return run
        return wrap

    def human(request: Request, who=Depends(ctx.principal)):
        if who.user_id is None:
            error = HTTPException(403, 'Sign in to Rynhult as yourself to do this; API tokens can\'t.')
            record_refusal(request, who, request.url.path.removeprefix(PREFIX), error)
            raise error
        return who

    def owner(request: Request, who=Depends(human)):
        if not who.is_owner:
            error = HTTPException(403, 'Only an owner can manage mobile app devices.')
            record_refusal(request, who, request.url.path.removeprefix(PREFIX), error)
            raise error
        return who

    def private(request, s, *, pairing=True):
        import ipaddress
        config = devices.settings(s)
        try:
            ip = ipaddress.ip_address(ctx.client_key(request).split('/')[0])
        except ValueError:
            raise HTTPException(403, 'Open Rynhult at its private LAN address to pair the mobile app.') from None
        if (getattr(request.state, 'edge_fingerprint', None)
                or not any(ip in net for net in PRIVATE if net.version == ip.version)
                or (request.url.hostname or '').lower().rstrip('.') !=
                    urlsplit(config['private_origin']).hostname):
            raise HTTPException(403, 'Open Rynhult at its private LAN address to pair the mobile app.')
        if not ctx.origin_ok(request.headers.get('origin')):
            raise HTTPException(403, 'Open Rynhult at its own address, reload the page and try again.')
        if pairing and not config['pairing_enabled']:
            raise HTTPException(409, 'Device pairing is off. Ask an owner to turn on LAN pairing in Settings → Remote access.')
        return config

    def recent(s, request, who):
        passkeys.recently_verified(s, request.state.session_hash, s.get(User, who.user_id))

    def live(row):
        if row is None or row.kind != 'native' or row.status == 'cancelled' or row.expires_at <= auth.now():
            raise HTTPException(410, 'This mobile app pairing expired or was cancelled. Start pairing again.')
        return row

    def move(s, row, before, after, **values):
        return s.execute(update(DevicePairing).where(DevicePairing.id == row.id, DevicePairing.kind == 'native',
            DevicePairing.status == before, DevicePairing.expires_at > auth.now()).values(status=after, **values)).rowcount == 1

    def cancel(s, row, action='mobile.pairing_cancel'):
        s.execute(update(DevicePairing).where(DevicePairing.id == row.id).values(status='cancelled'))
        s.add(AuditLog(actor='system', action=action, target=row.id, detail={'person': row.person}))
        s.commit()

    def active_policy(s):
        config = devices.settings(s)
        # Never interpret "public access is off" as a successful activation.
        return (config['enabled'] and config['desired_enabled'] and not config['pending']
                and config['last_reload_result'] == 'verified'
                and config['registry_revision'] == revision({'hostname': config['public_hostname'],
                    'leaves': [d.certificate_pem for d in edge.active_leaves(s, ctx.settings.mobile_enabled)]}))

    def activation(device, policy_ready):
        if not edge.usable(device):
            return 'inactive'
        if not policy_ready:
            return 'pending'
        return 'verified' if device.last_seen_at is not None else 'awaiting_connection'

    def begin(s, body, who, old=None):
        if s.scalar(select(User.id).where(User.username == body.person)) is None:
            raise HTTPException(404, 'Person not found.')
        s.execute(delete(DevicePairing).where(DevicePairing.expires_at <= auth.now()))
        if s.scalar(select(func.count()).select_from(DevicePairing).where(DevicePairing.person == body.person)) >= 10:
            raise HTTPException(429, 'This person has too many pairings in progress. Finish one or wait five minutes.')
        token = secrets.token_urlsafe(32)
        row = DevicePairing(id=secrets.token_hex(16), kind='native', person=body.person, label=body.label,
            token_hash=auth.digest(token), expires_at=auth.now() + timedelta(seconds=300),
            renew_id=old.id if old else None, renew_fingerprint=old.fingerprint if old else None)
        s.add(row)
        s.add(AuditLog(actor=who.username, action='mobile.pairing_start', target=row.id,
                       detail={'person': body.person, 'rotation': old is not None}))
        origin = devices.settings(s)['private_origin']
        s.commit()
        return {'id': row.id, 'link': origin + '/pair-mobile#' + row.id + ':' + token,
                'expires_at': row.expires_at, 'origin': origin}

    @api.post('/pairings')
    @audited('start', owner_failures=True)
    def start(body: BeginIn, request: Request, who=Depends(owner)):
        limit(request, who.username, bucket=owner_bucket, count=False)
        with ctx.db() as s:
            private(request, s)
            recent(s, request, who)
            return begin(s, body, who)

    @api.post('/devices/{id}/rotate')
    @audited('rotate', owner_failures=True)
    def rotate(id: str, body: BeginIn, request: Request, who=Depends(owner)):
        limit(request, who.username, bucket=owner_bucket, count=False)
        with ctx.db() as s:
            private(request, s)
            recent(s, request, who)
            old = s.get(Device, id)
            if old is None or old.kind != 'native' or old.person != body.person:
                raise HTTPException(404, 'Mobile app device not found.')
            if not edge.usable(old):
                raise HTTPException(409, 'This device expired or was revoked, so it can\'t be renewed. Pair the app again as a new device.')
            return begin(s, body, who, old)

    @api.post('/claim')
    @audited('claim')
    def claim(body: ClaimIn, request: Request):
        with ctx.db() as s:
            config = private(request, s)
            limit(request, bucket=device_bucket)
            row = live(s.scalar(select(DevicePairing).where(DevicePairing.token_hash == auth.digest(body.token))))
            if row.status != 'pending':
                raise HTTPException(410, 'This pairing was already used. Start pairing again.')
            try:
                key = mobile.proof(row, body, config['private_origin'])
            except (ValueError, TypeError, InvalidSignature):
                raise HTTPException(422, 'The app\'s pairing request could not be verified. Start pairing again.') from None
            old = s.get(Device, row.renew_id) if row.renew_id else None
            if row.renew_id and (old is None or old.installation_id != body.installation_id
                    or old.platform != body.platform or old.key_hash == key):
                raise HTTPException(409, 'To renew, use the app on the same phone. Otherwise pair it as a new device.')
            receipt = secrets.token_urlsafe(32)
            if not move(s, row, 'pending', 'claimed', receipt_hash=auth.digest(receipt), csr_pem=body.csr,
                        key_hash=key, installation_id=body.installation_id, platform=body.platform):
                raise HTTPException(410, 'This pairing was already used. Start pairing again.')
            s.refresh(row)
            s.add(AuditLog(actor='device-claim', action='mobile.claim', target=row.id, detail={'person': row.person, 'key_hash': key}))
            s.commit()
            return {'receipt': receipt, 'matching_code': devices.matching_code(row)}

    @api.post('/pairings/{id}/confirm')
    @audited('confirm', owner_failures=True)
    def confirm(id: str, body: ConfirmIn, request: Request, who=Depends(owner)):
        limit(request, who.username, bucket=owner_bucket, count=False)
        with ctx.db() as s:
            config = private(request, s)
            recent(s, request, who)
            row = live(s.get(DevicePairing, id))
            if row.status == 'ready':  # idempotent retry returns the already approved result
                if not secrets.compare_digest(body.matching_code, devices.matching_code(row) or ''):
                    raise HTTPException(409, 'This device was already confirmed with a different matching code.')
                return {'ok': True, 'device_id': row.device_id, 'activation': 'pending'}
            if row.status != 'claimed':
                raise HTTPException(409, 'Open the pairing link in the app first, or wait for the current confirmation to finish.')
            if not secrets.compare_digest(body.matching_code, devices.matching_code(row) or ''):
                if move(s, row, 'claimed', 'cancelled'):
                    s.add(AuditLog(actor=who.username, action='mobile.pairing_mismatch', target=id, detail={'person': row.person}))
                    s.commit()
                raise HTTPException(409, 'The matching code differs, so this pairing was cancelled. Start pairing again.')
            if config['pending']:
                raise HTTPException(409, 'A public access change is still pending. Apply it or turn public access off in Settings → Remote access, then confirm this device.')
            if not move(s, row, 'claimed', 'signing'):
                raise HTTPException(409, 'This device is already being confirmed.')
            csr, device_id, days = row.csr_pem, row.renew_id or secrets.token_hex(16), config['certificate_days']
            s.commit()  # no database lock across the privileged signer
        try:
            result = devices.sign({'operation': 'issue_native', 'device_id': device_id, 'csr': csr, 'days': days})
            ca = (ctx.settings.data_dir / 'device-ca.pem').read_text()
            cert = mobile.identity(result, device_id, csr, days, ca)
        except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError, InvalidSignature):
            with ctx.db() as s:
                row = s.get(DevicePairing, id)
                if row:
                    # An uncertain signer result is never blindly reissued. Start a new pairing.
                    cancel(s, row, 'mobile.issue_failed')
            raise HTTPException(503, 'The device signer is unavailable or gave a result Rynhult can\'t use. Start pairing again.') from None
        with ctx.db() as s:
            row = live(s.get(DevicePairing, id))
            config = private(request, s)
            approver = s.scalar(select(User).where(User.id == who.user_id).with_for_update())
            if not ctx.settings.mobile_enabled or approver is None or approver.role != 'owner':
                cancel(s, row)
                raise HTTPException(409, 'Your owner role or the mobile app setting changed while the device was being confirmed. Start pairing again.')
            try:
                recent(s, request, who)
            except HTTPException:
                cancel(s, row)
                raise
            if config['pending'] or s.scalar(select(User.id).where(User.username == row.person)) is None:
                cancel(s, row)
                raise HTTPException(409, 'The person was removed, or a public access change started, while the device was being confirmed. Start pairing again.')
            old = s.scalar(select(Device).where(Device.id == row.renew_id).with_for_update()) if row.renew_id else None
            if row.renew_id and (not edge.usable(old) or old.fingerprint != row.renew_fingerprint):
                cancel(s, row)
                raise HTTPException(409, 'The device changed or was revoked while it was being confirmed. Start pairing again.')
            if not move(s, row, 'signing', 'ready', device_id=device_id):
                raise HTTPException(410, 'This pairing was cancelled while the device was being confirmed. Start pairing again.')
            device = old or Device(id=device_id, person=row.person, label=row.label, kind='native')
            device.status, device.certificate_pem = 'active', result['certificate']
            device.fingerprint, device.serial = devices.fingerprint(cert), str(cert.serial_number)
            device.expires_at, device.last_seen_at = cert.not_valid_after_utc, None
            device.installation_id, device.platform, device.key_hash = row.installation_id, row.platform, row.key_hash
            s.add(device)
            s.add(AuditLog(actor=who.username, action='mobile.rotate' if old else 'mobile.issue', target=device_id,
                detail={'person': row.person, 'fingerprint': device.fingerprint, 'activation': 'pending'}))
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                cancel(s, s.get(DevicePairing, id))
                raise HTTPException(409, 'This app installation is already paired. Renew it, or pair a new installation.') from None
            return {'ok': True, 'device_id': device_id, 'activation': 'pending'}

    @api.post('/credential')
    @audited('credential')
    def credential(body: ReceiptIn, request: Request):
        with ctx.db() as s:
            private(request, s)
            limit(request, bucket=device_bucket, count=False)
            found = s.scalar(select(DevicePairing).where(DevicePairing.receipt_hash == auth.digest(body.receipt)))
            if found is None:
                # Only a receipt nobody was given counts: the phone polls its own until the owner confirms (or the
                # five-minute pairing expires), which would otherwise use up the bucket in under a minute.
                limit(request, bucket=device_bucket)
            row = live(found)
            device = s.get(Device, row.device_id) if row.device_id else None
            if row.status != 'ready' or not edge.usable(device) or device.key_hash != row.key_hash:
                raise HTTPException(409, 'An owner must confirm this device first, or its certificate is no longer current.')
            try:
                ca = (ctx.settings.data_dir / 'device-ca.pem').read_text()
                mobile.identity({'certificate': device.certificate_pem}, device.id, row.csr_pem, 90, ca)
            except (OSError, ValueError, InvalidSignature):
                raise HTTPException(409, 'Rynhult\'s device certificate authority changed. Open Rynhult at its private address and pair the app again.') from None
            # Public result is retryable until the original five-minute pairing expiry. Never a key or PKCS#12.
            return {'device_id': device.id, 'certificate': device.certificate_pem,
                    'chain': ca, 'expires_at': device.expires_at,
                    'activation': activation(device, active_policy(s))}

    @api.get('/devices')
    def listing(request: Request, who=Depends(human)):
        # The installation ID is a rotation-claim factor: only the private connection shows it. Everywhere a short,
        # one-way fingerprint tells installations apart.
        public = getattr(request.state, 'edge_fingerprint', None) is not None
        with ctx.db() as s:
            query = select(Device).where(Device.kind == 'native')
            if not who.is_owner:
                query = query.where(Device.person == who.username)
            policy_ready = active_policy(s)
            return {'devices': [{**devices.metadata(d),
                                 'installation': auth.digest(d.installation_id)[:12] if d.installation_id else None,
                                 **({} if public else {'installation_id': d.installation_id}),
                                 'platform': d.platform, 'activation': activation(d, policy_ready)} for d in s.scalars(query)]}

    @api.post('/session/bind')
    @audited('bind')
    async def bind(request: Request, response: Response, who=Depends(human)):
        def binding():
            with ctx.db() as s:
                recent(s, request, who)
                limit(request, who.username, bucket=bind_bucket)
                fingerprint = getattr(request.state, 'edge_fingerprint', None)
                device = s.scalar(select(Device).where(Device.fingerprint == fingerprint)) if fingerprint else None
                if not edge.usable(device) or device.kind != 'native' or device.person != who.username:
                    raise HTTPException(403, 'Use the mobile app paired to your account, over Rynhult\'s public address.')
                if not active_policy(s):
                    raise HTTPException(409, 'Public access for the mobile app isn\'t confirmed yet. An owner must apply it in Settings → Remote access.')
                record = s.get(AuthSession, request.state.session_hash)
                token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
                record.token_hash, record.csrf, record.device_fingerprint = auth.digest(token), csrf, fingerprint
                record.device_bound_at = auth.now()
                record.expires_at = min(record.expires_at, auth.now() + timedelta(hours=12), device.expires_at)
                device.last_seen_at = auth.now()
                s.add(AuditLog(actor=who.username, action='mobile.session_bind', target=device.id, detail={}))
                s.commit()
                return token, csrf
        token, csrf = await asyncio.to_thread(binding)
        await ctx.close_terminals(request.state.session_hash)
        ctx.set_login_cookies(response, token, csrf, max_age=12 * 3600)
        return {'ok': True}

    @api.post('/devices/{id}/revoke')
    @audited('revoke')
    async def revoke(id: str, request: Request, who=Depends(owner)):
        def revocation():
            with ctx.db() as s:
                recent(s, request, who)
                device = s.get(Device, id)
                if device is None or device.kind != 'native':
                    raise HTTPException(404, 'Mobile app device not found.')
                device.status = 'revoked'
                user = s.scalar(select(User).where(User.username == device.person))
                sessions = list(s.scalars(select(AuthSession.token_hash).where(AuthSession.user_id == user.id))) if user else []
                if user:
                    s.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
                s.execute(delete(PushSubscription).where(PushSubscription.username == device.person))
                for pairing in s.scalars(select(DevicePairing).where(DevicePairing.person == device.person)):
                    pairing.status = 'cancelled'
                    ctx.device_imports.pop(pairing.id)
                s.add(AuditLog(actor=who.username, action='mobile.revoke', target=id,
                               detail={'person': device.person, 'all_person_sessions': True, 'policy': 'pending'}))
                s.commit()
                return sessions
        sessions = await asyncio.to_thread(revocation)
        for session in sessions:
            await ctx.close_terminals(session)
        await ctx.edge.sweep()
        try:
            await ctx.remote_access.reconcile()
        except HTTPException:
            pass
        return {'ok': True, 'policy_applied': False}

    return api
