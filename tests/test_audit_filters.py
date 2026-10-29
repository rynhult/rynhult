"""The Audit page's person and date filters (UX review AU-1)."""
from __future__ import annotations

import datetime

from acc.server.models import AuditLog


async def test_audit_filters_by_person_and_day(api):
    db = api.app.state.db
    old = datetime.datetime(2026, 1, 2, 12, tzinfo=datetime.timezone.utc)
    with db() as s:
        s.add(AuditLog(actor="alex", action="session.create", target="a1", ts=old))
        s.add(AuditLog(actor="sam", action="session.stop", target="b2"))
        s.add(AuditLog(actor="alex", action="terminal.attach", target="a1", detail={"mode": "write"}))
        s.commit()
    by_sam = (await api.get("/api/audit", params={"actor": "sam"})).json()
    assert by_sam and {a["actor"] for a in by_sam} == {"sam"}
    alex_since = (await api.get("/api/audit", params={"actor": "alex", "since": "2026-06-01"})).json()
    assert [a["action"] for a in alex_since] == ["terminal.attach"]
    both = (await api.get("/api/audit", params={"actor": "alex", "action": "session."})).json()
    assert [a["action"] for a in both] == ["session.create"]
    assert (await api.get("/api/audit", params={"since": "not-a-day"})).status_code == 422
