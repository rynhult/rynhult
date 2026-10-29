"""Orchestrator v2 (docs/ORCHESTRATOR-V2.md on the orchestrator-v2-design branch): one module per build slice, so
parallel slices never edit the same file. This foundation (slice 0) only wires the seams; behaviour is unchanged.

  slice 1  worklist.py      working list and NOW            api/worklist.py    web WorkList.tsx    migration 0069
  slice 2  live.py          tree snapshot and live events    api/team_live.py   web TeamTree.tsx    (no table)
  slice 3  selection.py     explainable helper selection     api/selection.py   web SelectionReasons migration 0070
  slice 4  delivery.py      message delivery, task reports   (api/sessions.py)                      migration 0071
  slice 5  gates.py, reservations.py  gate queue, numbers    api/gates.py       web Gates.tsx       migration 0072
  slice 6  planning.py      planning session, roadmap, lead instructions  api/plan.py  web Plan.tsx  migration 0073

Each slice module may define, and only it changes:
  TEAM_FIELDS   {action: {argument names}}: what the acc-team spool accepts for its tools (teams.py validates with it)
  TEAM_ACTIONS  {action: async handler(supervisor, caller_id, payload)}: run instead of refusing an unknown action
  ON_STATE      [callable(db, session, previous, state)]: called inside the supervisor's state transition
Their tools' MCP descriptions live in that slice's section of bin/acc-mcp-team. Tables use the shared models.Base and
register through the import at the end of models.py. Migrations stay linear: a new table asks the coordinator for its
number when it is built and names main's head at that time as down_revision (orch0-r's rule).
"""
from __future__ import annotations

from . import assignments, delivery, gates, live, planning, project_briefs, reservations, selection, worklist
from .switch import enabled as orchestrator_enabled

SLICES = (worklist, live, selection, delivery, gates, reservations, planning, project_briefs, assignments)


def _merged(name: str) -> dict:
    merged: dict = {}
    for module in SLICES:
        if callable(getattr(module, "enabled", None)) and not module.enabled():
            continue
        part = getattr(module, name, {})
        if overlap := set(merged) & set(part):
            raise RuntimeError(f"orchestrator slices define the same team action twice: {sorted(overlap)}")
        merged.update(part)
    return merged


TEAM_FIELDS: dict[str, set[str]] = _merged("TEAM_FIELDS")
TEAM_ACTIONS: dict = _merged("TEAM_ACTIONS")
ON_STATE: list = [hook for module in SLICES for hook in getattr(module, "ON_STATE", [])]


def team_fields() -> dict[str, set[str]]:
    """The slices' team tools, while Orchestrator v2 is on; otherwise none (teams.py then knows only M17's)."""
    return _merged("TEAM_FIELDS") if orchestrator_enabled() else {}


def team_actions() -> dict:
    return _merged("TEAM_ACTIONS") if orchestrator_enabled() else {}


# One write budget per team, whoever calls (security review 2026-10-25, OV-7; threat review X-2, X-8): every team
# member shares one Unix account, so a helper can act as its lead, and a limit per caller would bound nothing.
WRITE_ACTIONS = frozenset({
    'start', 'message', 'stop', 'finish', 'merge',                                    # M17
    'worklist_add', 'worklist_update', 'worklist_ingest', 'worklist_done', 'now',      # slice 1
    'assign', 'give', 'select', 'report', 'gate', 'restack', 'reserve', 'release',                       # slices 3-5
    'brief_propose_documents', 'brief_interpret', 'brief_draft', 'brief_question', 'brief_share', 'brief_reference',
    'plan_update', 'plan_ready',                                                       # slice 6
})
WRITES_PER_MINUTE = 60
_writes: dict[str, list[float]] = {}
_audited: dict[str, float] = {}


def admit_write(supervisor, caller_id: str, action: str) -> None:
    """Count one team write, or refuse it (audited at most once a minute per team). Reads are never limited."""
    import time

    from ..models import AuditLog, Session
    from ..supervisor_common import LaunchRefused
    if action not in WRITE_ACTIONS:
        return
    with supervisor.db() as db:
        caller = db.get(Session, caller_id)
        team_id = caller.team_id if caller else None
        if team_id is None:
            return  # not a team member: the action's own checks refuse it
        now = time.monotonic()
        recent = [t for t in _writes.get(team_id, []) if t > now - 60]
        if len(recent) >= WRITES_PER_MINUTE:
            _writes[team_id] = recent
            if _audited.get(team_id, 0) <= now - 60:
                _audited[team_id] = now
                db.add(AuditLog(actor=f'session:{caller_id}', action='team.rate_limited', target=team_id,
                                detail={'action': action, 'limit': WRITES_PER_MINUTE}))
                db.commit()
            raise LaunchRefused(f'team write limit: at most {WRITES_PER_MINUTE} changes a minute for the whole team; wait')
        recent.append(now)
        _writes[team_id] = recent


def state_changed(db, session, previous: str, state: str) -> None:
    """The supervisor's one call site for state transitions (slice 2's live events, slice 1's idle-worker alert)."""
    if not orchestrator_enabled():
        return
    for module in SLICES:
        if callable(getattr(module, "enabled", None)) and not module.enabled():
            continue
        for hook in getattr(module, "ON_STATE", []):
            hook(db, session, previous, state)


ORCHESTRATOR_OFF = 'Orchestrator v2 was turned off'
PREVIEW_OFF = ORCHESTRATOR_OFF  # compatibility for in-flight integrations


async def reconcile_availability(supervisor) -> dict:
    """Once at accd start (owner decision 2026-10-26). With Orchestrator v2 off, nothing it started is left looking alive:
    queued or running gate jobs end as cancelled (their process is asked to stop), unfinished message deliveries fail,
    open selections are refused, each with ORCHESTRATOR_OFF in its history; the working list's questions leave the Inbox.
    With Orchestrator v2 on, open questions of running teams are in the Inbox again. Runs only where this main holds
    write authority (HA), like the delivery worker."""
    from contextlib import nullcontext

    from sqlalchemy import select

    from ..models import Team
    authority = getattr(supervisor.broker, 'authority', None)
    done = {'gates': 0, 'deliveries': 0, 'selections': 0, 'inbox': 0}
    with authority.activity() if authority else nullcontext():
        if orchestrator_enabled():
            with supervisor.db() as db:
                done['inbox'] = worklist.reraise_inbox(db) if worklist.enabled() else 0
                db.commit()
            return done
        with supervisor.db() as db:
            active = [(job.id, job.account, job.host, job.pid_record or {}) for job in
                      db.scalars(select(gates.GateJob).where(gates.GateJob.state.in_(gates.ACTIVE)))]
        stopped = {}
        for job_id, account, host, record in active:
            confirmed = True
            if record.get('pid'):
                try:
                    await supervisor.broker.call('gate_stop', account=account, host=host, job=job_id,
                                                 pid=record.get('pid'), start=record.get('start'))
                    await supervisor.broker.call('gate_cleanup', account=account, host=host, job=job_id)
                except Exception:
                    confirmed = False  # the machine is away: its wrapper still ends the job at the time limit
            stopped[job_id] = confirmed
        with supervisor.db() as db:
            for job_id, confirmed in stopped.items():
                job = db.get(gates.GateJob, job_id)
                if job is not None and job.state in gates.ACTIVE:
                    gates._finish(db, job, 'cancelled', ORCHESTRATOR_OFF + ('' if confirmed else '; the machine could not '
                                  'be asked to stop it, so it ends at its time limit'), actor='accd')
                    done['gates'] += 1
            for row in db.scalars(select(delivery.MessageDelivery).where(
                    delivery.MessageDelivery.state.not_in(delivery.TERMINAL))):
                delivery.transition(db, row, 'failed', error='orchestrator_off')
                done['deliveries'] += 1
            for row in db.scalars(select(selection.Selection).where(selection.Selection.state.in_(('proposed', 'overridden')))):
                row.state, row.reason = 'refused', ORCHESTRATOR_OFF
                selection.record_event(db, row, 'accd', 'refused')
                done['selections'] += 1
            for team_id in db.scalars(select(Team.id)):
                done['inbox'] += worklist.close_inbox(db, team_id)
            db.commit()
    return done


# Compatibility for existing worker integrations; new callers use feature availability.
reconcile_preview = reconcile_availability
