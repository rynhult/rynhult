"""Main's disabled exclusive-writer enrollment, ownership plans and hook replies.

Public negotiation stays behind the separate false qualification gate. All broker
requests use connection-bound opaque plans/grants and fresh original authority.
"""
from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path

from acc import providers
from acc.broker import ops
from acc.python_child import module_command
from acc.session_relay import protocol as p

from .. import claude_subscription
from ..admin_features import Features
from ..compiler import LaunchInputs, materialize, session_environment
from ..db import write_in_thread, write_intent
from ..models import RelayDecision, RelayOperation, RelayOwnership, RelayRun, Session, utcnow
from . import authority, store
from .hub import Hub
from .ownership import MODE, OwnershipHub
from .runtime import Launch


class ExclusiveLaunch(Launch):
    provider = 'claude'
    protocol_version = 3

    def __init__(self, manager, resolve):
        super().__init__(manager, resolve)
        from .claude_terminal_grants import ClaudeTerminalGrants
        self.terminal_grants = ClaudeTerminalGrants(self)
        self.plans = {}
        self.prepared_plans = {}  # one reviewed, pre-stop registered plan per owner
        self.owner_observations = {}
        self.owner_observation_tasks = set()
        self.recovery_plans = {}
        self.answers = {}
        self.answer_operations = {}
        self.turn_origins = {}
        self.owner_hub = OwnershipHub(self.ctx, placement=lambda db, session:
            manager.current_placement(db, session, provider=self.provider))

    @write_intent
    def enrollment(self, params):
        import time
        if (params.keys() != {'enrollment', 'linkId'} or params['enrollment'] != self.token
                or time.monotonic() - self.created > 60 or self.binding is not None):
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        with self.ctx.db() as db:
            session, _ = self.check_launch(db)
            who, actor = self.resolve.in_transaction(db) if hasattr(self.resolve, 'in_transaction') else self.resolve()
            if not session.native_id or not p.valid_opaque(session.native_id):
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            inputs = LaunchInputs.from_dict(session.launch_spec.inputs)
            if inputs.extra_args or inputs.initial_prompt or inputs.remote_control or inputs.endpoint:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            # External MCP writers do not run inside Claude's shell sandbox.
            # They cannot be admitted as transcript-authentic native input.
            if inputs.mcps:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            if inputs.permission_mode not in ('default', 'acceptEdits', 'plan'):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            if session.claude_auth == 'subscription' and not claude_subscription.allowed(db, who.user_id, Features(self.ctx.settings)):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            supervisor = self.ctx.supervisor
            delivery = supervisor.vault.deliver(db, session.project_id, session.owner,
                session.host != 'local', supervisor.catalog, inputs.mcps, provider='claude', claude_auth=session.claude_auth)
            files = materialize(session.launch_spec.files, delivery.catalog, 'claude')
            if json.loads(files.get('mcp.json', '{"mcpServers":{}}')) != {'mcpServers':{}}:
                raise p.RelayError(p.ErrorCode.UNSUPPORTED)
            files['mcp.json'] = '{"mcpServers":{}}\n'
            settings = json.loads(files['settings.json'])
            if settings.get('disableAllHooks') is not False:
                raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
            import shlex

            from .claude_settings import split
            settings, sdk_settings = split(settings, shlex.join([self.ctx.settings.hook_path, 'claude']),
                shlex.join([str(Path(self.ctx.settings.hook_path).with_name('acc-guard')), 'claude', 'PreToolUse']))
            channel = str(Path(session.run_dir) / 'relay/owner-channel.json')
            command = shlex.join(module_command('acc.session_relay.claude_hook', channel))
            for event in ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'Notification', 'PermissionRequest',
                          'PermissionDenied', 'PreToolUse', 'PostToolUse', 'PostToolUseFailure', 'Stop', 'StopFailure'):
                settings.setdefault('hooks', {}).setdefault(event, []).append({
                    'hooks': [{'type': 'command', 'command': command, 'timeout': 25}]})
                if event != 'PermissionRequest':
                    sdk_settings.setdefault('hooks', {}).setdefault(event, []).append({
                        'hooks': [{'type': 'command', 'command': command, 'timeout': 25}]})
            from acc.session_relay.claude_metadata_policy import freeze
            files['settings.json'] = json.dumps(freeze(settings, session.run_dir), indent=2) + '\n'
            files['relay-sdk-settings.json'] = json.dumps(freeze(sdk_settings, session.run_dir), indent=2) + '\n'
            env = session_environment(inputs, delivery.catalog, session.run_dir, files)
            delivery.check_env(env)
            env = {**env, **delivery.env}
            from acc.broker.claude_auth import supplied_mode
            observed = supplied_mode(env)
            if session.claude_auth not in ('api_key', 'subscription') or (
                    session.claude_auth == 'api_key' and observed != 'api_key'):
                raise p.RelayError(p.ErrorCode.POLICY_REFUSED)
            mode = session.claude_auth
            guards = {name: p.sha256_digest(files[name].encode()) for name in (*ops.GUARD_FILES,'mcp.json') if name in files}
            digest = p.sha256_digest(p.canonical_json(guards))
            from ..vault import Cipher
            installation = p.sha256_digest(Cipher(self.ctx.settings.data_dir / 'secrets/session_relay_cursor.key').key(create=True))
            self.binding = p.Binding(self.binding_id, installation, str(self.placement[2][0]), session.run_as,
                                    session.cwd, 'claude', session.native_id, digest)
            self.frozen_policy = {'launchDigest': session.launch_spec.digest, 'guardDigest': digest,
                'guardrails': json.loads(files['guardrails.json']), 'ownershipMode': MODE,
                'authMode': mode, 'personId': who.user_id}
            # The account broker resolves/pins its configured executable; main
            # cannot assume a path or symlink layout on that machine.
            spec = {'session_id': session.id, 'run_dir': session.run_dir, 'new_session': True,
                'sdk_settings_path': str(Path(session.run_dir) / 'relay-sdk-settings.json'),
                'ownership_mode': MODE, 'auth_mode': mode, 'person_id': who.user_id, 'settings_path': str(Path(session.run_dir) / 'settings.json'),
                'guard_intact': True, 'guard_digest': digest,
                'prior_owner_exit_verified': True, 'permission_mode': inputs.permission_mode,
                'tmux_name': session.tmux_name, 'model': inputs.model, 'effort': inputs.effort,
                'terminal_resume_args': providers.get('claude').args(inputs, run_dir=session.run_dir,
                    tmux_name=session.tmux_name, native_id=session.native_id, resume=True, files=files, extra=[])
                    + ['--setting-sources', '']}
            for filename, key in (('append-prompt.md', 'append_prompt_path'), ('mcp.json', 'mcp_config_path'),
                                  ('plugin/.claude-plugin/plugin.json', 'plugin_path')):
                if filename in files:
                    spec[key] = str(Path(session.run_dir) / ('plugin' if key == 'plugin_path' else filename))
            db.commit()
            return {'binding': dict(vars(self.binding)), 'runId': str(self.run_id), 'spec': spec, 'files': files, 'env': env}

    @write_intent
    def launched(self, params):
        result = super().launched(params)
        self.owner_hub.bind(self.resolve, self.session_id, view='chat')
        return result

    def check_run(self, db, *, resolve=None, action='control'):
        row, grant = super().check_run(db, resolve=resolve, action=action)
        if grant is not None:
            self.owner_hub._policy(db, db.get(Session, self.session_id), grant, db.get(RelayRun, grant.run_id))
        return row, grant

    @write_intent
    def fence_lost(self):
        super().fence_lost()
        from .native_prompt import retire
        retire(self)

    def resolve_grant(self, token):
        value = super().resolve_grant(token)
        grant = self.grants[token]
        if grant.operation.method == 'turn.submit' and value['receiptState'] == 'submitting':
            self.turn_origins.setdefault(grant.operation.operation_id, {
                'resolve': grant.resolve, 'actor': grant.operation.actor, 'runId': grant.operation.run_id})
        return value

    async def select_owner(self, resolve, *, target, revision, request_key, prepared=None, operation_id=None):
        async with self.terminal_grants.frame_lock, self.submit_lock:
            if self.prepared_plans and prepared is None:
                raise p.RelayError(p.ErrorCode.BUSY)
            with self.ctx.db() as db:
                owner = db.get(RelayOwnership, self.session_id)
                previous_authority = (dict(owner.authority.get('_sourceAuthority',owner.authority)) if owner else None)
            retained=self.terminal_grants.handover_client()
            plan = prepared if prepared is not None else await write_in_thread(lambda: self.owner_hub.prepare(resolve, self.session_id,
                target=target, revision=revision, request_key=request_key))
            if plan['phase'] == 'owned':
                return {**plan, 'changed': False}
            claimed = await write_in_thread(lambda: self.owner_hub.claim(resolve, self.session_id,
                plan['planId'], plan['planDigest'], revision=plan['revision'], operation_id=operation_id))
            return await self._activate_owner(resolve, claimed, previous_authority, retained=retained, public=prepared is not None)

    async def prepare_owner(self, resolve, *, target, revision, operation_id):
        """Review a registered plan without stopping or launching a provider."""
        async with self.terminal_grants.frame_lock, self.submit_lock:
            if self.prepared_plans and not any(plan['prepareOperationId'] == operation_id
                    for plan in self.prepared_plans.values()):
                raise p.RelayError(p.ErrorCode.BUSY)
            with self.ctx.db() as db:
                owner = db.get(RelayOwnership, self.session_id)
                previous = dict(owner.authority) if owner else None
            retained = self.terminal_grants.handover_client()
            plan = await write_in_thread(lambda: self.owner_hub.prepare(resolve, self.session_id,
                target=target, revision=revision, request_key=operation_id, operation_id=operation_id))
            if plan['phase'] == 'prepared':
                original = self.prepared_plans.get(plan['planId'])
                attachment = getattr(resolve, 'attachment_id', None)
                if original is not None and original['_attachment'] != attachment:
                    raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                self.prepared_plans.setdefault(plan['planId'], {'resolve': resolve,
                    '_previousAuthority': previous, '_attachment': attachment, '_retainedClient': retained, **plan})
            return plan

    async def commit_owner(self, resolve, plan_id, digest, *, revision, operation_id):
        """Consume a pre-registered L1 commit; public use is separately gated."""
        async with self.terminal_grants.frame_lock, self.submit_lock:
            prepared = self.prepared_plans.get(plan_id)
            if (prepared is None or prepared['planDigest'] != digest
                    or prepared['_attachment'] != getattr(resolve, 'attachment_id', None)):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            retained = self.terminal_grants.handover_client()
            if retained != prepared.get('_retainedClient'):
                raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            claimed = await write_in_thread(lambda: self.owner_hub.claim(resolve, self.session_id,
                plan_id, digest, revision=revision, operation_id=operation_id))
            try:
                result = await self._activate_owner(resolve, claimed, prepared['_previousAuthority'],
                    owner_resolve=prepared.get('_ownerResolve'), retained=retained)
                from .native_prompt import retire_inactive
                await write_in_thread(lambda: retire_inactive(self))
                return result
            finally:
                self.prepared_plans.pop(plan_id, None)

    async def cancel_owner(self, resolve, plan_id, *, operation_id):
        """Explicit pre-stop cancellation; cannot stop or replace a native owner."""
        async with self.submit_lock:
            prepared = self.prepared_plans.get(plan_id)
            if prepared is None or prepared['_attachment'] != getattr(resolve, 'attachment_id', None):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            result = await write_in_thread(lambda: self.owner_hub.abort(resolve, self.session_id, plan_id,
                operation_id=operation_id, previous_authority=prepared['_previousAuthority']))
            self.prepared_plans.pop(plan_id, None)
            return result

    async def _activate_owner(self, resolve, claimed, previous_authority, *, owner_resolve=None, retained=None, public=False):
        token = secrets.token_hex(32)
        self.plans[token] = {'resolve': resolve, '_previousAuthority': previous_authority,
            '_ownerResolve': owner_resolve, '_retainedClient': retained, '_public': public, **claimed}
        try:
            result = await self.peer.call('owner.select', {'plan': token})
            with self.ctx.db() as db:
                snapshot = self.owner_hub.snapshot(store.get(db, self.session_id), db.get(RelayOwnership, self.session_id))
            return {**snapshot, 'changed': result['changed']}
        except BaseException:
            await write_in_thread(lambda: self._recover_plan(claimed['planId']))
            raise
        finally:
            self.plans.pop(token, None)

    @write_intent
    def _recover_plan(self, plan_id):
        # A trusted connection/lifecycle failure can seal its own binding even
        # after the original person's cookie/consent has been revoked.
        with self.ctx.db() as db:
            row = store.lock(db, self.session_id)
            owner = db.get(RelayOwnership, self.session_id)
            if owner and owner.plan_id == plan_id and owner.phase != 'owned':
                if owner.phase in ('prepared', 'draining') and not self.peer.closed:
                    owner.phase = 'owned'  # stop boundary was never committed
                    previous = next((p.get('_previousAuthority') for p in (*self.plans.values(), *self.prepared_plans.values())
                                     if p.get('planId') == plan_id), None)
                    if previous is not None:
                        owner.authority = previous
                    if owner.target_run_id:
                        from ..models import SessionRun
                        db.get(SessionRun, owner.target_run_id).ended_at = utcnow()
                    if owner.commit_operation_id:
                        op = db.get(RelayOperation, owner.commit_operation_id)
                        if op.receipt['state'] == 'submitting':
                            store.settle(db, row, op, accepted=False, evidence=p.Evidence(
                                p.EvidenceKind.BROKER_REFUSAL, op.id, str(op.run_id)))
                else:
                    owner.phase = 'recovery_required'
                    store.seal(db, row)
                db.commit()

    def _plan(self, token):
        plan = self.plans.get(token)
        if not plan:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        if plan.get('_public'):
            from .exclusive_public import public_ready
            with self.ctx.db() as db:
                if not public_ready(self.ctx,store.get(db,self.session_id)):
                    raise p.RelayError(p.ErrorCode.UNSUPPORTED)
        self.owner_hub.verify(plan['resolve'], self.session_id, plan['planId'], plan['planDigest'])
        return plan

    @write_intent
    def answer_grant(self, token):
        value = self.answers.get(token)
        if value is None:
            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
        params, resolve = value['params'], value['resolve']
        from .native_prompt import refuse_allow
        refuse_allow(self, params)
        with self.ctx.db() as db:
            record = db.get(RelayDecision, (self.session_id, params['decisionId']))
            if not record:
                raise p.RelayError(p.ErrorCode.NOT_FOUND_OR_DENIED)
            decision = store.decision_from(record.decision)
            row, grant = authority.current(self.ctx, db, resolve, self.session_id, action='decide',
                expected_run=params['expectedRunId'], attached=True, decision=decision)
            op = store.operation(db, self.session_id, params['operationId'], grant.actor)
            payload = {key: item for key, item in params.items() if key != 'operationId'}
            digest = p.operation_digest('decision.reply', payload)
            p.check_registered(store.receipt_from(op.receipt), actor=grant.actor, session_id=self.session_id,
                               run_id=str(grant.run_id), method='decision.reply', payload_digest=digest)
            if (op.authority != grant.stamp() or op.deadline <= utcnow() or op.receipt['state'] != 'submitting'
                    or decision.status is not p.DecisionStatus.RESERVED or decision.winner != grant.actor
                    or decision.choice_id != params['choiceId'] or decision.action_digest != params['actionDigest']
                    or decision.revision != params['expectedRevision'] + 1
                    or decision.expires_at_ms <= int(utcnow().timestamp() * 1000)):
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            effect = next(choice.effect for choice in decision.choices if choice.id == decision.choice_id)
            if effect is not p.ChoiceEffect.DENY:
                self.owner_hub._policy(db, db.get(Session, self.session_id), grant, db.get(RelayRun, grant.run_id))
            from acc.broker.relay_runtime import operation_wire
            from acc.session_relay.adapters.base import AdapterOperation
            operation = AdapterOperation(op.id, self.session_id, str(grant.run_id), 'decision.reply', digest,
                grant.actor, self.fence, int(op.deadline.timestamp() * 1000), payload)
            return {'decisionId': decision.decision_id, 'actionDigest': decision.action_digest,
                    'effect': effect.value, 'operation': operation_wire(operation)}

    async def handle(self, method, params):
        if method == 'native.claude.register' and params.keys() == {'grant', 'sequence', 'text', 'revision'}:
            return await self.terminal_grants.prepare_native(params['grant'], params['sequence'], params['text'], params['revision'])
        if method == 'native.claude.claim' and params.keys() == {'grant'}:
            return await write_in_thread(lambda: self.terminal_grants.claim_native(params['grant']))
        if method == 'native.claude.complete' and params.keys() == {'grant', 'payloadDigest', 'nativeRef'}:
            return await write_in_thread(lambda: self.terminal_grants.complete_native(params['grant'], params['payloadDigest'], params['nativeRef']))
        if method in ('authority.answer', 'authority.answer_verify'):
            result = await write_in_thread(lambda: self.answer_grant(params['grant']))
            return True if method.endswith('verify') else result
        if method == 'authority.owner':
            def check():
                with self.ctx.db() as db:
                    if self.fence is None:
                        session, _ = self.check_launch(db)
                        _, actor = self.resolve.in_transaction(db) if hasattr(self.resolve, 'in_transaction') else self.resolve()
                        from types import SimpleNamespace
                        self.owner_hub._policy(db, session, SimpleNamespace(actor=actor),
                            SimpleNamespace(identity={'placement': self.placement}, frozen_policy=self.frozen_policy))
                        return params['runId'] == str(self.run_id)
                    owner = db.get(RelayOwnership, self.session_id)
                    resolve = self.resolve
                    if owner and owner.phase == 'starting' and params['runId'] == str(owner.target_run_id):
                        matches = [plan for plan in self.plans.values() if plan['targetRunId'] == params['runId']]
                        if len(matches) != 1:
                            raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                        resolve = matches[0]['resolve']
                    expected_actor = None
                    stream = self.terminal_grants.record
                    if owner and owner.phase == 'owned' and owner.view == 'terminal' and stream is not None:
                        self.terminal_grants._current(db, stream.value.grant)
                        resolve = stream.resolve
                        expected_actor = stream.value.actor
                    origin = self.turn_origins.get(params.get('operationId'))
                    if origin:
                        if origin['runId'] != params['runId']:
                            raise p.RelayError(p.ErrorCode.STALE_RUN)
                        resolve, expected_actor = origin['resolve'], origin['actor']
                    elif owner:
                        raw_actor = owner.previous_actor if owner.phase in ('prepared', 'draining', 'stopping') else owner.authority['actor']
                        if params['runId'] == str(owner.target_run_id) and owner.phase == 'starting':
                            raw_actor = owner.authority['actor']
                        if raw_actor:
                            expected_actor = store.actor_from(raw_actor)
                    action = 'control' if expected_actor is not None and expected_actor.kind is p.ActorKind.SERVICE else 'switch'
                    row, grant = authority.current(self.ctx, db, resolve, self.session_id, action=action)
                    if expected_actor is not None and expected_actor != grant.actor:
                        raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
                    self.owner_hub._policy(db, db.get(Session, self.session_id), grant, db.get(RelayRun, row.current_run_id))
                    allowed = {str(row.current_run_id)}
                    if owner and owner.phase == 'starting' and self.plans:
                        allowed.add(str(owner.target_run_id))
                    return params['runId'] in allowed and not row.sealed
            return await write_in_thread(check)
        if method == 'authority.owner_observation':
            from .claude_observer import verify
            return await asyncio.to_thread(verify, self, params['observation'])
        if method.startswith('authority.recovery_'):
            from .exclusive_recovery import broker
            return await broker(self,method,params)
        if method in ('authority.switch_resolve', 'authority.switch_verify'):
            plan = await write_in_thread(lambda: self._plan(params['plan']))
            if method.endswith('verify'):
                return True
            return {'sourceRunId': plan['sourceRunId'], 'targetRunId': plan['targetRunId'],
                    'bindingId': self.binding_id, 'target': plan['target']}
        if method == 'authority.switch_checkpoint':
            plan = self.plans.get(params['plan'])
            if not plan:
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            retained=plan.get('_retainedClient')
            if params['phase'] in ('stopping','starting'):
                expected=retained.proof() if retained is not None else None
                if params['evidence'].get('retained_input')!=expected:
                    raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                if params['phase']=='stopping' and self.terminal_grants.handover_client()!=retained:
                    raise p.RelayError(p.ErrorCode.INPUT_CONFLICT)
            result = await write_in_thread(lambda: self.owner_hub.checkpoint(plan['resolve'], self.session_id,
                plan['planId'], plan['planDigest'], params['phase'], params['evidence']))
            if params['phase']=='stopping' and retained is not None:
                self.terminal_grants.retained_clients[(retained.run_id,retained.attachment_id)]=retained
            if params['phase'] == 'owned':
                self.terminal_grants.invalidate()
                with self.ctx.db() as db:
                    row = store.get(db, self.session_id)
                    self.run_id = row.current_run_id
                    self.fence = p.Fence(row.authority_epoch, self.fence.broker_incarnation, self.manager.incarnation)
                self.resolve = plan.get('_ownerResolve') or plan['resolve']
                if plan.get('_public'):
                    from .runtime import RequestResolver
                    if isinstance(self.resolve,RequestResolver):
                        self.resolve=RequestResolver(self.resolve.ctx,self.resolve.request)
            return result
        if method == 'authority.switch_failed':
            plan = self.plans.get(params['plan'])
            if plan:
                await write_in_thread(lambda: self._recover_plan(plan['planId']))
            return True
        if method == 'authority.owner_fence':
            with self.ctx.db() as db:
                self.check_run(db, resolve=self.resolve)
            return {'authorityEpoch': self.fence.authority_epoch, 'hubIncarnation': self.fence.hub_incarnation}
        if method == 'publication':
            if params.get('kind') == 'native.prompt':
                from .native_prompt import publish
                return await write_in_thread(lambda: publish(self, params))
            if params.get('kind') == 'decision.applied' and 'application' not in params:
                from .native_decisions import applied
                return await write_in_thread(lambda:applied(self,
                    {key:value for key,value in params.items() if key!='runId'},native_run=params.get('runId')))
            if params.get('runId') != str(self.run_id):
                raise p.RelayError(p.ErrorCode.STALE_RUN)
            if params.get('kind') == 'decision.expired':
                @write_intent
                def expired():
                    with self.ctx.db() as db:
                        row, _ = self.check_run(db)
                        record = db.get(RelayDecision,(self.session_id,params['decisionId']))
                        if not record or record.run_id != self.run_id:
                            raise p.RelayError(p.ErrorCode.STALE_RUN)
                        decision = store.decision_from(record.decision)
                        if decision.action_digest != params['actionDigest']:
                            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                        if decision.status is p.DecisionStatus.PENDING:
                            if decision.expires_at_ms > int(utcnow().timestamp()*1000):
                                raise p.RelayError(p.ErrorCode.DECISION_EXPIRED)
                            record.decision = p.to_wire(p.advance_decision(decision,p.DecisionStatus.EXPIRED))
                            store.append(db,row,'decision.changed',{'decision':record.decision},
                                decision_id=record.id,priority=True)
                        # A chosen answer remains reserved/unknown. Expiry is
                        # not conclusive evidence that the provider applied it.
                        db.commit()
                    return True
                return await write_in_thread(expired)
            if params.get('kind') == 'operation.accepted':
                @write_intent
                def accepted():
                    with self.ctx.db() as db:
                        row = store.lock(db, self.session_id)
                        op = db.get(RelayOperation, params['operationId'])
                        if (not op or op.session_id != self.session_id or op.run_id != self.run_id
                                or row.binding_id != self.binding_id or op.receipt['payloadDigest'] != params['payloadDigest']):
                            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
                        if op.receipt['state'] in ('submitting', 'uncertain'):
                            store.settle(db, row, op, accepted=True, evidence=p.Evidence(
                                p.EvidenceKind.PROVIDER_ADMISSION, op.id, str(op.run_id), params['nativeRef']))
                            db.commit()
                    return True
                return await write_in_thread(accepted)
        result = await super().handle(method, params)
        if method == 'publication' and params.get('kind') == 'turn.completed':
            self.turn_origins.pop(params['operationId'], None)
        return result

    async def answer(self, resolve, params):
        if len(self.answer_operations) >= 4096:
            raise p.RelayError(p.ErrorCode.LIMIT)
        from .native_prompt import refuse_allow
        await write_in_thread(lambda: refuse_allow(self, params))
        try:
            answer = await write_in_thread(lambda: Hub(self.ctx).answer(resolve, self.session_id, params['decisionId'],
                expected_run=params['expectedRunId'], attached=True, choice_id=params['choiceId'],
                revision=params['expectedRevision'], digest=params['actionDigest'], operation_id=params['operationId']))
        except p.RelayError as error:
            if error.code is p.ErrorCode.DECISION_EXPIRED:
                await write_in_thread(lambda: self.refuse_expired_answer(resolve, params))
            raise
        from . import application_origins
        await write_in_thread(lambda: application_origins.remember(self, params))
        self.answer_operations[params['operationId']] = dict(params)
        token = secrets.token_hex(32)
        self.answers[token] = {'resolve': resolve, 'params': dict(params), 'decision': answer}
        cancelled = False
        try:
            result = await self.peer.call('owner.answer', {'grant': token})
        except (ConnectionError, TimeoutError):
            result = {'outcome': 'unknown'}
        except asyncio.CancelledError:
            cancelled = True
            result = {'outcome': 'unknown'}
        finally:
            self.answers.pop(token, None)
        settled = await write_in_thread(lambda: self.settle_answer(params, result))
        if cancelled:
            raise asyncio.CancelledError
        return settled

    def settle_answer(self, params, result):
        from .application_settlement import settle
        return settle(self.ctx, self.session_id, params, result)

    def publication(self, params):
        if params.get('kind') != 'decision.applied' or 'application' not in params:
            return super().publication(params)
        if params.keys() != {'kind', 'operationId', 'application', 'runId'}:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
        from . import application_origins
        original = self.answer_operations.get(params['operationId'])
        if original is None:
            original = application_origins.load(self, params['operationId'])
        if not original or params['runId'] != str(self.run_id) or params['runId'] != original['expectedRunId']:
            raise p.RelayError(p.ErrorCode.STALE_RUN)
        return self.settle_answer(original, {'outcome': 'accepted', 'application': params['application']})

    @write_intent
    def refuse_expired_answer(self, resolve, params):
        # Hub refused before reservation/dispatch. Do not leave its registered
        # intent queued forever, or expire any chosen/unknown provider outcome.
        with self.ctx.db() as db:
            row, grant = self.check_run(db, resolve=resolve)
            op = store.operation(db,self.session_id,params['operationId'],grant.actor)
            digest = p.operation_digest('decision.reply',{k:v for k,v in params.items() if k!='operationId'})
            p.check_registered(store.receipt_from(op.receipt),actor=grant.actor,session_id=self.session_id,
                run_id=str(grant.run_id),method='decision.reply',payload_digest=digest)
            if op.authority != grant.stamp():
                raise p.RelayError(p.ErrorCode.STALE_AUTHORITY)
            if op.receipt['state']=='queued':
                store.transition(db,row,op,p.ReceiptState.FAILED,reason=p.FailureReason.EXPIRED)
            db.commit()
