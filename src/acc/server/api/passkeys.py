"""WebAuthn ceremonies: UV is mandatory and each challenge is consumed even on failure."""
import json
import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from starlette.concurrency import run_in_threadpool
from webauthn import (
    generate_authentication_options,
    generate_registration_options,
    options_to_json,
    verify_authentication_response,
    verify_registration_response,
)
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url
from webauthn.helpers.structs import (
    AuthenticatorSelectionCriteria,
    PublicKeyCredentialDescriptor,
    ResidentKeyRequirement,
    UserVerificationRequirement,
)

from .. import attention, auth, passkeys
from ..db import WRITE_INTENT
from ..models import AuditLog, AuthSession, Invite, OIDCProvider, Passkey, PasskeyChallenge, PasskeyPolicy, User
from .context import Context


class ReauthIn(BaseModel):
    password: str = Field(max_length=1024)
    code: str = Field(max_length=100)


class CompleteIn(BaseModel):
    challenge_id: str = Field(max_length=64)
    credential: dict
    name: str = Field(default='Passkey', min_length=1, max_length=100)


class NameIn(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PolicyIn(BaseModel):
    owners: str = Field(pattern='^(allow|require)$')
    members: str = Field(pattern='^(allow|require)$')


class RecoverIn(ReauthIn):
    username: str = Field(max_length=64)


def router(ctx: Context):
    api = APIRouter(prefix='/api/auth/passkeys')
    cookie = ('__Host-' if ctx.settings.cookie_secure else '') + 'acc_webauthn'

    def audit(s, who, action, request, detail=None):
        s.add(AuditLog(actor=who, action='auth.passkey_' + action, target=ctx.client_key(request), detail=detail))

    def limit(request, *, session=None):
        """Per address only. Like password sign-in, a name others failed for still takes its owner's correct answer."""
        key = 'ip:' + ctx.client_key(request)
        if ctx.throttle.blocked(key):
            if session is not None:
                audit(session, 'unknown', 'failed', request)
                session.commit()
            else:
                with ctx.db() as s:
                    audit(s, 'unknown', 'failed', request)
                    s.commit()
            raise HTTPException(429, 'Too many attempts. Wait 10 minutes, then try again.')
        ctx.throttle.fail(key)

    def origin(request):
        try:
            return passkeys.relying_party(ctx.settings, request.headers.get('origin'))
        except HTTPException:
            limit(request)
            with ctx.db() as s:
                audit(s, 'unknown', 'failed', request)
                s.commit()
            raise

    def human(who):
        if who.user_id is None:
            raise HTTPException(403, 'Passkeys belong to a signed-in person; API tokens cannot manage them.')

    def recent(s, request, who):
        human(who)
        user = s.get(User, who.user_id)
        passkeys.recently_verified(s, request.state.session_hash, user)
        return user

    def enrollment_until(s, request, user):
        """When this company sign-in's window to add a passkey closes, or None: only a company-only member's sign-in
        that proved MFA, for five minutes, while the owner allows it (decision 0061)."""
        session_hash = getattr(request.state, 'session_hash', None)
        record = s.get(AuthSession, session_hash) if session_hash else None
        provider = s.get(OIDCProvider, 1) if ctx.settings.organizations_enabled else None
        if (user is None or record is None or record.user_id != user.id or record.method != 'oidc' or
                not user.company_only or user.role == 'owner' or record.passkey_enroll_until is None or
                record.passkey_enroll_until <= auth.now() or provider is None or not provider.enabled or
                provider.passkey_enrollment is False):
            return None
        return record.passkey_enroll_until

    def may_register(s, request, who):
        """Verified again recently, or inside a company-only member's enrollment window. Returns (user, window)."""
        human(who)
        user = s.get(User, who.user_id)
        try:
            passkeys.recently_verified(s, request.state.session_hash, user)
            return user, False
        except HTTPException:
            if enrollment_until(s, request, user) is None:
                raise
            return user, True

    def make_room(s, pending, keep):
        """Drop the oldest pending ceremonies beyond `keep` (making room for one more)."""
        stale = list(s.scalars(select(PasskeyChallenge.id).where(pending)
                               .order_by(PasskeyChallenge.expires_at.desc()).offset(keep - 1)))
        if stale:
            s.execute(delete(PasskeyChallenge).where(PasskeyChallenge.id.in_(stale)))

    def issue(s, request, response, options, purpose, who=None):
        # Expiry cleanup plus caps bound storage even across worker restarts. Sign-in ceremonies from anyone share one
        # pool where the oldest give way, so a flood from many addresses never refuses sign-in outright; a signed-in
        # person's own ceremonies have a small pool per login that the flood can't touch.
        s.execute(delete(PasskeyChallenge).where(PasskeyChallenge.expires_at < auth.now()))
        session_hash = getattr(request.state, 'session_hash', None) if who else None
        if session_hash is None:
            make_room(s, PasskeyChallenge.session_hash.is_(None), 1000)
        else:
            make_room(s, PasskeyChallenge.session_hash == session_hash, 5)
        identifier = secrets.token_urlsafe(32)
        s.add(PasskeyChallenge(id=auth.digest(identifier), challenge=bytes_to_base64url(options.challenge),
                              purpose=purpose, origin=request.headers['origin'], client=ctx.client_key(request),
                              session_hash=session_hash,
                              user_id=who.user_id if who else None, expires_at=auth.now() + timedelta(minutes=5)))
        s.commit()
        response.set_cookie(cookie, identifier, max_age=300, secure=ctx.settings.cookie_secure,
                            httponly=True, samesite='strict', path='/')
        return {'challenge_id': identifier, 'options': json.loads(options_to_json(options))}

    def consume(s, body, request, purpose, who=None):
        row = s.get(PasskeyChallenge, auth.digest(body.challenge_id))
        if row:
            s.delete(row)
            s.commit()  # burned before parsing untrusted authenticator data
        if (row is None or row.purpose != purpose or row.expires_at < auth.now() or
                row.origin != request.headers.get('origin') or row.client != ctx.client_key(request) or
                not secrets.compare_digest(request.cookies.get(cookie, ''), body.challenge_id) or
                row.session_hash != (getattr(request.state, 'session_hash', None) if who else None) or
                row.user_id != (who.user_id if who else None)):
            raise ValueError('invalid ceremony')
        return row

    def same_site(credential):
        client = json.loads(base64url_to_bytes(credential['response']['clientDataJSON']))
        if client.get('crossOrigin', False) is not False or client.get('topOrigin') is not None:
            raise ValueError('cross-origin ceremony')

    def failure(s, request, who='unknown', detail=None):
        audit(s, who, 'failed', request, detail)
        s.commit()
        raise HTTPException(401, 'Passkey verification failed or timed out. Open Rynhult at its usual address and try again.')

    @api.get('')
    def listing(request: Request, who=Depends(ctx.principal)):
        human(who)
        with ctx.db() as s:
            user = s.get(User, who.user_id)
            return {'passkeys': [{'id': p.id, 'name': p.name, 'rp_id': p.rp_id, 'created_at': p.created_at,
                                 'last_used_at': p.last_used_at} for p in s.scalars(
                                     select(Passkey).where(Passkey.user_id == who.user_id).order_by(Passkey.id))],
                    'recovery_code': bool(user.recovery_hash), 'policy': passkeys.policy(s),
                    'company_only': bool(user.company_only), 'enrollment_until': enrollment_until(s, request, user)}

    def password_snapshot(*, user_id=None, username=None):
        intent = WRITE_INTENT.set(False)
        try:
            with ctx.db() as s:
                query = select(User.id, User.password_hash, User.totp_secret, User.totp_last_step, User.recovery_hash)
                row = s.execute(query.where(User.id == user_id) if user_id is not None else
                                query.where(User.username == username)).first()
                s.rollback()
                return row
        finally:
            WRITE_INTENT.reset(intent)

    @api.post('/reauth')
    def reauth(body: ReauthIn, request: Request, who=Depends(ctx.principal)):
        human(who)
        limit(request)
        account_key = 'user:' + who.username.lower()
        locked = ctx.throttle.blocked(account_key)  # refuses only wrong answers, as password sign-in does
        ctx.throttle.fail(account_key)
        stored = password_snapshot(user_id=who.user_id)
        password_ok = auth.password_ok(stored.password_hash if stored else None, body.password)
        step = auth.totp_ok(stored.totp_secret, body.code, stored.totp_last_step) if password_ok else None
        with ctx.db() as s:
            user = s.get(User, who.user_id)
            if passkeys.required(s, user):
                raise HTTPException(403, 'A passkey is required to verify your sign-in.')
            if (step is None or user.password_hash != stored.password_hash or
                    user.totp_secret != stored.totp_secret or
                    (user.totp_last_step is not None and step <= user.totp_last_step)):
                if locked:
                    audit(s, who.username, 'failed', request)
                    s.commit()
                    raise HTTPException(429, 'Too many attempts. Wait 10 minutes, then try again.')
                failure(s, request, who.username)
            user.totp_last_step = step
            record = s.get(AuthSession, request.state.session_hash)
            record.reauthenticated_at, record.passkey_verified = auth.now(), False
            audit(s, who.username, 'reauth', request)
            s.commit()
        ctx.throttle.reset('user:' + who.username.lower())
        ctx.throttle.reset('ip:' + ctx.client_key(request))
        return {'ok': True}

    @api.post('/register/begin')
    def register_begin(request: Request, response: Response, who=Depends(ctx.principal)):
        rp = origin(request)
        limit(request)
        with ctx.db() as s:
            user, _ = may_register(s, request, who)
            existing = list(s.scalars(select(Passkey).where(Passkey.user_id == user.id)))
            if len(existing) >= 20:
                raise HTTPException(409, 'Remove an unused passkey first (maximum 20).')
            if not user.passkey_handle:
                user.passkey_handle = bytes_to_base64url(secrets.token_bytes(32))
            options = generate_registration_options(rp_id=rp, rp_name='Rynhult', user_name=user.username,
                user_id=base64url_to_bytes(user.passkey_handle),
                authenticator_selection=AuthenticatorSelectionCriteria(resident_key=ResidentKeyRequirement.REQUIRED,
                    require_resident_key=True, user_verification=UserVerificationRequirement.REQUIRED),
                exclude_credentials=[PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id)) for p in existing if p.rp_id == rp])
            return issue(s, request, response, options, 'register', who)

    @api.post('/register/complete')
    def register_complete(body: CompleteIn, request: Request, who=Depends(ctx.principal)):
        rp = origin(request)
        with ctx.db() as s:
            may_register(s, request, who)
            if s.get(PasskeyChallenge, auth.digest(body.challenge_id)) is None:
                limit(request, session=s)
            try:
                row = consume(s, body, request, 'register', who)
                _, window = may_register(s, request, who)
                same_site(body.credential)
                verified = verify_registration_response(credential=body.credential,
                    expected_challenge=base64url_to_bytes(row.challenge), expected_rp_id=rp,
                    expected_origin=row.origin, require_user_verification=True)
                identifier = bytes_to_base64url(verified.credential_id)
                if len(verified.credential_id) > 1023:  # WebAuthn: relying parties refuse longer credential IDs
                    raise ValueError('credential ID too long')
                if (s.scalar(select(Passkey.id).where(Passkey.credential_id == identifier)) or
                        s.scalar(select(func.count()).select_from(Passkey).where(Passkey.user_id == who.user_id)) >= 20):
                    raise ValueError('duplicate or too many credentials')
            except Exception:
                failure(s, request, who.username)
            name = body.name.strip()
            if not name:
                raise HTTPException(422, 'Give the passkey a name.')
            if window and s.execute(update(AuthSession).where(  # one passkey per window, even when raced
                    AuthSession.token_hash == request.state.session_hash, AuthSession.passkey_enroll_until > auth.now())
                    .values(passkey_enroll_until=None)).rowcount != 1:
                failure(s, request, who.username)
            record = Passkey(user_id=who.user_id, credential_id=identifier,
                             public_key=bytes_to_base64url(verified.credential_public_key), rp_id=rp,
                             name=name, sign_count=verified.sign_count)
            s.add(record)
            audit(s, who.username, 'registered', request, {'via': 'company_mfa'} if window else None)
            s.commit()
            ctx.throttle.reset('ip:' + ctx.client_key(request))
            return {'id': record.id}

    def begin_login(request, response, who=None):
        rp = origin(request)
        limit(request)
        with ctx.db() as s:
            allowed = None
            if who:
                human(who)
                allowed = [PublicKeyCredentialDescriptor(id=base64url_to_bytes(p.credential_id)) for p in
                           s.scalars(select(Passkey).where(Passkey.user_id == who.user_id, Passkey.rp_id == rp))]
                if not allowed:
                    raise HTTPException(409, 'No passkey is registered for this address. Verify with password and authenticator instead, if passkeys are not required for you.')
            options = generate_authentication_options(rp_id=rp, user_verification=UserVerificationRequirement.REQUIRED,
                                                       allow_credentials=allowed)
            return issue(s, request, response, options, 'reauth' if who else 'login', who)

    @api.post('/login/begin')
    def login_begin(request: Request, response: Response):
        return begin_login(request, response)

    @api.post('/verify/begin')
    def verify_begin(request: Request, response: Response, who=Depends(ctx.principal)):
        return begin_login(request, response, who)

    def complete_login(body, request, response, who=None):
        rp = origin(request)
        # A valid pending challenge was counted at begin; unknown/replayed completions count again.
        with ctx.db() as s:
            if s.get(PasskeyChallenge, auth.digest(body.challenge_id)) is None:
                limit(request, session=s)
            locked, account_name, regressed = False, None, None
            try:
                row = consume(s, body, request, 'reauth' if who else 'login', who)
                same_site(body.credential)
                credential = s.scalar(select(Passkey).where(Passkey.credential_id == body.credential.get('id'), Passkey.rp_id == rp))
                if credential is None or (who and credential.user_id != who.user_id):
                    raise ValueError('unknown credential')
                user = s.get(User, credential.user_id)
                account_name = user.username
                account_key = 'user:' + user.username.lower()
                locked = ctx.throttle.blocked(account_key)
                ctx.throttle.fail(account_key)
                handle = body.credential.get('response', {}).get('userHandle')
                if (handle is None and not who) or (handle is not None and handle != user.passkey_handle):
                    raise ValueError('wrong principal')
                # The library compares counters before checking the signature; a stored count of 0 lets it check
                # everything else first, so only a validly signed regression (a cloned authenticator) raises the alarm.
                verified = verify_authentication_response(credential=body.credential,
                    expected_challenge=base64url_to_bytes(row.challenge), expected_rp_id=rp, expected_origin=row.origin,
                    credential_public_key=base64url_to_bytes(credential.public_key),
                    credential_current_sign_count=0, require_user_verification=True)
                if (verified.new_sign_count or credential.sign_count) and verified.new_sign_count <= credential.sign_count:
                    regressed = credential
                    raise ValueError('signature counter went backwards')
            except Exception:
                if regressed:
                    attention.raise_item(s, f'passkey.clone.{regressed.id}', kind='sign_in', source='passkeys', severity='critical',
                        owner=account_name, title=f'Possible copied passkey: {regressed.name} ({account_name})',
                        detail='It signed correctly but its use counter went backwards, so another copy may exist. '
                               'The sign-in was refused. Remove this passkey and add a new one.')
                detail = {'reason': 'counter_regressed'} if regressed else None
                if locked:
                    audit(s, account_name, 'failed', request, detail)
                    s.commit()
                    raise HTTPException(429, 'Too many attempts. Wait 10 minutes, then try again.')
                failure(s, request, account_name or (who.username if who else 'unknown'), detail)
            if not who and user.company_only:
                # Their passkey only confirms a company sign-in (decision 0061): signing in with it alone would outlive
                # removing their company sign-in or disabling them in the directory.
                audit(s, user.username, 'failed', request, {'reason': 'company_only'})
                s.commit()
                raise HTTPException(403, 'Sign in with your company account. Your passkey confirms company sign-ins only.')
            credential.sign_count, credential.last_used_at = verified.new_sign_count, auth.now()
            if who:
                record = s.get(AuthSession, request.state.session_hash)
            else:
                token, csrf = ctx.create_login_session(s, user, request)
                s.flush()
                record = s.get(AuthSession, auth.digest(token))
                ctx.set_login_cookies(response, token, csrf)
            record.reauthenticated_at, record.passkey_verified = auth.now(), True
            audit(s, user.username, 'verified' if who else 'login', request)
            s.commit()
        ctx.throttle.reset('ip:' + ctx.client_key(request))
        ctx.throttle.reset('user:' + user.username.lower())
        response.delete_cookie(cookie, path='/', secure=ctx.settings.cookie_secure, httponly=True, samesite='strict')
        return {'ok': True}

    @api.post('/login/complete')
    def login_complete(body: CompleteIn, request: Request, response: Response):
        return complete_login(body, request, response)

    @api.post('/verify/complete')
    def verify_complete(body: CompleteIn, request: Request, response: Response, who=Depends(ctx.principal)):
        return complete_login(body, request, response, who)

    def owned(s, identifier, who):
        record = s.get(Passkey, identifier)
        if record is None or record.user_id != who.user_id:
            raise HTTPException(404, 'No such passkey.')
        return record

    @api.post('/{identifier:int}/name')
    def rename(identifier: int, body: NameIn, request: Request, who=Depends(ctx.principal)):
        human(who)
        if not body.name.strip():
            raise HTTPException(422, 'Give the passkey a name.')
        with ctx.db() as s:
            owned(s, identifier, who).name = body.name.strip()
            audit(s, who.username, 'renamed', request)
            s.commit()
        return {'ok': True}

    @api.post('/{identifier:int}/remove')
    def remove(identifier: int, request: Request, who=Depends(ctx.principal)):
        with ctx.db() as s:
            human(who)
            record = owned(s, identifier, who)
            user = recent(s, request, who)
            count = s.scalar(select(func.count()).select_from(Passkey).where(Passkey.user_id == user.id, Passkey.rp_id == record.rp_id))
            if passkeys.required(s, user) and count <= (2 if user.role == 'owner' else 1):
                raise HTTPException(409, "Passkeys are required, so this one can't be removed: owners keep two and members one at this address. Add another passkey first, or ask an owner to set the policy to Allow.")
            s.delete(record)
            audit(s, who.username, 'removed', request)
            s.commit()
        return {'ok': True}

    @api.post('/recovery-code')
    def recovery_code(request: Request, response: Response, who=Depends(ctx.principal)):
        code = secrets.token_urlsafe(32)
        with ctx.db() as s:
            recent(s, request, who).recovery_hash = auth.digest(code)
            audit(s, who.username, 'recovery_created', request)
            s.commit()
        response.headers['Cache-Control'] = 'no-store'
        return {'code': code}

    @api.post('/policy')
    def change_policy(body: PolicyIn, request: Request, who=Depends(ctx.owner)):
        rp = origin(request)
        with ctx.db() as s:
            recent(s, request, who)
            for user in s.scalars(select(User)):
                if getattr(body, 'owners' if user.role == 'owner' else 'members') == 'require':
                    count = s.scalar(select(func.count()).select_from(Passkey).where(Passkey.user_id == user.id, Passkey.rp_id == rp))
                    needs_recovery = passkeys.recovery_required(user)
                    if count < (2 if user.role == 'owner' else 1) or (needs_recovery and not user.recovery_hash):
                        raise HTTPException(409, 'Before requiring passkeys, each affected owner needs two passkeys and an offline recovery code; each member needs one passkey at this address, plus a recovery code unless they sign in only with a company account.')
            old = passkeys.policy(s)
            row = s.get(PasskeyPolicy, 1) or PasskeyPolicy(id=1)
            row.owners, row.members = body.owners, body.members
            s.add(row)
            new = passkeys.policy(s)
            relaxed = [role for role in ('owners', 'members') if old[role] == 'require' and new[role] == 'allow']
            if relaxed:
                attention.raise_item(s, 'passkey.policy_relaxed', kind='sign_in', source='passkeys', owner='',
                    title=f'{who.username} stopped requiring passkeys for {" and ".join(relaxed)}',
                    detail='Password and authenticator sign-in works again for them. To require passkeys again, go to Settings → Passkeys.')
            audit(s, who.username, 'policy_changed', request, {'from': old, 'to': new})
            s.commit()
        return {'ok': True}

    @api.post('/recover')
    async def recover(body: RecoverIn, request: Request, response: Response):
        origin(request)
        limit(request)
        account_key = 'user:' + body.username.strip().lower()
        locked = ctx.throttle.blocked(account_key)
        ctx.throttle.fail(account_key)
        stored = password_snapshot(username=body.username.strip())
        password_ok = await run_in_threadpool(auth.password_ok, stored.password_hash if stored else None, body.password)
        with ctx.db() as s:
            user = s.get(User, stored.id) if stored else None
            if (not password_ok or user is None or user.password_hash != stored.password_hash or
                    not user.recovery_hash or not secrets.compare_digest(user.recovery_hash, auth.digest(body.code))):
                if locked:
                    audit(s, 'unknown', 'failed', request)
                    s.commit()
                    raise HTTPException(429, 'Too many attempts. Wait 10 minutes, then try again.')
                failure(s, request)
            if user.role != 'owner' and passkeys.required(s, user):
                audit(s, user.username, 'failed', request)
                s.commit()
                raise HTTPException(409, 'Passkeys are required for members. Ask an owner to set Passkeys for members to Allow in Settings → Passkeys, then use your recovery code.')
            hashes = list(s.scalars(select(AuthSession.token_hash).where(AuthSession.user_id == user.id)))
            s.execute(delete(AuthSession).where(AuthSession.user_id == user.id))
            s.execute(delete(Passkey).where(Passkey.user_id == user.id))
            s.execute(delete(PasskeyChallenge).where(PasskeyChallenge.user_id == user.id))
            for invite in s.scalars(select(Invite).where(Invite.user_id == user.id, Invite.used_at.is_(None))):
                invite.used_at = auth.now()
            user.recovery_hash, user.passkey_handle = None, None
            row = s.get(PasskeyPolicy, 1)
            relaxed = bool(row and user.role == 'owner' and row.owners == 'require')
            if row and user.role == 'owner':
                row.owners = 'allow'
            attention.raise_item(s, f'passkey.recovered.{user.username}', kind='sign_in', source='passkeys', severity='critical',
                owner=user.username, title=f'{user.username} recovered sign-in with an offline recovery code',
                detail='Their passkeys and sign-ins were removed.' + (
                    ' Passkeys are no longer required for owners.' if relaxed else '') +
                    ' If this was not them, change their password and reset their sign-in.')
            token, csrf = ctx.create_login_session(s, user, request)
            audit(s, user.username, 'recovered', request, {'owner_policy_relaxed': True} if relaxed else None)
            s.commit()
        for old in hashes:
            await ctx.close_terminals(old)
        ctx.throttle.reset(account_key)
        ctx.throttle.reset('ip:' + ctx.client_key(request))
        ctx.set_login_cookies(response, token, csrf)
        return {'ok': True}

    return api
