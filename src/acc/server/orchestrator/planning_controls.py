"""Person-owned planning boundaries and assignment checks (orchestration lane D).

Planning is authoritative in the DB; an agent's proposal is never an accepted edit.
Assignment/Attempt imports are lazy so this lane also runs before lane C is replayed.
"""
from __future__ import annotations

import hashlib
import importlib
import json
from dataclasses import replace
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, mapped_column

from ..compiler import LaunchInputs
from ..models import API_PRINCIPAL, AuditLog, Base, LaunchSpec, Session, User, UTCDateTime, utcnow
from ..supervisor_common import LaunchRefused
from . import delivery, worklist
from .worklist import Refused, _text


class Constraint(Base):
    __tablename__ = 'plan_constraint'
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id'), primary_key=True)
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(String(16), default='person')
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default='active')
    brief_version: Mapped[int | None] = mapped_column(Integer)


class Requirement(Base):
    __tablename__ = 'plan_requirement'
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id'), primary_key=True)
    id: Mapped[str] = mapped_column(String(16), primary_key=True)
    text: Mapped[str] = mapped_column(Text)
    acceptance: Mapped[str] = mapped_column(Text, default='')
    source: Mapped[str] = mapped_column(String(16), default='person')
    version: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default='active')
    brief_version: Mapped[int | None] = mapped_column(Integer)


class PlanProposal(Base):
    __tablename__ = 'plan_proposal'
    item_id: Mapped[int] = mapped_column(ForeignKey('work_item.id'), primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id'), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    target_id: Mapped[str] = mapped_column(String(16))
    base_version: Mapped[int] = mapped_column(Integer)
    brief_version: Mapped[int | None] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    acceptance: Mapped[str] = mapped_column(Text, default='')


class PlanChangeNotice(Base):
    __tablename__ = 'plan_change_notice'
    __table_args__ = (UniqueConstraint('team_id', 'plan_version', 'assignment_id', 'attempt_id', name='uq_plan_notice'),)
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[str] = mapped_column(ForeignKey('team.id'), index=True)
    plan_version: Mapped[int] = mapped_column(Integer)
    assignment_id: Mapped[str] = mapped_column(String(32))
    attempt_id: Mapped[str] = mapped_column(String(32))
    session_id: Mapped[str | None] = mapped_column(ForeignKey('session.id'))
    fields: Mapped[list] = mapped_column(JSON)
    delivery_id: Mapped[str | None] = mapped_column(ForeignKey('message_delivery.id'))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


def rows(db, team_id, model):
    return list(db.scalars(select(model).where(model.team_id == team_id).order_by(model.id)))


def row_out(row):
    fields = ('id', 'text', 'source', 'version', 'state', 'brief_version')
    return {**{k: getattr(row, k) for k in fields}, **({'acceptance': row.acceptance} if isinstance(row, Requirement) else {})}


def row_snapshot(db, team_id):
    return {'constraints': [row_out(r) for r in rows(db, team_id, Constraint)],
            'requirements': [row_out(r) for r in rows(db, team_id, Requirement)]}


# Internal actors that are never a person, even if a username happened to match.
NOT_PEOPLE = frozenset({'system', 'supervisor', 'planning', 'scheduler', 'schedule', 'automation', 'lead', 'owner'})


def _is_person(db, actor):
    """An authenticated human principal: a signed-in person's username, or the installation API token (the owner's
    own credential). Never a session or agent (`session:…`), a schedule or other `kind:` actor, or an internal label."""
    if actor == API_PRINCIPAL:
        return True
    return (isinstance(actor, str) and ':' not in actor and actor not in NOT_PEOPLE
            and db.scalar(select(User.id).where(User.username == actor)) is not None)


def _person(db, actor):
    if not _is_person(db, actor):
        raise Refused('only the person changes constraints, requirements or run mode; propose a change instead')


def edit_rows(db, team_id, actor, changes, model, *, source='person', brief_version=None, proposed=False):
    if not proposed:
        _person(db, actor)
    elif source == 'person':
        source = 'lead'  # a proposal is the lead's text until the person accepts it, and is labelled so
    if not isinstance(changes, list) or len(changes) > 100:
        raise Refused('at most 100 planning rows per change')
    existing = {r.id: r for r in rows(db, team_id, model)}
    next_id = max((int(r[2:]) for r in existing), default=0)
    touched, seen = [], set()
    prefix = 'C' if model is Constraint else 'R'
    for change in changes:
        change = {'text': change} if isinstance(change, str) else change
        if not isinstance(change, dict) or set(change) - {'id', 'text', 'acceptance', 'state'}:
            raise Refused('a planning row has id, text, acceptance (requirements only), and state')
        if model is Constraint and 'acceptance' in change:
            raise Refused('acceptance belongs to a requirement')
        identity = change.get('id')
        row = existing.get(identity) if isinstance(identity, str) else None
        if identity is not None and row is None:
            raise Refused('unknown planning row; omit id to create one')
        if identity is not None and identity in seen:
            raise Refused('each planning row appears once')
        seen.add(identity)
        text = _text(change.get('text', row.text if row else None), 2000, 'planning text', required=True)
        acceptance = change.get('acceptance', getattr(row, 'acceptance', ''))
        if isinstance(acceptance, list) and len(acceptance) <= 20:
            acceptance = '\n'.join(_text(a, 200, 'acceptance line', required=True) for a in acceptance)
        acceptance = _text(acceptance, 4000, 'acceptance')
        state = change.get('state', 'active')
        if state not in ('active', 'proposed_change', 'retired'):
            raise Refused('planning row state is active, proposed_change or retired')
        if row is None:
            # Pending proposals have their own budget, so a lead can't fill the person's 100 rows.
            if proposed and sum(r.state == 'proposed_change' for r in existing.values()) >= 20:
                raise Refused('at most 20 proposed rows of each kind wait for the person; ask them to answer some first')
            if sum(r.state != 'proposed_change' for r in existing.values()) >= 100:
                raise Refused('a plan has at most 100 rows of each kind')
            next_id += 1
            identity = f'{prefix}-{next_id}'
            row = model(team_id=team_id, id=identity, text=text, source=source, version=1,
                        state='proposed_change' if proposed else state, brief_version=brief_version)
            if model is Requirement:
                row.acceptance = acceptance
            db.add(row)
            existing[identity] = row
        elif not proposed:
            row.text, row.state, row.version = text, state, row.version + 1
            if model is Requirement:
                row.acceptance = acceptance
            # An edit preserves its origin; a new brief explicitly records its version.
            if brief_version is not None:
                row.source, row.brief_version = source, brief_version
        touched.append(identity)
        if proposed:
            item = worklist.add(db, team_id, actor, 'lead', {
                'kind': 'decision', 'title': f'Proposed change to {identity}',
                'steps': f'Current: {row.text}\nProposed: {text}\nAcceptance: {acceptance}',
                'options': [{'id': 'accept', 'label': 'Accept change'}, {'id': 'keep', 'label': 'Keep current boundary'}]})
            db.add(PlanProposal(item_id=item.id, team_id=team_id, kind=prefix, target_id=identity,
                                base_version=row.version, brief_version=brief_version, text=text, acceptance=acceptance))
    db.flush()
    return touched


def _assignment_module():
    name = 'acc.server.orchestrator.assignments'
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise  # a broken installed adapter must never silently remove enforcement
        return None  # pre-lane-C database: there are no assignment rows yet


def assignment_rows(db, team_id):
    module = _assignment_module()
    return [] if module is None else list(db.scalars(select(module.Assignment).where(module.Assignment.team_id == team_id)))


def attempt_rows(db, assignment_id):
    module = _assignment_module()
    return [] if module is None else list(db.scalars(select(module.Attempt).where(module.Attempt.assignment_id == assignment_id)))


def affected(db, plan, fields):
    """One transaction changes the boundary, invalidates evidence and records durable attempt notices."""
    from . import planning
    changed_requirements = {f[12:] for f in fields if f.startswith('requirement:')}
    global_change = any(f.startswith('constraint:') for f in fields)
    phases = planning.phases(db, plan.team_id)
    changed_phases = {f[6:] for f in fields if f.startswith('phase:')}
    changed_phases.update(p.key for p in phases if set(p.requirement_ids or []) & changed_requirements)
    # Dependencies carry the effect forward, even if an intermediate phase was already done.
    for _ in phases:
        changed_phases.update(p.key for p in phases if set(p.depends_on or []) & changed_phases)
    for assignment in assignment_rows(db, plan.team_id):
        if assignment.team_id != plan.team_id:
            continue
        refs = set(assignment.requirement_ids or [])
        assignment_phases = {p.key for p in phases if set(p.requirement_ids or []) & refs}
        explicit_phase = getattr(assignment, 'phase_key', None) or (assignment.need or {}).get('phase')
        if explicit_phase:
            assignment_phases.add(explicit_phase)
        if not (global_change or refs & changed_requirements or assignment_phases & changed_phases):
            continue
        if assignment.state not in ('cancelled', 'superseded'):
            assignment.state = 'needs_revalidation'
        for attempt in attempt_rows(db, assignment.id):
            if attempt.assignment_id != assignment.id or attempt.state not in ('running', 'blocked', 'reported') or not attempt.session_id:
                continue
            session = db.get(Session, attempt.session_id)
            if session is None or session.team_id != plan.team_id or session.desired_state != 'running':
                continue
            text = (f'{delivery.NUDGE_ORIGIN}Plan change notice version {plan.version}: '
                    f'{", ".join(fields)} changed. Read plan() before continuing assignment {assignment.id}. '
                    'Earlier affected reports and verification need revalidation; agent text is not approval.')
            from . import scope_notices
            scope_notices.queue(db, assignment, attempt, session, version=plan.version, fields=fields, text=text)
    db.add(AuditLog(actor='planning', action='plan.change_notice', target=plan.team_id,
                    detail={'version': plan.version, 'fields': fields}))


def readonly_inputs(inputs):
    if inputs.provider not in ('claude', 'codex', 'copilot'):
        raise LaunchRefused('prepare mode needs a qualified read-only provider: Claude, Codex or Copilot')
    return replace(inputs, permission_mode='plan', sandbox='read-only', approval='on-request', extra_args=(),
                   options={**inputs.options, **({'mode': 'plan', 'permissions': 'ask'} if inputs.provider == 'copilot' else {})})


def _set_run_mode(db, plan, actor, mode):
    if mode not in ('prepare', 'build'):
        raise Refused('run mode is prepare or build')
    lead = db.get(Session, plan.team_id)
    human = _is_person(db, actor)  # the owner's API token is the person too (owner decision, 27 Oct)
    is_lead = lead is not None and actor == f'session:{lead.id}' and lead.id == plan.team_id
    if not human:
        if mode != 'prepare' or not is_lead:
            raise Refused('only the person (signed in, or with the owner API token) can widen Prepare to Build; '
                          'ask them to use Team > Plan > Run mode', 403)
        worklist._lead(db, lead.id)
    if plan.run_mode != mode and lead is not None:
        if (lead.desired_state == 'running' and not is_lead) or lead.team_stop_pending:
            raise Refused('stop the lead and wait for stop confirmation before changing run mode; then resume it', 409)
        if mode == 'prepare':
            members = db.scalars(select(Session).where(Session.team_id == plan.team_id, Session.id != lead.id))
            if any(m.desired_state == 'running' or m.team_stop_pending for m in members):
                raise Refused('stop the helpers and wait for confirmation before choosing prepare mode', 409)
            if any(a.state == 'launching' for row in assignment_rows(db, plan.team_id) for a in attempt_rows(db, row.id)):
                raise Refused('a helper launch is still in progress; wait before choosing prepare mode', 409)
            from ..models import TeamRequest
            pending = db.scalars(select(TeamRequest).join(Session, Session.id == TeamRequest.session_id).where(
                Session.team_id == plan.team_id, TeamRequest.delivered.is_(False)))
            if any(r.payload.get('action') == 'start' and (r.result or {}).get('error', '').startswith('request interrupted;') for r in pending):
                raise Refused('a helper launch is still in progress; inspect it before choosing prepare mode', 409)
        if not plan.build_inputs:
            # Existing plans receive an empty migration default. Capture their
            # approved launch inputs before the first prepare transition.
            plan.build_inputs = dict(lead.launch_spec.inputs)
        inputs = LaunchInputs.from_dict(plan.build_inputs)
        if mode == 'prepare':
            try:
                inputs = readonly_inputs(inputs)
            except LaunchRefused as error:
                raise Refused(str(error)) from None
        files = dict(lead.launch_spec.files)
        if 'settings.json' in files and lead.provider == 'claude':
            settings = json.loads(files['settings.json'])
            settings.setdefault('permissions', {})['defaultMode'] = inputs.permission_mode
            if mode == 'prepare':
                settings.pop('skipDangerousModePermissionPrompt', None)
            files['settings.json'] = json.dumps(settings, indent=2) + '\n'
        lead.launch_spec = LaunchSpec(provider=lead.provider, inputs=inputs.as_dict(), files=files,
            digest=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
    plan.run_mode = mode
    return plan


def set_run_mode(db, team_id, actor, mode):
    from . import planning
    plan = planning.plan_for(db, team_id)
    _set_run_mode(db, plan, actor, mode)
    planning._save(db, plan, actor, f'Run mode: {mode}')
    db.add(AuditLog(actor=actor, action='plan.run_mode', target=team_id, detail={'mode': mode, 'version': plan.version}))
    return plan


def draft_brief(db, team_id, actor, brief_version, requirements, constraints):
    from . import planning
    if type(brief_version) is not int or brief_version < 1:
        raise Refused('brief_version is a positive integer')
    proposed = not _is_person(db, actor)
    touched = [f'requirement:{i}' for i in edit_rows(db, team_id, actor, requirements, Requirement,
        source='brief', brief_version=brief_version, proposed=proposed)]
    touched += [f'constraint:{i}' for i in edit_rows(db, team_id, actor, constraints, Constraint,
        source='brief', brief_version=brief_version, proposed=proposed)]
    plan = planning.plan_for(db, team_id)
    planning._save(db, plan, actor, f'Brief version {brief_version}: {"proposed" if proposed else "accepted"} rows')
    if not proposed:
        affected(db, plan, touched)
    db.add(AuditLog(actor=actor, action='plan.brief_draft', target=team_id, detail={
        'version': plan.version, 'brief_version': brief_version, 'proposed': proposed, 'fields': touched}))
    return row_snapshot(db, team_id)


def accept_proposal(db, item, actor):
    from . import planning
    proposal = db.get(PlanProposal, item.id)
    if proposal is None:
        return
    _person(db, actor)
    model = Constraint if proposal.kind == 'C' else Requirement
    row = db.get(model, (item.team_id, proposal.target_id))
    if row is None or item.answer == 'accept' and row.version != proposal.base_version:
        raise Refused('the boundary changed since this proposal; ask for a new proposal', 409)
    if item.answer == 'accept':
        row.text, row.version, row.state = proposal.text, row.version + 1, 'active'
        if proposal.brief_version is not None:
            row.source, row.brief_version = 'brief', proposal.brief_version
        if model is Requirement:
            row.acceptance = proposal.acceptance
    elif row.state == 'proposed_change' and row.version == proposal.base_version:
        row.state, row.version = 'retired', row.version + 1
    plan = planning.plan_for(db, item.team_id)
    planning._save(db, plan, actor, f'{proposal.target_id}: person answered proposal {item.id}')
    db.add(AuditLog(actor=actor, action='plan.proposal_answer', target=item.team_id, detail={
        'item': item.id, 'target': row.id, 'answer': item.answer, 'version': plan.version}))
    if item.answer == 'accept':
        affected(db, plan, [f'{"constraint" if model is Constraint else "requirement"}:{row.id}'])


def _readonly_helper(inputs):
    """The helper's real launch is read-only: the same providers and settings readonly_inputs gives the lead."""
    if inputs.extra_args:
        return False
    if inputs.provider == 'claude':
        return inputs.permission_mode == 'plan'
    if inputs.provider == 'codex':
        return inputs.sandbox == 'read-only'
    if inputs.provider == 'copilot':
        return (inputs.options or {}).get('mode') == 'plan' and (inputs.options or {}).get('permissions', 'ask') == 'ask'
    return False


def check_assignment(db, team_id, assignment, inputs):
    """Lane C may call this directly, under the same transaction as start/claim.

    An Assignment has team_id, requirement_ids, task_type, need; inputs are the
    selected frozen grant inputs. Phases are derived from requirement links.
    """
    from . import planning, selection
    plan = planning.plan_for(db, team_id)
    if assignment.team_id != team_id:
        raise LaunchRefused('assignment belongs to another team')
    if plan.run_mode == 'prepare':
        choice = {'agent': inputs.provider, 'inputs': inputs.as_dict()}
        if (assignment.task_type != 'discovery' or selection.permission(choice) != 'read-only'
                or not _readonly_helper(inputs)):
            raise LaunchRefused('prepare mode permits only read-only discovery assignments on Claude, Codex or Copilot, '
                                'in their read-only permission and without extra arguments')
    refs = assignment.requirement_ids or []
    if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
        raise LaunchRefused('assignment requirement_ids must be a list')
    for identity in refs:
        row = db.get(Requirement, (team_id, identity))
        if row is None or row.state != 'active':
            raise LaunchRefused(f'requirement {identity} is not active')
    phases = planning.phases(db, team_id)
    by_key = {p.key: p for p in phases}
    keys = {p.key for p in phases if set(p.requirement_ids or []) & set(refs)}
    explicit = getattr(assignment, 'phase_key', None) or (assignment.need or {}).get('phase')
    if explicit:
        if explicit not in by_key:
            raise LaunchRefused(f'unknown assignment phase {explicit}')
        keys.add(explicit)
    for key in keys:
        phase = by_key[key]
        blocked = planning._blocked_by(phase, by_key)
        if phase.status == 'dropped' or blocked:
            raise LaunchRefused(f'phase {key} is blocked by {", ".join(blocked) if blocked else "its dropped status"}')
    # A card blocking R-n also blocks the phases that link R-n, and while any card blocks work an assignment must
    # say what it works on: otherwise a start that names nothing would never meet the person's blocks.
    covered = keys | set(refs) | {r for key in keys for r in (by_key[key].requirement_ids or [])}
    for card in db.scalars(select(worklist.WorkItem).where(worklist.WorkItem.team_id == team_id,
                            worklist.WorkItem.kind.in_(('decision', 'question')), worklist.WorkItem.state == 'open')):
        if card.blocks and not (keys or refs):
            raise LaunchRefused(f'decision card {card.id} blocks work: name the assignment\'s requirement_ids or phase')
        if set(card.blocks or []) & covered:
            raise LaunchRefused(f'blocked by decision card {card.id}: {card.title}')
