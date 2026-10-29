"""History search: every session's conversation (what was asked and answered) in SQLite full-text search.

The supervisor re-reads a session's conversation file through the broker after it has done something (its activity or
status changed since the last pass); an unchanged file costs one stat. Archived sessions stay searchable.
Mixed into acc.server.supervisor.Supervisor for indexing; search() is used by the API.
"""
from __future__ import annotations

import json
import re
import time

from sqlalchemy import bindparam, func, or_, select, text
from sqlalchemy.orm import Session as DbSession

from .broker_client import BrokerError, BrokerUnavailable
from .db import write_in_thread
from .machine_presence import connected, connected_hosts
from .models import Project, Session
from .supervisor_common import log, now

PER_PASS = 20  # sessions read per pass, so a backfill of old sessions never stalls the loop
MARK_START, MARK_END = "\x02", "\x03"  # around matched words in snippets; the UI turns them into highlights


class HistoryMixin:
    async def index_transcripts(self) -> None:
        retry = self.__dict__.setdefault("_history_retry", {})  # acc_id -> (next try, delay): failures back off
        clock = time.monotonic()
        # Sessions still backing off are left out before the limit (Q11): otherwise PER_PASS newer sessions that can't
        # be read take every slot, every pass, and an older one that can is never reached.
        cooling = [acc_id for acc_id, (until, _) in retry.items() if until > clock]
        machine = func.coalesce(Session.team_host, Project.host, Session.deleted_project["host"].as_string(), "local")
        with self.db() as db:
            due = db.execute(
                select(Session.id, Session.run_as, Session.provider, Session.native_id, Session.transcript_stamp,
                       machine, Session.indexed_at).outerjoin(Project, Session.project_id == Project.id)
                .where(Session.native_id.is_not(None), Session.management != "external",
                       or_(Session.indexed_at.is_(None), Session.activity_at > Session.indexed_at,
                           Session.status_at > Session.indexed_at),
                       Session.id.not_in(cooling), machine.in_(connected_hosts(self.broker)))
                .order_by(Session.indexed_at.is_not(None), Session.updated_at.desc()).limit(PER_PASS)).all()
        for acc_id, account, provider, native_id, stamp, host, indexed_at in due:
            if not connected(self.broker, host or "local"):
                continue
            started = now()  # what this read can contain: activity from here on makes the session due again
            try:
                result = await self.broker.call("transcript_messages", account=account, host=host or "local", provider=provider,
                                                native_id=native_id, stamp=stamp)
            except BrokerUnavailable as error:
                if (host or "local") == "local":
                    raise
                # Another machine that's offline or removed: back off this session; don't abort the supervisor pass.
                log.info("history: %s not indexed: %s", acc_id, error)
                result = {}
            except BrokerError as error:  # e.g. an account whose broker is gone, or a file it can't read
                log.info("history: %s not indexed: %s", acc_id, error)
                result = {}
            if not result.get("found"):  # not read: try again later (1 min, doubling to 6 h), not marked as indexed
                delay = min(max(retry.get(acc_id, (0, 30))[1] * 2, 60), 6 * 3600)
                retry[acc_id] = (time.monotonic() + delay, delay)
                continue
            await write_in_thread(self._store_transcript, acc_id, result, started, indexed_at)
            retry.pop(acc_id, None)

    def _store_transcript(self, acc_id: str, result: dict, started=None, indexed_at=None) -> None:
        with self.db() as db:
            session = db.get(Session, acc_id)
            if session is None:
                return
            if started is not None and session.indexed_at != indexed_at:
                return  # changed while this read was in flight (e.g. reset to be read again): the next pass reads
            if result.get("messages") is not None:
                redact = self.vault.redactor(db, session.vault_keys)
                replace(db, acc_id, [{**m, "text": redact.text(str(m.get("text") or ""))} for m in result["messages"]
                                     if isinstance(m, dict)])
                session.transcript_stamp = result.get("stamp")
            session.indexed_at = started or now()
            db.commit()


def replace(db: DbSession, acc_id: str, messages: list[dict]) -> None:
    rows = [{"text": str(m.get("text") or "")[:8000], "id": acc_id, "role": m.get("role"), "ts": m.get("ts"), "seq": i}
            for i, m in enumerate(messages) if m.get("text")]
    db.execute(text("DELETE FROM transcript_fts WHERE session_id = :id"), {"id": acc_id})
    if rows:
        db.execute(text("INSERT INTO transcript_fts (text, session_id, role, ts, seq) "
                        "VALUES (:text, :id, :role, :ts, :seq)"), rows)

    session = db.get(Session, acc_id)
    if session is not None and session.automation:
        # Search stores a prefix; completion markers near the end of a long answer must survive it.
        latest = next(((i, m) for i, m in reversed(list(enumerate(messages))) if m.get("role") == "assistant"), None)
        if latest:
            marker = re.search(r"^GOAL:\s*(?:done|blocked(?:\s*:\s*[^\n]+)?)\s*$", str(latest[1].get('text') or ''), re.M | re.I)
            session.automation = {**session.automation, "assistant_marker": {"seq": latest[0], "text": marker[0].strip() if marker else ''}}


def fts_query(q: str) -> str | None:
    """The user's words as an FTS5 query: each word quoted (no operator surprises), all required, the last one a
    prefix ("deplo" finds "deploy")."""
    words = re.findall(r"[\w][\w.@/-]*", q)[:12]
    if not words:
        return None
    terms = ['"' + w.replace('"', '""') + '"' for w in words]
    if len(words[-1]) >= 2:
        terms[-1] += "*"
    return " ".join(terms)


def indexed_sessions(db: DbSession, allowed: set[str]) -> int:
    """Count conversations with actual indexed messages, only within the caller's filtered scope."""
    if not allowed:
        return 0
    if db.bind.dialect.name == "postgresql":
        statement = text("SELECT count(DISTINCT session_id) FROM transcript_fts WHERE session_id IN :ids")
        statement = statement.bindparams(bindparam("ids", expanding=True))
        ids = sorted(allowed)
    else:
        statement = text("SELECT count(DISTINCT session_id) FROM transcript_fts "
                         "WHERE session_id IN (SELECT value FROM json_each(:ids))")
        ids = json.dumps(sorted(allowed))
    return db.execute(statement, {"ids": ids}).scalar_one()


def search(db: DbSession, q: str, allowed: set[str], limit: int = 30) -> list[dict]:
    """Sessions whose conversation matches, best first, each with up to three matching messages.

    Scoped in SQL, before anything is limited: only the caller's sessions are matched, so others' conversations can't
    crowd theirs out. The limit counts sessions, not messages (each keeps its best three hits, and `matches` counts
    all of its matching messages), so one long conversation can't push out every other session either."""
    query = fts_query(q)
    if not query or not allowed:
        return []
    if db.bind.dialect.name == "postgresql":
        words = re.findall(r"[\w][\w.@/-]*", q)[:12]
        # Quote terms as tsquery lexemes; the last term permits prefix matches.
        query = " & ".join("'" + w.replace("'", "''") + "'" +
                           (":*" if i == len(words) - 1 and len(w) >= 2 else "")
                           for i, w in enumerate(words))
        statement = text(
            "WITH m AS (SELECT session_id, role, ts, seq, "
            "ts_headline('english', text, to_tsquery('english', :q), :options) AS snip, "
            "-ts_rank(to_tsvector('english', text), to_tsquery('english', :q)) AS rank "
            "FROM transcript_fts WHERE to_tsvector('english', text) @@ to_tsquery('english', :q) "
            "AND session_id IN :ids), "
            "r AS (SELECT *, row_number() OVER (PARTITION BY session_id ORDER BY rank, seq) AS n, "
            "count(*) OVER (PARTITION BY session_id) AS total, min(rank) OVER (PARTITION BY session_id) AS best FROM m) "
            "SELECT session_id, role, ts, seq, snip, total FROM r WHERE n <= 3 "
            "ORDER BY best, session_id, n LIMIT :rows").bindparams(bindparam("ids", expanding=True))
        parameters = {"q": query, "ids": sorted(allowed), "rows": limit * 3,
                      "options": f"StartSel={MARK_START}, StopSel={MARK_END}, MaxWords=16, MinWords=8"}
    else:
        statement = text(
        "WITH m AS (SELECT session_id, role, ts, seq, snippet(transcript_fts, 0, :a, :b, '…', 16) AS snip, "
        "bm25(transcript_fts) AS rank FROM transcript_fts "
        "WHERE transcript_fts MATCH :q AND session_id IN (SELECT value FROM json_each(:ids))), "
        "r AS (SELECT *, row_number() OVER (PARTITION BY session_id ORDER BY rank, seq) AS n, "
        "count(*) OVER (PARTITION BY session_id) AS total, min(rank) OVER (PARTITION BY session_id) AS best FROM m) "
        "SELECT session_id, role, ts, seq, snip, total FROM r WHERE n <= 3 ORDER BY best, session_id, n LIMIT :rows")
        parameters = {"q": query, "a": MARK_START, "b": MARK_END, "ids": json.dumps(sorted(allowed)), "rows": limit * 3}
    rows = db.execute(statement, parameters).all()
    hits: dict[str, list[dict]] = {}
    matches: dict[str, int] = {}
    for acc_id, role, ts, seq, snippet, total in rows:
        hits.setdefault(acc_id, []).append({"role": role, "ts": ts, "seq": seq, "snippet": snippet})
        matches[acc_id] = total
    ordered = list(hits)[:limit]  # insertion order is best first
    sessions = {s.id: s for s in db.scalars(select(Session).where(Session.id.in_(ordered)))}
    return [{"session": {"id": s.id, "name": s.name, "project": s.project.slug if s.project else (s.deleted_project or {}).get("slug", "Deleted project"), "provider": s.provider,
                         "observed_state": s.observed_state, "desired_state": s.desired_state,
                         "created_at": s.created_at, "workdir": s.workdir},
             "matches": matches[s.id], "hits": hits[s.id]}
            for s in (sessions[i] for i in ordered if i in sessions)]
