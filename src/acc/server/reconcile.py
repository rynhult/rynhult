"""Supervisor reconcile: compare desired and observed state, resume with bounded retries, never start a new conversation.

Mixed into acc.server.supervisor.Supervisor; methods use its state (self.db, self.broker, self.settings, ...).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session as DbSession
from sqlalchemy.orm import selectinload

from .. import providers
from ..providers import copilot_settings
from ..providers.base import endpoint_of
from . import attention, containers, group_board, group_remote, risk_labels, states, vault
from .broker_client import BrokerError, BrokerUnavailable
from .compiler import CompileError, LaunchInputs, build_args, materialize, resume_skills_override, session_environment
from .ingest import read_spool_file, spool_path
from .machine_presence import connected
from .models import AuditLog, Event, ModelEndpoint, Session, SessionRun, VaultKey
from .public_errors import public_error
from .supervisor_common import log, now


def host_of(session: Session) -> str:
    """Use the frozen session placement; mutable project routing is only a legacy fallback."""
    return getattr(session, "host", None) or getattr(session, "team_host", None) or session.project.host or "local"


def settled_stop(session: Session) -> bool:
    """A stopped session _reconcile_one has nothing to do for once its pane is gone: no container grant to revoke,
    stop flags to clear, group or explanation work, idle time to reset or unconfirmed stop to forget."""
    return (session.desired_state == "stopped" and session.observed_state == "stopped" and not session.container
            and not session.team_stop_pending and not session.multirun_stop_pending and session.idle_since is None
            and session.project.group_id is None and "explanation.json" not in (session.launch_spec.files or {})
            and not (session.last_error or "").startswith("stop not confirmed:"))


def kill_params(session: Session, settings) -> dict:
    """The broker's kill for a session: its tmux session, and a container session's container too."""
    params = {"tmux_name": session.tmux_name}
    if session.container and session.container_generation:
        params.update(acc_id=session.id, container={"generation": session.container_generation,
                                                    "policy_id": settings.containers_policy})
    return params


class ReconcileMixin:
    async def reconcile(self) -> None:
        await self._recover_attempts()
        async with self._lock:
            with self.db() as db:
                # host_of() reads each session's placement and project: loaded with the sessions, not one by one.
                sessions = list(db.scalars(select(Session).where((Session.desired_state != "archived") | Session.team_stop_pending
                                                                  | Session.multirun_stop_pending | Session.last_error.startswith('stop not confirmed:'),
                                                                  Session.management == "acc").options(
                    selectinload(Session.project), selectinload(Session.placement), selectinload(Session.launch_spec))))
                ids = [session.id for session in sessions]
                keys = {session.id: (host_of(session), session.run_as) for session in sessions}
                quiet = {session.id: session.tmux_name for session in sessions if settled_stop(session)}
                held = self._replacement_held(db)
                wanted = ({(host_of(s), s.run_as) for s in sessions} | {("local", self.broker.default_account)}
                          | {(host, account) for _, host, account, _ in held})
            # Broker IO must not pin a read snapshot that later persists pane state.
            # Reload each session afterward, including concurrent edits/deletions.
            panes: dict[tuple[str, str], dict[str, dict]] = {}
            for host, account in sorted(wanted):
                if not connected(self.broker, host):
                    continue
                try:
                    panes[(host, account)] = {p["tmux_name"]: p for p in
                                              await self.broker.call("list", account=account, host=host)}
                except BrokerError as error:
                    if (host, account) == ("local", self.broker.default_account):
                        raise
                    log.warning("broker for %s on %s unavailable: %s", account, host, error)
            self.broker_ok = True
            self._report_held(held, panes)
            if quiet:
                # The settled set came from the snapshot before the broker calls, which can take seconds: a session
                # resumed or flagged meanwhile is read again (one query, still no transaction per settled session).
                with self.db() as db:
                    quiet = {s.id: s.tmux_name for s in db.scalars(select(Session).where(Session.id.in_(list(quiet))).options(
                        selectinload(Session.project), selectinload(Session.launch_spec))) if settled_stop(s)}
            from .supervisor import REMOTE_SPOOL_BUDGET, REMOTE_SPOOL_CHUNK, spool_chunk, spool_path_for
            spool_deadline = time.monotonic() + REMOTE_SPOOL_BUDGET
            for acc_id in ids:
                # No transaction for a session whose machine and account weren't listed: the reload below would skip it
                # too. Thousands of stopped sessions otherwise cost a transaction each, every pass (rc performance check).
                if acc_id in self._launching or keys[acc_id] not in panes:
                    continue
                if acc_id in quiet and panes[keys[acc_id]].get(quiet[acc_id]) is None:
                    continue  # stopped, and its pane is gone: _reconcile_one would change nothing
                with self.db() as db:
                    session = db.get(Session, acc_id)
                    if session is None or session.management != "acc":
                        continue
                    key = (host_of(session), session.run_as)
                    if key not in panes:
                        continue
                    try:
                        pane = panes[key].get(session.tmux_name)
                        pending = False
                        run = self._open_run(session)
                        if key[0] != 'local' and session.desired_state == 'running' and run and (not pane or pane['dead']):
                            run_id = run.id
                            params = dict(account=session.run_as, host=key[0], acc_id=acc_id,
                                          offset=self._mirror_size(acc_id))
                            db.rollback()  # no read snapshot while fetching/redacting the last remote hooks
                            remaining = spool_deadline - time.monotonic()
                            if remaining <= 0:
                                continue
                            result = await asyncio.wait_for(self.broker.call('read_spool', **params), remaining)
                            chunk = spool_chunk(result)
                            self._append_mirror(acc_id, chunk)
                            pending = (len(chunk) == REMOTE_SPOOL_CHUNK and
                                       not spool_path_for(self.settings, acc_id).with_name('mirror-full').exists())
                            db.refresh(session)
                            fresh = self._open_run(session)
                            if host_of(session) != key[0] or fresh is None or fresh.id != run_id:
                                continue  # concurrent stop/resume/routing change must be reconsidered next pass
                        await self._reconcile_one(db, session, pane, spool_pending=pending)
                        db.commit()
                    except BrokerUnavailable:
                        if key[0] == "local":
                            raise
                        db.rollback()
                    except Exception:
                        db.rollback()
                        log.exception("reconcile failed for session %s", acc_id)

    def _replacement_held(self, db: DbSession) -> list[tuple[str, str, str, str]]:
        """Restored remote sessions an activated replacement holds: (id, host, account, tmux name)."""
        from .models import Setting
        row = db.get(Setting, "backup.replacement")
        if not row:
            return []
        held = db.scalars(select(Session).where(Session.id.in_(row.value.get("sessions", [])),
                                                Session.desired_state == "archived", Session.management == "acc"))
        return [(s.id, host_of(s), s.run_as, s.tmux_name) for s in held if host_of(s) != "local"]

    def _report_held(self, held, panes) -> None:
        """A pane the old main left running stays as it is (no adoption, no kill, no input); owners are told."""
        with self.db() as db:
            for acc_id, host, account, tmux_name in held:
                if (host, account) not in panes:
                    continue  # machine not connected: nothing observed
                key = f"replacement_held:{acc_id}"
                pane = panes[(host, account)].get(tmux_name)
                if pane and not pane["dead"]:
                    if attention._open(db, key) is None:
                        db.add(AuditLog(actor="supervisor", action="session.replacement_held_running", target=acc_id,
                                        detail={"node": host}))
                    attention.raise_item(
                        db, key, kind="replacement_held", source="backup", severity="warn", session_id=acc_id, owner="",
                        title=f"A restored session is still running on {host}",
                        detail="It was still running when Rynhult was restored from backup, so Rynhult holds it: it gets "
                               "no input and its terminal is read-only. Check it there, then stop it on that machine, or "
                               "use Resume if it was approved during recovery.")
                else:
                    attention.resolve(db, key)
            db.commit()

    async def _recover_attempts(self) -> None:
        """Orchestrator attempts a crash or lost reply left launching: adopt or fail them (review C-L3)."""
        import time
        if time.monotonic() - self.__dict__.get('_attempts_checked', -1e9) < 30:
            return
        self._attempts_checked = time.monotonic()
        from .orchestrator import assignments
        if not assignments.enabled():
            return
        try:
            await assignments.recover_launching(self)
        except Exception:
            log.exception('orchestrator: attempt recovery failed; retrying on a later pass')

    async def _reconcile_one(self, db: DbSession, session: Session, pane: dict | None, *, spool_pending=False) -> None:
        if session.project is None:
            return  # detached, archived history has no workspace to reconcile
        from .models import Observation
        reply = db.get(Observation, 'request.reply.' + session.id)
        sent = self.__dict__.setdefault('_request_replies_sent', {})
        if reply and sent.get(session.id) != reply.observed_at:
            import json
            try:
                from ..request_replies import publish
                if host_of(session) == 'local':
                    publish(self.settings.request_replies_root, session.id, reply.value)
                    sent[session.id] = reply.observed_at
                else:
                    link = self.broker.hub.links.get(host_of(session)) if self.broker.hub else None
                    if link and 'request-replies-v1' in link.info.get('capabilities', []):
                        # Retry remote delivery until a later reconcile: queueing
                        # a frame is not confirmation that the daemon persisted it.
                        link.queue(json.dumps({'t': 'request-replies', 'session_id': session.id, 'replies': reply.value}))
            except (BrokerError, OSError, ValueError):
                pass  # stay unconfirmed; retry after store repair/reconnect
        # Reconcile holds the supervisor lock. Never wait on a folder lock here:
        # delete owns that folder lock before awaiting supervisor.stop().
        path = session.workdir or session.project.workspace_path
        async with self.workspace_operations.hold(path, host_of(session), wait=False, kind="launch") as held:
            if not held:
                return
            await self._reconcile_workspace(db, session, pane, spool_pending=spool_pending)

    async def _reconcile_workspace(self, db: DbSession, session: Session, pane: dict | None, *, spool_pending=False) -> None:
        if "explanation.json" in session.launch_spec.files:
            await self._reconcile_explanation(db, session, pane)
            return
        if session.desired_state == 'running' and ((session.project.group_id and not group_board.authorized(db, session, self.settings.groups_remote_enabled)) or group_remote.personal_conflict(db, session)):
            session.desired_state = 'stopped'
            self._set_state(db, session, 'stopped', 'supervisor')
        run = self._open_run(session)
        if pane and pane.get("last_output"):
            session.last_output_at = datetime.fromtimestamp(pane["last_output"], timezone.utc)

        if session.desired_state != "running":
            if pane and not pane["dead"]:
                await self.broker.call("kill", account=session.run_as, host=host_of(session),
                                       **kill_params(session, self.settings))
            if session.container:
                containers.revoke_grant(self.settings, session.id)
            if session.team_stop_pending:
                session.team_stop_pending = False
            if session.multirun_stop_pending:
                session.multirun_stop_pending = False
            if (session.last_error or "").startswith("stop not confirmed:"):
                session.last_error = None
            return

        if run is not None and (run.verified_at is None or not pane or pane['dead']):
            # Team helpers can start after ingest; final hooks can arrive after it too. Consume one bounded
            # chunk while its run is still open, before either startup verification or exit closure. If more
            # backlog is known, commit this chunk and reconsider next pass rather than invent a missing hook.
            before = session.spool_offset
            self._ingest(db, session)
            if spool_pending or (session.spool_offset > before and
                                 read_spool_file(spool_path(self.settings, session), session.spool_offset, 1)):
                return

        if pane and not pane["dead"]:
            if run is None:  # alive but untracked, e.g. accd restarted: adopt it
                run = SessionRun(session=session, reason="adopted", pid=pane.get("pid"), verified_at=now())
                db.add(run)
            await self._check_started(db, session, run, pane)
            working = await self._check_prompt(db, session)
            await self._check_changes(db, session)
            if not working:
                self._check_quiet(db, session)
            await self._check_automation(db, session)
            if session.project.group_id:
                from sqlalchemy import delete

                from .models import Group, GroupClaim, GroupWork
                work = db.get(GroupWork, session.id)
                if work:
                    work.heartbeat = now()
                    if session.observed_state in ('idle', 'stopped', 'exited', 'archived'):
                        db.execute(delete(GroupClaim).where(GroupClaim.session_id == session.id))
                    else:
                        for claim in db.scalars(select(GroupClaim).where(GroupClaim.session_id == session.id, GroupClaim.expires_at > now())):
                            claim.expires_at = now() + group_board.LEASE
                    group = db.get(Group, session.project.group_id)
                    group_board.maintain(db, group)
                    content = group_board.context(db, group, session.id, self.settings.groups_remote_enabled)
                    from .models import GroupCheckout, Observation, Placement
                    observation = db.get(Observation, 'group.paths.' + session.id)
                    checkout = db.get(GroupCheckout, work.checkout_id)
                    clone = db.get(Placement, checkout.placement_id) if checkout else None
                    scan = bool(clone and (not observation or now().timestamp() - observation.observed_at > 30))
                    scan_path, base_sha = clone.path if clone else None, work.base_sha
                    db.commit()
                    if scan:
                        try:
                            observed = await self.broker.call('group_observe', account=session.run_as, host=session.host,
                                path=scan_path, session_id=session.id, base=base_sha)
                            redactor = group_board.shared_redactor(self.vault, db, session.vault_keys or [])
                            observed['paths'] = [path for path in observed.get('paths', []) if isinstance(path, str) and redactor.text(path) == path]
                            group_board.observe_work(db, session, observed, self.settings.groups_remote_enabled)
                            db.commit()
                        except BrokerError:
                            pass  # last observation keeps its age; absence never proves no conflict
                    await self.broker.call('group_context', account=session.run_as, host=session.host,
                                           session_id=session.id, content=content)
            return

        if pane is not None and pane["exit_status"] is None and pane["exit_signal"] is None:
            # tmux marks a pane dead on PTY EOF, which can precede reaping the child. Keep the
            # run open until SIGCHLD supplies its result; replacing the pane now loses it forever.
            return

        if run is not None:  # the process we were tracking has ended
            if pane is not None:
                run.exit_status, run.exit_signal = pane["exit_status"], pane["exit_signal"]
                ended = "exited"
            else:
                ended = "missing"
            run.ended_at = now()
            db.add(Event(session_id=session.id, source="tmux", kind=f"process_{ended}",
                         payload={"exit_status": run.exit_status, "exit_signal": run.exit_signal,
                                  "end_reason": run.end_reason, "run": run.id}))
            if pane is not None and states.is_user_exit(run.exit_status, run.exit_signal):
                session.desired_state = "stopped"
                self._set_state(db, session, "exited", "tmux")
                db.add(AuditLog(actor="supervisor", action="session.user_exit", target=session.id,
                                detail={"end_reason": run.end_reason}))
                return
            if run.verified_at is None and run.reason != "launch":
                session.last_error = f"resume attempt ended ({ended}, status {run.exit_status})"
            if run.exit_signal or run.exit_status not in (None, 0):
                self.telemetry.capture(RuntimeError('Agent process exited abnormally'), 'session_crash', 'session',
                                       session.provider, -(run.exit_signal or 0) if run.exit_signal else run.exit_status, db=db)
            self._schedule_retry(db, session)

        if session.observed_state == "recovery_needed":
            return
        if session.next_attempt_at and now() < session.next_attempt_at:
            self._set_state(db, session, "recovering", "supervisor")
            return
        await self._attempt_resume(db, session, pane)

    async def _check_automation(self, db, session):
        from .admin_features import enabled
        if not enabled(self.settings, 'automation'):
            return
        from sqlalchemy import text

        from . import automation
        if not session.automation:
            return
        params = dict(account=session.run_as, host=session.host,
                      tmux_name=session.tmux_name, provider=session.provider)
        db.commit()  # resolve lazy placement/routing reads before ending the snapshot
        screen = await self.broker.call("automation_screen", **params)
        if not enabled(self.settings, 'automation'):
            return
        signal = screen.get("limit") or session.automation.get("hook_limit")
        if screen.get("prompt") is not None:
            self._set_state(db, session, "waiting_input", "screen")
        elif signal is not None and session.observed_state != "waiting_input" and not (
                session.automation.get("limit_seen") and (session.automation.get("dispatch") or {}).get("status") == "sent"):
            self._set_state(db, session, "idle", "limit")
        last = db.execute(text("SELECT text, seq FROM transcript_fts WHERE session_id=:id AND role='assistant' ORDER BY seq DESC LIMIT 1"),
                          {"id": session.id}).first()
        turn = str(last[1]) if last else "empty"
        assistant = last[0] if last else ""
        marker = session.automation.get("assistant_marker")
        if marker and last and marker["seq"] == last[1]:
            assistant = marker["text"]
        if not session.indexed_at or (session.status_at and session.indexed_at < session.status_at):
            # Still enforce caps and permission stops while history is unavailable, but never
            # continue a goal before its latest assistant completion marker has been read.
            assistant, turn = "", None
        command = automation.decide(db, session, signal, assistant, turn,
                                    usage_windows=None if session.run_as == self.broker.default_account else [])
        if command:
            state = dict(session.automation)
            state.pop("hook_limit", None)
            session.automation = state
        db.commit()  # durable claim before side effect: ambiguous delivery is never retried
        if command:
            try:
                await self.broker.call("send_text", account=session.run_as, host=session.host,
                                       tmux_name=session.tmux_name, text=command['text'], provider=session.provider)
            except BrokerError:
                session.automation = {**session.automation, "dispatch": {**session.automation['dispatch'],
                                      "status": "delivery uncertain; inspect the terminal"}}
                automation.cancel(db, session, 'delivery uncertain')
            else:
                session.automation = {**session.automation, "dispatch": {**session.automation['dispatch'], "status": "sent"}}
                self._set_state(db, session, "active", "automation")
            db.commit()

    async def _check_started(self, db: DbSession, session: Session, run: SessionRun, pane: dict) -> None:
        age = (now() - run.started_at).total_seconds()
        provider = providers.get(session.provider)
        if run.verified_at is None:
            if provider.start_verify == "settle" and age >= self.settings.codex_settle_seconds:
                if run.reason == "resume":
                    self._note_recovery(db, session, run)
                run.verified_at = now()  # e.g. Codex confirms by hook only on the next prompt (F2)
                session.recovery_attempts = 0
                session.next_attempt_at = None
                # It reports nothing until the next prompt (F2); a just-started TUI is waiting for input.
                self._set_state(db, session, "idle", "tmux")
            elif provider.start_verify == "hook" and age >= self.settings.claude_verify_timeout:
                run.verified_at = now()
                session.last_error = "The agent didn't report starting in time, so its status now comes from its terminal only."
                # A CLI that never reported starting is most often waiting at a screen of its own (a first-run or
                # mode dialog): say so instead of "working" (seen overnight 2026-10-22: hours lost at such a dialog).
                attention.raise_item(db, f"needs_input:{session.id}", kind="needs_input", source="tmux",
                                     session_id=session.id, title=f"{session.name} hasn't started",
                                     detail="It didn't report starting. Open its terminal: it may be waiting at a prompt.")
                self._set_state(db, session, "waiting_input", "tmux")
        if "remote_control" in provider.supports and session.launch_spec.inputs.get("remote_control") and not session.remote_url:
            params = dict(account=session.run_as, host=host_of(session), pid=pane["pid"])
            db.commit()  # flush pane/startup state before a concurrent API write invalidates this snapshot
            remote = await self.broker.call("claude_remote", **params)
            if remote.get("bridge_session_id"):
                session.remote_url = f"https://claude.ai/code/{remote['bridge_session_id']}"

    async def _check_prompt(self, db: DbSession, session: Session) -> bool:
        """A provider with no permission hook: its approval prompt on screen means "needs you" (status from the screen);
        once the prompt is gone, it is working again. Claude also exposes background work after its Stop hook.
        A terminal turn error means idle with its error as activity."""
        provider = providers.get(session.provider)
        if (not provider.prompt_patterns and provider.id != "claude"
                or session.observed_state not in ("active", "idle", "waiting_input")):
            return False
        if session.observed_state == "waiting_input" and session.status_source != "screen":
            return False  # a hook said so; a hook will say when it's answered
        db.commit()  # never hold the write lock while waiting for the broker (see _start)
        screen = await self.broker.call("screen_prompt", account=session.run_as, host=host_of(session), tmux_name=session.tmux_name,
                                         provider=session.provider)
        prompt = screen["prompt"]
        if prompt:
            prompt = self.vault.redactor(db, session.vault_keys).text(prompt)
        if prompt is not None and (session.observed_state != "waiting_input"
                                   or (not provider.screen_prompt_command and session.activity != prompt[:200])):
            prompt = prompt or "a command"
            attention.raise_item(db, f"needs_input:{session.id}", kind="needs_input", source="screen",
                                 session_id=session.id, title=f"{session.name} needs your input", detail=prompt[:500],
                                 risk=dict(risk_labels.DEFAULT))
            session.activity, session.activity_at = (f"Asking to run {prompt}" if provider.screen_prompt_command else prompt)[:200], now()
            self._set_state(db, session, "waiting_input", "screen")
        elif prompt is None and session.observed_state == "waiting_input":
            if not provider.screen_prompt_command:
                session.activity, session.activity_at = None, now()
            if session.activity and session.activity.startswith("Asking to run "):  # approved: the command runs now
                session.activity, session.activity_at = f"Running {session.activity[14:]}"[:200], now()
            self._set_state(db, session, "active", "screen")

        working = prompt is None and screen.get("working", False)
        if working and session.observed_state == "idle":
            if provider.id == "claude":
                session.activity, session.activity_at = "Working in background", now()
            self._set_state(db, session, "active", "screen")
        elif (not working and provider.id == "claude"
              and session.observed_state == "active" and session.status_source == "screen"):
            # Only undo our own background state; foreground work and input waits still belong to hooks.
            session.activity, session.activity_at = None, now()
            self._set_state(db, session, "idle", "screen")
        if prompt is None and not working and (error := screen.get("idle")) is not None:
            error = self.vault.redactor(db, session.vault_keys).text(error)[:200]
            if session.activity != error:
                session.activity, session.activity_at = error, now()
            if session.observed_state != "idle":
                self._set_state(db, session, "idle", "screen")
        return working

    async def _check_changes(self, db: DbSession, session: Session) -> None:
        """Refresh the summary of what changed in its workspace: every changes_interval while it works, less often
        while idle."""
        if session.management == "external":
            return
        every = self.settings.changes_interval * (1 if session.observed_state in ("active", "waiting_input") else 6)
        checked = self.__dict__.setdefault("_changes_checked", {})  # in memory: no database write per check
        last = checked.get(session.id)
        fresh_activity = last is not None and session.activity_at is not None and session.activity_at > last[1]
        if last is not None and time.monotonic() - last[0] < every and not fresh_activity:
            return  # nothing new since the last look (a tool event, e.g. an edit, makes it look again)
        checked[session.id] = (time.monotonic(), now())
        params = dict(account=session.run_as, host=host_of(session), path=session.cwd, base=session.base_commit)
        db.commit()  # lazy cwd/placement reads must finish before the broker await
        try:
            summary = await self.broker.call("workspace_changes", **params)
        except BrokerUnavailable:
            raise
        except BrokerError:
            return  # e.g. the folder is gone; the next check tries again
        summary = self.vault.redactor(db, session.vault_keys).deep(summary)
        if summary != session.changes:  # written only when something changed
            session.changes, session.changes_at = summary, now()

    def _check_quiet(self, db: DbSession, session: Session) -> None:
        """A provider that may end a turn without a hook: a working session whose screen is still for long enough has
        stopped (status from tmux)."""
        quiet = providers.get(session.provider).quiet_idle_seconds
        quiet = self.settings.quiet_idle_override or quiet
        if not quiet or session.observed_state != "active" or session.last_output_at is None:
            return
        since = now() - max(session.last_output_at, session.status_at or session.last_output_at)
        if since.total_seconds() >= quiet:
            self._set_state(db, session, "idle", "tmux")

    def _schedule_retry(self, db: DbSession, session: Session) -> None:
        delay = states.retry_delay(session.recovery_attempts, self.settings.retry_backoff)
        if delay is None:
            self._recovery_needed(db, session, session.last_error or "Rynhult tried to resume the session several times and gave up. Resume it to try again.")
            return
        session.next_attempt_at = now() + timedelta(seconds=delay)
        self._set_state(db, session, "recovering", "supervisor")

    async def _attempt_resume(self, db: DbSession, session: Session, pane: dict | None) -> None:
        from .admin_features import enabled
        if not enabled(self.settings, 'automation'):
            return
        if session.recovery_attempts >= len(self.settings.retry_backoff):
            self._recovery_needed(db, session, session.last_error or "Rynhult tried to resume the session several times and gave up. Resume it to try again.")
            return
        if not session.native_id:
            self._recovery_needed(db, session, "The agent never reported its conversation ID, so there's nothing to resume. Start a new session.")
            return
        found = await self.broker.call("transcript_exists", account=session.run_as, host=host_of(session), provider=session.provider,
                                       native_id=session.native_id)
        if not enabled(self.settings, 'automation'):
            return
        if not found["exists"]:
            self._recovery_needed(db, session, f"The conversation {session.native_id} wasn't found on the machine, so Rynhult didn't start a new one. Start a new session to continue.")
            return
        if pane is not None:
            await self.broker.call("kill", account=session.run_as, host=host_of(session),
                                   **kill_params(session, self.settings))  # clear the dead pane (and its container)
        session.recovery_attempts += 1
        await self._start(db, session, reason="resume")

    @staticmethod
    def _note_recovery(db: DbSession, session: Session, run: SessionRun) -> None:
        previous = next((r for r in reversed(session.runs) if r is not run and r.ended_at), None)
        cause = "its terminal closed (the machine restarted or it was stopped outside Rynhult)"
        if previous and previous.exit_signal:
            cause = f"it was killed (signal {previous.exit_signal})"
        elif previous and previous.exit_status not in (None, 0):
            cause = f"it exited with an error (status {previous.exit_status})"
        attention.raise_item(db, f"crash:{session.id}:{run.id}", kind="crash_recovered", severity="info",
                             source="supervisor", session_id=session.id,
                             title=f"{session.name} was resumed automatically",
                             detail=f"It stopped because {cause}. Rynhult restarted it in the same conversation.")

    def _recovery_needed(self, db: DbSession, session: Session, reason: str) -> None:
        session.last_error = reason
        session.next_attempt_at = None
        if session.observed_state != "recovery_needed":
            self._set_state(db, session, "recovery_needed", "supervisor")
            db.add(AuditLog(actor="supervisor", action="session.recovery_needed", target=session.id, detail={"reason": reason}))

    def _copilot_hooks_refused(self, db: DbSession, session: Session, reason: str, problem: str) -> None:
        """The broker refused Copilot because the folder's repository settings would switch its hooks off. Retrying
        can't help until someone changes the file, so the session waits for its person (Inbox item) at once."""
        # Never started: there is no conversation to resume, so a new session is the way back.
        message = copilot_settings.refusal(problem, "resume the session" if session.runs else "start a new session")
        db.add(Event(session_id=session.id, source="supervisor", kind="launch_failed", payload={"error": message}))
        db.add(AuditLog(actor="supervisor", action="session.copilot_hooks_refused", target=session.id,
                        detail={"reason": reason, "host": host_of(session), "account": session.run_as,
                                "cwd": session.cwd, "problem": problem}))
        self._recovery_needed(db, session, message)

    async def _start(self, db: DbSession, session: Session, reason: str) -> None:
        resume = reason != "launch"
        host, account = host_of(session), session.run_as
        home = str(self.settings.account_home(account))
        if resume and host != "local":
            try:
                home = (await self.layout(host, account))["home"]
            except Exception as error:  # unreachable (the launch below says so) or an older node
                log.info("layout of %s on %s: %s", account, host, error)
        # Gather broker facts before the final authority refresh. Never hold a
        # SQLite write transaction across these read-only broker waits.
        from . import image_capabilities
        image_inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
        from types import SimpleNamespace
        # Snapshot ORM routing/scope before commit. Lazy reads in the image helper
        # would otherwise reacquire this POST's write lock before its nested DB read.
        image_session = SimpleNamespace(host=session.host, run_as=session.run_as,
                                        container=image_capabilities.runtime_scope(session) == 'container')
        remote_path = (session.workdir or session.project.workspace_path) if session.team_id == session.id else None
        db.commit()
        has_remote = None
        if remote_path is not None:
            try:
                facts = await self.broker.call('git_refs', account=account, host=host, path=remote_path)
                has_remote = facts.get('has_remote') if type(facts.get('has_remote')) is bool else None
            except Exception:
                pass  # Unknown is never permission to publish.
        image_model = await image_capabilities.live_model(self, image_session, image_inputs)
        # A PostgreSQL repeatable-read snapshot can retain a revoked grant even when
        # its session write commits successfully. Drop it AND cached ORM authority.
        # No broker await may occur between this refresh and the pre-launch commit.
        db.commit()  # Drop any lazily loaded placement snapshot before authority is re-read.
        db.expire_all()
        if session.desired_state != "running":
            return
        if (session.project.group_id and not group_board.authorized(db, session, self.settings.groups_remote_enabled)) or group_remote.personal_conflict(db, session):
            session.desired_state = 'stopped'
            self._set_state(db, session, 'stopped', 'supervisor')
            return
        if (host_of(session), session.run_as) != (host, account):
            raise CompileError("The session's machine or account changed while it was starting. Resume it to try again.")
        if session.project.group_id and session.host != 'local':
            source = group_remote.source_placement(db, session)
            if not source or not group_remote.connected(self.broker, db, source):
                return
        inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
        run_dir = str(self.settings.session_run_dir(session))
        self.__dict__.get('_request_replies_sent', {}).pop(session.id, None)
        # The vault's keys: given to the project, usable by the person who launched it, and (on another machine) only
        # those allowed there. Read at every start, so a key given or taken since applies at the next resume.
        files, catalog, delivery, refused = session.launch_spec.files, self.catalog, None, None
        from . import agent_contract
        from .orchestrator.scope_notices import launch_scope
        scope_version, _ = launch_scope(db, session)
        if remote_path != ((session.workdir or session.project.workspace_path) if session.team_id == session.id else None):
            has_remote = None  # Facts from an earlier workspace do not describe this launch.
        files = agent_contract.apply(files, inputs, agent_contract.for_session(db, session, inputs, catalog,
            publish_zones=self.settings.publish_zones or ((self.settings.wildcard_zone,) if self.settings.wildcard_zone else ()), has_remote=has_remote))
        try:
            delivery = self.vault.deliver(db, session.project_id, session.owner, host_of(session) != "local",
                                          self.catalog, inputs.mcps)
            files = providers.get(inputs.provider).add_context(files, vault.context(delivery.plan))
            catalog = delivery.catalog
        except vault.VaultError as error:
            refused = error
        if session.project.group_id:
            from .models import Group
            files = providers.get(inputs.provider).add_context(files, group_board.context(db, db.get(Group, session.project.group_id), session.id,
                self.settings.groups_remote_enabled))
        if ((inputs.model, inputs.provider, inputs.endpoint) != (image_inputs.model, image_inputs.provider, image_inputs.endpoint)
                or image_capabilities.runtime_scope(session) != image_capabilities.runtime_scope(image_session)):
            image_model = {'image_input': None}
        files = image_capabilities.context_files(files, inputs.provider, image_model)
        used = delivery.plan if delivery else None
        # The audit line names shared keys; private ones by id only (their names are their owner's alone)
        used_ids, used_names = (used.ids, [k.name if k.shared else k.id for k in used.keys]) if used else ([], [])
        held_names = [k.name if k.shared else k.id for k in used.held_back] if used else []
        extra = resume_skills_override(inputs, files, self.catalog, home, self.codex_plugins(account)) if resume else []
        args = build_args(inputs, run_dir=run_dir, tmux_name=session.tmux_name, native_id=session.native_id, resume=resume,
                          files=files, extra_overrides=extra)
        launch = {"acc_id": session.id, "tmux_name": session.tmux_name, "provider": session.provider, "args": args,
                  "cwd": session.cwd, "files": files}
        if session.run_dir and session.run_as != "root":
            launch["run_dir"] = run_dir  # the broker refuses if its own folder for the session is elsewhere (Q4)
        if session.container:
            # A new generation per launch, and accd's grant for it: the root helper launches only what this says.
            if not self.settings.containers_enabled:
                refused = refused or CompileError("This session only runs in a container, and containers are turned off on this Rynhult. "
                                                  "An owner can turn containers back on, or switch the project off containers.")
            if not self.settings.containers_attach_qualified:
                # Every launch, not only new sessions: resumes, restarts and retries would run an agent nobody sees.
                from .api.sessions import CONTAINER_ATTACH_UNQUALIFIED
                refused = refused or CompileError(CONTAINER_ATTACH_UNQUALIFIED)
            session.container_generation = (session.container_generation or 0) + 1
            launch["container"] = {"generation": session.container_generation,
                                   "policy_id": self.settings.containers_policy}
            container_limits, _ = containers.limits_for(db, account, self.settings.default_account)
        endpoint_used = endpoint_of(files)
        from .admin_features import enabled
        if endpoint_used and not enabled(self.settings, 'local_models'):
            raise CompileError('Local models is turned off in Admin features.')
        endpoint = db.get(ModelEndpoint, endpoint_used["id"]) if endpoint_used else None  # read now: no transaction across the broker
        endpoint_key, endpoint_gone = (endpoint.api_key if endpoint else None), bool(endpoint_used and endpoint is None)
        # Never hold the database's write lock while waiting for the broker: database calls block the event loop, so
        # another writer waiting for the lock would stop the broker's reply from being handled (a 15 s stall).
        db.commit()
        try:
            if refused:
                raise refused
            if endpoint_gone:
                raise CompileError(f"The model connection {endpoint_used['name']} was removed. Add it back with the same ID under Providers → Model connections to resume.")
            environment = session_environment(inputs, catalog, run_dir, files, endpoint_key, native_id=session.native_id)
            delivery.check_env(environment)
            materialized = materialize(launch["files"], catalog, inputs.provider)
            if session.container:
                # Final synchronous barrier: queued/restored sessions, resumes and retries all pass here.
                # Existing live panes never come through here. No await between this check and dispatch.
                from .. import container_host_record
                qualified = container_host_record.check(self.settings.containers_record)
                if not qualified["passed"] or host_of(session) != "local":
                    db.add(AuditLog(actor=session.owner or "supervisor", action="container.launch_refused",
                                    target=session.id, detail={"reason": "host_not_qualified", "provider": session.provider}))
                    db.commit()
                    raise CompileError(f"container launch requires a qualified local host: {qualified['reason']}")
                try:
                    containers.write_grant(self.settings, session, launch["container"]["generation"], args,
                                           materialized, container_limits)
                except (OSError, ValueError) as error:
                    raise CompileError(f"Rynhult couldn't prepare the session's container ({error}).") from None
            from .security_settings import Conflict, check_generation
            try:
                check_generation(db, session)
            except Conflict as error:
                raise CompileError(str(error)) from None
            db.commit()
            result = await self.broker.call("launch", account=account, host=host_of(session),
                                            **{**launch, "files": materialized}, env={**environment, **delivery.env})
        except (BrokerError, CompileError, vault.VaultError) as error:
            problem = copilot_settings.refused_problem(str(error)) if isinstance(error, BrokerError) else None
            if problem:
                self._copilot_hooks_refused(db, session, reason, problem)
                return
            self.telemetry.capture(error, 'session_crash', 'session', session.provider, db=db)
            session.last_error = f"Couldn't start the session: {public_error(error)}"
            db.add(Event(session_id=session.id, source="supervisor", kind="launch_failed", payload={"error": public_error(error)}))
            self._schedule_retry(db, session)
            return
        db.expire_all()
        group_refused = False
        if session.project.group_id:
            # A grant can be revoked while the broker is dispatching. Keep IDs for
            # redaction even when that launch must be stopped and its env removed.
            session.vault_keys = sorted(set(session.vault_keys or []) | set(used_ids))
            try:
                current_keys = vault.plan(db, session.project_id, session.owner, host_of(session) != "local", self.catalog, inputs.mcps).ids
                group_refused = not set(used_ids) <= set(current_keys)
            except vault.VaultError:
                group_refused = True
        if session.project.group_id and (session.desired_state != 'running' or group_refused or not group_board.authorized(db, session, self.settings.groups_remote_enabled)):

            session.desired_state = 'stopped'
            self._set_state(db, session, 'stopped', 'supervisor')
            db.commit()
            await self.broker.call('kill', account=account, host=host, **kill_params(session, self.settings))
            await self.broker.call('clear_env', account=account, host=host, acc_id=session.id)
            return
        if used_ids:
            session.vault_keys = sorted(set(session.vault_keys or []) | set(used_ids))  # ids only: what to redact
            for key in db.scalars(select(VaultKey).where(VaultKey.id.in_(used_ids))):
                key.last_used_at = now()
        if session.project.group_id and used_ids:
            from .models import GroupKeyGrant
            consented = list(db.scalars(select(GroupKeyGrant.key_id).where(GroupKeyGrant.group_id == session.project.group_id,
                GroupKeyGrant.key_id.in_(used_ids))))
            db.add(AuditLog(actor=session.owner, action='group.key_use', target=session.project.group_id,
                detail={'project_id': session.project_id, 'session_id': session.id, 'key_ids': sorted(set(consented))}))
        if used_names or held_names:
            db.add(AuditLog(actor=session.owner or "supervisor", action="vault.use", target=session.id,
                            detail={"reason": reason, "keys": used_names, "held_back": held_names,
                                    "mcp": sorted(used.mcp_refs)}))
        session.remote_url = None  # re-read from the new process; M1 showed it is usually unchanged
        if session.base_commit is None and result.get("head"):
            session.base_commit = result["head"]  # retained for older node brokers
        started_run = SessionRun(session=session, reason=reason, pid=result.get("pid"))
        db.add(started_run)
        db.flush()
        db.add(Event(session_id=session.id, source="supervisor", kind=f"run_{reason}",
                     payload={"command": result.get("command"), "pid": result.get("pid"),
                              **({"scope_run_id": started_run.id, "scope_version": scope_version} if scope_version is not None else {})}))
        self._set_state(db, session, "starting" if reason == "launch" else "recovering", "supervisor")
