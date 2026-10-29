"""Single-user app login: password (argon2id) + TOTP (RFC 6238), server-side sessions.

First run: accd writes a one-time setup token to data/secrets/setup_token (acc 0600).
the owner reads it over SSH, opens /setup, chooses a password and enrols the authenticator
in the browser, so no secret ever passes through a terminal transcript.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import io
import os
import secrets
import struct
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import segno
from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import joinedload

from .models import AuthSession, RetiredUser, User

SESSION_DAYS = 14
LAST_SEEN_STEP = timedelta(minutes=5)
TOTP_STEP = 30
_hasher = PasswordHasher()
_DUMMY_HASH = _hasher.hash("no such user")  # checked for unknown users, so timing doesn't reveal which names exist


def now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- TOTP

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode().rstrip("=")


def totp(secret: str, at: float, step: int = TOTP_STEP, digits: int = 6) -> str:
    key = base64.b32decode(secret + "=" * (-len(secret) % 8))
    counter = struct.pack(">Q", int(at // step))
    digest = hmac.new(key, counter, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 10 ** digits
    return str(code).zfill(digits)


def totp_ok(secret: str, code: str, last_step: int | None, at: float | None = None) -> int | None:
    """Return the matched time step (one window of drift either way), refusing replays."""
    at = time.time() if at is None else at
    code = (code or "").strip().replace(" ", "")
    for drift in (0, -1, 1):
        step = int(at // TOTP_STEP) + drift
        if last_step is not None and step <= last_step:
            continue
        if hmac.compare_digest(totp(secret, step * TOTP_STEP), code):
            return step
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "Rynhult") -> str:
    label, issuer = quote(f"{issuer}:{username}"), quote(issuer)
    return f"otpauth://totp/{label}?secret={secret}&issuer={issuer}&algorithm=SHA1&digits=6&period={TOTP_STEP}"


# Scanners want dark modules on a light background with a quiet zone of at least four modules, whatever the page's
# theme: both QR codes carry their own white background (break-it 10).
def qr_svg(data: str) -> str:
    """Inline SVG for HTML (the authenticator enrolment QR)."""
    return segno.make(data, error="m").svg_inline(scale=5, border=4, dark="#000", light="#fff")


def qr_image_svg(data: str) -> str:
    """A standalone SVG for an <img> data: URL (it needs the namespace): dark modules on a white quiet zone, so a
    phone camera reads it on any theme."""
    out = io.BytesIO()
    segno.make(data, error="m").save(out, kind="svg", xmldecl=False, scale=5, border=4, dark="#000", light="#fff")
    return out.getvalue().decode()


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    return _hasher.hash(password)


def password_ok(stored: str | None, password: str) -> bool:
    """None (no such user) still costs one verification, then fails."""
    try:
        return _hasher.verify(stored or _DUMMY_HASH, password) and stored is not None
    except VerificationError:
        return False


# ---------------------------------------------------------------- setup token

def ensure_setup_token(db: DbSession, path) -> str | None:
    """While no user exists, keep a one-time setup token on disk; remove it afterwards."""
    if db.scalar(select(User).limit(1)) is not None:
        if path.exists():
            path.unlink()
        return None
    if not path.exists():
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(24))
        os.chmod(path, 0o600)
    return path.read_text().strip()


# ---------------------------------------------------------------- sessions

def digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_session(db: DbSession, user: User, user_agent: str, method: str | None = None,
                   lifetime: timedelta = timedelta(days=SESSION_DAYS)) -> tuple[str, str]:
    """Returns (session token for the httpOnly cookie, CSRF token for the readable cookie)."""
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    db.add(AuthSession(token_hash=digest(token), csrf=csrf, user_id=user.id, user_agent=user_agent[:200],
                       method=method, expires_at=now() + lifetime))
    return token, csrf


def lookup_session(db: DbSession, token: str | None, allow_step_up: bool = False,
                   edge_fingerprint: str | None = None, native: bool = False, bind: bool = False) -> AuthSession | None:
    """The live sign-in for a cookie. A company sign-in still waiting for its passkey check (step_up) counts only
    where the caller allows it (sign-in state, sign-out and the passkey check itself)."""
    if not token:
        return None
    # Load the principal before dirtying last_seen_at: a lazy relationship read
    # would autoflush outside callers' best-effort commit/rollback handling.
    record = db.get(AuthSession, digest(token), options=[joinedload(AuthSession.user)])
    if record is None or record.expires_at < now():
        return None
    if record.device_fingerprint:
        if record.device_bound_at is None and not bind:
            return None
        from .edge import usable  # the edge's own rule: active, unexpired and not retired by a rotation
        from .models import Device
        device = db.scalar(select(Device).where(Device.fingerprint == record.device_fingerprint))
        if (edge_fingerprint != record.device_fingerprint or not usable(device) or device.kind != 'native'
                or device.person != record.user.username):
            return None
    if native:
        from .models import Device
        device = db.scalar(select(Device).where(Device.fingerprint == edge_fingerprint))
        if device is None or device.person != record.user.username or (not record.device_fingerprint and not bind):
            return None
    if record.step_up and not record.passkey_verified and not allow_step_up:
        return None
    if record.method == "oidc" and record.user.role == "owner":  # owners are local-only, also after a promotion
        return None
    authority = db.info.get('ha_authority')
    if (authority is None or authority.accepting) and now() - record.last_seen_at > LAST_SEEN_STEP:  # a write on most requests would race the writers (db.py)
        record.last_seen_at = now()
    return record


# Names Rynhult itself writes where a username goes (audit actors, owners of rows): nobody may sign up as one, in any
# case. The bearer token's own name, "@api", can't be a username at all (models.API_PRINCIPAL).
RESERVED_NAMES = frozenset({"api", "supervisor", "acc", "system", "root"})


def reserved(name: str) -> bool:
    return name.lower() in RESERVED_NAMES


def taken(db: DbSession, name: str) -> bool:
    """Whether someone has or had this name, ignoring case (so "Casey" can't pose as "casey"). A removed person's name
    stays taken for good: what they owned must never pass to someone new."""
    return (db.scalar(select(User.id).where(func.lower(User.username) == name.lower())) is not None
            or db.scalar(select(RetiredUser.username).where(func.lower(RetiredUser.username) == name.lower())) is not None)


INVITE_HOURS = 72


def new_invite_token() -> str:
    return secrets.token_urlsafe(24)


def end_session(db: DbSession, token: str | None) -> None:
    if token and (record := db.get(AuthSession, digest(token))):
        db.delete(record)


class LoginThrottle:
    """In-memory limiter over a sliding window: per client address and per account. Attempts count before the
    password is checked, so a burst of parallel guesses can't all get through."""

    def __init__(self, limit: int = 5, window: float = 600.0, account_limit: int = 10):
        self.limit, self.window, self.account_limit = limit, window, account_limit
        self.failures: dict[str, list[float]] = {}

    def _recent(self, key: str) -> list[float]:
        recent = [t for t in self.failures.get(key, []) if time.monotonic() - t < self.window]
        if recent:
            self.failures[key] = recent
        else:
            self.failures.pop(key, None)
        return recent

    def blocked(self, key: str) -> bool:
        limit = self.account_limit if key.startswith("user:") else self.limit
        return len(self._recent(key)) >= limit

    def fail(self, key: str) -> None:
        if len(self.failures) > 10_000:  # never grow without bound: drop expired keys
            for stale in [k for k, times in self.failures.items() if all(time.monotonic() - t >= self.window for t in times)]:
                del self.failures[stale]
        self.failures.setdefault(key, []).append(time.monotonic())

    def reset(self, key: str) -> None:
        self.failures.pop(key, None)