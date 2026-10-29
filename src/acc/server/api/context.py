"""Shared state for the API routers: settings, database, supervisor, broker, publisher, and the auth plumbing.

Auth: the app login (password + TOTP, server-side sessions, CSRF double-submit, strict Origin checks); the
bootstrap bearer token (data/secrets/api_token) remains for local CLI use.
"""
from __future__ import annotations

import ipaddress
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import timedelta

from fastapi import HTTPException, Request, Response, WebSocket
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .. import auth, devices
from ..broker_client import BrokerError, Brokers, BrokerUnavailable, MachineOffline
from ..config import Settings
from ..models import API_PRINCIPAL, Node, Project, Session
from ..public_errors import public_error as _public_error
from ..public_errors import unknown_method
from ..publish import Publisher
from ..supervisor import Supervisor
from ..tty import Writers


@contextmanager
def refresh_read():
    """A contended optional GET refresh keeps its last cache; the next refresh retries.

    Refresh methods own their sessions, so these are rolled back before we read the cache.
    Do not hide other database failures.
    """
    try:
        yield
    except OperationalError as error:
        code = getattr(error.orig, "sqlite_errorcode", 0)
        if code & 0xff != sqlite3.SQLITE_BUSY and getattr(error.orig, "sqlstate", None) not in {"55P03", "40001"}:
            raise


SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
SESSION_COOKIE, CSRF_COOKIE, CSRF_HEADER = "acc_session", "acc_csrf", "x-acc-csrf"
# Over HTTPS the cookies carry the __Host- prefix: browsers then refuse any version set with a Domain or from another
# host, so a site published under the same domain can't plant or overwrite them.
HOST_PREFIX = "__Host-"
UNSAFE = {"POST", "PUT", "PATCH", "DELETE"}
# What a company sign-in still waiting for its passkey check may do (auth.lookup_session).
STEP_UP_ROUTES = {("GET", "/api/auth/csrf"), ("POST", "/api/auth/logout"), ("POST", "/api/auth/passkeys/verify/begin"),
                  ("POST", "/api/auth/passkeys/verify/complete")}


def network_key(address: str) -> str:
    """What rate limits count per: the address, but an IPv6 client's /64 (one site's or device's prefix), so rotating
    through its own addresses doesn't buy more attempts."""
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


@dataclass(frozen=True)
class Principal:
    """Who is asking: their role and the agent account their sessions run as."""
    username: str
    role: str  # owner | member
    account: str
    user_id: int | None = None

    @property
    def is_owner(self) -> bool:
        return self.role == "owner"


@dataclass
class Context:
    settings: Settings
    db: object  # sessionmaker
    supervisor: Supervisor
    broker: Brokers
    publisher: Publisher
    token: str
    writers: Writers = field(default_factory=Writers)
    remote_access: object = None
    device_imports: devices.Imports = field(default_factory=devices.Imports)
    throttle: auth.LoginThrottle = field(default_factory=auth.LoginThrottle)
    # Public downloads (the source bundle, the node bootstrap): per client address, every request counts.
    downloads: auth.LoginThrottle = field(default_factory=lambda: auth.LoginThrottle(limit=30, window=600.0))
    # Terminal text snapshots: per person, every read counts (each makes the broker capture the whole scrollback).
    terminal_reads: auth.LoginThrottle = field(default_factory=lambda: auth.LoginThrottle(limit=20, window=60.0))
    pending_setup: dict[str, tuple[str, float]] = field(default_factory=dict)  # setup token -> (totp secret, issued at)
    terminals: dict[str, set] = field(default_factory=dict)  # login session (token hash) -> its open terminal websockets
    edge: object = None  # edge.Edge: open public connections by client leaf (set by create_app)
    oidc: object = None  # oidc.Providers: company sign-in's discovery and key cache (set by create_app)
    remove_person: object = None  # People > Remove's whole path (set by the users router; company sign-in uses it too)
    terminal_recheck: float = 30.0  # seconds between checks that a terminal's login is still valid

    @property
    def session_cookie(self) -> str:
        return (HOST_PREFIX if self.settings.cookie_secure else "") + SESSION_COOKIE

    @property
    def csrf_cookie(self) -> str:
        return (HOST_PREFIX if self.settings.cookie_secure else "") + CSRF_COOKIE

    def origin_ok(self, origin: str | None) -> bool:
        return origin is None or origin in self.settings.allowed_origins

    def actor(self, request: Request) -> str:
        header = request.headers.get("authorization", "")
        if header:
            if getattr(request.state, 'edge_native', False):
                raise HTTPException(403, 'Native devices require a person sign-in, not an installation API token.')
            if secrets.compare_digest(header.encode(), f"Bearer {self.token}".encode()):
                request.state.principal = Principal(API_PRINCIPAL, "owner", self.settings.default_account)
                return API_PRINCIPAL
            raise HTTPException(401, "That API token isn't valid.")
        with self.db() as s:
            record = auth.lookup_session(s, request.cookies.get(self.session_cookie),
                                         allow_step_up=(request.method, request.url.path) in STEP_UP_ROUTES,
                                         edge_fingerprint=getattr(request.state, 'edge_fingerprint', None),
                                         native=getattr(request.state, 'edge_native', False),
                                         bind=request.url.path in ('/api/mobile/v1/session/bind', '/api/auth/logout',
                                             '/api/auth/passkeys/reauth', '/api/auth/passkeys/verify/begin',
                                             '/api/auth/passkeys/verify/complete'))
            if record is None:
                raise HTTPException(401, "Sign in to continue.")
            request.state.cookie_authenticated = True
            if request.method in UNSAFE:
                logout = request.method == "POST" and request.url.path == "/api/auth/logout"
                origin = request.headers.get("origin")
                if not self.origin_ok(origin) or (logout and (origin not in self.settings.allowed_origins or origin != f"{'https' if self.settings.cookie_secure else request.url.scheme}://{request.url.netloc}")):
                    raise HTTPException(403, "Open Rynhult at its own address, then try again.")
                if not logout and not secrets.compare_digest(request.headers.get(CSRF_HEADER, "").encode(), record.csrf.encode()):
                    raise HTTPException(403, "Reload the page and try again.", headers={"X-ACC-CSRF-Error": "mismatch"})
            username = record.user.username
            request.state.principal = Principal(username, record.user.role, record.user.account or self.settings.default_account,
                                                record.user.id)
            request.state.session_hash = record.token_hash
            try:
                s.commit()  # at most a last-seen update (every 5 minutes); never worth failing a request over
            except OperationalError:
                s.rollback()
            return username

    def principal(self, request: Request) -> Principal:
        """FastAPI dependency: the signed-in user (401 otherwise)."""
        self.actor(request)
        return request.state.principal

    async def close_terminals(self, session_hash: str) -> None:
        """Close every terminal opened with one login (it was logged out or revoked)."""
        for ws in list(self.terminals.pop(session_hash, ())):
            try:
                await ws.send_text('{"t": "closed", "reason": "signed out"}')
                await ws.close(code=4401)
            except (RuntimeError, OSError):  # already closed; OSError: the edge guard ended this public stream
                pass

    def owner(self, request: Request) -> Principal:
        """FastAPI dependency for owner-only routes (people, host, audit)."""
        who = self.principal(request)
        if not who.is_owner:
            raise HTTPException(403, "Only an owner can do this. Ask an owner.")
        return who

    # ---- ownership: owners see everything; members only rows they own. Others' rows answer 404, never 403, so ids
    # don't reveal what exists.

    @staticmethod
    def scope(query, model, who: Principal):
        if model is Project:
            query = query.where(Project.group_id.is_(None))
        return query if who.is_owner else query.where(model.owner == who.username)

    @staticmethod
    def inbox_scope(query, model, who: Principal):
        """Inbox items are personal: your own, plus (owners) the ones that belong to nobody: approvals, the host, ACC.
        Owners don't get members' session or login alerts."""
        if who.is_owner:
            return query.where((model.owner.is_(None)) | (model.owner == who.username))
        return query.where(model.owner == who.username)

    @staticmethod
    def owns(row, who: Principal) -> bool:
        return (row is not None and not (isinstance(row, Project) and row.group_id is not None)
                and (who.is_owner or getattr(row, "owner", None) == who.username))

    def project_for(self, s, who: Principal, project_id: int) -> Project:
        project = s.get(Project, project_id)
        if not self.owns(project, who):
            raise HTTPException(404, "That project wasn't found.")
        if not who.is_owner and project.host != 'local':
            from ..group_remote import reserved_for_other
            if reserved_for_other(s, project.host, project.account, who.username):
                raise HTTPException(404, 'That project wasn\'t found.')
        return project

    def session_for(self, s, who: Principal, acc_id: str) -> Session:
        session = s.get(Session, acc_id)
        from ..group_board import authorized
        if not self.owns(session, who) or (session and session.project and session.project.group_id and not who.is_owner and not authorized(s, session, self.settings.groups_remote_enabled)):
            raise HTTPException(404, "That session wasn't found.")
        if not who.is_owner and session.host != 'local' and not (session.project and session.project.group_id):
            from ..group_remote import reserved_for_other
            if reserved_for_other(s, session.host, session.run_as, who.username):
                raise HTTPException(404, 'That session wasn\'t found.')
        return session

    def create_login_session(self, s, user, request, *, verified=False):
        """Native sign-ins are nonportable from issuance, then require explicit edge binding. `verified`: the sign-in
        proved what re-verifying does (password and authenticator code); a recovery-code sign-in doesn't, so on a phone
        it isn't counted as a recent verification (security review 9, SR9-7)."""
        fingerprint = getattr(request.state, 'edge_fingerprint', None) if getattr(request.state, 'edge_native', False) else None
        if fingerprint:
            from ..edge import usable
            from ..models import AuthSession, Device
            device = s.scalar(select(Device).where(Device.fingerprint == fingerprint))
            if not usable(device) or device.person != user.username:
                raise HTTPException(403, 'This native identity belongs to another person or expired.')
        token, csrf = auth.create_session(s, user, request.headers.get('user-agent', ''))
        if fingerprint:
            record = s.get(AuthSession, auth.digest(token))
            record.device_fingerprint = fingerprint
            record.reauthenticated_at = auth.now() if verified else None
            record.expires_at = min(record.expires_at, auth.now() + timedelta(hours=12), device.expires_at)
        return token, csrf

    def set_login_cookies(self, response: Response, session_token: str, csrf: str, max_age: int = auth.SESSION_DAYS * 86400) -> None:
        if self.settings.cookie_secure:
            self.expire_legacy_cookies(response)
        response.set_cookie(self.session_cookie, session_token, max_age=max_age, httponly=True,
                            secure=self.settings.cookie_secure, samesite="strict", path="/")
        response.set_cookie(self.csrf_cookie, csrf, max_age=max_age, httponly=False,
                            secure=self.settings.cookie_secure, samesite="strict", path="/")

    def expire_legacy_cookies(self, response: Response) -> None:
        for name in (SESSION_COOKIE, CSRF_COOKIE):
            response.delete_cookie(name, path="/", secure=True, httponly=name == SESSION_COOKIE, samesite="strict")

    def client_key(self, request: Request | WebSocket) -> str:
        """The browser's (or a node's WebSocket's) address for rate limiting. On a Unix socket only Caddy can reach accd, and Caddy sets
        X-Forwarded-For itself (the last entry is the one it added). Over TCP (tests, local use) every local
        process could claim any address, so the header is ignored there."""
        if request.client is None:  # Unix socket
            forwarded = request.headers.get("x-forwarded-for", "")
            address = forwarded.split(",")[-1].strip() or "?"
        else:
            address = request.client.host
        return network_key(address)

    def machine(self, name: str | None, who: Principal | None = None):
        """None for this machine (no name, or "local"); otherwise the link of an enrolled machine that is connected
        now: 404 for a name that isn't one, 409 for one that's offline. Members can't list machines, so they get 404
        for both: which names exist isn't theirs to learn."""
        if not name or name == "local":
            return None
        hub = self.broker.hub
        with self.db() as s:
            known = s.scalar(select(Node.id).where(Node.name == name, Node.status == "active"))
        link = hub.links.get(name) if known is not None and hub is not None else None
        if link is None or link.closed:
            if known is None or (who is not None and not who.is_owner):
                raise HTTPException(404, f"There's no connected machine called {name}.")
            raise HTTPException(409, f"{name} isn't connected right now. Check Host → Machines, then try again.")
        if who is not None and not who.is_owner:
            from ..group_remote import reserved_for_other
            with self.db() as s:
                if reserved_for_other(s, name, who.account, who.username):
                    raise HTTPException(404, 'That machine account wasn\'t found.')
        return link

    @staticmethod
    def refused(error: Exception) -> HTTPException:
        message = _public_error(error)
        if isinstance(error, BrokerError) and unknown_method(error):
            return HTTPException(500, message)
        if isinstance(error, MachineOffline):
            return HTTPException(409, message)
        if isinstance(error, BrokerUnavailable):
            return HTTPException(503, f"The agent account's session service isn't answering: {message}")
        if isinstance(error, BrokerError):
            return HTTPException(502, message)
        return HTTPException(409, message)
