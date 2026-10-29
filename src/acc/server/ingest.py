"""Supervisor ingestion: hook spool files, agent requests, statusline and Remote Control links.

Mixed into acc.server.supervisor.Supervisor; methods use its state (self.db, self.broker, self.settings, ...).
"""
from __future__ import annotations

import json
import os
import re
import stat
import time
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import event as sa_event
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session as DbSession

from .. import providers
from . import attention, risk_labels, states
from .models import Event, Observation, Placement, Project, Session
from .supervisor_common import _trim, log, now

SPOOL_CHUNK = 1 << 20  # at most 1 MiB of a session's events per pass
SMALL_FILE = 64 << 10  # remote.json, statusline.json
FULL_PASS_SECONDS = 600  # however unchanged their files look, every session is read again this often
NATIVE_ID = re.compile(r"[0-9A-Fa-f][0-9A-Fa-f-]{7,63}|ses_[0-9A-Za-z]{10,60}")  # UUIDs; OpenCode's ses_…


TRUSTED_ROOTS: tuple[Path, ...] = ()  # set by the supervisor: run roots (root-owned) and accd's own data folder


def _open_beneath(path: Path) -> int:
    """Open `path` without following a symlink anywhere below the trusted root it lives in. The agent account owns
    its session folders and could replace one with a link (e.g. into another account's run folder, which accd's group
    can read); following only the last component's O_NOFOLLOW would not stop that."""
    root = next((r for r in TRUSTED_ROOTS if path == r or r in path.parents), None)
    if root is None:  # not under a known root (tests, odd configs): the leaf still must not be a link
        return os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    parts = path.relative_to(root).parts
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    finally:
        os.close(fd)


def read_spool_file(path: Path, offset: int = 0, limit: int = SMALL_FILE) -> bytes | None:
    """Read part of a file in a session's run folder, which the agent user controls: never follow a link (at any
    level below the run root), never block on a FIFO or device, read regular files only and at most `limit` bytes.
    None when it isn't there."""
    try:
        fd = _open_beneath(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:  # a symlink (ELOOP) or anything else unreadable
        log.warning("ignoring %s: not a plain file", path)
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            log.warning("ignoring %s: not a regular file", path)
            return None
        return os.pread(fd, limit, offset)
    finally:
        os.close(fd)


def _text(value, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def spool_path(settings, session: Session) -> Path:
    """Where a session's hook events are read from: its run folder, or (on another host) the primary's mirror of it."""
    if (session.host) != "local":
        return settings.data_dir / "remote-spool" / session.id / "events.jsonl"
    return settings.session_run_dir(session) / "events.jsonl"


def _file_mark(path: Path) -> tuple[int, int, int, int] | None:
    """A file's size, modification and change times and inode, without following a link; None when it isn't there.
    The inode and change time catch a file replaced by a rename that keeps its size and lands in the same timestamp
    tick, or copies the old modification time (independent review of the rc performance change)."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    return info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino


class IngestMixin:
    def ingest_marks(self) -> dict[str, tuple]:
        """Per non-archived session, everything a pass would read: its spool offset and the size and time of its
        event spool, statusline and (external sessions) remote.json. One query and a few lstat calls, no transaction
        per session. The host follows Session.host: placement, team host, project host, else local."""
        with self.db() as db:
            rows = db.execute(select(
                Session.id, Session.run_as, Session.run_dir, Session.provider, Session.management, Session.spool_offset,
                Session.team_host, Project.host, Placement.host).join(Project, Session.project_id == Project.id)
                .outerjoin(Placement, Session.placement_id == Placement.id)
                .where(Session.desired_state != "archived")).all()
        marks = {}
        for acc_id, run_as, run_dir, provider, management, offset, team_host, project_host, placement_host in rows:
            where = SimpleNamespace(id=acc_id, run_as=run_as, run_dir=run_dir,
                                    host=placement_host or team_host or project_host or "local")
            run = self.settings.session_run_dir(where)
            statusline = (provider in providers.PROVIDERS and "statusline" in providers.get(provider).supports)
            marks[acc_id] = (offset, management, _file_mark(spool_path(self.settings, where)),
                             _file_mark(run / "statusline.json") if statusline else None,
                             _file_mark(run / "remote.json") if management == "external" else None)
        return marks

    def ingest_all(self) -> None:
        """Each session in its own transaction: one session's bad data never stops the others.

        A session whose files are as they were after a pass that read all of them is skipped: nothing would change.
        Thousands of idle sessions otherwise cost a transaction each, every second (rc performance check). Any new
        byte, rewrite or offset change reads it again exactly as before, and every FULL_PASS_SECONDS all are read."""
        marks = self.ingest_marks()
        seen = getattr(self, "_ingest_seen", None) or {}
        full = time.monotonic() - getattr(self, "_ingest_full_at", float("-inf")) >= FULL_PASS_SECONDS
        done = {}
        for acc_id, mark in marks.items():
            if not full and seen.get(acc_id) == mark:
                done[acc_id] = mark
                continue
            try:
                with self.db() as db:
                    session = db.get(Session, acc_id)
                    if session is None or acc_id in self._launching:
                        # A fast CLI can emit SessionStart before the launch reply records its run.
                        continue
                    self._ingest(db, session)
                    self._observe_statusline(db, session)
                    if session.management == "external":
                        self._observe_remote(session)
                    db.commit()
                    # Settled only when this pass read the whole spool (one pass reads at most SPOOL_CHUNK).
                    if mark[2] is None or mark[2][0] - session.spool_offset < SPOOL_CHUNK:
                        done[acc_id] = mark
            except OperationalError as error:  # busy: this session's new lines are read again next pass
                log.info("ingest of %s deferred: %s", acc_id, error)
            except Exception:
                log.exception("ingest failed for session %s", acc_id)
        self._ingest_seen = done
        if full:
            self._ingest_full_at = time.monotonic()
        self._process_deferred()

    def _ingest(self, db: DbSession, session: Session) -> None:
        spool = spool_path(self.settings, session)
        chunk = read_spool_file(spool, session.spool_offset, SPOOL_CHUNK)
        if not chunk:
            return
        complete, _, _partial = chunk.rpartition(b"\n")
        if not complete and not chunk.endswith(b"\n"):
            if len(chunk) == SPOOL_CHUNK:  # one line longer than a whole chunk: skip it rather than stall
                db.add(Event(session_id=session.id, source="supervisor", kind="spool_parse_error", verified=False,
                             payload={"line": "(line over 1 MiB skipped)"}))
                session.spool_offset += len(chunk)
            return
        consumed = len(complete) + 1
        redact = self.vault.redactor(db, session.vault_keys)  # nothing an agent sent is stored with a key's value in it
        for raw in complete.split(b"\n"):
            if not raw.strip():
                continue
            try:
                record = redact.deep(json.loads(raw))
                if not isinstance(record, dict) or not isinstance(record.get("hook") or {}, dict):
                    raise ValueError("not an event object")
                with db.begin_nested():  # a line that fails part-way leaves nothing behind
                    self._apply_hook(db, session, record)
            except OperationalError:
                raise  # the database was busy, not the line's fault: nothing advances, the next pass reads it again
            except Exception as error:  # bad JSON, wrong shapes, deep nesting: record it and move on
                db.add(Event(session_id=session.id, source="supervisor", kind="spool_parse_error", verified=False,
                             payload={"line": redact.text(raw.decode(errors="replace"))[:500], "error": type(error).__name__}))
        session.spool_offset += consumed

    def _apply_hook(self, db: DbSession, session: Session, record: dict) -> None:
        hook = record.get("hook") or {}
        if not isinstance(hook, dict):
            hook = {}
        if not hook.get("hook_event_name") and _text(record.get("event"), 64):  # named by acc-hook's argument
            hook = {**hook, "hook_event_name": _text(record.get("event"), 64)}
        name = _text(hook.get("hook_event_name"), 64) or "unknown"
        provider = _text(record.get("provider"), 16) or session.provider
        if name == "AccTeam":
            self.queue_team_request(db, session, record, hook)
            return
        if name == "AccRequest":
            self._agent_request(db, session, hook)
            return
        db.add(Event(session_id=session.id, source=f"{provider}_hook", kind=name, payload=_trim(hook)))
        if provider in providers.PROVIDERS:  # each CLI's events in ACC's vocabulary (Claude Code's names and fields)
            hook = providers.get(provider).normalize_hook(hook)
            name = _text(hook.get("hook_event_name"), 64) or name
        if session.automation:
            from . import automation
            state = dict(session.automation)
            if name == "UserPromptSubmit":
                prompt = re.sub(r"\s+", " ", str(hook.get("prompt") or "")).strip()
                expected = state.pop("expected_prompt", None)
                initial = state.pop("expect_initial", False)
                session.automation = state
                if not initial and (not expected or prompt != expected):
                    automation.cancel(db, session)
            if name == "PermissionRequest" and not providers.get(session.provider).advisory_asks:
                automation.cancel(db, session, 'needs your input')
            if name in ("StopFailure", "Notification"):
                signal = automation.limit_signal(session.provider, str(hook.get('error') or hook.get('message') or ''))
                if signal:
                    session.automation = {**session.automation, "hook_limit": signal}
        hook_sid = hook.get("session_id")
        hook_sid = hook_sid if isinstance(hook_sid, str) and NATIVE_ID.fullmatch(hook_sid) else None
        if name == "SessionStart" and hook_sid:
            if session.native_id is None:
                session.native_id = hook_sid  # "learned" ids (Codex: with the first prompt, F2)
            run = self._open_run(session)
            if run and run.verified_at is None and hook_sid == session.native_id:
                if run.reason == "resume":
                    self._note_recovery(db, session, run)
                run.verified_at = now()
                session.recovery_attempts = 0
                session.next_attempt_at = None
                session.last_error = None
        if name == "SessionEnd":
            run = self._open_run(session)
            if run:
                run.end_reason = _text(hook.get("reason"), 64)
        if session.management == "external":
            if name == "LauncherError":
                self._recovery_needed(db, session, _text(hook.get("message"), 500) or "external launcher failed")
                return
            if name == "SessionEnd":
                self._set_state(db, session, "exited", f"{provider}_hook")
                return
            if session.observed_state == "archived":
                return
        elif session.observed_state in ("stopped", "exited", "archived", "recovery_needed"):
            return  # late hooks from a process that is going away do not revive it
        self._note_activity(session, name, hook)
        guard = record.get("guard") if isinstance(record.get("guard"), dict) else None
        if guard and name == "PreToolUse":
            self._guardrail_blocked(db, session, hook, guard)
        if name == "PermissionRequest" and provider in providers.PROVIDERS and providers.get(provider).advisory_asks:
            return  # see Provider.advisory_asks: the prompt on screen says when a person is needed
        new_state = states.state_from_hook(name, hook)
        if new_state == "waiting_input":
            self._needs_input(db, session, hook, provider)
        if name == "StopFailure":
            error = str(hook.get("error") or hook.get("message") or hook.get("reason") or "")[:440].strip()
            attention.raise_item(db, f"stop_failure:{session.id}", kind="stop_failure", source=f"{provider}_hook",
                                 session_id=session.id, title=f"{session.name} stopped with an error",
                                 detail=(f"{error.rstrip('.')}. " if error else "")
                                 + "Open the session and send your message again.")
        elif name == "Stop":
            attention.resolve(db, f"stop_failure:{session.id}")
        if new_state:
            self._set_state(db, session, new_state, f"{provider}_hook")

    def _needs_input(self, db: DbSession, session: Session, hook: dict, provider: str) -> None:
        tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
        target = tool_input.get("command") or tool_input.get("file_path") or tool_input.get("url") or tool_input.get("description")
        what = (f"{hook.get('tool_name')}: {target}" if hook.get("tool_name") and target else None) \
            or hook.get("message") or hook.get("tool_name") or hook.get("notification_type") or "waiting for you"
        policy = json.loads(session.launch_spec.files.get("guardrails.json", "{}"))
        risk = risk_labels.classify(hook, session.cwd, guard_path=Path(self.settings.hook_path).with_name("acc-guard"),
                                    policy=policy, run_dir=str(self.settings.session_run_dir(session)))
        attention.raise_item(db, f"needs_input:{session.id}", kind="needs_input", source=f"{provider}_hook",
                             session_id=session.id, title=f"{session.name} needs your input", detail=str(what)[:500],
                             risk=risk)

    @staticmethod
    def _guardrail_blocked(db: DbSession, session: Session, hook: dict, guard: dict) -> None:
        """acc-guard refused a tool call: the agent was told why and carries on; the session's owner is told too."""
        rule, reason = _text(guard.get("rule"), 32) or "custom", _text(guard.get("reason"), 300) or "blocked"
        what = states.describe_tool(str(hook.get("tool_name") or "a tool"), hook.get("tool_input"), session.cwd)
        db.add(Event(session_id=session.id, source="guardrails", kind="guardrail_blocked",
                     payload={"rule": rule, "reason": reason, "tool": what}))
        attention.raise_item(db, f"guardrail:{session.id}:{rule}", kind="guardrail_blocked", severity="info",
                             source="guardrails", session_id=session.id,
                             title=f"{session.name}: guardrail blocked {what[0].lower() + what[1:]}"[:200], detail=reason)
        session.activity, session.activity_at = f"Blocked: {what}"[:200], now()

    @staticmethod
    def _note_activity(session: Session, name: str, hook: dict) -> None:
        """What the agent is doing now: the tool it is running (or just ran), "Thinking" after a prompt, nothing once
        the turn is over."""
        tool = hook.get("tool_name")
        if name in ("PreToolUse", "PostToolUse", "PermissionRequest") and isinstance(tool, str) and tool:
            cwd = hook.get("cwd") if isinstance(hook.get("cwd"), str) else session.cwd
            tense = {"PreToolUse": "now", "PostToolUse": "done", "PermissionRequest": "ask"}[name]
            activity = states.describe_tool(tool, hook.get("tool_input"), cwd, tense)
        elif name == "UserPromptSubmit":
            activity = "Thinking"
        elif name in ("Stop", "StopFailure", "SessionStart", "SessionEnd", "Interrupt"):
            activity = None
        else:
            return
        session.activity, session.activity_at = activity, now()

    def _agent_request(self, db: DbSession, session: Session, hook: dict) -> None:
        """An agent asked ACC for something (acc-request). Recorded now, queued after this commit."""
        request = hook.get("request") if isinstance(hook.get("request"), dict) else {}
        db.add(Event(session_id=session.id, source="agent_request", kind=str(request.get("type"))[:40],
                     verified=False, payload=_trim(request)))
        if request.get("type") in ("publish", "unpublish", "tool", "work"):
            self._defer_request(db, (session.id, session.name, session.project_id, request))

    def _defer_request(self, db, request):
        """Publish proposals only after their hook/offset transaction commits, including savepoint rollback."""
        key = 'acc_hook_requests'
        if not db.info.get('acc_hook_callbacks'):
            def committed(session):
                if not session.in_nested_transaction():
                    self._deferred.extend(item for _, item in session.info.pop(key, []))

            def rolled_back(session, transaction):
                if transaction.parent is None:
                    session.info.pop(key, None)
                else:
                    session.info[key] = [(tx, item) for tx, item in session.info.get(key, []) if tx is not transaction]

            sa_event.listen(db, 'after_commit', committed)
            sa_event.listen(db, 'after_soft_rollback', rolled_back)
            db.info['acc_hook_callbacks'] = True
        db.info.setdefault(key, []).append((db.get_nested_transaction() or db.get_transaction(), request))

    def _process_deferred(self) -> None:
        """Agent proposals become requests in their own transaction; execution requires an authorized person."""
        deferred, self._deferred = self._deferred, []
        for session_id, name, project_id, request in deferred:
            if request.get("type") == "work":
                handler = getattr(self, 'group_work_request', None)
                if handler:
                    reply = handler(session_id, request)
                    with self.db() as db:
                        db.merge(Observation(key='group.work.reply.' + session_id,
                            value={'request_id': request.get('request_id'), 'reply': reply},
                            source='group_board', observed_at=now().timestamp()))
                        db.commit()
                continue
            if request.get("type") == "tool":
                try:
                    self.tools.request_session(session_id, **{k: v for k, v in request.items() if k != "type"})
                except (ValueError, TypeError) as error:
                    with self.db() as db:
                        attention.raise_item(db, f"tool_refused:{session_id}", kind="tool_failed", severity="info",
                                             source="tools", session_id=session_id, title=f"{name}: tool request refused",
                                             detail=str(error)[:500])
                        db.commit()
                continue
            if self.publisher is None:
                continue
            try:
                self.publisher.request(action=request["type"], host=str(request.get("host", "")),
                                       port=request.get("port") if isinstance(request.get("port"), int) else None,
                                       access=request.get("access", "public"), requested_by=f"session {name}",
                                       project_id=project_id, session_id=session_id,
                                       reason=str(request.get("reason") or "")[:500])
            except Exception as error:  # surface refusals to the owner instead of dropping them
                with self.db() as db:
                    attention.raise_item(db, f"publish_refused:{session_id}:{request.get('host')}", kind="publish_failed",
                                         severity="info", source=f"session {name}", session_id=session_id,
                                         title=f"{name} asked to {request.get('type')} {request.get('host')}",
                                         detail=f"It wasn't sent for approval: {error}")
                    db.commit()

    def _observe_remote(self, session: Session) -> None:
        """External launchers write remote.json; ACC cannot read their user's Claude registry."""
        raw = read_spool_file(self.settings.session_run_dir(session) / "remote.json")
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return
        bridge = data.get("bridge_session_id") if isinstance(data, dict) else None
        if isinstance(bridge, str) and re.fullmatch(r"session_[A-Za-z0-9_-]{1,100}", bridge):
            session.remote_url = f"https://claude.ai/code/{bridge}"

    def _observe_statusline(self, db: DbSession, session: Session) -> None:
        if session.provider not in providers.PROVIDERS or "statusline" not in providers.get(session.provider).supports:
            return
        path = self.settings.session_run_dir(session) / "statusline.json"
        try:
            mtime = path.lstat().st_mtime
        except FileNotFoundError:
            return
        current = db.get(Observation, f"claude.statusline.{session.id}")
        if current and current.observed_at >= mtime:
            return
        raw = read_spool_file(path)
        try:
            data = json.loads(raw) if raw else None
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        value = {"session_id": session.id, "rate_limits": data.get("rate_limits"),
                 "context_window": data.get("context_window"), "model": data.get("model"), "cost": data.get("cost")}
        db.merge(Observation(key=f"claude.statusline.{session.id}", value=value, source="statusline", observed_at=mtime))
        if isinstance(data.get("rate_limits"), dict):
            latest = db.get(Observation, "claude.rate_limits")
            if latest is None or latest.observed_at < mtime:
                db.merge(Observation(key="claude.rate_limits", value={**data["rate_limits"], "via_session": session.id},
                                     source="statusline", observed_at=mtime))
