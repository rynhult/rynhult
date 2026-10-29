"""LAN pairing and owner-controlled, disabled-by-default public edge intent."""
import base64
import ipaddress
import secrets
import unicodedata
from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError

from acc.remote_access_helper import revision

from .. import auth, devices, edge, passkeys
from ..models import AuditLog, AuthSession, Device, DevicePairing, PushSubscription, Setting, User
from .context import Context


class SettingsIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pairing_enabled: bool = Field(False, strict=True)
    public_hostname: str = Field('', max_length=253)
    private_origin: str = Field('', max_length=300)
    certificate_days: int = Field(90, ge=1, le=90, strict=True)
    expected_revision: str | None = Field(None, pattern=r'^[a-f0-9]{64}$')
    machine_identities: bool = Field(False, strict=True)


class LabelIn(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    label: str = Field(min_length=1, max_length=100)

    @field_validator('label')
    @classmethod
    def printable(cls, value):
        # Owners confirm by name: no control, bidi-override, zero-width or line-separator characters.
        if any(unicodedata.category(c) in ('Cc', 'Cf', 'Co', 'Cs', 'Zl', 'Zp') for c in value):
            raise ValueError('Use letters, numbers, spaces and punctuation only.')
        return value


class TokenIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    token: str = Field(min_length=43, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class ReceiptIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    receipt: str = Field(min_length=43, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')


class ConfirmIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    matching_code: str = Field(pattern=r'^[0-9]{6}$')


class ApplyIn(BaseModel):
    model_config = ConfigDict(extra='forbid')
    revision: str = Field(pattern=r'^[a-f0-9]{64}$')


class DownloadIn(ReceiptIn):
    format: Literal['pkcs12', 'mobileconfig'] = 'pkcs12'


PRIVATE = tuple(ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16',
                                                '100.64.0.0/10', '127.0.0.0/8', '::1/128', 'fd00::/8'))


def router(ctx: Context):
    api = APIRouter()
    imports = ctx.device_imports
    throttle = auth.LoginThrottle(limit=10, window=600)

    def human(who=Depends(ctx.principal)):
        if who.user_id is None:
            raise HTTPException(403, 'Devices belong to a signed-in person; API tokens cannot manage them.')
        return who

    def owner(who=Depends(human)):
        if not who.is_owner:
            raise HTTPException(403, 'Only an owner can manage remote access.')
        return who

    def private(request, s, *, require_enabled=True):
        config = devices.settings(s)
        try:
            ip = ipaddress.ip_address(ctx.client_key(request).split('/')[0])
        except ValueError:
            raise HTTPException(403, 'Open Rynhult at its private LAN address to pair a device.') from None
        # Never treat a loopback proxy of the public hostname as private traffic.
        if (not any(ip in net for net in PRIVATE if net.version == ip.version)
                or (config['public_hostname'] and (request.url.hostname or '').rstrip('.') == config['public_hostname'])):
            raise HTTPException(403, 'Open Rynhult at its private LAN address to pair a device.')
        if require_enabled and not config['pairing_enabled']:
            raise HTTPException(409, 'Device pairing is off. Ask an owner to turn on LAN pairing in Settings → Remote access.')
        if not ctx.origin_ok(request.headers.get('origin')):
            raise HTTPException(403, 'Open Rynhult at its own address, reload the page and try again.')
        return config

    def recent(s, request, who):
        passkeys.recently_verified(s, getattr(request.state, 'session_hash', None), s.get(User, who.user_id))

    def limit(request, who=None):
        keys = ['ip:' + ctx.client_key(request)] + (['person:' + who.username] if who else [])
        if any(throttle.blocked(k) for k in keys):
            raise HTTPException(429, 'Too many pairing attempts. Wait ten minutes and try again.')
        for key in keys:
            throttle.fail(key)

    def owned(s, id, who):
        row = s.get(Device, id)
        # Machine identities are managed with their machine (remove/rotate), never as a person's device.
        if row is None or row.kind != 'browser' or (not who.is_owner and row.person != who.username):
            raise HTTPException(404, 'Device not found.')
        return row

    def pending(s, id, who):
        row = s.get(DevicePairing, id)
        if row is None or row.kind != 'browser' or (not who.is_owner and row.person != who.username):
            raise HTTPException(404, 'Pairing not found.')
        return row

    def live(row):
        if row is None or row.kind != 'browser' or row.expires_at <= auth.now() or row.status in ('downloaded', 'cancelled'):
            raise HTTPException(410, 'This pairing has expired or was already used. Start again on the trusted device.')
        return row

    def move(s, row, before, after, **values):
        # Compare-and-set, so two racing requests can't both pass a status check (PostgreSQL reads committed).
        moved = s.execute(update(DevicePairing).where(DevicePairing.id == row.id, DevicePairing.status == before)
                          .values(status=after, **values)).rowcount == 1
        if moved:
            row.status = after
            for key, value in values.items():
                setattr(row, key, value)
        return moved

    @api.get('/api/remote-access')
    def get_settings(who=Depends(owner)):
        # A refresh audits authority changes through writer admission. Do that
        # before opening a read transaction that would block our own writer.
        authority = ctx.edge.authority()
        with ctx.db() as s:
            return {**devices.settings(s), 'rollover': ctx.edge.rollover(s, authority)}

    @api.get('/api/remote-access/review')
    async def review(response: Response, who=Depends(owner)):
        response.headers['Cache-Control'] = 'no-store'
        return await ctx.remote_access.review()

    @api.post('/api/remote-access/apply')
    async def apply(body: ApplyIn, request: Request, who=Depends(owner)):
        with ctx.db() as s:
            private(request, s, require_enabled=False)
            recent(s, request, who)
        return await ctx.remote_access.change(True, who.username, body.revision)

    @api.post('/api/remote-access/off')
    async def off(who=Depends(owner)):
        # Safety action: available even when pairing/CA/hostname/recent verification is unavailable.
        return await ctx.remote_access.change(False, who.username)

    @api.put('/api/remote-access')
    def put_settings(body: SettingsIn, request: Request, who=Depends(owner)):
        with ctx.db() as s:
            recent(s, request, who)
            # Settings can be prepared only from the current private address.
            try:
                devices.validate_names(body.public_hostname, body.private_origin)
            except ValueError as error:
                raise HTTPException(422, str(error)) from None
            s.scalar(select(Setting).where(Setting.key == 'remote_access').with_for_update())
            old = devices.settings(s)
            if body.expected_revision is not None and body.expected_revision != old['settings_revision']:
                raise HTTPException(409, 'Pairing settings changed in another tab. Reload this page and review the current settings before saving.')
            if old['desired_enabled'] and body.public_hostname != old['public_hostname']:
                raise HTTPException(409, 'Turn off public access before changing its hostname.')
            # Reuse address/host guard without requiring pairing to already be on.
            if old['public_hostname'] and (request.url.hostname or '').rstrip('.') == old['public_hostname']:
                raise HTTPException(403, 'Open Rynhult at its private LAN address to change remote access.')
            try:
                ip = ipaddress.ip_address(ctx.client_key(request).split('/')[0])
            except ValueError:
                raise HTTPException(403, 'Open Rynhult at its private LAN address to change remote access.') from None
            if not any(ip in net for net in PRIVATE if net.version == ip.version):
                raise HTTPException(403, 'Open Rynhult at its private LAN address to change remote access.')
            value = body.model_dump(exclude={'expected_revision'})
            s.merge(Setting(key='remote_access', value=value, updated_by=who.username))
            s.add(AuditLog(actor=who.username, action='remote_access.prepare', target='remote_access', detail=value))
            if not body.pairing_enabled:
                for row in s.scalars(select(DevicePairing).where(DevicePairing.status.in_(['pending', 'claimed', 'signing', 'ready']))):
                    row.status = 'cancelled'
                    imports.pop(row.id)
            try:
                s.commit()
            except IntegrityError:
                s.rollback()
                raise HTTPException(409, 'Pairing settings changed in another tab. Reload this page and review the current settings before saving.') from None
            # Turning machine identities off: the 15-second sweep ends their streams; reconciliation drops their leaves.
            return devices.settings(s)

    @api.get('/api/devices')
    def listing(mine: bool = False, who=Depends(human)):
        authority = ctx.edge.authority()
        with ctx.db() as s:
            query = select(Device).where(Device.kind == 'browser').order_by(Device.created_at.desc())
            if mine or not who.is_owner:
                query = query.where(Device.person == who.username)
            config = devices.settings(s)
            pending_policy = config['pending'] or (config['desired_enabled'] and
                revision(ctx.remote_access.payload()) != config['registry_revision'])
            return {'devices': [{**devices.metadata(d), 'previous_authority': edge.previous_authority(d, authority)}
                                for d in s.scalars(query)],
                    'pairing_enabled': config['pairing_enabled'], 'policy_pending': pending_policy}

    def begin(s, who, label, renew_id=None):
        # Bound pending records per person, purge expired capabilities on each start.
        s.execute(delete(DevicePairing).where(DevicePairing.expires_at <= auth.now()))
        count = len(list(s.scalars(select(DevicePairing.id).where(DevicePairing.person == who.username))))
        if count >= 10:
            raise HTTPException(429, 'Finish a pending pairing or wait five minutes.')
        if renew_id:
            # One live renewal per device: tapping Renew again replaces the earlier link, so two confirmations can't
            # replace the identity twice and strand the first import (break-it 10, B10-2).
            for earlier in s.scalars(select(DevicePairing).where(DevicePairing.renew_id == renew_id,
                                                                 DevicePairing.status.in_(('pending', 'claimed')))):
                if move(s, earlier, earlier.status, 'cancelled'):
                    imports.pop(earlier.id)
        token = secrets.token_urlsafe(32)
        row = DevicePairing(id=secrets.token_hex(16), person=who.username, label=label,
                            token_hash=auth.digest(token), expires_at=auth.now() + timedelta(seconds=devices.PAIRING_SECONDS),
                            renew_id=renew_id)
        s.add(row)
        s.flush()
        origin = devices.settings(s)['private_origin']
        result = {'id': row.id, 'label': row.label, 'link': origin + '/pair-device#' + token, 'expires_at': row.expires_at, 'qr_svg': auth.qr_image_svg(origin + '/pair-device#' + token)}
        s.commit()
        return result

    @api.post('/api/devices/pairings')
    def start(body: LabelIn, request: Request, response: Response, who=Depends(human)):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            private(request, s)
            recent(s, request, who)
            limit(request, who)
            return begin(s, who, body.label)

    @api.post('/api/devices/{id}/renew')
    def renew(id: str, request: Request, response: Response, who=Depends(human)):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            # Address first (no id oracle off the LAN), then ownership, then whether pairing is on.
            private(request, s, require_enabled=False)
            row = owned(s, id, who)
            private(request, s)
            recent(s, request, who)
            limit(request, who)
            if row.person != who.username or row.status != 'active' or row.expires_at <= auth.now():
                raise HTTPException(409, 'You can renew only your own device while it is still active. If it expired or was revoked, pair it again.')
            return begin(s, who, row.label, row.id)

    @api.post('/api/devices/pairings/{id}/cancel')
    def cancel(id: str, request: Request, response: Response, who=Depends(human)):
        """Close pairing: a link nobody confirmed yet stops working now, not five minutes later (break-it 10, B10-1).
        Once confirmed, the import stays for the device that is downloading it."""
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            private(request, s, require_enabled=False)
            row = pending(s, id, who)
            if row.status in ('pending', 'claimed') and move(s, row, row.status, 'cancelled'):
                imports.pop(row.id)
                s.add(AuditLog(actor=who.username, action='device.pairing_cancel', target=row.id,
                               detail={'person': row.person, 'label': row.label}))
                s.commit()
            return {'status': row.status}

    @api.get('/api/devices/pairings')
    def pairings(request: Request, who=Depends(owner)):
        with ctx.db() as s:
            private(request, s, require_enabled=False)
            return {'pairings': [{'id': r.id, 'person': r.person, 'label': r.label, 'status': r.status,
                                  'expires_at': r.expires_at} for r in s.scalars(select(DevicePairing).where(
                                      DevicePairing.kind == 'browser', DevicePairing.status == 'claimed', DevicePairing.expires_at > auth.now()))]}

    @api.get('/api/devices/pairings/{id}')
    def detail(id: str, request: Request, response: Response, who=Depends(human)):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            private(request, s, require_enabled=False)
            row = live(pending(s, id, who))
            private(request, s)
            value = imports.get(id)
            return {'status': row.status, 'label': row.label, 'person': row.person,
                    'password': value['password'] if value else None}

    @api.post('/api/device-pairing/claim')
    def claim(body: TokenIn, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            private(request, s)
            limit(request)
            row = live(s.scalar(select(DevicePairing).where(DevicePairing.token_hash == auth.digest(body.token))))
            if row.status != 'pending':
                raise HTTPException(410, 'This pairing link was already used.')
            receipt = secrets.token_urlsafe(32)
            if not move(s, row, 'pending', 'claimed', receipt_hash=auth.digest(receipt)):
                raise HTTPException(410, 'This pairing link was already used.')
            s.commit()
            return {'receipt': receipt, 'label': row.label, 'matching_code': devices.matching_code(row)}

    @api.post('/api/devices/pairings/{id}/confirm')
    def confirm(id: str, body: ConfirmIn, request: Request, response: Response, who=Depends(owner)):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            config = private(request, s)
            recent(s, request, who)
            row = live(pending(s, id, who))
            if row.status != 'claimed':
                raise HTTPException(409, 'Wait for the new device to open the pairing link.')
            # Only the receiving device shows the code; one wrong entry ends this pairing (no second guess).
            if not secrets.compare_digest(body.matching_code, devices.matching_code(row) or ''):
                if move(s, row, 'claimed', 'cancelled'):
                    imports.pop(row.id)
                    s.add(AuditLog(actor=who.username, action='device.pairing_mismatch', target=row.id,
                                   detail={'person': row.person, 'label': row.label}))
                    s.commit()
                raise HTTPException(409, 'The matching code differs, so this pairing was cancelled. Start pairing again.')
            if config['pending']:
                raise HTTPException(409, 'A public access change is still pending. Apply it or turn public access off in Settings → Remote access, then confirm this device.')
            person = s.scalar(select(User).where(User.username == row.person))
            if person is None:
                raise HTTPException(409, 'This person no longer has a Rynhult account.')
            old = s.get(Device, row.renew_id) if row.renew_id else None
            if row.renew_id and (old is None or old.status != 'active' or old.expires_at <= auth.now()):
                raise HTTPException(409, 'This device can no longer be renewed. Pair it again.')
            device_id = old.id if old else secrets.token_hex(16)
            # Claim the confirmation, then release the write lock: the signer may take up to its deadline.
            if not move(s, row, 'claimed', 'signing'):
                raise HTTPException(409, 'This device is already being confirmed.')
            s.commit()
            try:
                result = devices.sign({'operation': 'issue', 'device_id': device_id, 'days': config['certificate_days']})
                cert = devices.identity(result, device_id, config['certificate_days'])
                base64.b64decode(result['pkcs12'], validate=True)
            except (OSError, ValueError, KeyError, IndexError, TypeError, AttributeError):
                move(s, row, 'signing', 'claimed')
                s.commit()
                raise HTTPException(503, 'The device signer is unavailable. An owner must install and qualify it first.') from None
            # Revocation, person removal or pairing off may have happened while signing.
            s.expire_all()
            if devices.settings(s)['pending']:
                move(s, row, 'signing', 'cancelled')
                s.commit()
                raise HTTPException(409, 'A public access change started while this device was being confirmed. Apply it or turn public access off in Settings → Remote access, then pair again.')
            if row.renew_id:
                # A removal can delete the old row while the signer runs. Query
                # afresh instead of dereferencing an expired, deleted ORM object.
                old = s.scalar(select(Device).where(Device.id == row.renew_id))
                if old is None or old.status != 'active' or old.expires_at <= auth.now():
                    move(s, row, 'signing', 'cancelled')
                    s.commit()
                    raise HTTPException(409, 'This device can no longer be renewed. Pair it again.')
            if s.scalar(select(User.id).where(User.username == row.person)) is None:
                raise HTTPException(409, 'This person no longer has a Rynhult account.')
            if not move(s, row, 'signing', 'ready', device_id=device_id,
                        expires_at=auth.now() + timedelta(seconds=devices.IMPORT_SECONDS)):
                raise HTTPException(410, 'This pairing was cancelled while the device certificate was being made. Start pairing again.')
            try:
                imports.put(id, result)
            except ValueError as error:
                raise HTTPException(503, str(error)) from None
            # Replace the leaf on the same logical device, with zero old/new overlap.
            device = old or Device(id=device_id, person=row.person, label=row.label, kind='browser', status='active')
            previous = device.fingerprint if old else None
            device.certificate_pem = result['certificate']
            device.fingerprint = devices.fingerprint(cert)
            device.serial = str(cert.serial_number)
            device.expires_at = cert.not_valid_after_utc
            s.add(device)
            s.add(AuditLog(actor=who.username, action='device.renew' if old else 'device.pair', target=device_id,
                           detail={'person': row.person, 'previous_fingerprint': previous, 'policy': 'not applied'}))
            try:
                s.commit()
            except Exception:
                imports.pop(id)
                raise
            return {'ok': True, 'policy_applied': False}

    @api.post('/api/device-pairing/status')
    def status(body: ReceiptIn, request: Request, response: Response):
        response.headers['Cache-Control'] = 'no-store'
        with ctx.db() as s:
            private(request, s)
            row = live(s.scalar(select(DevicePairing).where(DevicePairing.receipt_hash == auth.digest(body.receipt))))
            return {'status': row.status, 'available': row.status == 'ready' and imports.get(row.id) is not None}

    @api.post('/api/device-pairing/download')
    def download(body: DownloadIn, request: Request):
        with ctx.db() as s:
            private(request, s)
            row = live(s.scalar(select(DevicePairing).where(DevicePairing.receipt_hash == auth.digest(body.receipt))))
            if row.status != 'ready':
                raise HTTPException(409, 'An owner must confirm this device before import.')
            value = imports.pop(row.id)
            if value is None:
                raise HTTPException(410, 'The import expired. Start pairing again.')
            row.status = 'downloaded'
            s.commit()
            bundle = base64.b64decode(value['pkcs12'])
            apple = body.format == 'mobileconfig'
            return Response(devices.mobileconfig(bundle) if apple else bundle,
                media_type='application/x-apple-aspen-config' if apple else 'application/x-pkcs12', headers={
                'Cache-Control': 'no-store', 'Content-Disposition': 'attachment; filename="acc-device.' + ('mobileconfig' if apple else 'p12') + '"',
                'Referrer-Policy': 'no-referrer', 'X-Content-Type-Options': 'nosniff'})

    @api.patch('/api/devices/{id}')
    def rename(id: str, body: LabelIn, who=Depends(human)):
        with ctx.db() as s:
            row = owned(s, id, who)
            detail = {'person': row.person, 'from': row.label, 'to': body.label}
            row.label = body.label
            s.add(AuditLog(actor=who.username, action='device.rename', target=id, detail=detail))
            s.commit()
            return devices.metadata(row)

    @api.delete('/api/devices/{id}')
    @api.post('/api/devices/{id}/revoke')
    async def revoke(id: str, request: Request, who=Depends(human)):
        with ctx.db() as s:
            row = owned(s, id, who)
            recent(s, request, who)
            row.status = 'revoked'
            user = s.scalar(select(User).where(User.username == row.person))
            sessions = list(s.scalars(select(AuthSession.token_hash).where(AuthSession.user_id == user.id))) if user else []
            if user:
                s.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            s.execute(delete(PushSubscription).where(PushSubscription.username == row.person))
            for pairing in s.scalars(select(DevicePairing).where(DevicePairing.person == row.person)):
                imports.pop(pairing.id)
                pairing.status = 'cancelled'
            removing = request.method == 'DELETE'
            if removing:
                s.delete(row)
            s.add(AuditLog(actor=who.username, action='device.remove' if removing else 'device.revoke', target=id,
                           detail={'person': row.person, 'policy': 'pending; not applied', 'all_person_sessions': True}))
            s.commit()
        for session in sessions:
            await ctx.close_terminals(session)
        await ctx.edge.sweep()  # public streams presenting the revoked leaf end now
        try:
            await ctx.remote_access.reconcile()
        except HTTPException:
            pass  # durable status remains pending and the worker retries OFF
        with ctx.db() as s:
            config = devices.settings(s)
        return {'ok': True, 'policy_applied': config['applied'], 'pending': config['pending']}

    @api.get('/api/remote-access/policy')
    def policy(who=Depends(owner)):
        # Only a public CA certificate is read. No helper or Caddy action occurs.
        with ctx.db() as s:
            try:
                ca = (ctx.settings.data_dir / 'device-ca.pem').read_text()
                return {'applied': False, 'fragment': devices.policy_fragment(s, ca, ctx.settings.mobile_enabled)}
            except (OSError, ValueError) as error:
                raise HTTPException(409, 'The public access configuration can\'t be shown yet. An owner must set up the device certificate authority and the public hostname first.') from error

    return api
