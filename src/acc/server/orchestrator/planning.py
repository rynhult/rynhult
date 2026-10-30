"""Orchestrator v2, slice 6: the planning session, the roadmap and the lead's default instructions.

A new v2 team starts in a planning session: the lead writes the idea, the scope (in, out, parked) and the phases with
their dependencies, puts each open design decision on the working list as its own card, then calls plan_ready. Until
then Rynhult refuses `start`, so no helper launches before there is a plan. Afterwards the lead keeps the roadmap
current with plan_update. Every change is a new numbered version with a reason, and the earlier versions stay readable.
Phases are never deleted: a phase the plan no longer needs is marked dropped and stays in the roadmap's history.
The person reads the plan on the Team tab, answers its questions on the working list in any order, can still talk
to the lead directly, and can edit the plan. Each person edit is a new version under their name. The lead can't
overwrite a person's edit it hasn't read (plan() first, as with NOW), and when it does change one, the person gets a
note saying so. Of the team's agents only the lead (depth 0) writes the plan. On by default; explicit Orchestrator/slice Off overrides disable it.
"""
from __future__ import annotations

import re
from datetime import datetime

from sqlalchemy import JSON, Float, ForeignKey, Integer, String, Text, UniqueConstraint, func, select
from sqlalchemy.orm import Mapped, mapped_column

from ..models import AuditLog, Base, Session, UTCDateTime, utcnow
from ..supervisor_common import LaunchRefused
from . import planning_controls as controls
from . import worklist
from .planning_controls import Constraint, PlanChangeNotice, Requirement  # noqa: F401 (public lane D interface)
from .worklist import Refused, _text

STATES = ("planning", "ready")
PHASE_STATES = ("planned", "active", "done", "dropped")
SCOPES = ("in", "out", "parked")
KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MAX_IDEA, MAX_SCOPE_LINES, MAX_LINE, MAX_PHASES, MAX_SUMMARY, MAX_REASON, MAX_DEPS = 4000, 30, 300, 50, 2000, 500, 10
HISTORY = 20  # versions listed with the plan; older ones stay readable by number

# The lead's standing instructions (docs/ORCHESTRATOR-V2.md, "Lead instructions"), added to a v2 lead's context at
# launch. Helpers never get them. Keep it in step with the playbook's "Default lead instructions" block.
LEAD_INSTRUCTIONS = """Orchestrator v2: you lead a team for one person (your person). Their attention is the scarce resource.
- Read the current project brief before plan_ready, if the brief tool is listed. Separate the person's original
  material, your interpretation, and the requirements. Admit unreadable images and request a usable description.
  Ask only about material ambiguity or scope changes; don't repeat questions already answered in a complete brief.
- Start with a planning session, before any helper: idea; scope (in, out, parked); requirements with acceptance;
  the person's constraints; phases and dependencies. Use plan_update and plan_ready only when listed.
  The app exports docs/ROADMAP.md and docs/PROJECT-BRIEF.md; do not ask read-only agents to write them.
- Respect the effective plan mode within the frozen provider grant. Prepare is read-only: only read-only discovery
  assignments, no implementation.
  The plan holds the effective mode; the brief's mode is a suggestion. Only a signed-in person can widen to Build
  using Team > Plan > Run mode. You may narrow to Prepare with plan_update after stopping helpers; this stops
  your lead so read-only permissions apply when the person resumes it.
  Reading a constraint does not authorize changing it. Propose changes for the person to decide; never rewrite
  person constraints yourself. Respect decision blocks on phases and requirements; dropped prerequisites do not
  satisfy dependencies. Revalidate assignments after material requirement/brief changes and notify affected helpers.
- Read plan() before plan_update; retain versions and give reasons, mark obsolete phases dropped. Your person
  may answer asynchronously, edit the plan or talk directly. Record agreements on the working list and in the plan.
- One decision per card, never bundled: options, consequences, a recommendation and its reason. Work on a default
  only when reversible and in scope, with no blocking dependency. A default is never your person's answer or approval.
  Scope, machine changes, public actions, money and owner approvals wait for the real answer. Never work around a refusal.
- Use working-list cards for what needs your person; progress belongs in NOW (3-6 lines: running, waiting,
  shipped, next), refreshed at least hourly. Ingest each answer with what changed; preserve the person's edits.
- For each assignment choose the smallest sufficient profile. Set need.role to its profile ID or catalog role.
  No-profile candidates are only for role: general. Explain the chosen profile and why. A missing matching profile
  means ask the person to add <profile> to the grant; name the closest choices, never widen the frozen grant.
- Use assign{goal,task_type,acceptance,requirement_ids,need}, then select{assignment,pin?}, then
  start{assignment,selection,name,kind}. A pin still goes through ranking and the task allow-list. No plain start in v2.
  Each brief includes scope, exclusions, related tests, acceptance, requirement IDs and role-specific Git rules.
  Default kind: temporary; helpers are disposable. Stop and replace completed, blocked or failed helpers within
  concurrency limits. Use give{helper,assignment} for a new assignment to an idle persistent helper.
- A new attempt on the same assignment must use its continuation pack: assignment and referenced requirements and
  constraints, latest report/handoff from every earlier attempt (paths and summaries; read full content as needed),
  earlier branch/head and what was merged. Continue from the previous head unless you explicitly choose fresh.
  Do not rebrief from scratch or lose earlier evidence. Keep both attempts attributable. If launch is uncertain,
  inspect/adopt the recorded attempt; do not duplicate it with a new selection.
- Read-only reviewers never fix. Persist findings through report{findings_md}; remediation is a new assignment.
  Security-sensitive fixes get a second independent review of the fixed head, preferably a different model.
  Bind review evidence to the reviewed revision; a review of an older head is not approval of the fix.
- Only use gates, reserve and restack when listed as available. Helpers run bounded related checks and list skips;
  the gate owns the full suite. Gate unavailability means limited verification, never an invented pass or an
  unmanaged full run. Retain original failures. A retry-passed rerun is not integration success until a person
  accepts a flake task or a fresh clean gate passes. Only the owner's project list defines known flakes; list them in the PR.
  Reserve numbers only when available; otherwise ask the person/lead to coordinate numbers. Restack only stopped
  helpers when available; record conflicts and verify the resulting head, base and gate commands.
- Watch idle, stuck and capacity-errored helpers. On reports check evidence and available gates, not confidence.
  Respect stops, expiry and assignment changes; report limits. Only the root opens one consolidated PR, never merges main.
- Native subagents share the parent's slot and permissions, are not shown in the tree, and their count cannot be capped.
  Council uses managed helpers within grant limits, or sequential single-session perspectives; these are not independent.
  Profiles, skills and native subagents confer no additional authority."""


def enabled() -> bool:
    return worklist.enabled()


class OrchestrationPlan(Base):
    """A team's plan: the idea, the scope and the session state. `version` counts saved versions (plan_version)."""
    __tablename__ = "orchestration_plan"
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), primary_key=True)
    state: Mapped[str] = mapped_column(String(16), default="planning")
    idea: Mapped[str] = mapped_column(Text, default="")
    scope: Mapped[dict] = mapped_column(JSON, default=dict)  # {in: [...], out: [...], parked: [...]}
    version: Mapped[int] = mapped_column(Integer, default=0)
    run_mode: Mapped[str] = mapped_column(String(16), default="build")
    build_inputs: Mapped[dict] = mapped_column(JSON, default=dict)
    # The person's edits the lead must read before changing: {"idea" | "scope" | "phase:<key>": {version, by}}
    person_edits: Mapped[dict] = mapped_column(JSON, default=dict)
    lead_seen_version: Mapped[int] = mapped_column(Integer, default=0)  # the version the lead last read with plan()
    ready_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)


class PlanPhase(Base):
    __tablename__ = "plan_phase"
    __table_args__ = (UniqueConstraint("team_id", "key", name="uq_plan_phase_key"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), index=True)
    key: Mapped[str] = mapped_column(String(32))
    position: Mapped[float] = mapped_column(Float)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(Text, default="")
    check: Mapped[str] = mapped_column(Text, default="")  # how the phase will be checked
    status: Mapped[str] = mapped_column(String(16), default="planned")
    depends_on: Mapped[list] = mapped_column(JSON, default=list)  # phase keys
    requirement_ids: Mapped[list] = mapped_column(JSON, default=list)


class PlanVersion(Base):
    """One saved version of the plan, with its reason: the readable history."""
    __tablename__ = "plan_version"
    __table_args__ = (UniqueConstraint("team_id", "version", name="uq_plan_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey("team.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(500))
    actor: Mapped[str] = mapped_column(String(64))
    snapshot: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


# ------------------------------------------------------------------------------------------------ the plan

def _has_helpers(db, team_id: str) -> bool:
    return bool(db.scalar(select(func.count()).select_from(Session).where(
        Session.team_id == team_id, Session.parent_session_id.is_not(None))))


def plan_for(db, team_id: str) -> OrchestrationPlan:
    plan = db.get(OrchestrationPlan, team_id)
    if plan is None:
        # Helpers can't start while a team plans, so a team that already has them (from before slice 6) is past
        # planning: reading or editing its plan must not create one that refuses every start.
        plan = OrchestrationPlan(team_id=team_id, state="ready" if _has_helpers(db, team_id) else "planning", idea="",
                                 scope={}, version=0, person_edits={}, lead_seen_version=0, run_mode="build",
                                 build_inputs=(db.get(Session, team_id).launch_spec.inputs if db.get(Session, team_id) else {}))
        db.add(plan)
        db.flush()
    return plan


def phases(db, team_id: str) -> list[PlanPhase]:
    return list(db.scalars(select(PlanPhase).where(PlanPhase.team_id == team_id).order_by(PlanPhase.position, PlanPhase.id)))


def _scope(value) -> dict:
    if not isinstance(value, dict) or set(value) - set(SCOPES):
        raise Refused("scope is {in, out, parked}, each a list of short lines")
    out = {}
    for part in SCOPES:
        lines = value.get(part, [])
        if not isinstance(lines, list) or len(lines) > MAX_SCOPE_LINES:
            raise Refused(f"scope.{part} is a list of at most {MAX_SCOPE_LINES} lines")
        out[part] = [_text(line, MAX_LINE, f"a scope.{part} line", required=True, one_line=True) for line in lines]
    return out


def _phase(value) -> dict:
    if not isinstance(value, dict) or set(value) - {"key", "title", "summary", "check", "status", "depends_on", "requirement_ids"}:
        raise Refused("a phase is {key, title, summary, check, status, depends_on}")
    key = value.get("key")
    if not isinstance(key, str) or not KEY.match(key):
        raise Refused("a phase key is short and lowercase (a-z, 0-9, -)")
    out = {"key": key}
    if "title" in value:
        out["title"] = _text(value["title"], 200, "a phase's title", required=True, one_line=True)
    if "summary" in value:
        out["summary"] = _text(value["summary"], MAX_SUMMARY, "a phase's summary")
    if "check" in value:
        out["check"] = _text(value["check"], MAX_SUMMARY, "a phase's check")
    if "status" in value:
        if value["status"] not in PHASE_STATES:
            raise Refused(f"a phase's status is one of {', '.join(PHASE_STATES)}")
        out["status"] = value["status"]
    if "depends_on" in value:
        deps = value["depends_on"]
        if (not isinstance(deps, list) or len(deps) > MAX_DEPS
                or any(not isinstance(d, str) or not KEY.match(d) for d in deps) or len(set(deps)) != len(deps)):
            raise Refused(f"depends_on lists at most {MAX_DEPS} distinct phase keys")
        if key in deps:
            raise Refused(f"phase {key} can't depend on itself")
        out["depends_on"] = deps
    if "requirement_ids" in value:
        ids = value['requirement_ids']
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) or not re.fullmatch(r'R-[1-9][0-9]*', i) for i in ids):
            raise Refused('phase requirement_ids lists R-n identifiers')
        out['requirement_ids'] = list(dict.fromkeys(ids))
    return out


def _cycle(rows: list[PlanPhase]) -> list[str] | None:
    """A dependency cycle among the phases, as keys in order, or None."""
    edges = {row.key: list(row.depends_on or []) for row in rows}
    state: dict[str, int] = {}
    path: list[str] = []

    def visit(key):
        state[key] = 1
        path.append(key)
        for dep in edges.get(key, []):
            if state.get(dep) == 1:
                return path[path.index(dep):] + [dep]
            if dep not in state and (found := visit(dep)):
                return found
        state[key] = 2
        path.pop()
        return None

    for key in edges:
        if key not in state and (found := visit(key)):
            return found
    return None


def _snapshot(plan: OrchestrationPlan, rows: list[PlanPhase]) -> dict:
    return {"state": plan.state, "idea": plan.idea, "scope": plan.scope or {},
            "phases": [{"key": r.key, "title": r.title, "summary": r.summary, "check": r.check, "status": r.status,
                        "depends_on": list(r.depends_on or []), "requirement_ids": r.requirement_ids or []} for r in rows],
            "run_mode": plan.run_mode}


def _save(db, plan: OrchestrationPlan, actor: str, reason: str) -> PlanVersion:
    plan.version = (plan.version or 0) + 1
    row = PlanVersion(team_id=plan.team_id, version=plan.version, reason=reason, actor=actor,
                      snapshot={**_snapshot(plan, phases(db, plan.team_id)), **controls.row_snapshot(db, plan.team_id)})
    db.add(row)
    db.flush()
    return row


def _apply(db, plan: OrchestrationPlan, body: dict) -> list[str]:
    """Validate and apply a change to the idea, the scope or phases (each phase added or changed by its key; others
    stay as they are). Returns the fields it touched: "idea", "scope", "phase:<key>"."""
    team_id = plan.team_id
    if not set(body) & {"idea", "scope", "phases", "constraints", "requirements", "run_mode", "propose_constraints", "propose_requirements"}:
        raise Refused("change the idea, the scope or phases")
    touched = []
    if "idea" in body:
        plan.idea = _text(body["idea"], MAX_IDEA, "idea", required=True)
        touched.append("idea")
    if "scope" in body:
        plan.scope = _scope(body["scope"])
        touched.append("scope")
    given = body.get("phases", [])
    if not isinstance(given, list):
        raise Refused("phases is a list")
    changes = [_phase(value) for value in given]
    if len({c["key"] for c in changes}) != len(changes):
        raise Refused("each phase key appears once")
    existing = {row.key: row for row in phases(db, team_id)}
    if len(set(existing) | {c["key"] for c in changes}) > MAX_PHASES:
        raise Refused(f"a plan has at most {MAX_PHASES} phases")
    last = max((row.position for row in existing.values()), default=0.0)
    for change in changes:
        row = existing.get(change["key"])
        if row is None:
            if "title" not in change:
                raise Refused(f"new phase {change['key']} needs a title")
            last += 1.0
            row = PlanPhase(team_id=team_id, key=change["key"], position=last, summary="", check="", status="planned",
                            depends_on=[], requirement_ids=[])
            db.add(row)
            existing[row.key] = row
        for field in ("title", "summary", "check", "status", "depends_on", "requirement_ids"):
            if field in change:
                setattr(row, field, change[field])
        touched.append(f"phase:{change['key']}")
    rows = list(existing.values())
    for row in rows:
        for identity in row.requirement_ids or []:
            if db.get(Requirement, (team_id, identity)) is None:
                raise Refused(f'unknown requirement {identity}')
        missing = [d for d in row.depends_on or [] if d not in existing]
        if missing:
            raise Refused(f"phase {row.key} depends on unknown phases: {', '.join(missing)}")
    if cycle := _cycle(rows):
        raise Refused(f"the phases depend on each other in a circle: {' -> '.join(cycle)}")
    db.flush()
    return touched


def _field_words(field: str) -> str:
    if field.startswith("phase:"):
        return f"phase {field[6:]}"
    return {"idea": "the idea", "scope": "the scope"}.get(field, field)


def update(db, team_id: str, actor: str, body: dict, *, warnings=None) -> OrchestrationPlan:
    """The lead's change. Refused if it touches something the person edited after the lead last read the plan; a
    change to something the person edited (and the lead has read) is recorded on the working list for them."""
    if set(body) - TOOL_FIELDS['plan_update']:
        raise Refused('the lead cannot edit constraints or requirements; propose a change instead')
    reason = _text(body.get("reason"), MAX_REASON, "reason", required=True, one_line=True)
    plan = plan_for(db, team_id)
    edits = dict(plan.person_edits or {})
    wanted = ([f for f in ("idea", "scope") if f in body]
              + [f"phase:{p.get('key')}" for p in body.get("phases") or [] if isinstance(p, dict)])
    unread = [f for f in wanted if f in edits and edits[f]["version"] > (plan.lead_seen_version or 0)]
    if unread:
        raise Refused("your person edited " + ", ".join(f"{_field_words(f)} (version {edits[f]['version']})" for f in unread)
                      + " since you last read the plan: call plan() first")
    touched = _apply(db, plan, body)
    if 'run_mode' in body:
        controls._set_run_mode(db, plan, actor, body['run_mode'])
        touched.append('run_mode')
    for key, model in [('propose_constraints', Constraint), ('propose_requirements', Requirement)]:
        if key in body:
            controls.edit_rows(db, team_id, actor, body[key], model, proposed=True, warnings=warnings)
    overwritten = [f for f in touched if f in edits]
    for field in overwritten:
        edits.pop(field)
    plan.person_edits = edits
    _save(db, plan, actor, reason)
    controls.affected(db, plan, touched)
    # lead_seen_version moves only when the lead reads the plan (plan()): its own save must not count as having read
    # a person's edit to another field, which it could then overwrite unread.
    if overwritten:  # never silently: the person hears that the lead changed what they wrote
        worklist.add(db, team_id, "system", "system", {
            "kind": "note", "title": f"The lead changed your plan edit: {', '.join(_field_words(f) for f in overwritten)}"[:200],
            "steps": f"Plan version {plan.version}. The lead's reason: {reason}\nYour version stays in Plan history."})
    db.add(AuditLog(actor=actor, action="plan.update", target=team_id,
                    detail={"version": plan.version, "fields": touched, "overwrote_person": overwritten}))
    return plan


def person_update(db, team_id: str, who: str, body: dict) -> OrchestrationPlan:
    """The person edits the plan. Needs the version they loaded; each edit is a new version under their name."""
    if set(body) - {"version", "idea", "scope", "phases", "reason", "constraints", "requirements", "run_mode"}:
        raise Refused("unknown fields")
    plan = plan_for(db, team_id)
    if body.get("version") != plan.version:
        raise Refused("The plan changed since you loaded it. It now shows the latest version; your text is kept, so check it and save again.", 409)
    reason = _text(body.get("reason") or f"Edited by {who}", MAX_REASON, "reason", required=True, one_line=True)
    touched = []
    for key, model, prefix in [('constraints', Constraint, 'constraint'), ('requirements', Requirement, 'requirement')]:
        if key in body:
            touched += [f'{prefix}:{i}' for i in controls.edit_rows(db, team_id, who, body[key], model)]
    if 'run_mode' in body:
        controls._set_run_mode(db, plan, who, body['run_mode'])
        touched.append('run_mode')
    touched += _apply(db, plan, body)
    _save(db, plan, who, reason)
    controls.affected(db, plan, touched)
    plan.person_edits = {**(plan.person_edits or {}), **{f: {"version": plan.version, "by": who} for f in touched}}
    db.add(AuditLog(actor=who, action="plan.person_edit", target=team_id, detail={"version": plan.version, "fields": touched}))
    return plan


def open_questions(db, team_id: str) -> list[dict]:
    """The working list's decisions and questions that are still waiting for the person."""
    rows = db.scalars(select(worklist.WorkItem).where(
        worklist.WorkItem.team_id == team_id, worklist.WorkItem.kind.in_(("decision", "question")),
        worklist.WorkItem.state == "open").order_by(worklist.WorkItem.position, worklist.WorkItem.id))
    return [{"id": row.id, "kind": row.kind, "title": row.title,
             **({'blocks': row.blocks} if row.blocks else {})} for row in rows]


def ready(db, team_id: str, actor: str, reason) -> OrchestrationPlan:
    """End the planning session. Open questions may stay open: the person answers them in their own time."""
    reason = _text(reason, MAX_REASON, "reason", required=True, one_line=True)
    plan = plan_for(db, team_id)
    if plan.state == "ready":
        raise Refused("the plan is already ready: keep it current with plan_update")
    live = [row for row in phases(db, team_id) if row.status != "dropped"]
    if not plan.idea.strip() or not (plan.scope or {}).get("in") or not live:
        raise Refused("a plan needs its idea, what is in scope, and at least one phase first (plan_update)")
    from .project_briefs import require_ready
    require_ready(db, team_id)
    plan.state, plan.ready_at = "ready", utcnow()
    _save(db, plan, actor, reason)
    questions = open_questions(db, team_id)
    # A note, not a need: the plan is for the person to read when they have time, so it raises no Inbox item.
    worklist.add(db, team_id, "system", "system", {
        "kind": "note", "title": f"Plan version {plan.version} is ready: {len(live)} phases, {len(questions)} open questions",
        "steps": "Read it on this tab under Plan. Answer the open questions here in any order; the lead follows each "
                 "card's recommendation until you answer."})
    db.add(AuditLog(actor=actor, action="plan.ready", target=team_id,
                    detail={"version": plan.version, "phases": len(live), "open_questions": len(questions)}))
    return plan


def require_ready(db, team_id: str | None) -> None:
    """Refuse a helper launch while the team is still planning (a team with helpers from before slice 6 is exempt)."""
    if team_id is None:
        return
    from .project_briefs import require_ready as require_brief_ready
    try:
        require_brief_ready(db, team_id, helper_start=True)
    except Refused as error:
        raise LaunchRefused(str(error)) from None
    plan = db.get(OrchestrationPlan, team_id)
    if plan is None:
        if not enabled() or _has_helpers(db, team_id):
            return
        plan = plan_for(db, team_id)
    if plan.state == "planning":
        raise LaunchRefused("finish the planning session first: write the plan with plan_update, put open decisions "
                            "on the working list, then call plan_ready")


# ------------------------------------------------------------------------------------------------ views

def _blocked_by(row: PlanPhase, by_key: dict[str, PlanPhase]) -> list[str]:
    memo = {}
    def satisfied(key, seen):
        if key in memo:
            return memo[key]
        if key in seen or key not in by_key or by_key[key].status != 'done':
            memo[key] = False
            return False
        memo[key] = all(satisfied(d, seen | {key}) for d in by_key[key].depends_on or [])
        return memo[key]
    return [d for d in row.depends_on or [] if not satisfied(d, {row.key})]


def mermaid(rows: list[PlanPhase]) -> str:
    """The roadmap as a Mermaid flowchart: an arrow from each phase to the phases that wait for it."""
    ids = {row.key: f"p{index}" for index, row in enumerate(rows)}
    lines = ["flowchart TD"]
    for row in rows:
        # Mermaid syntax and directives, never markup from the plan: the UI renders it with securityLevel strict.
        label = re.sub(r'["\[\]{}()<>|#&;`%\\:=]', "", row.title)[:80].strip() or row.key
        lines.append(f'  {ids[row.key]}["{label} · {row.status}"]')
    for row in rows:
        lines.extend(f"  {ids[dep]} --> {ids[row.key]}" for dep in row.depends_on or [] if dep in ids)
    for status in PHASE_STATES:
        members = [ids[row.key] for row in rows if row.status == status]
        if members:
            lines.append(f"  class {','.join(members)} {status}")
    lines += ["  classDef done stroke-width:3px", "  classDef active stroke-dasharray:0",
              "  classDef dropped stroke-dasharray:4 4", "  classDef planned stroke-width:1px"]
    return "\n".join(lines)


_MD_INLINE = re.compile(r"([\\`*_\[\]<>|~#!&])")
_MD_LINE_START = re.compile(r"^(?:([-+=])|(\d+)([.)]))")


def _md(text: str | None, *, block: bool = False) -> str:
    """Plan text as literal Markdown text: the idea, scope, titles and checks can't open a fence, a heading, a list, a
    table column, a link or HTML in docs/ROADMAP.md (review 2026-10-26, L-5). A block keeps its lines; inline text is
    one line."""
    def line(value: str) -> str:
        value = _MD_INLINE.sub(r"\\\1", value.strip())
        return _MD_LINE_START.sub(lambda m: f"\\{m[1]}" if m[1] else f"{m[2]}\\{m[3]}", value)
    return "\n".join(map(line, (text or "").split("\n") if block else [" ".join((text or "").split())]))


def markdown(plan: OrchestrationPlan, rows: list[PlanPhase], questions: list[dict], boundaries: dict | None = None) -> str:
    """docs/ROADMAP.md as the lead commits it. Everything the plan's authors wrote goes through _md."""
    by_key = {row.key: row for row in rows}
    scope = plan.scope or {}
    out = ["# Roadmap", "", f"Plan version {plan.version}, {'ready' if plan.state == 'ready' else 'in the planning session'}.",
           "", "## Idea", "", _md(plan.idea, block=True) if plan.idea else "(not written yet)", "", "## Scope", ""]
    for part, words in (("in", "In"), ("out", "Out"), ("parked", "Parked for later")):
        out.append(f"**{words}:**")
        out.extend(f"- {_md(line)}" for line in scope.get(part, [])) if scope.get(part) else out.append("- (none)")
        out.append("")
    out += ["## Run mode", "", getattr(plan, 'run_mode', 'build'), ""]
    for key, title in [('constraints', 'Constraints'), ('requirements', 'Requirements')]:
        out += [f'## {title}', '']
        for boundary in (boundaries or {}).get(key, []):
            out.append(f"- {boundary['id']} ({boundary['state']}, {boundary['source']}, revision {boundary['version']}): {_md(boundary['text'])}")
            if boundary.get('acceptance'):
                out.append(f"  Acceptance: {_md(boundary['acceptance'])}")
        out.append('')
    out += ["## Phases", "", "```mermaid", mermaid(rows), "```", "",
            "| Phase | Status | Depends on | Checked by |", "|---|---|---|---|"]
    for row in rows:
        deps = ", ".join(_md(by_key[d].title) for d in row.depends_on or []) or "—"
        out.append(f"| {_md(row.title)} | {row.status} | {deps} | {_md(row.check) if row.check else '—'} |")
    out += ["", "## Open questions", ""]
    out.extend(f"- {_md(q['title'])} (working list item {q['id']})" for q in questions) if questions else out.append("None.")
    return "\n".join(out) + "\n"


def view(db, team_id: str) -> dict:
    plan = plan_for(db, team_id)
    rows = phases(db, team_id)
    by_key = {row.key: row for row in rows}
    questions = open_questions(db, team_id)
    requirement_states = {r.id: r.state for r in controls.rows(db, team_id, Requirement)}
    history = list(db.scalars(select(PlanVersion).where(PlanVersion.team_id == team_id)
                              .order_by(PlanVersion.version.desc()).limit(HISTORY)))
    return {"state": plan.state, "idea": plan.idea, "scope": {part: (plan.scope or {}).get(part, []) for part in SCOPES},
            "run_mode": plan.run_mode, **controls.row_snapshot(db, team_id),
            "version": plan.version, "ready_at": plan.ready_at, "person_edits": plan.person_edits or {},
            "phases": [{"key": r.key, "title": r.title, "summary": r.summary, "check": r.check, "status": r.status,
                        "depends_on": list(r.depends_on or []), "requirement_ids": r.requirement_ids or [], "blocked_by": _blocked_by(r, by_key),
                        "inactive_requirements": [i for i in r.requirement_ids or [] if requirement_states.get(i) != 'active'],
                        "decision_blocks": [{'id': q['id'], 'title': q['title']} for q in questions
                            if set(q.get('blocks', [])) & ({r.key} | set(r.requirement_ids or []))]} for r in rows],
            "change_notices": [{'version': n.plan_version, 'assignment': n.assignment_id, 'attempt': n.attempt_id,
                                'fields': n.fields} for n in db.scalars(select(PlanChangeNotice).where(
                                    PlanChangeNotice.team_id == team_id).order_by(PlanChangeNotice.id.desc()).limit(20))],
            "open_questions": questions, "mermaid": mermaid(rows), "markdown": markdown(plan, rows, questions, controls.row_snapshot(db, team_id)),
            "history": [{"version": h.version, "reason": h.reason, "actor": h.actor, "at": h.created_at} for h in history]}


def version(db, team_id: str, number: int) -> dict | None:
    row = db.scalar(select(PlanVersion).where(PlanVersion.team_id == team_id, PlanVersion.version == number))
    return None if row is None else {"version": row.version, "reason": row.reason, "actor": row.actor,
                                     "at": row.created_at, "plan": row.snapshot}


# ------------------------------------------------------------------------------------------------ the lead's tools

TOOL_FIELDS = {
    "plan": set(),
    "plan_update": {"idea", "scope", "phases", "reason", "propose_constraints", "propose_requirements", "run_mode"},
    "plan_ready": {"reason"},
    "plan_version": {"version"},
}


def lead_action(supervisor, caller_id: str, payload: dict):
    action = payload["action"]
    with supervisor.db() as db:
        lead, team = worklist._lead(db, caller_id)
        actor = f"session:{lead.id}"
        if action == "plan":
            out = view(db, team.id)
            plan_for(db, team.id).lead_seen_version = out["version"]
            db.commit()
            return out
        if action == "plan_version":
            number = payload.get("version")
            found = version(db, team.id, number) if type(number) is int else None
            if found is None:
                raise Refused("no such plan version")
            return found
        if action == 'plan_update' and set(payload) - (TOOL_FIELDS[action] | {'action', 'id'}):
            raise Refused('the lead cannot edit constraints or requirements; propose a change instead')
        redact = supervisor.vault.redactor(db, lead.vault_keys).deep
        warnings = []
        if action == "plan_update":
            update(db, team.id, actor, redact({k: payload[k] for k in TOOL_FIELDS[action] if k in payload}), warnings=warnings)
        elif action == "plan_ready":
            ready(db, team.id, actor, redact(payload.get("reason")))
        else:
            raise Refused("unknown plan tool")
        out = view(db, team.id)
        if warnings:
            out['warnings'] = warnings
        db.commit()
        return out


async def _tool(supervisor, caller_id, payload):
    try:
        out = lead_action(supervisor, caller_id, payload)
        if payload['action'] == 'plan_update' and payload.get('run_mode') == 'prepare':
            await supervisor.stop(caller_id, f'session:{caller_id}')
        return out
    except Refused as error:
        raise LaunchRefused(str(error)) from None


TEAM_FIELDS: dict[str, set[str]] = dict(TOOL_FIELDS)
TEAM_ACTIONS: dict = {name: _tool for name in TOOL_FIELDS}
def scope_state_changed(db, session, previous, state):
    from .scope_notices import refresh
    refresh(db, session)


ON_STATE: list = [scope_state_changed]


# Stable lane E/C adapters. Callers authenticate the actor and hold the team's write transaction.
def draft_brief(db, team_id, actor, brief_version, requirements, constraints):
    """Authorized adapter, in the caller's write transaction; does not commit.

    Rows are strings or {id?, text, acceptance?, state?}; omit id for new rows.
    A username means a person; session:<id> means proposals only. Keep the brief's
    verbatim submitted text separately. Updates name existing R-n/C-n identifiers.
    """
    return controls.draft_brief(db, team_id, actor, brief_version, requirements, constraints)


def set_run_mode(db, team_id, actor, mode):
    """Authorized adapter; does not commit. Only a signed-in human can widen.
    A lead may narrow; its tool handler stops it before read-only resume.
    """
    return controls.set_run_mode(db, team_id, actor, mode)


def check_assignment(db, team_id, assignment, inputs):
    return controls.check_assignment(db, team_id, assignment, inputs)


def require_start(db, team_id, payload, allowed=None):
    if team_id is None:
        return
    plan = db.get(OrchestrationPlan, team_id)
    if plan is None and (not enabled() or _has_helpers(db, team_id)):
        return  # a pre-planning legacy team has no lane D contract yet
    plan = plan or plan_for(db, team_id)
    identity = payload.get('assignment')
    if identity:
        assignment = next((r for r in controls.assignment_rows(db, team_id) if r.id == identity), None)
        if assignment is None:
            raise LaunchRefused('no such assignment in this team')
        if allowed is None:
            from ..models import Team
            from .selection import Selection
            team = db.get(Team, team_id)
            helper = db.get(Session, payload.get('helper')) if payload.get('action') == 'give' and isinstance(payload.get('helper'), str) else None
            if helper is not None and helper.team_id == team_id:
                allowed = {'agent': helper.provider, 'inputs': helper.launch_spec.inputs}
            selection_id = payload.get('selection')
            selection = db.get(Selection, selection_id) if isinstance(selection_id, str) else None
            if allowed is None and selection is not None and selection.team_id == team_id and type(selection.chosen) is int:
                candidate = selection.candidates[selection.chosen] if 0 <= selection.chosen < len(selection.candidates) else {}
                index = candidate.get('grant_index')
                if type(index) is int and 0 <= index < len(team.limits['agents']):
                    allowed = team.limits['agents'][index]
            if allowed is None and plan.run_mode == 'prepare':
                raise LaunchRefused('prepare mode requires a selected read-only discovery assignment')
        inputs = controls.LaunchInputs.from_dict(allowed['inputs']) if allowed else controls.LaunchInputs(provider='claude')
        check_assignment(db, team_id, assignment, inputs)
    elif plan.run_mode == 'prepare' or any(card.blocks for card in db.scalars(select(worklist.WorkItem).where(
            worklist.WorkItem.team_id == team_id, worklist.WorkItem.state == 'open'))):
        raise LaunchRefused('an assignment with requirement_ids is required for planning start checks')


PREPARE_ACTIONS = frozenset({'plan', 'plan_update', 'plan_ready', 'plan_version', 'worklist', 'worklist_add',
    'worklist_update', 'worklist_ingest', 'worklist_done', 'now', 'report', 'brief', 'assign', 'select',
    'start', 'give', 'list', 'status', 'read', 'message', 'stop', 'finish', 'gate_status'})


def require_action(db, team_id, action):
    """Provider permission cannot constrain a server-side Git merge or gate tool.
    Existing prepare contracts remain restrictive even when a feature override is off.
    """
    plan = db.get(OrchestrationPlan, team_id) if team_id else None
    if plan and plan.run_mode == 'prepare' and action not in PREPARE_ACTIONS and not str(action).startswith('brief_'):
        raise LaunchRefused(f'prepare mode refuses {action}; only planning, reports and read-only discovery are allowed')


def require_resume(db, session):
    plan = db.get(OrchestrationPlan, session.team_id) if session.team_id else None
    if plan is None or plan.run_mode != 'prepare':
        return
    from .selection import permission
    inputs = controls.LaunchInputs.from_dict(session.launch_spec.inputs)
    if permission({'agent': session.provider, 'inputs': inputs.as_dict()}) != 'read-only':
        raise LaunchRefused('prepare mode requires read-only discovery; this session has write permission')
    if session.parent_session_id is not None:
        for assignment in controls.assignment_rows(db, plan.team_id):
            if any(a.session_id == session.id for a in controls.attempt_rows(db, assignment.id)):
                check_assignment(db, plan.team_id, assignment, inputs)
                return
        raise LaunchRefused('prepare mode requires a read-only discovery assignment before resuming a helper')
