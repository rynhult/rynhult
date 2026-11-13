"""Short-lived helper sessions: normal records/hooks, never recovery or additional model turns."""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from .. import providers
from . import budgets, explanations
from .broker_client import BrokerError
from .compiler import LaunchInputs, compile_files
from .models import API_PRINCIPAL, AuditLog, Event, LaunchSpec, Observation, Project, Session, SessionRun
from .supervisor_common import now


def project_account(supervisor, project_id):
    with supervisor.db() as db:
        return db.get(Project, project_id).account


class ExplanationRuns:
    async def launch_explanation(self, project_id: int, owner: str, provider: str, model: str,
                                 cache_key: str, changes: dict, source_id: str, *, claude_auth: str = "subscription") -> dict:
        from .supervisor import LaunchRefused
        prepared = explanations.prompt(changes)
        from acc.broker.claude_auth import supplied_mode
        from acc.providers.claude_auth import AUTH_ENV, DOCS, MESSAGE, credential_env

        from .vault import plan

        with self.db() as db:
            source = db.get(Session, source_id)
            source_project_id = source.project_id
            if provider == 'claude':
                available = plan(db, source.project_id, owner, False, self.catalog)
                claude_auth = 'api_key' if any(k.variable in AUTH_ENV for k in available.keys) else 'subscription'
            delivery = self.vault.deliver(db, source.project_id, owner, False, self.catalog, provider=provider, claude_auth=claude_auth) if provider == 'claude' else None
            credentials = credential_env(delivery.env) if delivery else {}
            key_ids = [key.id for key in delivery.plan.keys if key.variable in credentials] if delivery else []
        note = None
        if provider == 'claude' and supplied_mode(credentials) == 'unknown':
            health = await self.broker.call('auth_health', account=project_account(self, project_id), host='local')
            if not (health.get('codex') or {}).get('logged_in'):
                raise LaunchRefused(f'{MESSAGE} {DOCS}')
            provider, model = 'codex', ''  # Let Codex choose its own default model.
            credentials = {}
            key_ids = []
            note = 'Using Codex because Claude helpers need a vault API key or configured cloud credentials.'
        elif provider == 'claude':
            note = 'Using Claude with API or cloud credentials supplied by Rynhult at launch.'
        with self.db() as db:
            if paused := budgets.paused(db, owner, provider, project_id=source_project_id):
                raise LaunchRefused(f'Your budget pauses new {providers.get(provider).label} launches ({", ".join(paused)}).')
        acc_id = uuid.uuid4().hex[:12]
        inputs = LaunchInputs(provider=provider, model=model, permission_mode="plan", sandbox="read-only",
                              approval="never", options={"mode": "plan"} if provider in ("antigravity", "copilot")
                              else {"mode": "ask"} if provider == "cursor" else {"agent": "plan"} if provider == "opencode" else {})
        with self.db() as db:
            project = db.get(Project, project_id)
            account = project.account
        layout = await self.layout("local", account)
        run_dir = f"{layout['run_dir'].rstrip('/')}/{acc_id}"
        from . import child_caps, guardrails, session_caps
        with self.db() as db:
            source = db.get(Session, source_id)
            if source is None or (source.owner or API_PRINCIPAL) != (owner or API_PRINCIPAL):
                raise LaunchRefused('Explanation source is outside the parent owner/project scope.')
            try:
                if source.management != 'acc':
                    raise LaunchRefused(f'limited by {source.id}: external source has no qualified managed grant')
                if source.container:
                    raise LaunchRefused(f'limited by {source.id}: no qualified container explanation executor is available; native fallback refused')
                policy = guardrails.policy(db, f'{run_dir}/workspace', owner, project_id,
                                           source.project.private_workspace, machine='local', account=account)
                inputs, policy = child_caps.constrain(db, source, inputs, policy, explanation=True)
                policy['explanation_only'] = True
                policy['parent_session_id'] = source.id
                parent_proof = child_caps.proof(db, source)
            except ValueError as error:
                raise LaunchRefused(str(error)) from None
        compiled = compile_files(inputs, layout["hook_path"], layout["statusline_path"], catalog=self.catalog,
                                 agent_home=layout["home"], guardrails=policy)
        native_id = str(uuid.uuid4()) if providers.get(provider).session_id == "assigned" else None
        spec_data = {"provider": provider, "model": model, "native_id": native_id, "cache_key": cache_key,
                     "changes": changes, "source_id": source_id}
        files = {**compiled.files, "prompt.txt": prepared, "explanation.json": json.dumps(spec_data),
                 child_caps.FILE: json.dumps(parent_proof, sort_keys=True)}
        value = {"status": "running", "revision": explanations.revision(changes), "helper_id": acc_id,
                 "provider": provider, "model": model, "provider_note": note, "created_at": now().isoformat(), "steps": [],
                 "input_summarised": "Large diff: summarised per file first" in prepared,
                 "diff_truncated": bool(changes.get("truncated"))}
        with self.db() as db:
            session = Session(id=acc_id, project_id=project_id, provider=provider, name="Explain these changes",
                              tmux_name=f"acc-explain-{acc_id}", native_id=native_id, owner=owner, run_as=account,
                              vault_keys=key_ids, claude_auth=claude_auth,
                              workdir=f"{run_dir}/workspace", run_dir=run_dir,
                              launch_spec=LaunchSpec(provider=provider, inputs=inputs.as_dict(), files=files,
                                                     digest=hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()),
                              desired_state="running", observed_state="starting")
            session.parent_session_id = source_id
            try:
                files[session_caps.FILE] = session_caps.attach(db, session)
                session.launch_spec.files = dict(files)
                session.launch_spec.digest = child_caps.digest(files)
                session_caps.admit_launch(db, session)
            except ValueError as error:
                raise LaunchRefused(str(error)) from None
            db.add(session)
            db.merge(Observation(key=cache_key, value=value, source="explanation", observed_at=time.time()))
            db.add(AuditLog(actor=owner, action="session.explain", target=source_id,
                            detail={"helper_id": acc_id, "provider": provider, "model": model,
                                    "revision": value["revision"]}))
            db.commit()  # reservation survives a disconnect; never repeat a possibly delivered launch
            name = session.tmux_name
        try:
            with self.db() as db:
                current = db.get(Session, acc_id)
                # Reservation committed before dispatch; recheck the current
                # parent after publication rather than borrowing its old read.
                files[session_caps.FILE] = json.dumps(session_caps.wire(db, current), sort_keys=True)
            result = await self.broker.call("launch", account=account, host="local", acc_id=acc_id, tmux_name=name,
                                            provider=provider, args=[], env=credentials, files=files,
                                            cwd=f"{run_dir}/workspace", run_dir=run_dir)
        except (BrokerError, ValueError) as error:
            with self.db() as db:
                session = db.get(Session, acc_id)
                self._finish_explanation(db, session, {**value, "status": "error",
                                                       "error": f"helper launch failed: {error}"})
                db.commit()
            raise LaunchRefused(str(error)) from None
        with self.db() as db:
            from . import claude_auth
            claude_auth.store(db, db.get(Session, acc_id), result.get('auth_mode', 'unknown'))
            db.add(SessionRun(session_id=acc_id, reason="launch", pid=result.get("pid"), verified_at=now()))
            db.add(Event(session_id=acc_id, source="supervisor", kind="run_launch",
                         payload={"command": result.get("command"), "pid": result.get("pid")}))
            self._set_state(db, db.get(Session, acc_id), "active", "supervisor")
            db.commit()
        return value

    def _finish_explanation(self, db, session, value: dict) -> None:
        data = json.loads(session.launch_spec.files["explanation.json"])
        db.merge(Observation(key=data["cache_key"], value=value, source="explanation", observed_at=time.time()))
        archived = session.desired_state == 'archived'
        session.desired_state = 'archived' if archived else 'stopped'
        if not (session.last_error or '').startswith('stop not confirmed:'):
            session.last_error = value.get('error')
        self._close_run(session, end_reason="explanation")
        self._set_state(db, session, 'archived' if archived else 'exited' if value['status'] == 'ready' else 'stopped', 'supervisor')

    async def _reconcile_explanation(self, db, session, pane) -> None:
        if session.desired_state != 'running':
            if pane and not pane['dead']:
                await self.broker.call('kill', account=session.run_as, host='local', tmux_name=session.tmux_name)
            from .security_settings import clear_stop_marker
            clear_stop_marker(db, session, actor='supervisor', reason='explanation_stop_confirmed')
            stopped_data = json.loads(session.launch_spec.files['explanation.json'])
            cached = db.get(Observation, stopped_data['cache_key']) if stopped_data.get('cache_key') else None
            if cached is not None and cached.value.get('helper_id') == session.id and cached.value.get('status') == 'running':
                self._finish_explanation(db, session, {**cached.value, 'status': 'error',
                    'error': 'helper stopped without a walkthrough; regenerate to retry', 'steps': []})
            return
        data = json.loads(session.launch_spec.files["explanation.json"])
        cached = db.get(Observation, data["cache_key"])
        if cached is None or cached.value.get("helper_id") != session.id:
            return
        value = cached.value
        if value["status"] != "running":
            if pane and not pane["dead"]:
                await self.broker.call("kill", account=session.run_as, host="local", tmux_name=session.tmux_name)
            return
        db.commit()
        try:
            result = await self.broker.call("explanation_result", account=session.run_as, host="local", acc_id=session.id)
            if result["status"] == "running":
                age = (now() - session.created_at).total_seconds()
                if session.desired_state == "running" and pane and not pane["dead"] and age < explanations.DEADLINE + 30:
                    return
                raise ValueError("helper stopped without a walkthrough or exceeded its time limit; regenerate to retry")
            if result["status"] == "error":
                raise ValueError(result["error"])
            document = explanations.validate(result["raw"], data["changes"])
            value = {**value, **document, "status": "ready", "created_at": now().isoformat()}
        except (BrokerError, ValueError, KeyError, TypeError) as error:
            value = {**value, "status": "error", "error": str(error)[:300], "steps": []}
        db.refresh(session)
        self._finish_explanation(db, session, value)
        db.commit()
        await self.broker.call("kill", account=session.run_as, host="local", tmux_name=session.tmux_name)
        from .security_settings import clear_stop_marker
        clear_stop_marker(db, session, actor='supervisor', reason='explanation_stop_confirmed')
