"""One check for every Claude conversation id handed to the CLI or the Agent SDK.

GHSA-h4mw-j7qp-8mwm: claude-agent-sdk < 0.2.121 passes `resume` to the CLI unvalidated, so a value
starting with '-' becomes a flag (e.g. --mcp-config, giving command execution). Rynhult stays on
0.1.50 until the relay is re-qualified, so only canonical lowercase UUIDs may reach any argv.
"""
from __future__ import annotations

import uuid


def canonical_session_id(value: object) -> bool:
    """True only for a canonical UUID string: lowercase, hyphenated, no braces/urn, never '-' first."""
    if not isinstance(value, str) or not value or value.startswith('-'):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
