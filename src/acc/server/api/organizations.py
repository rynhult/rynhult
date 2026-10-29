"""Company sign-in (OIDC) for organizations mode: owner settings, identity bindings and the sign-in flow.

Hidden (404) unless ACC_ORGANIZATIONS=1, and single sign-on stays off until an owner turns it on. Local sign-in
(password + TOTP, passkeys) never depends on any of this: it is the break-glass path when the provider is down.
Owners never sign in through the provider: installation authority is never mapped from an IdP (docs/ORGANIZATIONS.md).
"""
from __future__ import annotations

import asyncio
import secrets
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from ...organization_ids import person_id
from .. import auth, passkeys
from ..db import WRITE_INTENT
from ..models import AuditLog, AuthSession, ExternalIdentity, OIDCFlow, OIDCProvider, Passkey, Setting, SSOInvite, User
from ..oidc import (
    ENROLLMENT_MAX_AUTH_AGE,
    FLOW_SECONDS,
    MAX_ACR_VALUES,
    OIDCError,
    acr_refusal,
    check_issuer,
    diagnostic_text,
    directory_names,
    enrollment_authentication_recent,
    fold,
    mfa_proven,
    new_secret,
    private_entry_refusal,
)
from ..vault import VaultError
from .context import HOST_PREFIX, Context
from .users import account_refusal

ORGANIZATION = "installation"  # one organization per installation (decision 0008)
# Company sign-ins are shorter than local ones (an IdP disable is only seen at the next sign-in): an owner setting,
# 12 hours unless changed, within these bounds.
SESSION_HOURS, MIN_SESSION_HOURS, MAX_SESSION_HOURS = 12, 1, 24
RECENT_LOCAL = timedelta(minutes=15)  # linking an existing member needs a local sign-in this fresh
INVITE_HOURS = 7 * 24
# Open (started, not yet returned) sign-in attempts. A browser holds one: starting again replaces it. Beyond that,
# an address (an office NAT, an IPv6 /64), a sign-in link and a local sign-in being linked each hold only so many.
MAX_OPEN_PER_ADDRESS, MAX_OPEN_PER_INVITE, MAX_OPEN_PER_SESSION = 30, 3, 3
NAMESPACE_KEY = "organizations.namespace"
PRIVATE_HOSTS_KEY = "organizations.oidc_private_hosts"  # owner-allowed LAN provider hosts with their confirmed DNS
MAX_PRIVATE_HOSTS = 20
BINDING_COOKIE = "acc_oidc"
USERNAME = r"^[A-Za-z0-9][A-Za-z0-9._@-]{0,63}$"
ACCOUNT = r"^[a-z_][a-z0-9_-]{0,31}$"


class ProviderIn(BaseModel):
    issuer: str = Field("", max_length=300)
    client_id: str = Field("", max_length=200)
    client_secret: str | None = Field(None, max_length=4096)  # None keeps the stored one; "" removes it
    enabled: bool = False
    session_hours: int = Field(SESSION_HOURS, ge=MIN_SESSION_HOURS, le=MAX_SESSION_HOURS)
    mfa_acr: list[str] = Field(default_factory=list, max_length=MAX_ACR_VALUES)  # this tenant's acr values for MFA
    passkey_enrollment: bool = True  # company-only members may add a passkey right after an MFA-proven sign-in


class ProbeIn(BaseModel):
    issuer: str | None = Field(None, max_length=300)


class PrivateHostResolve(BaseModel):
    url: str = Field(min_length=9, max_length=300)


class PrivateHostIn(PrivateHostResolve):
    addresses: list[str] = Field(min_length=1, max_length=32)


class IdentityIn(BaseModel):
    username: str = Field(pattern=USERNAME)
    subject: str = Field(min_length=1, max_length=255)
    account: str | None = Field(None, pattern=ACCOUNT)


class SSOInviteIn(BaseModel):
    username: str = Field(pattern=USERNAME)  # a new member's name, or the existing member to link
    # Their directory account, e.g. robin@example.com: the provider's verified email or UPN must be exactly this.
    email: str = Field(min_length=3, max_length=320, pattern=r"^[^@\s]+@[^@\s]+$")
    account: str | None = Field(None, pattern=ACCOUNT)
    existing: bool = False


def secret_aad(issuer: str, client_id: str) -> str:
    # Bound to the issuer and client: a secret moved to another provider row or configuration does not decrypt.
    return f"oidc:client_secret:{issuer}:{client_id}"


def admitted_by_directory(s, issuer: str, subject: str, claims: dict) -> User | None:
    """Hook for directory-driven membership (docs/ORGANIZATIONS.md, phase 1 SCIM / group sync): a person whose
    verified identity a synchronized directory group or organizational unit maps to a member. Not built yet, so it
    admits nobody; per-person invites and pre-provisioning are the only ways in. When built, it must look up the
    synchronized binding by (issuer, subject) or an immutable directory attribute, never by email, and keep owners out."""
    return None


def router(ctx: Context) -> APIRouter:
    settings, db = ctx.settings, ctx.db
    api = APIRouter()
    throttle = auth.LoginThrottle(limit=30, window=600.0)  # failed callbacks of this browser's own flows, per address
    cookie = (HOST_PREFIX if settings.cookie_secure else "") + BINDING_COOKIE

    @contextmanager
    def writing():
        token = WRITE_INTENT.set(True)  # GET routes here write: take the write lock up front (db.WRITE_INTENT)
        try:
            with db() as s:
                yield s
        finally:
            WRITE_INTENT.reset(token)

    def org_owner(request: Request):
        if not settings.organizations_enabled:
            raise HTTPException(404, "Not Found")
        return ctx.owner(request)

    def provider_row(s, *, lock=False) -> OIDCProvider | None:
        query = select(OIDCProvider).where(OIDCProvider.id == 1)
        return s.scalar(query.with_for_update() if lock else query)

    def active(s) -> OIDCProvider | None:
        row = provider_row(s) if settings.organizations_enabled else None
        return row if row is not None and row.enabled and row.issuer and row.client_id else None

    def sealed(s, issuer, client_id, value) -> str:
        from ..models import InfrastructureCredential, VaultKey
        store = ctx.supervisor.vault
        empty = not s.scalar(select(VaultKey.id).limit(1)) and not s.scalar(select(InfrastructureCredential.id).limit(1))
        return store.cipher.seal("oidc", secret_aad(issuer, client_id), value, create=empty)

    def client_secret(row: OIDCProvider) -> str | None:
        if not row.secret_ciphertext:
            return None
        return ctx.supervisor.vault.cipher.open("oidc", secret_aad(row.issuer, row.client_id), row.secret_ciphertext)

    def public(row: OIDCProvider | None) -> dict:
        return {"issuer": row.issuer if row else "", "client_id": row.client_id if row else "",
                "has_secret": bool(row and row.secret_ciphertext), "enabled": bool(row and row.enabled),
                "session_hours": row.session_hours if row and row.session_hours else SESSION_HOURS,
                "mfa_acr": list(row.mfa_acr or []) if row else [],
                "passkey_enrollment": bool(row.passkey_enrollment) if row and row.passkey_enrollment is not None else True,
                "redirect_uri": settings.oidc_redirect_uri}

    def revoke_sso_sessions(s, user_id: int | None = None) -> list[str]:
        query = select(AuthSession).where(AuthSession.method == "oidc")
        if user_id is not None:
            query = query.where(AuthSession.user_id == user_id)
        records = list(s.scalars(query))
        for record in records:
            s.delete(record)
        return [r.token_hash for r in records]

    def private_hosts(s) -> tuple:
        row = s.get(Setting, PRIVATE_HOSTS_KEY)
        return tuple(row.value.get("hosts", ())) if row else ()

    def namespace(s) -> str:
        row = s.get(Setting, NAMESPACE_KEY)
        if row is None:
            row = Setting(key=NAMESPACE_KEY, value={"uuid": str(uuid.uuid4())}, updated_by="system")
            s.add(row)
            s.flush()
        return row.value["uuid"]

    def bind(s, user: User, issuer: str, subject: str, by: str) -> ExternalIdentity:
        if user.role == "owner":
            raise HTTPException(409, "Owners sign in with their own passkey or password, never through company sign-in.")
        if user.person_id is None:
            user.person_id = person_id(namespace(s), user.id)
            s.flush()
        identity = ExternalIdentity(organization=ORGANIZATION, issuer=issuer, subject=subject,
                                    person_id=user.person_id, created_by=by)
        s.add(identity)
        s.flush()  # IntegrityError: this (issuer, subject) is already someone's
        return identity

    def new_member(s, username: str, account: str | None) -> User:
        if auth.reserved(username):
            raise HTTPException(422, f"{username!r} is reserved for Rynhult itself. Choose another username.")
        if auth.taken(s, username):
            raise HTTPException(409, "That username is taken. Choose another one.")
        refusal = account_refusal(s, "member", account or settings.default_account,
                                  ctx.broker.default_account, ctx.broker.services_account)
        if refusal:
            raise HTTPException(409, refusal)
        # No usable local credentials: a random password nobody knows and a random authenticator secret.
        user = User(username=username, role="member", account=account, password_hash=auth.hash_password(new_secret()),
                    totp_secret=auth.new_totp_secret(), company_only=True)
        s.add(user)
        s.flush()
        return user

    # ------------------------------------------------------------ owner settings

    @api.get("/api/organizations/oidc")
    def get_provider(who=Depends(org_owner)):
        with db() as s:
            return public(provider_row(s))

    @api.put("/api/organizations/oidc")
    async def put_provider(body: ProviderIn, request: Request, who=Depends(org_owner)):
        issuer, client_id = body.issuer.strip(), body.client_id.strip()
        if issuer:
            try:
                issuer = check_issuer(issuer)
            except OIDCError as error:
                raise HTTPException(422, error.detail) from None
        if body.enabled and not (issuer and client_id):
            raise HTTPException(422, "Enter the issuer and client ID before turning company sign-in on.")
        acr_values = list(dict.fromkeys(value.strip() for value in body.mfa_acr if value.strip()))
        refusal = acr_refusal(acr_values)
        if refusal:
            raise HTTPException(422, refusal)
        with writing() as s:
            row = provider_row(s, lock=True)
            # Counting another acr value as MFA, or letting company-only members add passkeys again, relaxes sign-in as
            # relaxing the passkey policy does: the same fresh verification, so not a copied cookie or the API token.
            added = [value for value in acr_values if value not in (row.mfa_acr or [] if row else [])]
            reopened = body.passkey_enrollment and row is not None and row.passkey_enrollment is False
            if added or reopened:
                actor = s.get(User, who.user_id) if who.user_id is not None else None
                try:
                    passkeys.recently_verified(s, getattr(request.state, "session_hash", None), actor)
                except HTTPException:
                    s.add(AuditLog(actor=who.username, action="organizations.oidc_change_refused", target=issuer or None,
                                   detail={"reason": "verification_required", "mfa_acr_added": added,
                                           "passkey_enrollment": reopened}))
                    s.commit()
                    raise
            if row is None:
                row = OIDCProvider(id=1, issuer="", client_id="", revision=0)
                s.add(row)
            moved = (row.issuer, row.client_id) != (issuer, client_id)
            old_secret = None if moved or not row.secret_ciphertext else row.secret_ciphertext
            row.issuer, row.client_id = issuer, client_id
            try:
                if body.client_secret:
                    row.secret_ciphertext = sealed(s, issuer, client_id, body.client_secret.strip())
                elif body.client_secret == "":
                    row.secret_ciphertext = None
                else:  # kept, but only for the same issuer and client: it is sealed to them
                    row.secret_ciphertext = old_secret
            except VaultError as error:
                raise HTTPException(409, str(error)) from None
            turned_off = row.enabled and not body.enabled
            row.enabled, row.revision, row.updated_by = body.enabled, (row.revision or 0) + 1, who.username
            row.session_hours = body.session_hours
            row.mfa_acr, row.passkey_enrollment = acr_values, body.passkey_enrollment
            if not body.passkey_enrollment:  # windows already open close with it
                for record in s.scalars(select(AuthSession).where(AuthSession.passkey_enroll_until.is_not(None))):
                    record.passkey_enroll_until = None
            ended = revoke_sso_sessions(s) if moved or turned_off else []
            # A shorter lifetime applies to company sign-ins already open, too.
            for record in s.scalars(select(AuthSession).where(AuthSession.method == "oidc")):
                record.expires_at = min(record.expires_at, record.created_at + timedelta(hours=body.session_hours))
            s.add(AuditLog(actor=who.username, action="organizations.oidc_changed", target=issuer or None,
                           detail={"client_id": client_id, "enabled": body.enabled, "secret": bool(row.secret_ciphertext),
                                   "secret_changed": body.client_secret is not None, "sessions_ended": len(ended),
                                   "session_hours": body.session_hours, "mfa_acr": acr_values,
                                   "passkey_enrollment": body.passkey_enrollment}))
            s.commit()
            out = public(row)
        for session_hash in ended:
            await ctx.close_terminals(session_hash)
        return out

    @api.post("/api/organizations/oidc/test")
    async def test_provider(body: ProbeIn, who=Depends(org_owner)):
        """Discovery and keys only: no sign-in, no token request, nothing saved."""
        with db() as s:
            row = provider_row(s)
            issuer = body.issuer if body.issuer is not None else (row.issuer if row else "")
            client_id = row.client_id if row else ""
            allowed = private_hosts(s)
        try:
            provider = ctx.oidc.probe(check_issuer(issuer), client_id, allowed)
            found = await asyncio.to_thread(provider.discovery)
            keys = await asyncio.to_thread(provider.refresh_keys)
        except OIDCError as error:
            return {"ok": False, "reason": error.reason, "detail": error.detail}
        return {"ok": True, "issuer": found.issuer, "authorization_endpoint": found.authorization_endpoint,
                "token_endpoint": found.token_endpoint, "keys": len(keys)}

    # ------------------------------------------------------------ provider hosts on the LAN

    async def resolve_private(url: str) -> dict:
        """An https provider address an owner wants to allow, with what DNS says now: LAN addresses only."""
        try:
            target = httpx.URL(check_issuer(url))
        except (OIDCError, httpx.InvalidURL):
            raise HTTPException(422, "Enter the provider's https address, without a query or user name.") from None
        port = target.port or 443
        try:
            addresses = await asyncio.to_thread(ctx.oidc.resolver(), target.host, port)
        except OIDCError as error:
            raise HTTPException(422, error.detail) from None
        refusal = private_entry_refusal(addresses)
        if refusal:
            raise HTTPException(422, refusal)
        return {"host": target.host, "port": port, "addresses": addresses}

    @api.get("/api/organizations/oidc/private-hosts")
    def list_private_hosts(who=Depends(org_owner)):
        with db() as s:
            return {"hosts": list(private_hosts(s))}

    @api.post("/api/organizations/oidc/private-hosts/resolve")
    async def resolve_private_host(body: PrivateHostResolve, who=Depends(org_owner)):
        return await resolve_private(body.url)

    @api.post("/api/organizations/oidc/private-hosts", status_code=201)
    async def allow_private_host(body: PrivateHostIn, who=Depends(org_owner)):
        found = await resolve_private(body.url)
        if sorted(set(body.addresses)) != found["addresses"]:
            raise HTTPException(422, "The provider's addresses changed since you checked. Check them again and confirm the new ones.")
        with writing() as s:
            row = s.get(Setting, PRIVATE_HOSTS_KEY)
            hosts = [h for h in (row.value.get("hosts", []) if row else []) if (h["host"], h["port"]) != (found["host"], found["port"])]
            if len(hosts) >= MAX_PRIVATE_HOSTS:
                raise HTTPException(422, "You have reached the limit of provider addresses. Remove one you no longer use first.")
            entry = {"id": secrets.token_hex(8), **found, "added_by": who.username, "added_at": datetime.now().astimezone().isoformat()}
            value = {"hosts": [*hosts, entry]}
            if row is None:
                s.add(Setting(key=PRIVATE_HOSTS_KEY, value=value, updated_by=who.username))
            else:
                row.value, row.updated_by = value, who.username
            s.add(AuditLog(actor=who.username, action="organizations.oidc_private_host_allowed",
                           target=f"{found['host']}:{found['port']}", detail={"addresses": found["addresses"]}))
            s.commit()
        return entry

    @api.delete("/api/organizations/oidc/private-hosts/{entry_id}")
    def remove_private_host(entry_id: str, who=Depends(org_owner)):
        with writing() as s:
            row = s.get(Setting, PRIVATE_HOSTS_KEY)
            hosts = list(row.value.get("hosts", [])) if row else []
            entry = next((h for h in hosts if h["id"] == entry_id), None)
            if entry is None:
                raise HTTPException(404, "Provider address not found.")
            row.value, row.updated_by = {"hosts": [h for h in hosts if h is not entry]}, who.username
            s.add(AuditLog(actor=who.username, action="organizations.oidc_private_host_removed",
                           target=f"{entry['host']}:{entry['port']}", detail={"addresses": entry["addresses"]}))
            s.commit()
        return {"ok": True}

    # ------------------------------------------------------------ who may sign in

    @api.get("/api/organizations/identities")
    def list_identities(who=Depends(org_owner)):
        with db() as s:
            rows = s.execute(select(ExternalIdentity, User.username, User.company_only, User.role)
                             .join(User, User.person_id == ExternalIdentity.person_id).order_by(User.username)).all()
            invites = s.execute(select(SSOInvite).where(SSOInvite.used_at.is_(None), SSOInvite.withdrawn_at.is_(None),
                                                       SSOInvite.expires_at > auth.now())
                                .order_by(SSOInvite.created_at.desc())).scalars().all()
            linked = {i.user_id: s.get(User, i.user_id) for i in invites if i.user_id is not None}
            return {"identities": [{"id": i.id, "username": name, "person_id": i.person_id, "issuer": i.issuer,
                                    "subject": i.subject, "created_at": i.created_at, "last_login_at": i.last_login_at,
                                    "removes_person": removes_person(company_only, role), "owner": role == "owner"}
                                   for i, name, company_only, role in rows],
                    "invites": [{"id": i.id, "username": linked[i.user_id].username if i.user_id is not None else i.username,
                                 "existing": i.user_id is not None,
                                 "email": i.email,
                                 "account": (linked[i.user_id].account if i.user_id is not None else i.account) or settings.default_account,
                                 "created_at": i.created_at, "expires_at": i.expires_at} for i in invites]}

    @api.post("/api/organizations/identities", status_code=201)
    def preprovision(body: IdentityIn, who=Depends(org_owner)):
        """A new member who signs in with the provider subject the owner copied from it."""
        with writing() as s:
            row = provider_row(s)
            if row is None or not row.issuer:
                raise HTTPException(409, "Save the provider's issuer first.")
            user = new_member(s, body.username.strip(), body.account)
            try:
                identity = bind(s, user, row.issuer, body.subject.strip(), who.username)
            except IntegrityError:
                s.rollback()
                raise HTTPException(409, "That subject already signs in as another person.") from None
            s.add(AuditLog(actor=who.username, action="organizations.identity_bound", target=user.username,
                           detail={"issuer": row.issuer, "subject": identity.subject, "person_id": user.person_id,
                                   "how": "preprovisioned"}))
            s.commit()
            return {"id": identity.id, "username": user.username, "person_id": user.person_id}

    def removes_person(company_only: bool, role: str) -> bool:
        """Removing their company sign-in removes the person: a member only it lets in, never an owner."""
        return bool(company_only) and role != "owner"

    @api.delete("/api/organizations/identities/{identity_id}")
    async def unbind(identity_id: int, resources: str | None = Query(None, pattern=r"^(transfer|archive)$"),
                     who=Depends(org_owner)):
        """Stop a person signing in with the provider. A member company sign-in alone lets in (company_only: no password
        anyone knows, and their passkey only confirms a company sign-in) would be left with no way in, so they are
        removed through People > Remove, with the owner's `resources` choice. Anyone else keeps their local sign-in, and
        an owner is never removed this way: owners never sign in through the provider, so only the binding goes."""
        with writing() as s:
            identity = s.get(ExternalIdentity, identity_id)
            if identity is None:
                raise HTTPException(404, "Company sign-in not found for this person.")
            user = s.scalar(select(User).where(User.person_id == identity.person_id))
            if not removes_person(user.company_only, user.role):
                ended = revoke_sso_sessions(s, user.id)
                s.add(AuditLog(actor=who.username, action="organizations.identity_unbound", target=user.username,
                               detail={"issuer": identity.issuer, "subject": identity.subject,
                                       "sessions_ended": len(ended)}))
                s.delete(identity)
                s.commit()
                removing = None
            else:
                removing = user.id
                s.rollback()
        if removing is None:
            for session_hash in ended:
                await ctx.close_terminals(session_hash)
            return {"ok": True, "removed": False}
        if resources is None:
            raise HTTPException(422, "They sign in only with their company account, so this removes them from Rynhult. "
                                     "Choose whether to transfer or archive their projects, sessions and keys.")

        def unbound(s, user: User) -> None:
            identity = s.get(ExternalIdentity, identity_id)
            if (identity is None or identity.person_id != user.person_id
                    or not removes_person(user.company_only, user.role)):
                raise HTTPException(409, "Their company sign-in changed meanwhile. Reload the page, check it and try again.")
            s.add(AuditLog(actor=who.username, action="organizations.identity_unbound", target=user.username,
                           detail={"issuer": identity.issuer, "subject": identity.subject, "person_id": user.person_id,
                                   "person_removed": True}))
            s.delete(identity)

        return {**await ctx.remove_person(removing, resources, who, also=unbound), "removed": True}

    @api.post("/api/organizations/sso-invites", status_code=201)
    def create_invite(body: SSOInviteIn, who=Depends(org_owner)):
        token = auth.new_invite_token()
        with writing() as s:
            if body.existing:
                user = s.scalar(select(User).where(User.username == body.username))
                if user is None:
                    raise HTTPException(404, "Person not found.")
                if user.role == "owner":
                    raise HTTPException(409, "Owners sign in with their own passkey or password, never through company sign-in.")
                invite = SSOInvite(user_id=user.id, email=fold(body.email))
            else:
                name = body.username.strip()
                if auth.reserved(name):
                    raise HTTPException(422, f"{name!r} is reserved for Rynhult itself. Choose another username.")
                if auth.taken(s, name):
                    raise HTTPException(409, "That username is taken. Choose another one.")
                refusal = account_refusal(s, "member", body.account or settings.default_account,
                                          ctx.broker.default_account, ctx.broker.services_account)
                if refusal:
                    raise HTTPException(409, refusal)
                invite = SSOInvite(username=name, account=body.account, email=fold(body.email))
            invite.token_hash, invite.created_by = auth.digest(token), who.username
            invite.expires_at = auth.now() + timedelta(hours=INVITE_HOURS)
            s.add(invite)
            s.flush()
            s.add(AuditLog(actor=who.username, action="organizations.sso_invite", target=body.username,
                           detail={"invite": invite.id, "existing": body.existing, "email": invite.email}))
            s.commit()
        origin = settings.oidc_redirect_uri.removesuffix("/api/auth/oidc/callback")
        return {"id": invite.id, "url": f"{origin}/api/auth/oidc/start?invite={token}", "expires_at": invite.expires_at}

    @api.delete("/api/organizations/sso-invites/{invite_id}")
    def withdraw_invite(invite_id: int, who=Depends(org_owner)):
        # Every path is audited: the withdrawal, a repeat (still {"ok": true}) and each refusal.
        with writing() as s:
            invite = s.get(SSOInvite, invite_id)
            if invite is None:
                s.add(AuditLog(actor=who.username, action="org.invite_withdraw_refused", target=str(invite_id),
                               detail={"invite": invite_id, "reason": "missing"}))
                s.commit()
                raise HTTPException(404, "Sign-in link not found.")
            username = s.get(User, invite.user_id).username if invite.user_id is not None else invite.username
            if invite.used_at is not None:
                s.add(AuditLog(actor=who.username, action="org.invite_withdraw_refused", target=username,
                               detail={"invite": invite.id, "username": username, "reason": "used"}))
                s.commit()
                raise HTTPException(409, "That sign-in link was already used. It stays in the list as history.")
            action = "org.invite_withdrawn" if invite.withdrawn_at is None else "org.invite_withdraw_repeated"
            if invite.withdrawn_at is None:
                invite.withdrawn_at = auth.now()
            s.add(AuditLog(actor=who.username, action=action, target=username,
                           detail={"invite": invite.id, "username": username}))
            s.commit()
        return {"ok": True}

    # ------------------------------------------------------------ the sign-in flow (public)

    def to_login(reason: str, clear: bool = True) -> RedirectResponse:
        response = RedirectResponse(f"/login?sso={reason}", status_code=303)
        if clear:
            response.delete_cookie(cookie, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
        return response

    def refused(s, reason: str, client: str, detail: dict | None = None) -> None:
        s.add(AuditLog(actor="-", action="auth.sso_failed", target=client, detail={"reason": reason, **(detail or {})}))
        s.commit()

    def spent(invite: SSOInvite) -> str | None:
        """Why an invite link can no longer start a sign-in, or None."""
        return ("invite_withdrawn" if invite.withdrawn_at is not None else "invite_used" if invite.used_at is not None
                else "invite_expired" if invite.expires_at < auth.now() else None)

    def spent_answer(reason: str) -> RedirectResponse:
        return to_login("invite_withdrawn" if reason == "invite_withdrawn" else "invite_expired")

    def foreign(request: Request, invite: str | None) -> bool:
        """A start some other site's page made this browser send (an <img>, a form, a script): refused before it can
        store anything. Browsers say so in Origin (any origin but Rynhult's) and Sec-Fetch-Site; a plain start must
        come from Rynhult's own sign-in page or a typed address. A sign-in link may arrive from a mail client."""
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return True
        return not invite and request.headers.get("sec-fetch-site", "same-origin") in ("cross-site", "same-site")

    @api.get("/api/auth/oidc/start")
    async def start(request: Request, invite: str | None = None):
        client = ctx.client_key(request)
        if foreign(request, invite):
            return to_login("cross_site")
        if throttle.blocked(f"ip:{client}"):
            return to_login("throttled")
        with db() as s:
            row = active(s)
            if row is None:
                return to_login("disabled")
            issuer, client_id, revision = row.issuer, row.client_id, row.revision
            enrollment = row.passkey_enrollment is not False
            allowed = private_hosts(s)
            invite_id = session_hash = gone = None
            if invite:
                found = s.scalar(select(SSOInvite).where(SSOInvite.token_hash == auth.digest(invite.strip())))
                if found is None:  # nothing to name, and anyone can send one: not audited
                    return to_login("invite_expired")
                invite_id, gone = found.id, spent(found)
                if gone is None and found.user_id is not None:  # linking an existing member: their own recent local sign-in too
                    record = auth.lookup_session(s, request.cookies.get(ctx.session_cookie))
                    fresh = record and max(record.created_at, record.reauthenticated_at or record.created_at) > auth.now() - RECENT_LOCAL
                    if not record or record.user_id != found.user_id or not fresh or record.method == "oidc":
                        return to_login("link_needs_local_sign_in")
                    session_hash = record.token_hash
            s.rollback()
        if gone is not None:  # audited like the callback's refusals: the invite's id, never its token
            with writing() as s:
                refused(s, gone, client, {"invite": invite_id})
            return spent_answer(gone)
        provider = ctx.oidc.get(issuer, client_id, revision, allowed)
        state, nonce, verifier, binding = new_secret(), new_secret(), new_secret() + new_secret(), new_secret()
        try:
            target = await asyncio.to_thread(provider.authorization_url, redirect_uri=settings.oidc_redirect_uri,
                                             state=state, nonce=nonce, verifier=verifier,
                                             max_age=ENROLLMENT_MAX_AUTH_AGE if enrollment else None)
        except OIDCError as error:
            with writing() as s:
                refused(s, error.reason, client, {"detail": error.detail} if error.detail else None)
            return to_login("unavailable")
        with writing() as s:
            # Discovery ran outside the transaction; the owner may have withdrawn this invite meanwhile.
            if invite_id is not None:
                found = s.get(SSOInvite, invite_id)
                if found is None:
                    return to_login("invite_expired")
                if (gone := spent(found)) is not None:
                    refused(s, gone, client, {"invite": invite_id})
                    return spent_answer(gone)
            s.execute(delete(OIDCFlow).where(OIDCFlow.expires_at < auth.now()))
            previous = request.cookies.get(cookie)
            if previous:  # this browser's earlier attempt: replaced, never piling up
                s.execute(delete(OIDCFlow).where(OIDCFlow.binding_hash == auth.digest(previous)))

            def open_count(*where) -> int:
                return s.scalar(select(func.count()).select_from(OIDCFlow).where(*where))
            if (open_count(OIDCFlow.client == client) >= MAX_OPEN_PER_ADDRESS
                    or (invite_id is not None and open_count(OIDCFlow.invite_id == invite_id) >= MAX_OPEN_PER_INVITE)
                    or (session_hash is not None and open_count(OIDCFlow.session_hash == session_hash) >= MAX_OPEN_PER_SESSION)):
                s.rollback()
                return to_login("throttled", clear=False)
            s.add(OIDCFlow(state_hash=auth.digest(state), binding_hash=auth.digest(binding), nonce=nonce,
                           verifier=verifier, provider_revision=revision, invite_id=invite_id, session_hash=session_hash,
                           client=client, expires_at=auth.now() + timedelta(seconds=FLOW_SECONDS)))
            s.commit()
        response = RedirectResponse(target, status_code=303)
        # Lax, not Strict: the provider's redirect back is a cross-site top-level GET. httpOnly, ten minutes.
        response.set_cookie(cookie, binding, max_age=FLOW_SECONDS, httponly=True, secure=settings.cookie_secure,
                            samesite="lax", path="/")
        return response

    @api.get("/api/auth/oidc/callback")
    async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
        client = ctx.client_key(request)
        key = f"ip:{client}"
        if throttle.blocked(key):
            return to_login("throttled")
        binding = request.cookies.get(cookie, "")
        with writing() as s:
            flow = s.get(OIDCFlow, auth.digest(state)) if state and len(state) <= 128 else None
            if flow is None or not binding or not secrets.compare_digest(flow.binding_hash, auth.digest(binding)):
                # Unknown, used, or started in another browser (login CSRF): never consume someone else's flow. Only
                # counted with this browser's own flow cookie: forged links from other sites can't lock out an address.
                if binding:
                    throttle.fail(key)
                refused(s, "state", client)
                return to_login("expired", clear=False)  # a stray link must not end this browser's own flow
            # One use, committed before anything else happens: a replayed callback finds nothing.
            if s.execute(delete(OIDCFlow).where(OIDCFlow.state_hash == flow.state_hash)).rowcount != 1:
                s.rollback()
                return to_login("expired")
            nonce, verifier, revision = flow.nonce, flow.verifier, flow.provider_revision
            invite_id, link_session, expired = flow.invite_id, flow.session_hash, flow.expires_at < auth.now()
            invite = s.get(SSOInvite, invite_id) if invite_id is not None else None
            if invite is not None and invite.withdrawn_at is not None:
                refused(s, "invite_withdrawn", client, {"invite": invite_id})
                return to_login("invite_withdrawn")
            row = active(s)
            if expired or error or not code or len(code) > 4096 or row is None or row.revision != revision:
                reason = "expired" if expired else "denied" if error else "disabled" if row is None or row.revision != revision else "failed"
                throttle.fail(key)
                refused(s, reason, client)
                return to_login(reason)
            issuer, client_id, hours = row.issuer, row.client_id, row.session_hours or SESSION_HOURS
            acr_values, enrollment = list(row.mfa_acr or []), row.passkey_enrollment is not False
            allowed = private_hosts(s)
            try:
                secret = client_secret(row)
            except VaultError:
                refused(s, "secret_unreadable", client)
                return to_login("unavailable")
            s.commit()
        provider = ctx.oidc.get(issuer, client_id, revision, allowed)

        def verified() -> dict:
            tokens = provider.exchange(code=code, redirect_uri=settings.oidc_redirect_uri, verifier=verifier,
                                       client_secret=secret)
            access = tokens.get("access_token") if isinstance(tokens.get("access_token"), str) else None
            return provider.verify(tokens["id_token"], nonce=nonce, access_token=access)
        try:
            claims = await asyncio.to_thread(verified)
        except OIDCError as failure:
            throttle.fail(key)
            with writing() as s:
                invite = s.get(SSOInvite, invite_id) if invite_id is not None else None
                detail = {"issuer": issuer, **failure.provider_detail}
                if failure.detail:
                    detail["detail"] = failure.detail
                if invite is not None and invite.withdrawn_at is not None:
                    # The withdrawal decides the answer; the provider's own error still matters to the owner.
                    refused(s, "invite_withdrawn", client, {**detail, "invite": invite_id,
                                                            "provider_reason": failure.reason})
                    return to_login("invite_withdrawn")
                refused(s, failure.reason, client, detail)
            return to_login("unavailable" if failure.reason == "provider_unavailable" else "failed")
        subject = claims["sub"]
        old_token = request.cookies.get(ctx.session_cookie)
        with writing() as s:
            # The provider request ran outside this transaction. Serialize final
            # admission with owner changes, which revoke existing company sessions.
            current = provider_row(s, lock=True)
            if current is None or not current.enabled or current.revision != revision:
                refused(s, "disabled", client)
                return to_login("disabled")
            invite = s.get(SSOInvite, invite_id) if invite_id is not None else None
            if invite is not None and invite.withdrawn_at is not None:
                refused(s, "invite_withdrawn", client, {"issuer": issuer, "invite": invite_id})
                return to_login("invite_withdrawn")
            identity = s.scalar(select(ExternalIdentity).where(
                ExternalIdentity.organization == ORGANIZATION, ExternalIdentity.issuer == issuer,
                ExternalIdentity.subject == subject))
            user, how = None, None
            if identity is not None:
                user = s.scalar(select(User).where(User.person_id == identity.person_id))
            elif (user := admitted_by_directory(s, issuer, subject, claims)) is not None:
                how = "directory"
            elif invite_id is not None:
                if invite is not None and invite.used_at is None and invite.expires_at >= auth.now():
                    names = directory_names(claims)
                    if invite.email not in names:  # the link is for one directory account only
                        returned = next((value.strip() for name in ("preferred_username", "upn", "email")
                                         if isinstance(value := claims.get(name), str) and fold(value.strip()) in names), None)
                        returned = diagnostic_text(returned, (code, verifier, secret or ""), limit=320) if returned else None
                        refused(s, "invite_mismatch", client, {"issuer": issuer, "subject": subject, "invite": invite.id,
                                                             "returned_account": returned or None,
                                                             "invited_account": invite.email})
                        return to_login("invite_mismatch")
                    try:
                        if invite.user_id is not None:
                            record = s.get(AuthSession, link_session) if link_session else None
                            if record is None or record.user_id != invite.user_id or record.expires_at < auth.now():
                                refused(s, "link_session_ended", client, {"issuer": issuer, "subject": subject})
                                return to_login("link_needs_local_sign_in")
                            user, how = s.get(User, invite.user_id), "linked"
                        else:
                            user, how = new_member(s, invite.username, invite.account), "invited"
                        identity = bind(s, user, issuer, subject, invite.created_by)
                    except (HTTPException, IntegrityError):
                        s.rollback()
                        refused(s, "invite_conflict", client, {"issuer": issuer, "subject": subject})
                        return to_login("failed")
                    invite.used_at = auth.now()
                    s.add(AuditLog(actor=user.username, action="organizations.identity_bound", target=user.username,
                                   detail={"issuer": issuer, "subject": subject, "person_id": user.person_id,
                                           "how": how, "invite": invite.id, "approved_by": invite.created_by}))
            if user is None:
                refused(s, "not_provisioned", client, {"issuer": issuer, "subject": subject})
                return to_login("not_provisioned")
            if user.role == "owner":  # promoted after binding: owner authority never comes from the provider
                refused(s, "owner", client, {"issuer": issuer, "subject": subject})
                return to_login("owner_local_only")
            # Company MFA counts only when the token proves it; otherwise a required passkey is still required.
            mfa = mfa_proven(claims, acr_values)
            step_up = passkeys.required(s, user) and not mfa
            if step_up and s.scalar(select(Passkey.id).where(Passkey.user_id == user.id).limit(1)) is None:
                s.rollback()  # nothing is bound or used: without MFA proof this person can't get in yet
                refused(s, "mfa_required", client, {"issuer": issuer, "subject": subject})
                return to_login("mfa_required")
            # Session fixation: whatever sign-in this browser presented ends; the new one has fresh tokens.
            ended = []
            if old_token and s.get(AuthSession, auth.digest(old_token)) is not None:
                ended.append(auth.digest(old_token))
                auth.end_session(s, old_token)
            identity.last_login_at = auth.now()
            session_token, csrf = auth.create_session(s, user, request.headers.get("user-agent", ""), method="oidc",
                                                      lifetime=timedelta(hours=hours))
            s.flush()
            record = s.get(AuthSession, auth.digest(session_token))
            record.step_up = step_up
            # A member with no local credentials can't verify again to add a passkey; MFA just proven stands in, once
            # and briefly (decision 0061).
            enroll = mfa and enrollment and user.company_only and enrollment_authentication_recent(claims, auth.now().timestamp())
            if enroll:
                record.passkey_enroll_until = auth.now() + timedelta(minutes=passkeys.REAUTH_MINUTES)
            s.add(AuditLog(actor=user.username, action="auth.login", target=client,
                           detail={"method": "oidc", "issuer": issuer, "mfa": mfa, "passkey_step_up": step_up,
                                   "passkey_enrollment": enroll}))
            s.commit()
        for session_hash in ended:
            await ctx.close_terminals(session_hash)
        throttle.reset(key)
        response = RedirectResponse("/", status_code=303)
        response.delete_cookie(cookie, path="/", secure=settings.cookie_secure, httponly=True, samesite="lax")
        ctx.set_login_cookies(response, session_token, csrf, max_age=hours * 3600)
        return response

    return api
