"""Offline root-console recovery. No HTTP handler imports or invokes this module."""
from __future__ import annotations

import argparse
import os
import sys
from datetime import timedelta

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .server import auth
from .server.db import WRITE_INTENT, make_engine
from .server.models import AuditLog, AuthSession, Invite, Passkey, PasskeyChallenge, RecoveryCode, User


def reset_owner_mfa(db, username: str, *, force: bool = False) -> str:
    # Check here too: callers cannot bypass the console's root requirement.
    if os.getuid() != 0 or os.geteuid() != 0:
        raise PermissionError('Owner recovery requires real and effective root on the host.')
    user = db.scalar(select(User).where(User.username == username).with_for_update())
    if user is None:
        raise ValueError('No such person.')
    if user.role != 'owner' and not force:
        raise ValueError('Person is not an owner; use --force to reset a non-owner.')
    for model in (AuthSession, Passkey, PasskeyChallenge, RecoveryCode):
        db.execute(delete(model).where(model.user_id == user.id))
    for invite in db.scalars(select(Invite).where(Invite.user_id == user.id, Invite.used_at.is_(None))):
        invite.used_at = auth.now()
    user.totp_secret, user.totp_last_step = '', None
    user.password_hash = auth.hash_password(auth.new_invite_token())
    user.recovery_hash, user.passkey_handle = None, None
    token = auth.new_invite_token()
    db.add(Invite(token_hash=auth.digest(token), user_id=user.id, role=user.role,
                  created_by='@root-console', expires_at=auth.now() + timedelta(minutes=15)))
    db.add(AuditLog(actor='@root-console', action='auth.owner_mfa_reset', target=user.username,
                    detail={'forced': force, 'uid': os.getuid()}))
    return '/invite/' + token


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['reset-owner-mfa'])
    parser.add_argument('user')
    parser.add_argument('--force', action='store_true')
    parser.add_argument('--database', required=True, help='offline database URL; stop rynhultd first')
    args = parser.parse_args()
    if os.getuid() != 0 or os.geteuid() != 0:
        parser.error('requires root on the host')
    if not sys.stdout.isatty():
        parser.error('use an interactive terminal; recovery links must not enter redirected output')
    engine = None
    intent = WRITE_INTENT.set(True)
    try:
        engine = make_engine(args.database)
        with Session(engine) as db:
            link = reset_owner_mfa(db, args.user, force=args.force)
            db.commit()
        # Only the console receives the token, never an audit field or a file.
        print('Open at your trusted Rynhult origin within 15 minutes: ' + link)
    except Exception:
        parser.exit(1, 'Recovery refused; transaction rolled back. Check the user, database and schema version.\n')
    finally:
        WRITE_INTENT.reset(intent)
        if engine is not None:
            engine.dispose()


if __name__ == '__main__':
    main()
