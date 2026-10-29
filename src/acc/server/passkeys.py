"""Local human credentials; independent of future external identities and device pairing."""
from datetime import timedelta
from urllib.parse import urlsplit

from fastapi import HTTPException

from . import auth
from .models import AuthSession, PasskeyPolicy

REAUTH_MINUTES = 5


def policy(s):
    row = s.get(PasskeyPolicy, 1)
    return {'owners': row.owners, 'members': row.members} if row else {'owners': 'allow', 'members': 'allow'}


def required(s, user):
    return policy(s)['owners' if user.role == 'owner' else 'members'] == 'require'


def recovery_required(user, role=None):
    return (user.role if role is None else role) == 'owner' or not user.company_only


def recently_verified(s, session_hash, user):
    """This login verified again within five minutes, with a passkey when the person's role requires one. 403 otherwise."""
    record = s.get(AuthSession, session_hash) if session_hash else None
    if (user is None or record is None or record.user_id != user.id or record.reauthenticated_at is None or
            auth.now() - record.reauthenticated_at > timedelta(minutes=REAUTH_MINUTES) or
            (required(s, user) and not record.passkey_verified)):
        raise HTTPException(403, 'Verify your sign-in again in Settings → Passkeys, then try again within five minutes.')


def relying_party(settings, origin):
    # Never derive trust from Host, Forwarded, X-Forwarded-Host or the proxy's HTTP URL.
    if not origin or origin not in settings.allowed_origins:
        raise HTTPException(403, "Passkeys don't work at this page's address. Open Rynhult at its own address and try again.")
    parsed = urlsplit(origin)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment
            or (parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('localhost', '127.0.0.1', '::1')))):
        raise HTTPException(403, 'Passkeys need HTTPS at the configured Rynhult address (or localhost for testing).')
    return parsed.hostname
