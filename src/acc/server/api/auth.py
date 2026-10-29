"""Health and authentication: first-run setup, login, logout."""
from __future__ import annotations

import secrets
import time
from threading import Lock

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import delete, select
from sqlalchemy.exc import OperationalError

from ... import __version__
from .. import admin_features, auth, passkeys
from ..db import WRITE_INTENT
from ..models import AuditLog, AuthSession, Invite, OIDCProvider, Passkey, PasskeyChallenge, User
from ..orchestrator.switch import enabled as orchestrator_enabled
from .context import Context
from .schemas import InviteBeginIn, InviteCompleteIn, LoginIn, PasswordIn, SetupBeginIn, SetupCompleteIn
from .users import account_refusal


def router(ctx: Context) -> APIRouter:
    settings, db, supervisor, throttle, pending_setup, origin_ok, actor, set_login_cookies, client_key = ctx.settings, ctx.db, ctx.supervisor, ctx.throttle, ctx.pending_setup, ctx.origin_ok, ctx.actor, ctx.set_login_cookies, ctx.client_key
    api = APIRouter()
    enrolment_lock = Lock()

    def enrolment_secret(pending, key):
        # Retries and other tabs must not invalidate a QR already scanned. Keep
        # the original expiry; concurrent sync endpoints share this dictionary.
        with enrolment_lock:
            secret, issued = pending.get(key, (None, 0))
            if secret is None or time.time() - issued > 900:
                secret = auth.new_totp_secret()
                pending[key] = (secret, time.time())
            return secret

    @api.get("/api/health")
    async def health():
        authority = db.kw.get('info', {}).get('ha_authority')
        if authority:
            # Unauthenticated: cached for a second so probes can't drive database connections.
            # A standby is healthy for observation, never ready for commands.
            ha = authority.public_status()
            return {'ok': ha['database'] == 'ok', 'version': __version__, 'build': ctx.updates.build, 'ha': ha}
        return {"ok": True, "broker": supervisor.broker_ok, "version": __version__, "build": ctx.updates.build}

    @api.get("/api/auth/state")
    def auth_state(request: Request):
        with db() as s:
            setup_required = s.scalar(select(User).limit(1)) is None
            record = auth.lookup_session(s, request.cookies.get(ctx.session_cookie), allow_step_up=True,
                edge_fingerprint=getattr(request.state, 'edge_fingerprint', None),
                native=getattr(request.state, 'edge_native', False), bind=True)
            username = record.user.username if record else None
            if record is not None:
                request.state.cookie_authenticated = True
                role, account, method = record.user.role, record.user.account, record.method  # survive a best-effort rollback
                step_up = record.step_up and not record.passkey_verified
            provider = s.get(OIDCProvider, 1) if settings.organizations_enabled else None
            sso = bool(provider and provider.enabled and provider.issuer and provider.client_id)
            try:
                s.commit()  # at most a last-seen update; never worth failing the request
            except OperationalError:
                s.rollback()
        out = {"setup_required": setup_required, "authenticated": username is not None, "username": username,
               "sso": sso}  # whether the sign-in page offers company sign-in (organizations mode, turned on by an owner)
        if record is not None:
            out["role"] = role
            out["sign_in_method"] = method or "local"
            out["step_up_required"] = step_up  # signed in with the company, but a passkey check must come first
            out["account"] = account or settings.default_account
        if username and not out.get("step_up_required"):  # how this installation names itself (not shown before login)
            out["instance"] = {"host": settings.host_label, "agent_user": settings.agent_user,
                               "wildcard_zone": settings.wildcard_zone, "version": __version__,
                               "build": ctx.updates.build,
                               "source_url": settings.source_url,
                               # this exact version's source, once install.sh has built it
                               "source_download": "/api/nodes/bundle"
                               if (settings.bundle_dir / f"acc-{__version__}.tar.gz").is_file() else None,
                               "private_ca": settings.node_ca_file is not None,
                               "features": {key: settings.features.enabled(key) for key in admin_features.FEATURES},
                               "organizations": settings.organizations_enabled,
                               "orchestrator": orchestrator_enabled(),  # effective Orchestrator v2 availability
                               # New project: where its folder goes, and whether a GitHub repository can be made
                               "workspaces_root": str(settings.account_root(out["account"])),
                               "github": ctx.supervisor.github_available()}
        return out

    @api.post("/api/auth/setup/begin")
    def setup_begin(body: SetupBeginIn, request: Request):
        if not origin_ok(request.headers.get("origin")):
            raise HTTPException(403, "This request came from a page at a different address. Open Rynhult at its own address, reload the page and try again.")
        key = client_key(request)
        if throttle.blocked(key):
            raise HTTPException(429, "Too many attempts. Wait 10 minutes, then try again.")
        with db() as s:
            expected = auth.ensure_setup_token(s, settings.setup_token_path)
        if expected is None:
            raise HTTPException(409, "Rynhult is already set up. Sign in instead.")
        if not secrets.compare_digest(body.setup_token.strip(), expected):
            throttle.fail(key)
            raise HTTPException(403, "That setup token is wrong. Read it again on the server and paste it exactly.")
        secret = enrolment_secret(pending_setup, expected)
        uri = auth.otpauth_uri(secret, "owner", issuer=f"Rynhult {settings.host_label}")
        return {"totp_secret": secret, "otpauth_uri": uri, "qr_svg": auth.qr_svg(uri)}

    @api.post("/api/auth/setup/complete")
    def setup_complete(body: SetupCompleteIn, request: Request, response: Response):
        if not origin_ok(request.headers.get("origin")):
            raise HTTPException(403, "This request came from a page at a different address. Open Rynhult at its own address, reload the page and try again.")
        with db() as s:
            expected = auth.ensure_setup_token(s, settings.setup_token_path)
            if expected is None or not secrets.compare_digest(body.setup_token.strip(), expected):
                raise HTTPException(403, "That setup token is wrong or was already used. Reload the page and start setup again.")
            secret, issued = pending_setup.get(expected, (None, 0))
            if secret is None or time.time() - issued > 900:
                raise HTTPException(409, "The authenticator setup expired. Start setup again.")
            step = auth.totp_ok(secret, body.code, None)
            if step is None:
                raise HTTPException(403, "That authenticator code didn't match. Check that the device's clock is right, then enter a new code.")
            if auth.reserved(body.username.strip()):
                raise HTTPException(422, f"{body.username.strip()!r} is reserved for Rynhult itself. Choose another username.")
            if auth.taken(s, body.username.strip()):
                raise HTTPException(409, "That username is taken. Choose another one.")
            user = User(username=body.username.strip(), password_hash=auth.hash_password(body.password),
                        totp_secret=secret, totp_last_step=step, role="owner")  # whoever sets Rynhult up owns it
            s.add(user)
            s.flush()
            session_token, csrf = ctx.create_login_session(s, user, request, verified=True)
            s.add(AuditLog(actor=user.username, action="auth.setup", target=user.username))
            s.commit()
            auth.ensure_setup_token(s, settings.setup_token_path)  # deletes the token file
        pending_setup.clear()
        set_login_cookies(response, session_token, csrf)
        return {"ok": True, "username": body.username.strip()}

    @api.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        if not origin_ok(request.headers.get("origin")):
            raise HTTPException(403, "This request came from a page at a different address. Open Rynhult at its own address, reload the page and try again.")
        key = client_key(request)
        name = body.username.strip()
        keys = (f"ip:{key}", f"user:{name.lower()}")
        if throttle.blocked(keys[0]):
            raise HTTPException(429, "Too many attempts. Wait 10 minutes, then try again.")
        # A locked account still lets in a fully correct password and code: anyone (a local agent through Caddy,
        # say) can fail ten times for a name, and that must not lock its owner out. Guesses stay capped per address.
        locked = throttle.blocked(keys[1])
        for k in keys:  # counted before checking, so parallel guesses can't all slip through
            throttle.fail(k)
        # The slow part (argon2) runs outside any write transaction: a flood of logins never holds the database lock.
        intent = WRITE_INTENT.set(False)
        try:
            with db() as s:
                user = s.scalar(select(User).where(User.username == name))
                stored = (user.id, user.password_hash, user.totp_secret, user.totp_last_step) if user else None
                s.rollback()
        finally:
            WRITE_INTENT.reset(intent)
        password_ok = auth.password_ok(stored[1] if stored else None, body.password)
        step = auth.totp_ok(stored[2], body.code, stored[3]) if password_ok else None
        with db() as s:
            user = s.get(User, stored[0]) if step is not None else None
            if user is None or (user.totp_last_step is not None and step <= user.totp_last_step):  # replayed meanwhile
                s.add(AuditLog(actor=name[:64], action="auth.login_failed", target=key))
                s.commit()
                if locked:
                    raise HTTPException(429, "Too many attempts. Wait 10 minutes, then try again.")
                raise HTTPException(401, "That sign-in didn't work. Check the username, password and code.")
            if passkeys.required(s, user):
                s.add(AuditLog(actor=user.username, action="auth.login_failed", target=key, detail={"reason": "passkey_required"}))
                s.commit()
                raise HTTPException(403, "A passkey is required. Use a passkey or your offline recovery code.")
            for k in keys:
                throttle.reset(k)
            user.totp_last_step = step
            session_token, csrf = ctx.create_login_session(s, user, request, verified=True)
            s.add(AuditLog(actor=user.username, action="auth.login", target=key))
            s.commit()
        set_login_cookies(response, session_token, csrf)
        return {"ok": True, "username": user.username}

    @api.get("/api/auth/csrf")
    def csrf_token(request: Request, response: Response, who: str = Depends(actor)):
        session_hash = getattr(request.state, 'session_hash', None)
        if session_hash is None:
            raise HTTPException(401, "Sign in to continue.")
        with db() as s:
            record = s.get(AuthSession, session_hash)
            if record is None:
                raise HTTPException(401, "Sign in to continue.")
            token = record.csrf
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Vary'] = 'Cookie'
        response.set_cookie(ctx.csrf_cookie, token, httponly=False, secure=settings.cookie_secure,
                            samesite='strict', path='/')
        return {'csrf': token}

    @api.post("/api/auth/logout")
    async def logout(request: Request, response: Response, who: str = Depends(actor)):
        token = request.cookies.get(ctx.session_cookie)
        with db() as s:
            auth.end_session(s, token)
            s.commit()
        await ctx.close_terminals(auth.digest(token or ""))  # terminals opened with this login close now
        response.delete_cookie(ctx.session_cookie, path="/", secure=settings.cookie_secure, httponly=True, samesite="strict")
        response.delete_cookie(ctx.csrf_cookie, path="/", secure=settings.cookie_secure, samesite="strict")
        if settings.cookie_secure:
            ctx.expire_legacy_cookies(response)
        return {"ok": True}

    # ------------------------------------------------------------ invites (one-time links from an owner)

    pending_invites: dict[str, tuple[str, float]] = {}  # invite token hash -> (authenticator secret, issued at)

    def open_invite(s, token: str) -> Invite:
        invite = s.scalar(select(Invite).where(Invite.token_hash == auth.digest(token.strip())))
        if invite is None or invite.used_at is not None or invite.expires_at < auth.now():
            raise HTTPException(410, "This link has expired or was already used. Ask an owner for a new one.")
        return invite

    @api.post("/api/auth/invite/begin")
    def invite_begin(body: InviteBeginIn, request: Request):
        """Who the link is for, and its unexpired authenticator secret to enrol."""
        if not origin_ok(request.headers.get("origin")):
            raise HTTPException(403, "This request came from a page at a different address. Open Rynhult at its own address, reload the page and try again.")
        key = f"ip:{client_key(request)}"
        if throttle.blocked(key):
            raise HTTPException(429, "Too many attempts. Wait 10 minutes, then try again.")
        with db() as s:
            try:
                invite = open_invite(s, body.token)
            except HTTPException:
                throttle.fail(key)
                raise
            existing = s.get(User, invite.user_id) if invite.user_id else None
            name = existing.username if existing else None
        secret = enrolment_secret(pending_invites, invite.token_hash)
        uri = auth.otpauth_uri(secret, name or "new user", issuer=f"Rynhult {settings.host_label}")
        return {"reset_for": name, "role": invite.role, "totp_secret": secret, "otpauth_uri": uri, "qr_svg": auth.qr_svg(uri)}

    @api.post("/api/auth/invite/complete")
    async def invite_complete(body: InviteCompleteIn, request: Request, response: Response):
        if not origin_ok(request.headers.get("origin")):
            raise HTTPException(403, "This request came from a page at a different address. Open Rynhult at its own address, reload the page and try again.")
        with db() as s:
            invite = open_invite(s, body.token)
            reset_user = s.get(User, invite.user_id) if invite.user_id else None
            role = reset_user.role if reset_user else invite.role
            if passkeys.policy(s)['owners' if role == 'owner' else 'members'] == 'require':
                raise HTTPException(409, "Passkeys are required for this role. Ask an owner to set it to Allow in Settings → Passkeys before you join or reset your sign-in.")
            secret, issued = pending_invites.get(invite.token_hash, (None, 0))
            if secret is None or time.time() - issued > 900:
                raise HTTPException(409, "The authenticator setup expired. Start again.")
            step = auth.totp_ok(secret, body.code, None)
            if step is None:
                raise HTTPException(403, "That authenticator code didn't match. Check that the device's clock is right, then enter a new code.")
            if invite.user_id:  # a reset: new password and authenticator, every old sign-in ends
                user = s.get(User, invite.user_id)
                old = [r.token_hash for r in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id))]
                for record in s.scalars(select(AuthSession).where(AuthSession.user_id == user.id)):
                    s.delete(record)
                s.execute(delete(Passkey).where(Passkey.user_id == user.id))
                s.execute(delete(PasskeyChallenge).where(PasskeyChallenge.user_id == user.id))
                user.passkey_handle, user.recovery_hash = None, None
                # A password and authenticator of their own: no longer a member only company sign-in lets in (so
                # removing their company sign-in later keeps them, and passkeys they add sign in like anyone's).
                user.company_only = False
                action = "auth.reset"
            else:
                name = (body.username or "").strip()
                if not name:
                    raise HTTPException(422, "Choose a username.")
                if auth.reserved(name):
                    raise HTTPException(422, f"{name!r} is reserved for Rynhult itself. Choose another username.")
                if auth.taken(s, name):
                    raise HTTPException(409, "That username is taken. Choose another one.")
                refusal = account_refusal(s, invite.role, invite.account or settings.default_account,
                                          ctx.broker.default_account, ctx.broker.services_account)
                if refusal:  # another invite for the same account was used first
                    raise HTTPException(409, f"{refusal} Ask an owner for a new invite.")
                user = User(username=name, role=invite.role, account=invite.account, password_hash="", totp_secret="")
                s.add(user)
                old, action = [], "auth.joined"
            user.password_hash, user.totp_secret, user.totp_last_step = auth.hash_password(body.password), secret, step
            invite.used_at = auth.now()
            s.flush()
            session_token, csrf = ctx.create_login_session(s, user, request, verified=True)
            s.add(AuditLog(actor=user.username, action=action, target=user.username,
                           detail={"role": user.role, "account": user.account, "invite": invite.id}))
            s.commit()
            username = user.username
        pending_invites.pop(invite.token_hash, None)
        for session_hash in old:
            await ctx.close_terminals(session_hash)
        set_login_cookies(response, session_token, csrf)
        return {"ok": True, "username": username}

    # ------------------------------------------------------------ your own sign-ins and password

    @api.get("/api/auth/sessions")
    def my_sessions(request: Request, who=Depends(ctx.principal)):
        """Where you are signed in. Ids are a prefix of the stored hash, never the cookie."""
        current = getattr(request.state, "session_hash", None)
        with db() as s:
            records = s.scalars(select(AuthSession).where(AuthSession.user_id == who.user_id)
                                .order_by(AuthSession.last_seen_at.desc())).all() if who.user_id else []
            return [{"id": r.token_hash[:16], "user_agent": r.user_agent, "created_at": r.created_at,
                     "last_seen_at": r.last_seen_at, "expires_at": r.expires_at, "current": r.token_hash == current}
                    for r in records]

    async def _revoke(who, keep: str | None, only: str | None = None) -> int:
        with db() as s:
            records = [r for r in s.scalars(select(AuthSession).where(AuthSession.user_id == who.user_id))
                       if r.token_hash != keep and (only is None or r.token_hash.startswith(only))]
            hashes = [r.token_hash for r in records]
            for record in records:
                s.delete(record)
            s.add(AuditLog(actor=who.username, action="auth.sessions_revoked", target=who.username, detail={"count": len(hashes)}))
            s.commit()
        for session_hash in hashes:
            await ctx.close_terminals(session_hash)
        return len(hashes)

    @api.post("/api/auth/sessions/{session_id}/revoke")
    async def revoke_session(session_id: str, request: Request, who=Depends(ctx.principal)):
        if len(session_id) != 16 or not session_id.isalnum():
            raise HTTPException(404, "Sign-in not found. It may have ended already.")
        if (getattr(request.state, "session_hash", "") or "").startswith(session_id):
            raise HTTPException(409, "This is the sign-in you are using now. Use Sign out to end it.")
        if not await _revoke(who, keep=None, only=session_id):
            raise HTTPException(404, "Sign-in not found. It may have ended already.")
        return {"ok": True}

    @api.post("/api/auth/sessions/revoke-others")
    async def revoke_others(request: Request, who=Depends(ctx.principal)):
        return {"revoked": await _revoke(who, keep=getattr(request.state, "session_hash", None))}

    @api.post("/api/auth/password")
    async def change_password(body: PasswordIn, request: Request, who=Depends(ctx.principal)):
        """New password; every other sign-in ends."""
        if who.user_id is None:
            raise HTTPException(409, "The API token has no password to change. Sign in as a person to change your password.")
        key = f"user:{who.username.lower()}"
        if throttle.blocked(key):
            raise HTTPException(429, "Too many attempts. Wait 10 minutes, then try again.")
        intent = WRITE_INTENT.set(False)
        try:
            with db() as s:
                stored = s.get(User, who.user_id).password_hash
                s.rollback()
        finally:
            WRITE_INTENT.reset(intent)
        if not auth.password_ok(stored, body.current_password):
            throttle.fail(key)
            raise HTTPException(403, "Your current password is wrong.")
        with db() as s:
            s.get(User, who.user_id).password_hash = auth.hash_password(body.new_password)
            s.add(AuditLog(actor=who.username, action="auth.password_changed", target=who.username))
            s.commit()
        await _revoke(who, keep=getattr(request.state, "session_hash", None))
        return {"ok": True}

    return api
