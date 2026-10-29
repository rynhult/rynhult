"""Session supervisor: spool ingestion, state tracking and the reconcile loop.

accd owns desired state; the broker only executes. Rules:
- desired=running and the pane is gone or crashed -> resume the same native conversation,
  with bounded retries, then recovery_needed. Never start a fresh conversation in its place.
- a clean exit is the owner's decision -> desired=stopped, no resume.
- desired=stopped/archived -> never relaunched.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import sessionmaker

from .. import providers
from . import attention, budgets, containers, group_board, guardrails, orchestrator, vault
from . import ingest as ingest_module
from .backups import BackupMixin
from .broker_client import BrokerError, Brokers, BrokerUnavailable, MachineOffline
from .catalog import load as load_catalog
from .compiler import LaunchInputs, compile_files
from .config import Settings
from .db import MaintenanceBusy, write_in_thread
from .explanation_runs import ExplanationRuns
from .github import GitHub
from .history import HistoryMixin
from .ingest import IngestMixin, read_spool_file, spool_path
from .issue_sources import IssueSources
from .machine_presence import connected, evaluate, sleep_due, transition
from .models import (
    AuditLog,
    Event,
    Group,
    GroupLaunch,
    LaunchSpec,
    ModelEndpoint,
    Node,
    Observation,
    Placement,
    Project,
    Session,
    SessionRun,
)
from .node_tasks import joined_thread
from .orchestrator import project_briefs
from .polling import PollingMixin
from .reconcile import ReconcileMixin, kill_params
from .supervisor_common import LaunchRefused, log, now
from .teams import TeamMixin, team_files
from .workspace_operations import WorkspaceOperations

REMOTE_SPOOL_BUDGET = 10.0  # seconds for all hosts' spools in one pass
REMOTE_BACKOFF_MIN, REMOTE_BACKOFF_MAX = 15.0, 300.0  # a failing host is skipped this long (doubling)
REMOTE_SPOOL_CHUNK = 512 * 1024  # the most one read_spool answer may carry (the broker's own cap, ops.SPOOL_READ)
MAX_MIRROR = 64 << 20  # bytes mirrored per remote session
LAYOUT_TTL = 300.0  # seconds another machine's layout (Supervisor.layout) is reused
BUSY_WARN = 300.0  # seconds of skipped passes before the maintenance lock counts as stuck and is logged as a warning
ENV_CLEANUP = "env_cleanup"  # Observation: stopped sessions whose keys are still to be removed from disk (D21)
ENV_CLEARED = ("stopped", "exited", "archived", "recovery_needed")  # states in which a session holds no keys on disk


def spool_chunk(result) -> bytes:
    """The bytes of a read_spool answer, checked: an object whose data is strict base64 of at most
    REMOTE_SPOOL_CHUNK bytes. ValueError otherwise."""
    if not isinstance(result, dict):
        raise ValueError("not an object")
    data = result.get("data") or ""
    if not isinstance(data, str):
        raise ValueError("data is not a string")
    if len(data) > 4 * -(-REMOTE_SPOOL_CHUNK // 3):
        raise ValueError(f"over {REMOTE_SPOOL_CHUNK} bytes")
    try:
        decoded = base64.b64decode(data, validate=True)
    except (ValueError, TypeError) as error:
        raise ValueError(f"bad base64: {error}") from None
    if len(decoded) > REMOTE_SPOOL_CHUNK:
        raise ValueError(f"over {REMOTE_SPOOL_CHUNK} bytes")
    return decoded


def spool_path_for(settings, acc_id: str):
    return settings.data_dir / "remote-spool" / acc_id / "events.jsonl"  # same as ingest.spool_path for a remote session


class Supervisor(TeamMixin, PollingMixin, IngestMixin, ReconcileMixin, HistoryMixin, BackupMixin, ExplanationRuns):
    def __init__(self, settings: Settings, db: sessionmaker, broker: Brokers):
        ingest_module.TRUSTED_ROOTS = tuple(p for p in (settings.run_dir, settings.root_run_dir, settings.data_dir) if p)
        self.settings = settings
        self.db = db
        self.broker = broker
        self.broker_ok = False
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()
        self.workspace_operations = WorkspaceOperations()
        self._launching: set[str] = set()
        self._autostarted: set[int] = set()
        self._autostart_held: set[int] = set()  # autostart refused these (compose files not approved: D4)
        self.publisher = None  # set by the app
        self.catalog = load_catalog(settings.catalog_dir, settings.catalog_local_dir)
        self._skills_synced = False
        self.codex_plugins_by_account: dict[str, dict] = {}  # each account's own Codex plugins (from its broker)
        self._github: GitHub | None = None
        self._deferred: list[tuple] = []
        self._autostart_done = False
        self.push = None  # set by the app (Web Push); notifications are skipped without it
        self._push_cursor: int | None = None
        self._host_backoff: dict[str, tuple[int, float]] = {}  # remote spools: host -> (failures, skip until)
        self.vault = vault.Vault(settings.vault_key_path)
        from .telemetry import Telemetry
        self.telemetry = Telemetry(settings, db, self.vault)
        self.issue_sources = IssueSources()
        self._env_pending = True  # look for removals queued before a restart (D21)
        self.init_backups()

    # ---------------------------------------------------------------- public actions

    def endpoint_info(self, db: DbSession, endpoint_id: str | None) -> dict | None:
        """A model endpoint as the compiler needs it (no key), or None."""
        endpoint = db.get(ModelEndpoint, endpoint_id) if endpoint_id else None
        return {"id": endpoint.id, "name": endpoint.name, "styles": endpoint.styles,
                "context_tokens": endpoint.context_tokens} if endpoint else None

    def codex_plugins(self, account: str) -> dict:
        return self.codex_plugins_by_account.get(account, {"plugins": [], "skill_paths": []})

    def set_catalog(self, catalog) -> None:
        """A catalog change from the dashboard: new launches use it; Codex skill links are re-synced."""
        self.catalog = catalog
        self._skills_synced = False
        self._wake.set()

    def known_layout(self, host: str, account: str) -> dict | None:
        """layout() without asking anyone: this machine's, or another's as last reported (None if never)."""
        if host == "local":
            return {"run_dir": str(self.settings.account_run_dir(account)), "hook_path": self.settings.hook_path,
                    "statusline_path": self.settings.statusline_path, "home": str(self.settings.account_home(account))}
        cached = self.__dict__.get("_layouts", {}).get((host, account))
        return cached[1] if cached else None

    async def layout(self, host: str, account: str) -> dict:
        """Where a session of `account` on `host` lives (Q4): its run folder's parent, the hook and statusline commands
        and the account's home. This machine's from settings; another machine's from its broker (ping), since a node
        installs elsewhere (/opt/acc on Linux, ~/Library/Application Support/ACC on a Mac). Cached for a while."""
        known = self.known_layout(host, account)
        if known is not None and (host == "local" or time.monotonic() - self._layouts[(host, account)][0] < LAYOUT_TTL):
            return known
        cache = self.__dict__.setdefault("_layouts", {})
        info = await self.broker.call("ping", account=account, host=host)
        if not all(isinstance(info.get(k), str) and info[k].startswith("/") for k in ("run_dir", "hook_path", "home")):
            raise LaunchRefused(f"{host} runs an older version of Rynhult's machine software. Update Rynhult on {host}, then try again.")
        layout = {"run_dir": info["run_dir"], "hook_path": info["hook_path"], "home": info["home"],
                  "statusline_path": info.get("statusline_path") or str(Path(info["hook_path"]).with_name("acc-statusline"))}
        cache[(host, account)] = (time.monotonic(), layout)
        return layout

    async def create_session(self, project_id: int, inputs: LaunchInputs, name: str, actor: str,
                             workdir: str | None = None, owner: str | None = None,
                             worktree: dict | None = None, orchestrator: dict | None = None,
                             team_member: dict | None = None, schedule_run_id: str | None = None,
                             multirun_entry: tuple[str, int] | None = None, container: bool = False,
                             group_checkout: str | None = None, group_task: str | None = None, automation_policy: dict | None = None) -> str:
        from .admin_features import launch_refusal
        if problem := launch_refusal(self.settings, automation=automation_policy, orchestrator=orchestrator or team_member,
                                     schedule=bool(schedule_run_id), multirun=bool(multirun_entry)):
            raise LaunchRefused(problem)
        from .orchestrator import planning
        planning_assignment = None
        if team_member:
            team_member = dict(team_member)
            planning_assignment = team_member.pop('planning_assignment', None)
        build_inputs = inputs.as_dict()
        if orchestrator and planning.enabled() and orchestrator.get('run_mode') == 'prepare':
            inputs = planning.controls.readonly_inputs(inputs)
        with self.db() as db:
            project = db.get(Project, project_id)
            if project is not None:
                containers.require_creation(db, self.settings, project, container,
                                            (team_member or {}).get("team_host") or project.host or "local", actor, inputs.provider)
            if project is not None and project.group_id is not None:
                if not group_checkout or not group_task or orchestrator or team_member or schedule_run_id or multirun_entry or container:
                    raise LaunchRefused("A group session needs a reviewed checkout of its own and a task before it can start.")
                _, _, placement = group_board.launch_binding(db, project, group_checkout, owner, remote_enabled=self.settings.groups_remote_enabled)
                if placement.host != 'local':
                    from . import group_remote
                    if not group_remote.connected(self.broker, db, placement):
                        raise LaunchRefused('Your machine for this group is offline, or its agent account changed. Reconnect it, then try again.')
                where = (placement.host, placement.account)
            else:
                where = ((team_member or {}).get("team_host") or project.host or "local", project.account) if project is not None else ("local", "")
        if project is not None and project.group_id is None and where[0] != 'local':
            from .group_remote import reserved_for_other
            with self.db() as db:
                from .models import User
                person = db.scalar(select(User).where(User.username == owner))
                if person and person.role != 'owner' and reserved_for_other(db, where[0], where[1], owner):
                    raise LaunchRefused('That agent account on this machine belongs to another person. Choose another machine.')
        with self.db() as identity_db:
            identity_project = identity_db.get(Project, project_id)
        identity_warning = None
        if identity_project is not None and identity_project.trusted:
            from .git_identity import ensure
            identity_warning = await ensure(self, identity_project, actor,
                         checkout=worktree.get("path") if team_member and worktree and not container else None, host=where[0])
        try:
            layout = await self.layout(*where) if project is not None else None
        except MachineOffline as error:
            raise LaunchRefused(str(error)) from None
        except BrokerError as error:
            raise LaunchRefused(f"Rynhult couldn't reach {where[0]} to start the session ({error}). Check that it's on and connected in Host → Machines, then try again.") from None
        with self.db() as db:
            project = db.get(Project, project_id)
            group = None
            if project is not None:
                containers.require_creation(db, self.settings, project, container,
                                            (team_member or {}).get("team_host") or project.host or "local", actor, inputs.provider)
            if project is not None and project.group_id is not None:
                group, checkout, placement = group_board.launch_binding(db, project, group_checkout, owner, where[1], remote_enabled=self.settings.groups_remote_enabled)
                if placement.host != 'local':
                    from . import group_remote
                    if not group_remote.connected(self.broker, db, placement):
                        raise LaunchRefused('Your machine for this group is offline, or its agent account changed. Reconnect it, then try again.')
                prior = db.get(Session, group_checkout)
                if prior:
                    if prior.owner != owner or prior.project_id != project_id:
                        raise LaunchRefused("This checkout already belongs to another session.")
                    return prior.id
                group_task = group_board.safe_task(group_task)
                group_board.reserve(db, group)
                workdir = placement.path + '/sessions/' + group_checkout
            if project is None or project.archived:
                raise LaunchRefused("That project doesn't exist any more, or it's archived.")
            if project.import_mode != "managed":
                raise LaunchRefused("This project is read-only in Rynhult, so sessions can't start in it. Import it as managed to start sessions.")
            if not project.trusted and group is None:
                raise LaunchRefused("You haven't trusted this project's folder yet. Start the session again and choose Trust and launch.")
            if team_member:
                from .models import Team
                team = db.get(Team, team_member['team_id'])
                if not team or team.stopped or now() >= team.expires_at:
                    raise LaunchRefused("This team was stopped or its time ran out. Start a new Orchestrator session to continue.")
                current_plan = db.get(planning.OrchestrationPlan, team.id)
                if current_plan is not None:
                    if planning_assignment:
                        assignment = next((r for r in planning.controls.assignment_rows(db, team.id)
                                           if r.id == planning_assignment), None)
                        if assignment is None:
                            raise LaunchRefused('no such assignment at helper publication')
                        planning.check_assignment(db, team.id, assignment, inputs)
                    elif current_plan and current_plan.run_mode == 'prepare':
                        raise LaunchRefused('prepare helpers require a read-only discovery assignment')
            account = where[1]  # sessions run as the account that owns the project's folder
            try:  # refused before anything is recorded: two keys with one variable, an MCP key it can't have
                key_plan = vault.plan(db, project.id, owner or project.owner, (where[0] != "local"), self.catalog,
                           inputs.mcps)
            except vault.VaultError as error:
                raise LaunchRefused(str(error)) from None
            if orchestrator:
                try:
                    # A schedule launches as its owner's principal, but no person is present: brief as context only.
                    orchestrator = project_briefs.launch_grant(db, project.id, orchestrator, person=schedule_run_id is None)
                    inputs = project_briefs.launch_inputs(db, project.id, inputs, orchestrator.get('brief_version'))
                    from .orchestrator.kickoff import initial_inputs
                    inputs = initial_inputs(db, project.id, inputs, orchestrator)
                except project_briefs.Refused as error:
                    raise LaunchRefused(str(error)) from None
            compiled = compile_files(inputs, layout["hook_path"], layout["statusline_path"],
                                     catalog=self.catalog, agent_home=layout["home"],
                                     codex_plugins=self.codex_plugins(account),
                                     endpoint=self.endpoint_info(db, inputs.endpoint),
                                     guardrails=guardrails.policy(db, (worktree or {}).get("path") or workdir
                                                                  or project.workspace_path,
                                                                  owner or project.owner, project.id, project.private_workspace))
            if orchestrator or team_member:
                from dataclasses import replace
                compiled = replace(compiled, files=team_files(compiled.files, inputs.provider, layout["hook_path"],
                                                              lead=bool(orchestrator), container=container, inputs=inputs))
                if orchestrator:
                    context = project_briefs.launch_context(db, project.id, orchestrator.get('brief_version'))
                    if context:
                        compiled = replace(compiled, files=providers.get(inputs.provider).add_context(compiled.files, context))
                compiled = replace(compiled, digest=hashlib.sha256(json.dumps(compiled.files, sort_keys=True).encode()).hexdigest())
            if group:
                group_task = group_board.shared_redactor(self.vault, db, key_plan.ids).text(group_task)
                from dataclasses import replace
                files = providers.get(inputs.provider).add_context(compiled.files,
                    f'Your group session: {group_checkout}; person: {owner}; machine: {placement.host}; branch: acc/{group_checkout}; base: {checkout.base_sha}.\n'
                    + group_board.context(db, group, group_checkout, self.settings.groups_remote_enabled))
                compiled = replace(compiled, files=files, digest=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest())
            spec = LaunchSpec(provider=inputs.provider, inputs=inputs.as_dict(), files=compiled.files, digest=compiled.digest)
            from .models import Schedule, ScheduleRun, User
            if problem := launch_refusal(self.settings, automation=automation_policy, orchestrator=orchestrator or team_member,
                                         schedule=bool(schedule_run_id), multirun=bool(multirun_entry), group=bool(project.group_id)):
                raise LaunchRefused(problem)
            intent = db.get(ScheduleRun, schedule_run_id) if schedule_run_id else None
            if schedule_run_id and (intent is None or intent.status != "claimed"):
                raise LaunchRefused("This scheduled run was cancelled before it started.")
            if intent:
                schedule = db.get(Schedule, intent.schedule_id)
                person = db.scalar(select(User).where(User.username == schedule.owner))
                if (person is None or schedule.owner != owner or project.account != schedule.account
                        or project.account != where[1]
                        or (person.account or self.settings.default_account) != schedule.account
                        or (person.role != "owner" and project.owner != person.username)):
                    raise LaunchRefused("The schedule's owner lost access to the project, or their agent account changed, while the run was starting. Check the schedule, then run it again.")
                if schedule.deleted or (intent.trigger == "timer" and not schedule.enabled):
                    raise LaunchRefused("The schedule was paused or deleted before this run started.")
            acc_id = group_checkout if group else (intent.reserved_session_id if intent else (team_member or {}).get('attempt_id') or uuid.uuid4().hex[:12])
            safe = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]", "-", name.lower())).strip("-")[:30] or project.slug  # tmux: ASCII only
            session = Session(
                id=acc_id, project=project, provider=inputs.provider, name=name,
                tmux_name=f"acc-attempt-{acc_id}" if (team_member or {}).get("attempt_id") else f"acc-{safe}-{acc_id[:8]}", launch_spec=spec,
                native_id=providers.get(inputs.provider).new_native_id() if providers.get(inputs.provider).session_id == "assigned" else None,
                desired_state="running", observed_state="starting", workdir=workdir, run_as=account,
                worktree=(worktree or {}).get("path"), worktree_branch=(worktree or {}).get("branch"),
                run_dir=f"{layout['run_dir'].rstrip('/')}/{acc_id}", owner=owner or project.owner,
                container=container,
            )
            if team_member:
                for key, value in team_member.items():
                    if key != 'attempt_id':
                        setattr(session, key, value)
            if multirun_entry:
                from .models import MultiRunEntry
                session.multirun_id = multirun_entry[0]
                db.add(session)
                db.flush()
                db.get(MultiRunEntry, multirun_entry).session_id = acc_id
            if orchestrator:
                from datetime import timedelta

                from .models import Team
                session.team_id = acc_id
                from .orchestrator.switch import preview
                db.add(Team(id=acc_id, protocol_version=2 if preview() else 1, limits=orchestrator, expires_at=now() + timedelta(hours=orchestrator["hours"])))
                db.flush()
                try:
                    project_briefs.pin_team(db, acc_id, project.id, orchestrator.get('brief_version'), actor=actor,
                                            run_mode=orchestrator.get('run_mode'), person=schedule_run_id is None)
                except project_briefs.Refused as error:
                    raise LaunchRefused(str(error)) from None
                db.add(AuditLog(actor=actor, action="team.grant", target=acc_id, detail=orchestrator))
            binding = db.scalar(select(Placement).where(
                Placement.project_id == project.id, Placement.person == session.owner,
                Placement.host == where[0], Placement.account == session.run_as, Placement.path == session.cwd))
            if binding is None:
                binding = Placement(project_id=project.id, person=session.owner, host=where[0],
                                    account=session.run_as, path=session.cwd, trust='approved' if group else 'legacy', sync_status='ready' if group else 'legacy')
                db.add(binding)
                db.flush()
            session.placement = binding
            db.add(session)
            if orchestrator and planning.enabled():
                db.flush()
                plan = planning.plan_for(db, acc_id)
                plan.run_mode = orchestrator.get('run_mode', 'build')
                plan.build_inputs = build_inputs
            if team_member and team_member.get('attempt_id'):
                from .orchestrator.assignments import Assignment, Attempt
                db.flush()
                attempt = db.get(Attempt, team_member['attempt_id'])
                assignment = db.get(Assignment, attempt.assignment_id) if attempt else None
                if (attempt is None or attempt.state != 'launching' or assignment is None
                        or assignment.team_id != session.team_id or assignment.created_by != session.parent_session_id):
                    raise LaunchRefused('attempt launch authority changed')
                attempt.session_id, attempt.state = session.id, 'running'

            if group:
                from .models import GroupWork
                db.flush()
                db.add(GroupWork(session_id=acc_id, checkout_id=group_checkout, task=group_task, base_sha=checkout.base_sha))
                db.add(GroupLaunch(id=acc_id, session_id=acc_id))
                db.add(AuditLog(actor=owner, action='group.source_trust', target=group.id,
                    detail={'project_id': project_id, 'checkout_id': acc_id, 'base_sha': checkout.base_sha}))
            if intent:
                db.flush()  # foreign key: attach the session and intent in the same transaction
                intent.session_id, intent.status = acc_id, "running"
                self.telemetry.count(db, "schedule.run")
            if identity_warning:
                db.flush()  # the warning event references the newly created session
                db.add(Event(session_id=acc_id, source="supervisor", kind="git_identity_warning",
                             payload={"message": identity_warning}))
            db.add(AuditLog(actor=actor, action="session.create", target=acc_id,
                            detail={"project": project.slug, "provider": inputs.provider, "digest": compiled.digest,
                                    **({"workdir": workdir} if workdir else {}),
                                    **({"worktree": worktree["path"], "branch": worktree["branch"]} if worktree else {})}))
            db.commit()
            cwd, host = session.cwd, session.host
        # From here the session exists and owns its folder (and worktree): a failure is the session's to show and
        # reconcile's to handle, never the caller's to clean up.
        self._launching.add(acc_id)
        try:
            # Reconcile must not try to resume a published session before its first launch reaches the broker.
            try:
                await self.broker.call("trust_workspace", account=account, host=host, path=cwd)
            except BrokerError as error:
                log.warning("trust_workspace %s: %s", cwd, error)
            async with self._lock:
                with self.db() as db:
                    session = db.get(Session, acc_id)
                    # A person can cancel while trust_workspace is in flight.
                    if session.desired_state == "running":
                        await self._start(db, session, reason="launch")
                    db.commit()
        except Exception:  # including an unavailable broker: the session shows it and reconcile retries
            log.exception("launch of %s failed after it was recorded; reconcile takes over", acc_id)
        finally:
            self._launching.discard(acc_id)
        return acc_id

    def register_external(self, project_id: int, provider: str, name: str, native_id: str, run_as: str,
                          unit: str, actor: str) -> str:
        """Record a session started outside Rynhult (e.g. root's own systemd unit). Observe-only."""
        with self.db() as db:
            project = db.get(Project, project_id)
            if project is not None and project.group_id is not None:
                raise LaunchRefused("Sessions started outside Rynhult can't be added to a group project.")
            if project is None:
                raise LaunchRefused("That project doesn't exist any more, or it's archived.")
            inputs = {"external": True, "provider": provider, "run_as": run_as, "unit": unit}
            digest = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
            acc_id = uuid.uuid4().hex[:12]
            db.add(Session(
                id=acc_id, project=project, provider=provider, name=name, tmux_name=f"ext-{acc_id}",
                native_id=native_id, management="external", run_as=run_as, external_unit=unit,
                launch_spec=LaunchSpec(provider=provider, inputs=inputs, files={}, digest=digest),
                desired_state="running", observed_state="starting", status_source="supervisor",
            ))
            db.add(AuditLog(actor=actor, action="session.register_external", target=acc_id, detail=inputs))
            db.commit()
        return acc_id

    async def stop(self, acc_id: str, actor: str, archive: bool = False, *, for_sleep: bool = False) -> None:
        async with self._lock:
            with self.db() as db:
                session = self._get(db, acc_id)
                if session.project is None:
                    return  # detached history has no process or checkout to stop
                if for_sleep and not await self._sleep_ready(db, session):
                    return
                if session.management == "external":
                    if not archive:
                        raise LaunchRefused(f"Rynhult didn't start this session, so it can't stop it. Stop it where it was started ({session.external_unit}).")
                    session.desired_state = "archived"
                    self._set_state(db, session, "archived", "api")
                    db.add(AuditLog(actor=actor, action="session.archive", target=acc_id))
                    db.commit()
                    return
                member = bool(session.team_id or session.project.group_id)  # retain capacity until broker confirms group stop
                multirun = bool(session.multirun_id)
                if member:
                    session.team_stop_pending = True
                if multirun:
                    session.multirun_stop_pending = True
                account, kill = session.run_as, kill_params(session, self.settings)
                container = session.container
                worktree, repo, name, branch = (session.worktree, session.team_repo or session.project.workspace_path, session.name,
                                                session.worktree_branch)
                host = session.host
                # The intent first: if anything below fails or accd stops, the session is already stopped/archived
                # (reconcile kills a pane that should not run) and never "running" in a folder that is gone.
                session.desired_state = "archived" if archive else "stopped"
                session.sleep_paused_at = now() if for_sleep else None
                if for_sleep:
                    session.last_error = 'stop not confirmed: awaiting machine confirmation'
                self._close_run(session, end_reason="machine_sleep" if for_sleep else "user_archive" if archive else "user_stop")
                self._set_state(db, session, "archived" if archive else "stopped", "supervisor" if for_sleep else "api")
                db.add(AuditLog(actor=actor, action="session.archive" if archive else "session.stop", target=acc_id))
                if for_sleep:
                    node = db.scalar(select(Node).where(Node.name == host))
                    db.add(AuditLog(actor=actor, action="session.sleep_pause", target=acc_id,
                                    detail={"machine": host, "idle_minutes": node.idle_sleep_minutes}))
                db.commit()  # and no write lock is held while the broker works (reconcile waits on self._lock)
            confirmed = False
            try:
                await self.broker.call("kill", account=account, host=host, **kill)
                confirmed = True
                if member or multirun or for_sleep:
                    with self.db() as db:
                        session = self._get(db, acc_id)
                        session.team_stop_pending = False
                        session.multirun_stop_pending = False
                        if for_sleep:
                            session.last_error = None
                        db.commit()
            except BrokerError as error:  # reconcile kills it later: desired_state already says so
                log.warning("stop %s: kill failed: %s", acc_id, error)
                if multirun or for_sleep:
                    with self.db() as db:
                        self._get(db, acc_id).last_error = f"stop not confirmed: {error}"
                        db.commit()
            if container:  # whatever is left of its container, the root helper's reconcile stops
                containers.revoke_grant(self.settings, acc_id)
            await self._clear_envs(only=acc_id)  # its keys leave the machine now (queued above by _set_state)
            if container and archive:  # merge bundles prepared for it; the run folder itself stays for history
                try:
                    await self.broker.call("team_clear_bundles", account=account, host=host, acc_id=acc_id)
                except BrokerError as error:
                    log.warning("archive %s: merge bundles kept: %s", acc_id, error)
            if multirun:
                if not confirmed:
                    raise LaunchRefused("Stop requested, but the machine hasn't confirmed it yet. Rynhult will keep trying to stop it.")
                with self.db() as db:
                    session = self._get(db, acc_id)
                    if (session.last_error or "").startswith("stop not confirmed:"):
                        session.last_error = None
                    db.commit()
            removed = None
            if archive and worktree and member and not confirmed:
                removed = {"removed": False, "reason": "stop is not confirmed yet"}
            elif archive and worktree and not multirun:
                try:
                    removed = await self.broker.call("remove_worktree", account=account, host=host, repo=repo, path=worktree)
                except BrokerError as error:
                    removed = {"removed": False, "reason": str(error)[:300]}
            if removed is None:
                return
            with self.db() as db:
                if removed is not None:
                    db.add(Event(session_id=acc_id, source="supervisor", kind="worktree_removed" if removed["removed"]
                                 else "worktree_kept", payload={"path": worktree, **removed}))
                    if not removed["removed"]:
                        attention.raise_item(db, f"worktree:{acc_id}", kind="worktree_kept", severity="info",
                                             source="supervisor", session_id=acc_id,
                                             title=f"{name}: worktree kept ({removed['reason']})"[:200],
                                             detail=f"{worktree} on {branch}. Commit or discard the "
                                                    "changes there, then delete the folder with `git worktree remove`.")
                db.commit()

    async def resume(self, acc_id: str, actor: str) -> None:
        with self.db() as db:
            session = self._get(db, acc_id)
            if session.project is None:
                raise LaunchRefused("This project was deleted. Its archived sessions cannot be resumed.")
            path, host = session.workdir or session.project.workspace_path, session.host
        return await self.workspace_operations.run(path, lambda: self._resume_operation(acc_id, actor), host, kind="resume")

    async def _resume_operation(self, acc_id: str, actor: str) -> None:
        with self.db() as db:
            member = self._get(db, acc_id).parent_session_id is not None
        if member:
            async with self.__dict__.setdefault('_team_lock', asyncio.Lock()):
                await self._resume(acc_id, actor)
        else:
            await self._resume(acc_id, actor)

    async def _resume(self, acc_id: str, actor: str) -> None:
        """Manual resume: same conversation only. Resets the retry budget."""
        with self.db() as db:
            session = self._get(db, acc_id)
            if "explanation.json" in session.launch_spec.files:
                raise LaunchRefused("An explanation can't be resumed. Open the original session's Changes tab and explain the changes again.")
            if session.management == "external":
                raise LaunchRefused(f"Rynhult didn't start this session, so it can't resume it. Start it where it was started ({session.external_unit}).")
            if session.project.private_workspace and not json.loads(session.launch_spec.files.get("guardrails.json", "{}" )).get("local_only"):
                raise LaunchRefused("This workspace is now a Development workspace, so this session can't resume. Start a new session in it.")
            exception = None
            if session.desired_state == "archived":
                from ..backup_activation import resume_permitted
                if not resume_permitted(db, session, consume=True):
                    raise LaunchRefused("This session is archived, so it can't be resumed.")
                exception = {"replacement_exception": True, "node": session.host}
            if ((session.team_id or session.project.group_id) and session.team_stop_pending) or session.multirun_stop_pending:
                raise LaunchRefused("The machine hasn't confirmed that this session stopped. Wait a moment, then try again.")
            if session.sleep_paused_at and (session.last_error or '').startswith('stop not confirmed:'):
                raise LaunchRefused("The machine hasn't confirmed that this session stopped. Wait a moment, then try again.")
            if session.parent_session_id:
                from .models import Team
                team = db.get(Team, session.team_id)
                if team.stopped or now() >= team.expires_at:
                    raise LaunchRefused("This team was stopped or its time ran out. Start a new Orchestrator session to continue.")
                running = len(list(db.scalars(select(Session.id).where(Session.team_id == team.id,
                                   Session.parent_session_id.is_not(None), (Session.desired_state == "running") | Session.team_stop_pending, Session.id != session.id))))
                if running >= team.limits["at_once"]:
                    raise LaunchRefused("The team already has as many helpers running as its limit allows. Stop a helper first.")
                from .orchestrator import assignments
                assignments.require_resume(db, session)
            if session.project.group_id:
                if budgets.paused(db, session.owner, session.provider):
                    raise LaunchRefused("Your budget pauses this agent, so the group session can't resume. Check your budget first.")
                if not group_board.authorized(db, session, self.settings.groups_remote_enabled):
                    raise LaunchRefused("You no longer have access to this group session.")
                if session.host != 'local':
                    from . import group_remote
                    source = group_remote.source_placement(db, session)
                    if not source or not group_remote.connected(self.broker, db, source):
                        raise LaunchRefused('Your machine for this group is offline, or its agent account changed. Reconnect it, then try again.')
                if session.desired_state != 'running':
                    group_board.reserve(db, db.get(Group, session.project.group_id))
                    db.add(GroupLaunch(id=uuid.uuid4().hex, session_id=session.id))
            from .orchestrator import planning
            planning.require_resume(db, session)
            session.desired_state = "running"
            session.sleep_paused_at = None
            session.recovery_attempts = 0
            session.next_attempt_at = None
            session.last_error = None
            if session.observed_state in ("stopped", "exited", "recovery_needed", "archived"):
                self._set_state(db, session, "recovering", "api")
            db.add(AuditLog(actor=actor, action="session.resume", target=acc_id, **({"detail": exception} if exception else {})))
            db.commit()
        self._wake.set()

    async def stop_held(self, acc_id: str, actor: str) -> None:
        """Owner-confirmed: stop the one pane the old main left running for a held restored session, through the node
        channel. The session stays archived; nothing else is stopped, adopted or resumed."""
        from ..backup_activation import held
        with self.db() as db:
            session = self._get(db, acc_id)
            if session.management != "acc" or session.host == "local" or not held(db, session):
                raise LaunchRefused("Only a held restored session on another machine can be stopped here.")
            host, account, params = session.host, session.run_as, kill_params(session, self.settings)
            db.rollback()
        if not connected(self.broker, host):
            raise LaunchRefused(f"{host} isn't connected, so nothing was stopped. Try again when it's back online.")
        try:
            await self.broker.call("kill", account=account, host=host, **params)
        except BrokerError as error:
            with self.db() as db:
                db.add(AuditLog(actor=actor, action="session.replacement_held_stop", target=acc_id,
                                detail={"node": host, "outcome": "failed"}))
                db.commit()
            log.warning("held stop on %s not confirmed: %s", host, error)  # the broker's detail is for the log
            raise LaunchRefused(f"{host} did not confirm the stop. Check that session's pane on {host} before trying "
                                "again.") from None
        with self.db() as db:
            db.add(AuditLog(actor=actor, action="session.replacement_held_stop", target=acc_id,
                            detail={"node": host, "outcome": "stopped"}))
            attention.resolve(db, f"replacement_held:{acc_id}")
            db.commit()

    async def interrupt(self, acc_id: str, actor: str) -> None:
        await self.cancel_automation(acc_id)
        with self.db() as db:
            session = self._get(db, acc_id)
            if session.management == "external":
                raise LaunchRefused("Rynhult didn't start this session, so it can't type into it.")
            account, tmux_name, host = session.run_as, session.tmux_name, session.host
            provider = session.provider
            db.rollback()  # no write lock while the broker works
        await self.broker.call("interrupt", account=account, host=host, tmux_name=tmux_name, provider=provider)
        with self.db() as db:
            session = self._get(db, acc_id)
            # Most CLIs send no hook for a user interrupt; Rynhult sent it, so it knows the turn ended.
            if session.observed_state in ("active", "waiting_input") and not providers.get(session.provider).interrupt_hook:
                self._set_state(db, session, "idle", "api")
            db.add(AuditLog(actor=actor, action="session.interrupt", target=acc_id))
            db.commit()

    async def cancel_automation(self, acc_id: str) -> None:
        from . import automation

        def work():
            with self.db() as db:
                automation.cancel(db, self._get(db, acc_id))
                db.commit()

        async with self._lock:
            await write_in_thread(work)

    async def send_text(self, acc_id: str, actor: str, text: str) -> dict | None:
        """Type a message into a running session's prompt and submit it (review feedback from the Changes tab)."""
        from .orchestrator import delivery
        if delivery.enabled():
            result = delivery.enqueue(self, acc_id, actor, text)
            delivery.start_worker(self)
            return result
        await self.cancel_automation(acc_id)
        with self.db() as db:
            session = self._get(db, acc_id)
            if session.management == "external":
                raise LaunchRefused("Rynhult didn't start this session, so it can't type into it.")
            if session.observed_state not in ("active", "waiting_input", "idle"):
                raise LaunchRefused("The session isn't running. Resume it first.")
            account, tmux_name, host = session.run_as, session.tmux_name, session.host
            provider = providers.get(session.provider)
            db.rollback()  # no write lock while the broker works
        if host != "local":
            await self._fetch_host_spools(host, [(acc_id, account)])
        with self.db() as db:
            self._ingest(db, self._get(db, acc_id))
            db.commit()
            last_event = db.scalar(select(Event.id).where(Event.session_id == acc_id).order_by(Event.id.desc()).limit(1)) or 0
        await self.broker.call("send_text", account=account, host=host, tmux_name=tmux_name, text=text, provider=provider.id)
        for _ in range(40):  # about 8 s: the broker may wait out a paste burst before Enter is accepted
            if host != "local":
                await self._fetch_host_spools(host, [(acc_id, account)])
            with self.db() as db:
                # This also runs from the supervisor's team stage, where the regular ingest loop is waiting on us.
                self._ingest(db, self._get(db, acc_id))
                db.commit()
                events = db.scalars(select(Event).where(
                    Event.session_id == acc_id, Event.id > last_event, Event.source == f"{provider.id}_hook"))
                if any(provider.normalize_hook(event.payload).get("hook_event_name") == "UserPromptSubmit"
                       for event in events):
                    break
            await asyncio.sleep(0.2)
        else:
            raise LaunchRefused("The message was typed, but Rynhult couldn't confirm it was sent. Check the session's terminal before sending it again.")
        with self.db() as db:
            text = self.vault.redactor(db, self._get(db, acc_id).vault_keys).text(text)
            db.add(AuditLog(actor=actor, action="session.message", target=acc_id, detail={"text": text}))
            db.commit()

    # ---------------------------------------------------------------- loop

    async def fetch_remote_spools(self) -> None:
        """Multi-host: copy new hook events of sessions on other hosts into local mirrors, which ingest then reads.

        A node controls its answers and how long they take, so: one overall time budget per pass, hosts in parallel,
        a host that fails (or runs out the budget) is left alone for a while, every reply is checked, and nothing
        here raises: local ingest always runs after it."""
        with self.db() as db:
            remote = [(s.id, s.run_as, s.host) for s in db.scalars(
                select(Session).join(Project, Session.project_id == Project.id)
                .where(Session.desired_state != "archived", Session.management == "acc")) if s.host != "local"]
        by_host: dict[str, list[tuple[str, str]]] = {}
        clock = asyncio.get_running_loop().time
        for acc_id, account, host in remote:
            if not connected(self.broker, host):
                continue
            if self._host_backoff.get(host, (0, 0.0))[1] <= clock():
                by_host.setdefault(host, []).append((acc_id, account))
        if not by_host:
            return
        tasks = {self.broker.hub.start(host, self._fetch_host_spools(host, sessions), "spools"): host
                 for host, sessions in by_host.items()}
        try:
            done, pending = await asyncio.wait(tasks, timeout=REMOTE_SPOOL_BUDGET)
        finally:
            # Also join children when the supervisor itself is cancelled during wait().
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        for task, host in tasks.items():
            failed = task in pending or task.cancelled() or task.exception() is not None or not task.result()
            if task in done and not task.cancelled() and task.exception() is not None:
                log.warning("remote spools of host %s: %r", host, task.exception())
            if failed:
                failures = self._host_backoff.get(host, (0, 0.0))[0] + 1
                delay = min(REMOTE_BACKOFF_MIN * 2 ** (failures - 1), REMOTE_BACKOFF_MAX)
                self._host_backoff[host] = (failures, clock() + delay)
                if task in pending:
                    log.warning("remote spools of host %s: no answer within %gs; next try in %gs", host,
                                REMOTE_SPOOL_BUDGET, delay)
            else:
                self._host_backoff.pop(host, None)

    async def _fetch_host_spools(self, host: str, sessions: list[tuple[str, str]]) -> bool:
        """One host's sessions, one after another. False when the host itself failed (offline, protocol errors)."""
        for acc_id, account in sessions:
            try:
                result = await self.broker.call("read_spool", account=account, host=host, acc_id=acc_id,
                                                offset=self._mirror_size(acc_id))
            except BrokerUnavailable as error:
                log.info("remote spools of host %s: %s", host, error)
                return False  # its node is offline or broken: the events wait there
            except BrokerError as error:
                log.warning("read_spool %s on %s: %s", acc_id, host, error)
                continue
            try:
                self._append_mirror(acc_id, spool_chunk(result))
            except ValueError as error:
                log.warning("read_spool %s on %s: bad answer (%s)", acc_id, host, error)
            except Exception:
                log.exception("mirroring the spool of %s on %s failed", acc_id, host)
        return True

    def _mirror_size(self, acc_id: str) -> int:
        """How far into the remote spool the mirror has read. The mirror is redacted, so its own size differs; a mirror
        from before redaction (no `offset` file) was a byte-for-byte copy."""
        mirror = spool_path_for(self.settings, acc_id)
        offset = mirror.with_name("offset")
        try:
            return int(offset.read_text())
        except (OSError, ValueError):
            return mirror.stat().st_size if mirror.exists() else 0

    def _append_mirror(self, acc_id: str, data: bytes) -> None:
        """Add to a session's mirror, up to MAX_MIRROR: past that, mirroring stops and one error event says so.

        Only whole lines are mirrored, each redacted with the session's keys as it is written (the mirror is Rynhult's
        copy on this machine; review D-low): a line cut by the chunk is read again next time."""
        if not data:
            return
        mirror = spool_path_for(self.settings, acc_id)
        full = mirror.with_name("mirror-full")
        if full.exists():
            return
        complete, newline, _ = data.rpartition(b"\n")
        if not newline:
            if len(data) < REMOTE_SPOOL_CHUNK:
                return  # the rest of this line hasn't arrived yet
            complete = data  # one line longer than a whole chunk: mirrored as it is cut (ingest skips it)
        consumed = len(complete) + len(newline)
        with self.db() as db:
            session = db.get(Session, acc_id)
            redact = self.vault.redactor(db, session.vault_keys if session else None)
        text = complete.decode(errors="surrogateescape")
        if redact:
            text = "\n".join(redact.text(line, json_escaped=True) for line in text.split("\n"))
        out = text.encode(errors="surrogateescape") + newline
        mirror.parent.mkdir(parents=True, exist_ok=True)
        size = mirror.stat().st_size if mirror.exists() else 0
        if size + len(out) > MAX_MIRROR:
            full.touch()
            log.warning("remote spool of %s reached %d bytes; no longer mirrored", acc_id, MAX_MIRROR)
            with self.db() as db:
                db.add(Event(session_id=acc_id, source="supervisor", kind="remote_spool_full", verified=False,
                             payload={"bytes": size, "limit": MAX_MIRROR}))
                db.commit()
            return
        offset = self._mirror_size(acc_id) + consumed
        with mirror.open("ab") as handle:
            handle.write(out)
        scratch = mirror.with_name("offset.new")
        scratch.write_text(str(offset))
        scratch.replace(mirror.with_name("offset"))

    async def ingest_pass(self) -> None:
        try:
            await self.fetch_remote_spools()
        except Exception:  # never in the way of local sessions' events
            log.exception("fetching remote spools failed")
        self.ingest_all()
        await self.expire_teams()
        await self.process_team_requests()

    async def ingest_container_audit(self) -> None:
        """The container launcher's audit lines, into Rynhult's audit log (kept as long as it is)."""
        def work():
            with self.db() as db:
                if containers.ingest_audit(db, self.settings):
                    db.commit()
        await joined_thread(work)

    async def evaluate_machines(self) -> None:
        from .models import Node
        with self.db() as db:
            for node in db.scalars(select(Node).where(Node.status == 'active')):
                online = connected(self.broker, node.name)
                # Preserve observed outages across restarts. Time while Rynhult was down is not
                # proof that a node was down too: a new observation starts its own threshold.
                if not online and node.offline_since is None:
                    transition(db, node, False)
                elif online and node.offline_since is not None:
                    transition(db, node, True)
                    self._host_backoff.pop(node.name, None)
                evaluate(db, node)
            db.commit()

    async def pause_idle_workstations(self) -> None:
        with self.db() as db:
            nodes = {n.name: n for n in db.scalars(select(Node).where(
                Node.status == 'active', Node.role == 'workstation', Node.idle_sleep_minutes.is_not(None)))}
            ids = []
            for session in db.scalars(select(Session).where(Session.observed_state == 'idle', Session.desired_state == 'running')):
                if session.host not in nodes or not connected(self.broker, session.host):
                    continue
                if session.idle_since is None:
                    session.idle_since = now()  # existing idle sessions get a fresh observation window after upgrade
                if sleep_due(session, nodes[session.host], now()):
                    ids.append(session.id)
            db.commit()
        for acc_id in ids:
            try:
                await self.stop(acc_id, 'supervisor', for_sleep=True)
            except BrokerError as error:
                log.warning("sleep pause %s deferred: %s", acc_id, error)

    async def _sleep_ready(self, db: DbSession, session: Session) -> bool:
        """Fail closed: refresh the screen, output and hooks under the stop/resume lock."""
        node = db.scalar(select(Node).where(Node.name == session.host))
        if not sleep_due(session, node, now()) or not connected(self.broker, session.host):
            return False
        await self._check_prompt(db, session)
        db.commit()
        panes = await self.broker.call('list', account=session.run_as, host=session.host)
        pane = next((p for p in panes if p['tmux_name'] == session.tmux_name), None)
        if not pane or pane['dead'] or not pane.get('last_output'):
            return False
        if session.host != 'local':
            result = await self.broker.call('read_spool', account=session.run_as, host=session.host,
                                            acc_id=session.id, offset=self._mirror_size(session.id))
            data = spool_chunk(result)
            if len(data) >= REMOTE_SPOOL_CHUNK or (data and not data.endswith(b'\n')):
                return False  # catch up through normal ingestion before considering a pause
            self._append_mirror(session.id, data)
            if spool_path_for(self.settings, session.id).with_name('mirror-full').exists():
                return False
        db.refresh(session)
        db.refresh(node)
        session.last_output_at = datetime.fromtimestamp(pane['last_output'], timezone.utc)
        self._ingest(db, session)
        if (read_spool_file(spool_path(self.settings, session), session.spool_offset, 1)
                or any(isinstance(event, Event) and event.kind == 'spool_parse_error' for event in db.new)):
            db.commit()
            return False
        needs_input = attention._open(db, f'needs_input:{session.id}')
        ready = not needs_input and sleep_due(session, node, now()) and connected(self.broker, session.host)
        db.commit()
        return ready

    async def evaluate_budgets(self) -> None:
        """People's own usage budgets (opt-in): raise or clear their inbox items."""
        with self.db() as db:
            budgets.evaluate(db)
            for group in db.scalars(select(Group).where(Group.archived.is_(False))):
                group_board.maintain(db, group)
            db.commit()

    async def run(self) -> None:
        from .admin_features import CURRENT
        if features := getattr(self.settings, "features", None):
            CURRENT.set(features)
        # Ordinary stage errors leave other stages running. A capture barrier skips
        # the whole pass: every database stage would encounter the same contention.
        async def stage(name: str, work) -> None:
            try:
                await work()
            except BrokerUnavailable:
                raise
            except MaintenanceBusy:
                raise  # abandon this pass; the loop logs one expected-contention message
            except Exception as error:
                log.exception("supervisor stage %s failed", name)
                if name != "telemetry":
                    self.telemetry.capture(error, "supervisor_stage", "supervisor")

        async def sync_skills() -> None:  # every agent account's Codex skill links and plugin list
            codex_skills = [s for s, item in self.catalog.skills.items() if "codex" in item.providers]
            for account in self.broker.accounts():
                await self.broker.call("sync_codex_skills", account=account, skills=codex_skills)
                self.codex_plugins_by_account[account] = await self.broker.call("codex_plugins", account=account)
            self._skills_synced = True

        async def autostart() -> None:
            self._autostart_done = await self.autostart_projects()

        try:
            await self._loop(stage, sync_skills, autostart)
        finally:
            if self._backup_task is not None:
                self._backup_task.cancel()
                await asyncio.gather(self._backup_task, return_exceptions=True)

    async def purge_infrastructure(self) -> None:
        from .infrastructure import purge_retired
        with self.db() as db:
            purge_retired(db)
            db.commit()

    async def _loop(self, stage, sync_skills, autostart) -> None:
        last_reconcile = 0.0
        last_issue_comments = 0.0
        last_auth = -1e9
        last_github = -1e9
        last_host = -1e9
        last_providers = -1e9
        last_history = -1e9
        last_budgets = -1e9
        last_containers = -1e9
        last_infrastructure = -1e9
        last_telemetry = -1e9
        loop = asyncio.get_running_loop()
        busy_since = warned_at = None  # a capture holds writes for milliseconds; minutes of skipped passes is not that

        def skipped():
            nonlocal busy_since, warned_at
            self._wake.clear()  # consume a wake even when capture prevented reaching reconcile
            log.info("supervisor pass skipped: backup capture or maintenance is busy")
            busy_since = loop.time() if busy_since is None else busy_since
            if loop.time() - busy_since >= BUSY_WARN and (warned_at is None or loop.time() - warned_at >= BUSY_WARN):
                warned_at = loop.time()
                log.warning("supervisor passes skipped for %d s: the maintenance lock is still held (backup capture, "
                            "update or restore); nothing is ingested or reconciled until it is released",
                            loop.time() - busy_since)

        while True:
            maintenance_busy = False
            try:
                await stage("machines", self.evaluate_machines)
                if not self._autostart_done:
                    await stage("autostart", autostart)
                if not self._skills_synced:
                    await stage("skills", sync_skills)
                if self._models_stale():
                    await stage("models", self.refresh_models)
                if loop.time() - last_host >= self.settings.host_interval:
                    last_host = loop.time()
                    await stage("host", lambda: joined_thread(self.refresh_host))
                if loop.time() - last_providers >= self.settings.provider_interval:
                    last_providers = loop.time()
                    await stage("providers", self.refresh_providers)
                if loop.time() - last_github >= self.settings.github_interval and self.settings.github_token_path.exists():
                    last_github = loop.time()
                    await stage("github", self.refresh_github)
                if loop.time() - last_issue_comments >= 60:
                    last_issue_comments = loop.time()
                    await stage("issue_comments", self.refresh_issue_comments)
                if loop.time() - last_auth >= self.settings.auth_interval:
                    last_auth = loop.time()
                    await stage("auth", self.refresh_auth)
                if loop.time() - last_infrastructure >= 60:
                    last_infrastructure = loop.time()
                    await stage("infrastructure_retention", self.purge_infrastructure)
                await stage("ingest", self.ingest_pass)
                if loop.time() - last_containers >= self.settings.container_audit_interval:
                    last_containers = loop.time()
                    await stage("containers", self.ingest_container_audit)
                if loop.time() - last_budgets >= 60:
                    last_budgets = loop.time()
                    await stage("budgets", self.evaluate_budgets)
                if loop.time() - last_history >= self.settings.history_interval:
                    last_history = loop.time()
                    await stage("history", self.index_transcripts)
                if getattr(self, "scheduler", None):
                    await stage("schedules", self.scheduler.tick)
                await stage("push", self.push_attention)
                await stage("backups", self.schedule_backups)
                if self._env_pending:
                    await stage("keys", self.clear_envs)
                if loop.time() - last_reconcile >= self.settings.reconcile_interval or self._wake.is_set():
                    self._wake.clear()
                    await stage("reconcile", self.reconcile)
                    last_reconcile = loop.time()
                await stage("workstation_sleep", self.pause_idle_workstations)
            except BrokerUnavailable as error:
                if self.broker_ok:
                    log.warning("broker unavailable: %s", error)
                self.broker_ok = False
            except MaintenanceBusy:  # a check between stages (_models_stale) read the database during a capture
                maintenance_busy = True
                skipped()
            except Exception:
                log.exception("supervisor iteration failed")
            if not maintenance_busy:
                try:
                    if loop.time() - last_telemetry >= 60:
                        await stage("telemetry", self.telemetry.tick)
                        last_telemetry = loop.time()
                except MaintenanceBusy:
                    maintenance_busy = True
                    skipped()
            if not maintenance_busy:
                busy_since = warned_at = None
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.settings.ingest_interval)
            except asyncio.TimeoutError:
                pass

    # ---------------------------------------------------------------- helpers

    @staticmethod
    def _open_run(session: Session) -> SessionRun | None:
        return session.runs[-1] if session.runs and session.runs[-1].ended_at is None else None

    def _close_run(self, session: Session, end_reason: str) -> None:
        run = self._open_run(session)
        if run:
            run.ended_at = now()
            run.end_reason = run.end_reason or end_reason

    def _set_state(self, db: DbSession, session: Session, state: str, source: str) -> None:
        previous = session.observed_state
        if state != 'idle':
            session.idle_since = None
        elif previous != 'idle' or session.idle_since is None:
            session.idle_since = now()
        if previous != state:
            db.add(Event(session_id=session.id, source=source, kind="state", payload={"from": previous, "to": state}))
            session.observed_state = state
            self._attention_on_transition(db, session, previous, state)
            orchestrator.state_changed(db, session, previous, state)  # orchestrator v2 slices' listeners
            if state in ("stopped", "exited", "archived") and session.project_id is not None:
                self.backup_soon(session.project_id)  # its commits go to the backup now, not in 15 minutes
            if state in ENV_CLEARED and session.management == "acc":
                self._queue_env_cleanup(db, session)
        session.status_source = source
        session.status_at = now()

    # ---------------------------------------------------------------- keys on disk (D21)

    def _queue_env_cleanup(self, db: DbSession, session: Session) -> None:
        """The session isn't running: its session.env and secret-filled files are removed (now, or once its machine is
        reachable again). Recorded in the same transaction as its state, so a restart of accd doesn't forget it."""
        pending = db.get(Observation, ENV_CLEANUP)
        value = dict(pending.value) if pending else {}
        value[session.id] = {"account": session.run_as, "host": session.host,
                             "files": providers.get(session.provider).secret_files(session.launch_spec.files or {})}
        db.merge(Observation(key=ENV_CLEANUP, value=value, source="supervisor", observed_at=time.time()))
        self._env_pending = True

    async def clear_envs(self) -> None:
        """Supervisor stage: retry removals that couldn't happen yet (an offline machine, a broker that was down)."""
        async with self._lock:
            await self._clear_envs()

    async def _clear_envs(self, only: str | None = None) -> None:
        """Remove queued sessions' keys from disk. The caller holds self._lock, so a resume can't start in between: a
        session that runs again (its launch wrote the files afresh) is dropped from the queue untouched."""
        with self.db() as db:
            pending = dict((db.get(Observation, ENV_CLEANUP) or Observation(value={})).value)
            if not pending:
                self._env_pending = False
                return
            states = dict(db.execute(select(Session.id, Session.observed_state).where(Session.id.in_(list(pending)))).all())
            group_sessions = set(db.scalars(select(Session.id).join(Project).where(
                Session.id.in_(list(pending)), Project.group_id.is_not(None))))
        done = []
        for acc_id, entry in pending.items():
            if only is not None and acc_id != only:
                continue
            if acc_id in states and states[acc_id] not in ENV_CLEARED:
                done.append(acc_id)  # running again
                continue
            if not connected(self.broker, entry["host"]):
                continue
            try:
                await self.broker.call("clear_env", account=entry["account"], host=entry["host"], acc_id=acc_id,
                                       files=entry.get("files") or [])
            except BrokerUnavailable as error:
                log.info("keys of %s not removed yet (%s): retrying later", acc_id, error)
                continue
            except BrokerError as error:
                log.warning("keys of %s not removed: %s", acc_id, error)
                if acc_id in group_sessions:
                    continue  # group revocation is pending until removal is confirmed
            done.append(acc_id)
        with self.db() as db:
            current = db.get(Observation, ENV_CLEANUP)
            left = {k: v for k, v in (current.value if current else {}).items() if k not in done}
            if current is not None:
                current.value, current.observed_at = left, time.time()
                db.commit()
        self._env_pending = bool(left)

    @staticmethod
    def _attention_on_transition(db: DbSession, session: Session, previous: str, state: str) -> None:
        if previous == "waiting_input" and state != "waiting_input":
            attention.resolve(db, f"needs_input:{session.id}")
        if state == "recovery_needed":
            attention.raise_item(db, f"recovery:{session.id}", kind="recovery_needed", severity="critical",
                                 source="supervisor", session_id=session.id,
                                 title=f"{session.name} needs recovery", detail=session.last_error)
        if state in ("idle", "active", "waiting_input", "stopped", "archived"):
            attention.resolve(db, f"recovery:{session.id}")
        if state in ("stopped", "archived", "exited"):
            attention.resolve(db, f"needs_input:{session.id}")

    @staticmethod
    def _get(db: DbSession, acc_id: str) -> Session:
        session = db.get(Session, acc_id)
        if session is None:
            raise LaunchRefused("That session wasn't found.")
        return session
