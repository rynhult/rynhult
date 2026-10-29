"""The one runtime gate for the macOS standalone pieces: start-at-login LaunchAgent, Keychain and tmux recovery.

macOS execution stays blocked until the staff-group question (docs/reviews/2026-10-25-review-desktop-phase1-mac.md,
finding 3) is answered on the owner's Mac. Every entry point that touches the system calls `require_ready` first. It
re-reads the root-owned identity receipt and runs the same `verify_identity` check that setup and every app start
use: same GUIDs and numeric IDs, hidden, false shell, outside admin, and, through dsmemberutil, which also resolves
nested and calculated groups, outside staff and admin. An unknown answer fails closed. Off macOS it always refuses,
so on Linux only pure generation, dry runs and fakes run.
"""
from __future__ import annotations

import subprocess
import sys


class Blocked(RuntimeError):
    """macOS standalone stays off: not macOS, not provisioned, or the agent's isolation isn't proven."""


def require_ready(owner_uid: int, *, platform: str | None = None, read=None, verify=None) -> dict:
    platform = sys.platform if platform is None else platform
    if platform != 'darwin':
        raise Blocked('macOS standalone runs only on macOS')
    if read is None or verify is None:
        from mac_setup import read_identity, verify_identity
        read, verify = read or read_identity, verify or verify_identity
    try:
        value = read(owner_uid)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise Blocked(f'Desktop execution identity unreadable: {error}') from None
    if value is None:
        raise Blocked('Desktop execution is not provisioned; run the one-time setup first')
    try:
        verify(value)  # includes the staff/admin membership check; refusal or an unknown answer blocks
    except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
        raise Blocked(f'macOS standalone is blocked: {error}') from None
    return value
