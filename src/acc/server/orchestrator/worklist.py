"""Orchestrator v2, slice 1: the team's working list and NOW (docs/ORCHESTRATOR-V2.md, sections 2, 3 and 6).

An ordered list per team: what the lead needs from its person (complete steps), decisions with options and a
recommendation, questions, the lead's own tasks, notes; answers ingested and marked; a NOW summary; done items.
The person reads and edits it in the Team tab; the lead uses the worklist tools through its own spool. Only the lead
(depth 0) writes it, never a helper, and the lead can't answer, dismiss or overwrite what the person decided or
edited. On by default; explicit Orchestrator or slice Off overrides make its API
answer 404 and remove its tools.

Also gap 1 of the design: a helper idle for IDLE_MINUTES with no next task becomes a "need" item for the lead and an
Inbox item for its person, raised on state transitions, a timer and every read; never a one-shot poller.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import JSON, Boolean, Float, ForeignKey, Integer, String, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from .. import attention
from ..models import AuditLog, Base, Session, Team, UTCDateTime, utcnow
from ..nodes import node_lines, node_text
from ..supervisor_common import LaunchRefused
from .switch import slice_enabled

KINDS = ("need", "decision", "question", "task", "note")
PERSON_FACING = ("need", "decision", "question")  # these raise an Inbox item while open
STATES = ("open", "answered", "ingested", "done", "dismissed")
LINK_KINDS = ("session", "branch", "report", "delivery")
MAX_OPEN, MAX_TITLE, MAX_STEPS, MAX_ANSWER, MAX_NOW, MAX_NOTE, MAX_REASON = 200, 200, 16384, 8192, 4096, 2000, 1024
IDLE_MINUTES = 5
OPTION_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


def enabled() -> bool:
    return slice_enabled("ACC_ORCHESTRATOR_V2")


class OrchestrationRun(Base):
    """A team's list header: NOW, and one revision for the whole list (optimistic concurrency)."""
    __tablename__ = "orchestration_run"
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), primary_key=True)
    now: Mapped[str] = mapped_column(Text, default="")
    now_updated_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    now_updated_by: Mapped[str | None] = mapped_column(String(64))  # "lead" or a username
    now_revision: Mapped[int] = mapped_column(Integer, default=0)
    lead_seen_now: Mapped[int] = mapped_column(Integer, default=0)  # the NOW revision the lead last read
    revision: Mapped[int] = mapped_column(Integer, default=0)


class WorkItem(Base):
    __tablename__ = "work_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), index=True)
    position: Mapped[float] = mapped_column(Float)
    kind: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(200))
    steps: Mapped[str] = mapped_column(Text, default="")
    options: Mapped[list | None] = mapped_column(JSON)
    recommendation: Mapped[dict | None] = mapped_column(JSON)
    # What the lead goes ahead with until the person answers (slice 6, migration 0073): {option} for a decision,
    # {text} for a question. The person's answer always replaces it.
    default_answer: Mapped[dict | None] = mapped_column(JSON)
    state: Mapped[str] = mapped_column(String(16), default="open")
    answer: Mapped[str | None] = mapped_column(Text)
    answered_by: Mapped[str | None] = mapped_column(String(64))
    answered_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    ingested_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    done_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    note: Mapped[str | None] = mapped_column(Text)  # the lead's ingest/done note
    source: Mapped[str] = mapped_column(String(64))  # "lead", "system" or the username that created or last edited it
    owner_edited: Mapped[bool] = mapped_column(Boolean, default=False)
    about: Mapped[str | None] = mapped_column(String(32), index=True)  # the helper a system item is about
    links: Mapped[list] = mapped_column(JSON, default=list)
    blocks: Mapped[list] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class Refused(Exception):
    """A request the list refuses; `status` maps to the HTTP answer (the tools return the message)."""
    def __init__(self, message: str, status: int = 422):
        super().__init__(message)
        self.status = status


# ------------------------------------------------------------------------------------------------ validation

def _text(value, limit: int, what: str, *, required: bool = False, one_line: bool = False) -> str:
    """Bounded text, cleaned of control, bidi and zero-width characters (nodes.node_text's rule; security review
    2026-10-25, OV-9): one-line fields lose line breaks too."""
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise Refused(f"{what} must be text of at most {limit} characters")
    value = node_text(value, limit) if one_line else node_lines(value, limit)
    if required and not value.strip():
        raise Refused(f"{what} must be text of at most {limit} characters")
    return value


def _options(value) -> list | None:
    if value is None:
        return None
    if not isinstance(value, list) or not 2 <= len(value) <= 6:
        raise Refused("a decision has 2 to 6 options")
    out, seen = [], set()
    for option in value:
        if not isinstance(option, dict) or set(option) - {"id", "label", "consequence"}:
            raise Refused("an option is {id, label, consequence}")
        if not isinstance(option.get("id"), str) or not OPTION_ID.match(option["id"]) or option["id"] in seen:
            raise Refused("option ids are short, unique and lowercase (a-z, 0-9, -)")
        seen.add(option["id"])
        out.append({"id": option["id"], "label": _text(option.get("label"), 200, "an option's label", required=True, one_line=True),
                    "consequence": _text(option.get("consequence"), 1000, "an option's consequence", one_line=True)})
    return out


def _recommendation(value, options: list | None) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) - {"option", "reason"}:
        raise Refused("a recommendation is {option, reason}")
    option = value.get("option")
    if option is not None and (not options or option not in {o["id"] for o in options}):
        raise Refused("the recommended option must be one of the item's options")
    return {"option": option, "reason": _text(value.get("reason"), MAX_REASON, "the recommendation's reason", required=True)}


def _default(value, kind: str, options: list | None) -> dict | None:
    """The answer in effect until the person answers: one of a decision's options, or a question's stated answer."""
    if value is None:
        return None
    if kind not in ("decision", "question"):
        raise Refused("only a decision or question has a default until answered")
    if kind == "decision":
        if not isinstance(value, dict) or set(value) != {"option"} or value["option"] not in {o["id"] for o in options or []}:
            raise Refused("a decision's default is {option}, one of its option ids")
        return {"option": value["option"]}
    if not isinstance(value, dict) or set(value) != {"text"}:
        raise Refused("a question's default is {text}")
    return {"text": _text(value["text"], MAX_REASON, "the default answer", required=True)}


def _blocks(db, team_id, value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 100 or any(not isinstance(i, str) for i in value):
        raise Refused('blocks lists phase keys or R-n requirement ids')
    from . import planning
    known = {p.key for p in planning.phases(db, team_id)}
    known.update(r.id for r in planning.controls.rows(db, team_id, planning.Requirement))
    if any(i not in known for i in value):
        raise Refused('blocks contains an unknown phase or requirement')
    return list(dict.fromkeys(value))


def in_effect(item: WorkItem) -> dict | None:
    """What the team works on now: the person's answer once given, else the lead's stated default."""
    if item.answer is not None:
        return {"from": "answer", "value": item.answer}
    if item.default_answer and item.state == "open" and not item.blocks:
        return {"from": "default", "value": item.default_answer.get("option") or item.default_answer.get("text")}
    return None


def _links(value) -> list:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 20:
        raise Refused("at most 20 links")
    out = []
    for link in value:
        if (not isinstance(link, dict) or set(link) != {"kind", "id"} or link["kind"] not in LINK_KINDS
                or not isinstance(link["id"], str) or not 1 <= len(link["id"]) <= 120):
            raise Refused("a link is {kind: session|branch|report|delivery, id}")
        out.append({"kind": link["kind"], "id": link["id"]})
    return out


# ------------------------------------------------------------------------------------------------ the list

def run_for(db, team_id: str) -> OrchestrationRun:
    run = db.get(OrchestrationRun, team_id)
    if run is None:
        run = OrchestrationRun(team_id=team_id, now="", now_revision=0, lead_seen_now=0, revision=0)
        db.add(run)
        db.flush()
    return run


def _bump(run: OrchestrationRun) -> None:
    run.revision = (run.revision or 0) + 1


def item_out(item: WorkItem) -> dict:
    return {"id": item.id, "position": item.position, "kind": item.kind, "title": item.title, "steps": item.steps,
            "options": item.options, "recommendation": item.recommendation, "default": item.default_answer,
            "in_effect": in_effect(item), "state": item.state, "blocks": item.blocks or [],
            "answer": item.answer, "answered_by": item.answered_by, "answered_at": item.answered_at,
            "ingested_at": item.ingested_at, "done_at": item.done_at, "note": item.note, "source": item.source,
            "owner_edited": item.owner_edited, "links": item.links or [], "revision": item.revision,
            "created_at": item.created_at, "updated_at": item.updated_at}


def snapshot(db, team_id: str) -> dict:
    run = run_for(db, team_id)
    items = list(db.scalars(select(WorkItem).where(WorkItem.team_id == team_id).order_by(WorkItem.position, WorkItem.id)))
    return {"now": run.now, "now_updated_at": run.now_updated_at, "now_updated_by": run.now_updated_by,
            "now_revision": run.now_revision or 0, "revision": run.revision, "idle_minutes": IDLE_MINUTES,
            "items": [item_out(i) for i in items]}


def agent_source(source: str) -> bool:
    """Written by an agent of the team: the lead's own items, and helpers' report needs relayed to the list."""
    return source == "lead" or source.startswith("session:")


def person_source(username: str) -> str:
    """A person's name as an item's source or NOW's author. Usernames may be `lead` (only `system` is reserved), which
    would read as the lead's own text and lose the person's protections: such a name is marked as a person's."""
    return f"person:{username}" if username in ("lead", "system") or agent_source(username) else username


def author(db, item: WorkItem) -> str:
    """Who wrote an item, in words, so agent text never appears in Rynhult's or the person's voice (security review
    2026-10-25; threat review X-4, S1-2, S1-3). Only `system` items are Rynhult's own text."""
    if item.source == "system":
        return "Rynhult"
    if item.source == "lead":
        return "The team lead (an agent)"
    if item.source.startswith("session:"):
        helper = db.get(Session, item.source[8:])
        name = re.sub(r"[^\w .-]", "", helper.name if helper else "")[:64].strip() or "a helper"
        return f"Helper {name} (an agent), relayed"
    return item.source


def _inbox_detail(item: WorkItem) -> str:
    effect = in_effect(item)
    if effect and effect["from"] == "default":
        label = next((o["label"] for o in item.options or [] if o["id"] == effect["value"]), effect["value"])
        return f"Until you answer, the lead goes ahead with: {label}"[:500] + ". Open the lead's Team tab, working list."
    return "Open the lead's Team tab, working list."


def _inbox(db, item: WorkItem, lead: Session | None) -> None:
    key = f"worklist:{item.id}"
    team = db.get(Team, item.team_id)
    # A stopped team's lead can't act on an answer: its cards stay on the list, but ask nothing in the Inbox.
    if item.kind in PERSON_FACING and item.state == "open" and not (team and team.stopped):
        attention.raise_item(db, key, kind="team_worklist", source="orchestrator", severity="info",
                             title=f"{author(db, item)} asks: {item.title}"[:200],
                             detail=_inbox_detail(item), session_id=item.team_id,
                             owner=lead.owner if lead else None)
    else:
        attention.resolve(db, key)


def close_inbox(db, team_id: str) -> int:
    """Close a team's working-list Inbox items; the items stay on the list. Returns how many were open."""
    closed = 0
    for item_id in db.scalars(select(WorkItem.id).where(WorkItem.team_id == team_id)):
        if attention._open(db, f"worklist:{item_id}") is not None:
            attention.resolve(db, f"worklist:{item_id}")
            closed += 1
    return closed


def team_stopped(db, team_id: str) -> None:
    """The team was stopped or ran out of time: close its working list's Inbox items (break-it review 2026-10-26).
    The items stay on the list, readable and answerable for the record."""
    close_inbox(db, team_id)


def reraise_inbox(db) -> int:
    """Orchestrator v2 is on (again): every open question of a running team is in the Inbox. Returns how many."""
    raised = 0
    rows = db.scalars(select(WorkItem).join(Team, Team.id == WorkItem.team_id).where(
        WorkItem.state == "open", WorkItem.kind.in_(PERSON_FACING), Team.stopped.is_(False)))
    for item in rows:
        if attention._open(db, f"worklist:{item.id}") is None:
            _inbox(db, item, db.get(Session, item.team_id))
            raised += 1
    return raised


def _audit(db, actor: str, team_id: str, action: str, item: WorkItem | None = None, **detail) -> None:
    db.add(AuditLog(actor=actor, action=f"worklist.{action}", target=team_id,
                    detail={**({"item": item.id, "kind": item.kind} if item else {}), **detail}))


def add(db, team_id: str, actor: str, source: str, body: dict, *, about: str | None = None, blocks=None) -> WorkItem:
    kind = body.get("kind")
    if kind not in KINDS:
        raise Refused(f"kind is one of {', '.join(KINDS)}")
    open_count = db.scalar(select(func.count()).select_from(WorkItem).where(
        WorkItem.team_id == team_id, WorkItem.state.in_(("open", "answered", "ingested"))))
    if open_count >= MAX_OPEN:
        raise Refused(f"the list already has {MAX_OPEN} open items: finish or dismiss some first", 409)
    options = _options(body.get("options")) if kind == "decision" else None
    if kind == "decision" and not options:
        raise Refused("a decision needs its options")
    if kind != "decision" and body.get("options") is not None:
        raise Refused("only a decision has options")
    blocking = _blocks(db, team_id, body.get('blocks', blocks))
    if blocking and kind not in ('decision', 'question'):
        raise Refused('only decisions and questions block work')
    last = db.scalar(select(func.max(WorkItem.position)).where(WorkItem.team_id == team_id)) or 0.0
    item = WorkItem(team_id=team_id, position=last + 1.0, kind=kind, state="open", source=source, about=about,
                    title=_text(body.get("title"), MAX_TITLE, "title", required=True, one_line=True),
                    steps=_text(body.get("steps"), MAX_STEPS, "steps"), options=options,
                    recommendation=_recommendation(body.get("recommendation"), options),
                    default_answer=_default(body.get("default"), kind, options),
                    links=_links(body.get("links")), blocks=blocking, owner_edited=not (source == "system" or agent_source(source)), revision=1)
    db.add(item)
    db.flush()
    _bump(run_for(db, team_id))
    _inbox(db, item, db.get(Session, team_id))
    _audit(db, actor, team_id, "add", item)
    return item


def person_update(db, team_id: str, item_id: int, who: str, body: dict) -> WorkItem:
    """The person edits, reorders, answers, reopens, completes or dismisses an item. Needs the item's revision."""
    item = db.get(WorkItem, item_id)
    if item is None or item.team_id != team_id:
        raise Refused("no such item", 404)
    if body.get("revision") != item.revision:
        raise Refused("the item changed since you loaded it", 409)
    allowed = {"revision", "title", "steps", "options", "recommendation", "position", "answer", "answer_text", "state", "blocks"}
    if set(body) - allowed:
        raise Refused("unknown fields")
    if 'answer_text' in body:
        if item.kind != 'decision' or 'answer' in body:
            raise Refused('use a different answer only for a decision, without an option answer')
        from uuid import uuid4
        option = {'id': 'person-' + uuid4().hex[:16],
                  'label': _text(body['answer_text'], 200, 'different answer', required=True, one_line=True),
                  'consequence': 'Answer written by the person'}
        item.options = [*(item.options or []), option]
        body = {**body, 'answer': option['id']}
    edited = False
    if "title" in body:
        item.title, edited = _text(body["title"], MAX_TITLE, "title", required=True, one_line=True), True
    if "steps" in body:
        item.steps, edited = _text(body["steps"], MAX_STEPS, "steps"), True
    if "options" in body:
        if item.kind != "decision":
            raise Refused("only a decision has options")
        item.options, edited = _options(body["options"]), True
        if item.default_answer and item.default_answer.get("option") not in {o["id"] for o in item.options}:
            item.default_answer = None
    if "recommendation" in body:
        item.recommendation, edited = _recommendation(body["recommendation"], item.options), True
    if 'blocks' in body:
        if item.kind not in ('decision', 'question'):
            raise Refused('only decisions and questions block work')
        item.blocks, edited = _blocks(db, team_id, body['blocks']), True
    if "position" in body:
        if not isinstance(body["position"], (int, float)) or isinstance(body["position"], bool):
            raise Refused("position is a number")
        item.position = float(body["position"])
    if "answer" in body:
        if item.kind not in ("decision", "question", "need"):
            raise Refused("only a need, decision or question takes an answer")
        answer = _text(body["answer"], MAX_ANSWER, "answer", required=True)
        if item.kind == "decision" and answer not in {o["id"] for o in item.options or []}:
            raise Refused("answer a decision with one of its option ids")
        item.answer, item.answered_by, item.answered_at, item.state = answer, who, utcnow(), "answered"
        item.ingested_at = None
        from . import planning
        planning.controls.accept_proposal(db, item, who)
    if "state" in body:
        if body["state"] not in ("open", "done", "dismissed"):
            raise Refused("a person sets an item open, done or dismissed")
        # A decision card is the person's to answer (owner decision 2026-10-26): closing it unanswered would end its
        # default without an answer, and the lead couldn't tell "drop it" from "go ahead".
        if body["state"] != "open" and item.kind == "decision" and item.answer is None and "answer" not in body:
            raise Refused("answer this decision first: a decision is closed by answering it")
        item.state = body["state"]
        item.done_at = utcnow() if item.state == "done" else None
    if edited:
        item.owner_edited, item.source = True, person_source(who)
    item.revision += 1
    _bump(run_for(db, team_id))
    _inbox(db, item, db.get(Session, team_id))
    _audit(db, who, team_id, "answer" if "answer" in body else "edit", item,
           fields=sorted(set(body) - {"revision", "answer"}))
    return item


def person_now(db, team_id: str, who: str, text: str, revision, now_revision=None) -> OrchestrationRun:
    """The person replaces NOW. With now_revision (the NOW they started editing from), only a newer NOW refuses it, not
    unrelated list changes; without it, any change to the list does, as before."""
    run = run_for(db, team_id)
    if now_revision is not None:
        if now_revision != (run.now_revision or 0):
            raise Refused("NOW changed since you started editing it", 409)
    elif revision != run.revision:
        raise Refused("the list changed since you loaded it", 409)
    run.now = _text(text, MAX_NOW, "NOW")
    run.now_updated_at, run.now_updated_by = utcnow(), person_source(who)
    run.now_revision = (run.now_revision or 0) + 1
    _bump(run)
    _audit(db, who, team_id, "now")
    return run


# ------------------------------------------------------------------------------------------------ the lead's tools

TOOL_FIELDS = {
    "worklist": set(),
    "worklist_add": {"kind", "title", "steps", "options", "recommendation", "default", "links", "blocks"},
    "worklist_update": {"item", "title", "steps", "options", "recommendation", "default", "links", "position", "blocks", "note"},
    "worklist_ingest": {"item", "note"},
    "worklist_done": {"item", "note"},
    "now": {"text"},
}


def _lead(db, caller_id: str) -> tuple[Session, Team]:
    caller = db.get(Session, caller_id)
    team = db.get(Team, caller.team_id) if caller and caller.team_id else None
    from .verification import allowed
    if not allowed(team, caller, 'worklist_update'):
        raise Refused("team is stopped, expired, or caller is not running")
    if caller.parent_session_id is not None or caller.id != team.id:
        raise Refused("only the team's lead keeps the working list; send needs in your report instead")
    return caller, team


def _lead_item(db, team_id: str, payload: dict) -> WorkItem:
    item_id = payload.get("item")
    item = db.get(WorkItem, item_id) if isinstance(item_id, int) and not isinstance(item_id, bool) else None
    if item is None or item.team_id != team_id:
        raise Refused("no such item")
    return item


def lead_action(supervisor, caller_id: str, payload: dict):
    """One worklist tool call from the lead's spool. Agent text is untrusted: size-capped and vault-redacted."""
    action = payload["action"]
    with supervisor.db() as db:
        lead, team = _lead(db, caller_id)
        redact = supervisor.vault.redactor(db, lead.vault_keys).deep
        actor = f"session:{lead.id}"
        sweep(db, team.id)
        ingest_reports(db, team.id)  # slice 4's needs become items before the lead reads or edits the list
        if action == "worklist":
            run = run_for(db, team.id)
            run.lead_seen_now = run.now_revision
            out = snapshot(db, team.id)
            for item in out["items"]:  # provenance: an answer is the person's, framed as such
                if item["answer"] is not None:
                    item["answer_from"] = f"person:{item['answered_by']}"
            db.commit()
            return out
        if action == "worklist_add":
            body = redact({k: payload[k] for k in TOOL_FIELDS[action] if k in payload})
            item = add(db, team.id, actor, "lead", body)
        elif action == "worklist_update":
            item = _lead_item(db, team.id, payload)
            if 'note' in payload:
                if set(payload) & (TOOL_FIELDS[action] - {'item', 'note'}):
                    raise Refused('send a discussion reply separately from edits')
                item.note = _text(redact(payload['note']), MAX_NOTE, 'reply', required=True)
                item.updated_at = utcnow()
                item.revision += 1
                _bump(run_for(db, team.id))
                _audit(db, actor, team.id, 'reply', item)
                db.commit()
                return item_out(item)
            from .planning_controls import PlanProposal
            if db.get(PlanProposal, item.id) is not None:
                raise Refused('a boundary proposal is immutable; propose a new change instead')
            # The lead rewrites only what it wrote: never a person's item, Rynhult's own text or a helper's relayed need.
            if item.owner_edited:
                raise Refused("the person edited this item: add a note item instead of rewriting it")
            if item.source != "lead":
                raise Refused("only items the lead wrote can be rewritten: add a note item instead")
            if item.state not in ("open", "ingested"):
                raise Refused("the item is answered, done or dismissed")
            changes = redact({k: payload[k] for k in ("title", "steps", "options", "recommendation", "default", "links", "blocks")
                              if k in payload})
            if item.answer is not None and set(changes) - {"links"}:
                # An answer is stored as an option id: rewording the card after it would change what the person said.
                raise Refused("the item is answered: its wording stays as the person answered it")
            if "title" in changes:
                item.title = _text(changes["title"], MAX_TITLE, "title", required=True, one_line=True)
            if "steps" in changes:
                item.steps = _text(changes["steps"], MAX_STEPS, "steps")
            if "options" in changes:
                if item.kind != "decision":
                    raise Refused("only a decision has options")
                item.options = _options(changes["options"])
            if "recommendation" in changes:
                item.recommendation = _recommendation(changes["recommendation"], item.options)
            if "default" in changes:
                item.default_answer = _default(changes["default"], item.kind, item.options)
            elif "options" in changes and item.default_answer and item.default_answer.get("option") not in {
                    o["id"] for o in item.options or []}:
                item.default_answer = None  # its option is gone: no default rather than a dangling one
            if 'blocks' in changes:
                blocking = _blocks(db, team.id, changes['blocks'])
                if item.kind not in ('decision', 'question'):
                    raise Refused('only decisions and questions block work')
                if set(item.blocks or []) - set(blocking):
                    raise Refused('only the person may remove a decision block')
                item.blocks = blocking
            if "links" in changes:
                item.links = _links(changes["links"])
            if "position" in payload:
                if not isinstance(payload["position"], (int, float)) or isinstance(payload["position"], bool):
                    raise Refused("position is a number")
                item.position = float(payload["position"])
            item.revision += 1
            _audit(db, actor, team.id, "lead_update", item)
        elif action == "worklist_ingest":
            item = _lead_item(db, team.id, payload)
            if item.state != "answered":
                raise Refused("only an answered item can be ingested")
            item.state, item.ingested_at = "ingested", utcnow()
            item.note = _text(redact(payload.get("note")), MAX_NOTE, "note", required=True)
            item.revision += 1
            _audit(db, actor, team.id, "ingest", item)
        elif action == "worklist_done":
            item = _lead_item(db, team.id, payload)
            if item.kind in ("decision", "question") and item.state == "open":
                raise Refused("the person hasn't answered this yet")
            if item.state in ("done", "dismissed"):
                raise Refused("the item is already closed")
            if item.kind == "need" and not (item.source == "system" or agent_source(item.source)):
                raise Refused("the person added this need: they mark it done")
            item.state, item.done_at = "done", utcnow()
            item.note = _text(redact(payload.get("note")), MAX_NOTE, "note")
            item.revision += 1
            _audit(db, actor, team.id, "done", item)
        elif action == "now":
            run = run_for(db, team.id)
            if run.now_updated_by not in (None, "lead") and run.lead_seen_now < run.now_revision:
                raise Refused("the person edited NOW since you last read it: call worklist() first")
            run.now = _text(redact(payload.get("text")), MAX_NOW, "NOW", required=True)
            run.now_updated_at, run.now_updated_by = utcnow(), "lead"
            run.now_revision = (run.now_revision or 0) + 1
            run.lead_seen_now = run.now_revision
            _bump(run)
            _audit(db, actor, team.id, "now")
            db.commit()
            return {"ok": True, "revision": run.revision}
        else:
            raise Refused("unknown worklist tool")
        _bump(run_for(db, team.id))
        _inbox(db, item, lead)
        db.commit()
        return item_out(item)


async def _tool(supervisor, caller_id, payload):
    try:
        return lead_action(supervisor, caller_id, payload)
    except Refused as error:
        raise LaunchRefused(str(error)) from None


# ------------------------------------------------------------------------------------------------ idle helpers (gap 1)

DB = {"factory": None}  # the app's database factory, handed over by api/worklist.py when the router is made


def ingest_reports(db, team_id: str) -> int:
    """Slice 4's needs outbox (docs/ORCHESTRATOR-V2.md, "Slice 4 implementation and slice 1 interface"): every helper
    report's needs become `need` items for the lead to triage, and the report is marked ingested in this same
    transaction, so a rollback leaves its needs pending. Callers hold an authorized per-team write transaction; the
    team's run row is locked so two readers never ingest the same report twice. Returns how many items it added."""
    if not enabled():
        return 0
    from . import delivery
    run_for(db, team_id)
    db.execute(select(OrchestrationRun).where(OrchestrationRun.team_id == team_id).with_for_update())
    seen = {link["id"] for item in db.scalars(select(WorkItem).where(WorkItem.team_id == team_id, WorkItem.kind == "need"))
            for link in item.links or [] if link.get("kind") == "report"}
    added = 0
    for report in delivery.pending_needs(db, team_id):
        reporter = db.get(Session, report.session_id)
        name = reporter.name if reporter else report.session_id
        try:
            with db.begin_nested():  # all of a report's needs, or none (the list is full): it then stays pending
                for index, need in enumerate(report.needs or []):
                    provenance = f"{report.session_id}/{report.task_id}/{index}"
                    if provenance in seen:
                        continue  # already an item (a crash between add and mark): never twice
                    seen.add(provenance)
                    # The helper's own words: recorded as its, shown as "relayed", never as Rynhult's (review X-4, S1-3).
                    add(db, team_id, f"session:{report.session_id}", f"session:{report.session_id}", {
                        "kind": "need", "title": str(need.get("title") or "A helper needs something")[:MAX_TITLE],
                        "steps": (str(need.get("steps") or "") + f"\n\nFrom {name}'s report.")[:MAX_STEPS],
                        # not `about`: sweep() owns system items about a helper (its idle alert)
                        "links": [{"kind": "session", "id": report.session_id}, {"kind": "report", "id": provenance}]})
                    added += 1
                delivery.mark_needs_ingested(report)
        except Refused:
            break  # e.g. MAX_OPEN reached: these needs wait for the next read
    return added


def sweep(db, team_id: str | None = None, now: datetime | None = None) -> int:
    """Raise one "need" item (and Inbox item) per helper idle for IDLE_MINUTES with no next task; close it when the
    helper works, stops or is gone. Returns how many items it raised."""
    if not enabled():
        return 0
    if team_id:
        from .planning_controls import refresh_proposals
        refresh_proposals(db, team_id)
    now = now or datetime.now(timezone.utc)
    query = select(Session).where(Session.team_id.is_not(None), Session.parent_session_id.is_not(None))
    if team_id:
        query = query.where(Session.team_id == team_id)
    raised = 0
    for helper in db.scalars(query):
        team = db.get(Team, helper.team_id)
        live = team is not None and not team.stopped and now < team.expires_at
        idle = (helper.observed_state == "idle" and helper.desired_state == "running" and helper.idle_since is not None
                and helper.idle_since <= now - timedelta(minutes=IDLE_MINUTES))
        open_item = db.scalar(select(WorkItem).where(WorkItem.about == helper.id, WorkItem.source == "system",
                                                     WorkItem.state == "open"))
        if live and idle and open_item is None:
            add(db, helper.team_id, "system", "system", {
                "kind": "need", "title": f"Helper “{helper.name}” is idle with no next task"[:MAX_TITLE],
                "steps": (f"Helper “{helper.name}” has been idle for {IDLE_MINUTES} minutes or more and nothing is queued for it.\n"
                          "1. Give it its next task (the lead's message tool, or the session's message box).\n"
                          "2. Or merge its branch and finish it from the Team tab.\n"
                          "This item closes by itself once it works again or stops."),
                "links": [{"kind": "session", "id": helper.id}]}, about=helper.id)
            raised += 1
        elif open_item is not None and not (live and helper.observed_state == "idle" and helper.desired_state == "running"):
            open_item.state, open_item.done_at, open_item.note = "done", utcnow(), "resolved: the helper is no longer idle"
            open_item.revision += 1
            _bump(run_for(db, open_item.team_id))
            _inbox(db, open_item, db.get(Session, open_item.team_id))
    return raised


def _check_later() -> None:
    factory = DB["factory"]
    if factory is None:
        return
    with factory() as db:
        sweep(db)
        db.commit()


def _on_state(db, session, previous: str, state: str) -> None:
    if not enabled() or session.team_id is None or session.parent_session_id is None:
        return
    if state == "idle":
        try:
            asyncio.get_running_loop().call_later(IDLE_MINUTES * 60 + 1, _check_later)
        except RuntimeError:
            pass  # no event loop (tests, tools): the next read or transition sweeps
    sweep(db, session.team_id)


TEAM_FIELDS: dict[str, set[str]] = dict(TOOL_FIELDS)
TEAM_ACTIONS: dict = {name: _tool for name in TOOL_FIELDS}
ON_STATE: list = [_on_state]
