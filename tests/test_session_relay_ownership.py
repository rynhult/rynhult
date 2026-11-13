"""Real main plans + authenticated broker stream; real SDK with owned fake CLIs.

Native draft/drain/tree proof is an explicit fixture here, never qualification.
"""
import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_claude_agent import CLI, until
from test_session_relay_runtime import runtime_case  # noqa: F401, F811

from acc.broker.claude_hooks import ClaudeHookServer
from acc.broker.session_relay import private_dir
from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelayOwnership, RelaySession, Session, SessionRun
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p
from acc.session_relay.adapters.base import CloseMode, RuntimeAttestation, Watermark
from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter
from acc.session_relay.adapters.claude_switch import LaunchedClaudeOwner, terminal_resume_command
from acc.session_relay.adapters.claude_terminal import ClaudeTerminalOwner
from acc.session_relay.claude_hook import exchange
from acc.session_relay.terminal_host import TerminalRun

SID = 'abc123def456'


@pytest.fixture
async def ownership_case(runtime_case, monkeypatch, request, tmp_path):  # noqa: F811
    pytest.importorskip('claude_agent_sdk')
    case = runtime_case
    cli = CLI
    subscription = getattr(request, "param", None) == "subscription"
    if hasattr(request, "param") and not subscription:
        cli = tmp_path / "fake-claude-native-id"
        cli.write_text(CLI.read_text().replace("fixture-conversation", request.param))
        cli.chmod(0o700)
    if subscription:
        from acc.server import claude_subscription
        case.ctx.settings = replace(case.ctx.settings, feature_overrides={
            **case.ctx.settings.feature_overrides, 'claude_subscription_chat': True})
        @write_intent
        def accept_fixture_consent():
            with case.db() as db:
                claude_subscription.update(db, case.resolve()[0], True, claude_subscription.VERSION)
                db.commit()
        accept_fixture_consent()
    case.ctx.supervisor.vault.deliver = lambda *_args, **_kwargs: SimpleNamespace(
        env={'ANTHROPIC_API_KEY': 'fixture-only-not-a-key'}, catalog=None, check_env=lambda _: None)
    @write_intent
    def reset():
        with case.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, case.launch.run_id))
            db.flush()
            session = db.get(Session, SID)
            session.provider, session.native_id, session.claude_auth = 'claude', ('fixture-conversation' if subscription else getattr(request, 'param', 'fixture-conversation')), 'subscription' if subscription else 'api_key'
            session.launch_spec.provider = 'claude'
            session.launch_spec.inputs = {'provider': 'claude'}
            session.launch_spec.files = {'settings.json': '{"disableAllHooks":false,"hooks":{}}', 'guardrails.json': '{}'}
            db.commit()
    reset()
    case.factory.config.providers['claude'] = str(cli.resolve())
    factories = []
    class FakeFactory:
        exit_qualified = True  # Explicit fake process-tree proof, never a native qualification.
        def __init__(self, transport, spec, value, runtime):
            self.runtime, self.spec = runtime, spec
            self.directory = Path(spec.spec['run_dir']) / 'relay'
            fd = private_dir(self.directory)
            __import__('os').close(fd)
            from acc.broker import ops
            ops._materialize_run(Path(spec.spec['run_dir']), value['files'], value['env'], guard_files=(*ops.GUARD_FILES,'mcp.json'))
            self.guards = {name: p.sha256_digest(content.encode()) for name, content in value['files'].items()
                           if name in (*ops.GUARD_FILES,'mcp.json')}
            self.current = self.hooks = None
            self.owners = []
            self.snapshot = ''
            self.closed = False
            factories.append(self)
        async def authorize(self, spec=None, operation=None):
            if operation and operation.method == 'decision.reply':
                return await self.runtime.peer.call('authority.answer_verify', {'grant': self.runtime.answer_grant})
            if operation:
                return await self.runtime.verify(operation)
            return await self.runtime.owner_authorized()
        async def prepare(self, spec):
            if self.hooks:
                await self.hooks.close()
            self.runtime.spec = spec
            self.hooks = ClaudeHookServer(self.directory, spec, self.authorize, self.runtime.publish,
                self.runtime.observe, lambda identifier: self.current.permission_closed(identifier)
                    if hasattr(self.current, 'permission_closed') else None, accept_pid=lambda pid:pid==__import__('os').getpid())
            await self.hooks.start()
        async def publish(self, event):
            if event['kind'] == 'decision':
                await self.runtime.publish({'kind': 'decision', 'decisionId': event['id'],
                    'actionDigest': event['actionDigest'], 'expiresAtMs': event['expiresAtMs'], 'choices': ['accept', 'decline'],
                    'description': 'Artificial tool review: ' + json.dumps(event['input'])})
            elif event['kind'] in ('operation.accepted', 'turn.completed'):
                await self.runtime.publish(event)
        async def chat(self, spec, initial=False):
            await self.prepare(spec)
            reviewed = replace(spec, spec={**spec.spec, 'cli_path': str(cli.resolve()), 'initial_chat': False})
            adapter = ClaudeAgentAdapter(policy=lambda *_: not self.runtime.peer.closed,
                publish=self.publish, authorize=self.authorize)
            self.current = adapter
            observed = await adapter.start(reviewed)
            self.owners.append(adapter)
            return LaunchedClaudeOwner(adapter, observed)
        async def terminal(self, spec):
            await self.prepare(spec)
            reviewed = replace(spec, spec={**spec.spec, 'cli_path': str(cli.resolve())})
            process = await asyncio.create_subprocess_exec(*terminal_resume_command(reviewed), cwd=spec.binding.workspace,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            assert json.loads(await process.stdout.readline())['type'] == 'terminal_ready'
            async def boundary(*_):
                return Watermark(1, 0, 0)  # trusted fixture proof, not native qualification
            async def exited(process):
                return process.returncode is not None
            async def detach(_):
                pass
            owner = ClaudeTerminalOwner(spec=reviewed, process=process, host=SimpleNamespace(drain=boundary,detach=detach,run=TerminalRun(spec.spec['session_id'],spec.run_id)),
                grant=SimpleNamespace(epoch=1), boundary=boundary, authorize=self.authorize, tree_exited=exited)
            owner.observe_hook({'session_id': spec.binding.native_conversation, 'hook_event_name': 'Stop'})
            self.current = owner
            self.owners.append(owner)
            return LaunchedClaudeOwner(owner, RuntimeAttestation(spec.binding.binding_id, spec.run_id,
                                       spec.binding.native_conversation, f'fixture:{process.pid}'))
        async def exited(self):
            return self.current.process.returncode is not None
        async def close(self):
            if self.closed:
                return
            self.closed = True
            if self.current:
                await self.current.close(CloseMode.STOP)
            if self.hooks:
                await self.hooks.close()
    import acc.broker.claude_owner as module
    monkeypatch.setattr(module, 'ClaudeOwnerFactory', FakeFactory)
    launch = case.manager.new_exclusive_launch(case.resolve)
    @write_intent
    def reserve():
        with case.db() as db:
            launch.reserve(db, db.get(Session, SID))
            db.commit()
    reserve()
    case.launch = launch
    errors = []
    import traceback

    from acc.broker.relay_ownership import ExclusiveRuntime
    original = ExclusiveRuntime.handle
    async def recorded(self, method, params):
        try:
            return await original(self, method, params)
        except Exception:
            errors.append(traceback.format_exc())
            raise
    monkeypatch.setattr(ExclusiveRuntime, 'handle', recorded)
    main_original = launch.handle
    async def main_recorded(method, params):
        try:
            return await main_original(method, params)
        except Exception:
            errors.append(traceback.format_exc())
            raise
    launch.handle = main_recorded
    try:
        await launch.launch(SID)
    except Exception as error:
        raise AssertionError('Artificial fixture runtime failed: ' + '\n'.join(errors)) from error
    case.owned_factory = factories[0]
    yield case


async def select_owner(case, view, key):
    with case.db() as db:
        revision = db.get(RelaySession, SID).revision
    return await case.manager.select_owner(case.resolve, SID, target=view, revision=revision, request_key=key)


async def test_main_broker_sdk_tui_sdk_preserves_native_id_and_exits_before_resume(ownership_case):
    case = ownership_case
    original_run = case.launch.run_id
    first = case.owned_factory.current
    terminal = await select_owner(case, 'terminal', 'to-terminal')
    assert first.process.returncode is not None
    assert terminal['view'] == 'terminal'
    second = case.owned_factory.current
    chat = await select_owner(case, 'chat', 'to-chat')
    assert second.process.returncode is not None
    assert chat['view'] == 'chat' and case.launch.run_id != original_run
    with case.db() as db:
        row = db.get(RelaySession, SID)
        assert row.capabilities == {} and not row.sealed
        assert db.get(Session, SID).native_id == 'fixture-conversation'
        assert db.get(RelayOwnership, SID).phase == 'owned'
        assert db.get(SessionRun, original_run).ended_at
    count = len(case.owned_factory.owners)
    await select_owner(case, 'chat', 'to-chat')
    assert len(case.owned_factory.owners) == count


async def test_native_hook_decision_reaches_hub_and_only_reserved_answer_releases_it(ownership_case):
    case = ownership_case
    await select_owner(case, 'terminal', 'terminal')
    factory = case.owned_factory
    payload = {'session_id': 'fixture-conversation', 'prompt_id':'fixture-prompt', 'hook_event_name': 'PermissionRequest',
               'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}}
    await asyncio.to_thread(exchange, factory.directory / 'owner-channel.json',
        {**payload,'hook_event_name':'PreToolUse','tool_use_id':'fixture-tool'})
    pending = asyncio.create_task(asyncio.to_thread(exchange, factory.directory / 'owner-channel.json', payload))
    await until(lambda: bool(factory.hooks.pending))
    decision_id = next(iter(factory.hooks.pending))
    from acc.server.models import RelayDecision
    await until(lambda: _decision(case, decision_id) is not None)
    decision = _decision(case, decision_id)
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'decisionId': decision_id,
              'choiceId': 'accept', 'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
    registered = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'answer',
                                      'decision.reply', p.operation_digest('decision.reply', params))
    result = await case.launch.answer(case.resolve, {**params, 'operationId': registered.operation_id})
    assert result == {'outcome': 'unknown'}
    assert await pending == {'behavior': 'allow'}
    with case.db() as db:
        assert db.get(RelayOperation, registered.operation_id).receipt['state'] == 'uncertain'
        assert db.get(RelayDecision, (SID, decision_id)).decision['status'] == 'outcome_unknown'
    with pytest.raises(p.RelayError):
        await case.launch.answer(case.resolve, {**params, 'operationId': registered.operation_id})


def _decision(case, identifier):
    from acc.server.models import RelayDecision
    with case.db() as db:
        row = db.get(RelayDecision, (SID, identifier))
        return dict(row.decision) if row else None


async def test_switch_busy_is_not_queued_and_authority_loss_during_drain_cannot_launch(ownership_case):
    case = ownership_case
    source = case.owned_factory.current
    source.active = True
    with pytest.raises(Exception):
        await select_owner(case, 'terminal', 'busy')
    source.active = False
    assert case.owned_factory.current is source
    assert len(case.owned_factory.owners) == 1
    with case.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'owned'
    original = source.prepare_switch
    async def revoked():
        evidence = await original()
        case.state['revoked'] = True
        return evidence
    source.prepare_switch = revoked
    with pytest.raises(Exception):
        await select_owner(case, 'terminal', 'revoked')
    assert len(case.owned_factory.owners) == 1 and source.process.returncode is None
    case.state['revoked'] = False


async def test_uncertain_stop_requires_recovery_without_target_launch(ownership_case):
    case = ownership_case
    source = case.owned_factory.current
    original = source.stop_owner
    async def lost_stop():
        await original()
        raise ConnectionError('fixture lost stop result')
    source.stop_owner = lost_stop
    with pytest.raises(Exception):
        await select_owner(case, 'terminal', 'lost-stop')
    assert len(case.owned_factory.owners) == 1 and source.process.returncode is not None
    with case.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'recovery_required'
        assert db.get(RelaySession, SID).sealed


async def test_opaque_chat_submit_settles_only_after_owned_sdk_result_and_then_can_switch(ownership_case):
    case = ownership_case
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': 'owned-chat-fixture',
              'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    registered = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'chat-turn',
                                      'turn.submit', p.operation_digest('turn.submit', params))
    receipt = await case.manager.submit(case.resolve, SID, {**params, 'operationId': registered.operation_id})
    assert receipt.state in (p.ReceiptState.UNCERTAIN, p.ReceiptState.ACCEPTED)
    def finished():
        with case.db() as db:
            return (db.get(RelayOperation, registered.operation_id).receipt['state'] == 'accepted'
                    and db.get(RelaySession, SID).active_operation_id is None)
    await until(finished)
    await select_owner(case, 'terminal', 'after-query')
    assert case.owned_factory.current.process.returncode is None


async def test_sdk_callback_decision_uses_same_registered_identity_and_real_person(ownership_case):
    case = ownership_case
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': 'approve-fixture',
              'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    registered = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'sdk-turn',
                                      'turn.submit', p.operation_digest('turn.submit', params))
    await case.manager.submit(case.resolve, SID, {**params, 'operationId': registered.operation_id})
    adapter = case.owned_factory.current
    await until(lambda: bool(adapter.pending))
    decision_id = next(iter(adapter.pending))
    await until(lambda: _decision(case, decision_id) is not None)
    decision = _decision(case, decision_id)
    answer = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'decisionId': decision_id,
              'choiceId': 'decline', 'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
    op = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'sdk-answer',
                              'decision.reply', p.operation_digest('decision.reply', answer))
    await case.launch.answer(case.resolve, {**answer, 'operationId': op.operation_id})
    await until(lambda: not adapter.active)
    assert op.receipt.actor == case.resolve()[1]
    with case.db() as db:
        assert db.get(RelayOperation, op.operation_id).receipt['payloadDigest'] == op.receipt.payload_digest


async def test_pending_decision_and_terminal_writer_fence_chat_before_native_send(ownership_case):
    case = ownership_case
    await select_owner(case, 'terminal', 'terminal-owner')
    params = {'sessionId': SID, 'expectedRunId': str(case.launch.run_id), 'text': 'must-not-send',
              'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
    op = Hub(case.ctx).register(case.resolve, SID, str(case.launch.run_id), 'wrong-view',
                               'turn.submit', p.operation_digest('turn.submit', params))
    with pytest.raises(p.RelayError):
        await case.manager.submit(case.resolve, SID, {**params, 'operationId': op.operation_id})
    with case.db() as db:
        assert db.get(RelayOperation, op.operation_id).receipt['state'] == 'failed'
    assert len(case.owned_factory.owners) == 2


async def test_stop_seals_and_reaps_the_actual_current_owner(ownership_case):
    case = ownership_case
    await select_owner(case, 'terminal', 'stop-owner')
    from acc.server.session_relay.hub import seal_for_stop
    seal_for_stop(case.ctx, SID)
    assert await case.manager.stop(SID)
    assert case.owned_factory.current.process.returncode is not None
    with case.db() as db:
        assert db.get(RelaySession, SID).sealed


async def test_sdk_minimal_environment_masks_inherited_credentials(tmp_path, monkeypatch):
    pytest.importorskip('claude_agent_sdk')
    from test_session_relay_claude_agent import spec
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'artificial-inherited-key-sentinel')
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', '/artificial-inherited-config-root')
    reviewed = spec(tmp_path)
    reviewed = replace(reviewed, spec={**reviewed.spec, 'launch_env': {'HOME': str(tmp_path), 'PATH': '/usr/bin:/bin'}})
    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=lambda _: asyncio.sleep(0))
    try:
        await adapter.start(reviewed)
        assert adapter.client.options.env['ANTHROPIC_API_KEY'] == ''
        assert adapter.client.options.env['CLAUDE_CONFIG_DIR'] == str(tmp_path / '.claude')
        child_env = Path(f'/proc/{adapter.process.pid}/environ').read_bytes()
        assert b'artificial-inherited-key-sentinel' not in child_env
        assert b'CLAUDE_CONFIG_DIR=' + str(tmp_path / '.claude').encode() + b'\0' in child_env
        assert b'artificial-inherited-config-root' not in child_env
    finally:
        await adapter.close(CloseMode.STOP)


async def test_main_recovery_seals_ownership_without_replaying_or_launching(ownership_case):
    case = ownership_case
    count = len(case.owned_factory.owners)
    from acc.server.session_relay.maintenance import recover
    recover(case.ctx)
    with case.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'recovery_required'
        row = db.get(RelaySession, SID)
        assert row.sealed and row.capabilities == {}
    assert len(case.owned_factory.owners) == count


async def test_plan_binds_actor_revision_digest_and_expires_before_stop(ownership_case):
    case = ownership_case
    hub = case.launch.owner_hub
    with case.db() as db:
        revision = db.get(RelaySession, SID).revision
    plan = hub.prepare(case.resolve, SID, target='terminal', revision=revision, request_key='prepared')
    with pytest.raises(p.RelayError):
        hub.claim(case.resolve, SID, plan['planId'], p.sha256_digest(b'wrong'), revision=plan['revision'])
    from datetime import timedelta

    from acc.server.models import utcnow
    @write_intent
    def expire():
        with case.db() as db:
            db.get(RelayOwnership, SID).expires_at = utcnow() - timedelta(seconds=1)
            db.commit()
    expire()
    with pytest.raises(p.RelayError):
        hub.claim(case.resolve, SID, plan['planId'], plan['planDigest'], revision=plan['revision'])
    assert len(case.owned_factory.owners) == 1 and case.owned_factory.current.process.returncode is None


async def test_prepared_switch_records_previous_actor_and_original_channel(ownership_case):
    case = ownership_case
    from sqlalchemy import select

    from acc.server.models import AuditLog
    with case.db() as db:
        revision = db.get(RelaySession, SID).revision
    case.launch.owner_hub.prepare(case.resolve, SID, target='terminal', revision=revision, request_key='audit-plan')
    with case.db() as db:
        audit = db.scalar(select(AuditLog).where(AuditLog.action == 'relay.owner.prepare'))
        assert audit.actor == 'alex'
        assert audit.detail['channel'] == 'chat'
        assert audit.detail['previous']['principal'] == 'alex'
        assert audit.detail['previous']['channel'] == 'chat'


async def test_sdk_native_hook_observations_do_not_fall_back_to_a_second_client(ownership_case):
    case = ownership_case
    factory = case.owned_factory
    for event in ('SessionStart', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse', 'Stop'):
        payload = {'session_id': 'fixture-conversation', 'hook_event_name': event, 'tool_use_id': 'tool-fixture'}
        assert await asyncio.to_thread(exchange, factory.directory / 'owner-channel.json', payload) == {'observed': True}
    assert not factory.current.native_tools and len(factory.owners) == 1


async def test_native_permission_mode_and_unknown_tool_families_are_denied_without_cards(ownership_case):
    case = ownership_case
    factory = case.owned_factory
    for tool in ('ExitPlanMode', 'EnterPlanMode', 'mcp__account_tools__install', 'unqualified-future-tool'):
        payload = {'session_id': 'fixture-conversation', 'hook_event_name': 'PermissionRequest',
                   'tool_name': tool, 'tool_input': {}}
        assert await asyncio.to_thread(exchange, factory.directory / 'owner-channel.json', payload) == {'behavior': 'deny'}
        result = await factory.current._permission(tool, {}, None)
        assert result.behavior == 'deny'
    assert not factory.hooks.pending and not factory.current.pending


async def test_native_auth_probe_cannot_outlive_owner_revocation(monkeypatch):
    import threading
    from types import SimpleNamespace

    from acc.broker.claude_owner import ClaudeOwnerFactory

    entered, release = threading.Event(), threading.Event()
    allowed = [True]
    checks = []
    async def current():
        checks.append(allowed[0])
        return allowed[0]
    def probe(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return 'api_key'
    monkeypatch.setattr('acc.broker.claude_auth.probe', probe)
    owner = object.__new__(ClaudeOwnerFactory)
    owner.guard_check = lambda: None
    owner.environment = lambda spec: {}
    owner.executable = '/fixture/claude'
    owner.base = SimpleNamespace(run_id='fixture-owner',binding=SimpleNamespace(workspace='/fixture'),
        spec={'terminal_resume_args': [], 'auth_mode': 'api_key'})
    owner.runtime = SimpleNamespace(spec=owner.base, owner_authorized=current)
    task = asyncio.create_task(owner.authorize())
    try:
        assert await asyncio.to_thread(entered.wait, 5)
        allowed[0] = False
        release.set()
        assert await task is False
        assert checks == [True, False]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


def test_subscription_billing_selectors_are_removed_before_materialization_and_both_owners():
    from types import SimpleNamespace

    from acc.broker.claude_owner import credential_environment
    from acc.providers.claude_auth import AUTH_ENV

    env = {key: 'artificial' for key in AUTH_ENV}
    env.update({'PROJECT_SETTING': 'retained', 'AWS_REGION': 'fixture-region'})
    subscription = SimpleNamespace(spec={'auth_mode': 'subscription'})
    api = SimpleNamespace(spec={'auth_mode': 'api_key'})
    assert credential_environment(subscription, env) == {'PROJECT_SETTING': 'retained', 'AWS_REGION': 'fixture-region'}
    assert credential_environment(api, env) == env
    assert set(AUTH_ENV) <= env.keys()  # Do not mutate another reviewed launch.


async def test_native_root_exit_and_observed_children_do_not_claim_complete_tree_exit(monkeypatch):
    import subprocess
    import sys
    from types import SimpleNamespace

    from acc.broker.claude_owner import ClaudeOwnerFactory, TmuxProcess

    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], env={'PATH': '/usr/bin:/bin'})
    process = None
    def tmux(config, *args, **kwargs):
        return SimpleNamespace(returncode=0, stdout=(f'$1 %1 {child.pid} {__import__("os").getpid()}' if args[-1] == '#{session_id} #{pane_id} #{pane_pid} #{pid}' else str(child.pid)) if args[0] == 'display-message' else '')
    monkeypatch.setattr('acc.broker.claude_owner.ops.tmux', tmux)
    monkeypatch.setattr('acc.broker.claude_owner.ops.kill', lambda *_: child.terminate())
    try:
        process = TmuxProcess(SimpleNamespace(), 'fixture-only')
        process.terminate()
        await process.wait()
        assert child.wait(timeout=5) is not None
        assert await process.tree_exited(process) is False
        factory = object.__new__(ClaudeOwnerFactory)
        factory.current = SimpleNamespace(process=process)
        factory.closed = False
        factory.close_complete = False
        assert await factory.exited() is False
        with pytest.raises(p.RelayError):
            await factory.prepare(None)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=5)
        if process:
            process.close()


async def test_unqualified_native_exit_refuses_before_stopping_the_existing_owner(ownership_case):
    case = ownership_case
    source = case.owned_factory.current
    case.owned_factory.exit_qualified = False
    with pytest.raises(Exception):
        await select_owner(case, 'terminal', 'no-complete-tree-proof')
    assert source.process.returncode is None
    assert case.owned_factory.current is source
    assert len(case.owned_factory.owners) == 1
    with case.db() as db:
        assert db.get(RelayOwnership, SID).phase == 'owned'


async def test_private_hook_exchange_supports_long_paths_without_moving_the_socket(tmp_path):
    directory = tmp_path / ('x' * 80) / ('y' * 80)
    observed = []
    async def authorize():
        return True
    async def observe(payload):
        observed.append(payload)
    async def publish(payload):
        raise AssertionError('No decision expected')
    spec = SimpleNamespace(run_id='run1', binding=SimpleNamespace(native_conversation='fixture-native'))
    server = ClaudeHookServer(directory, spec, authorize, publish, observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    payload = {'session_id': 'fixture-native', 'hook_event_name': 'Notification'}
    try:
        await server.start()
        assert (directory / 'native-hook.sock').exists()
        assert await asyncio.to_thread(exchange, directory / 'owner-channel.json', payload) == {'observed': True}
        assert observed == [payload]
    finally:
        await server.close()
    assert server.fd is None
    assert not (directory / 'native-hook.sock').exists()


async def test_native_permission_revoked_during_observation_denies_without_hub_card(tmp_path):
    current = True
    observed, closed, published = [], [], []
    async def authorize():
        return current
    async def observe(payload):
        nonlocal current
        observed.append(payload)
        current = False
    async def publish(payload):
        published.append(payload)
    spec = SimpleNamespace(run_id='run1', binding=SimpleNamespace(native_conversation='fixture-native'))
    server = ClaudeHookServer(tmp_path / 'hook', spec, authorize, publish, observe, closed.append, accept_pid=lambda pid:pid==__import__('os').getpid())
    payload = {'session_id': 'fixture-native', 'hook_event_name': 'PermissionRequest',
               'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}}
    try:
        await server.start()
        assert await asyncio.to_thread(exchange, server.directory / 'owner-channel.json', payload) == {'behavior': 'deny'}
        assert len(observed) == 1 and not published
        assert closed == [observed[0]['_relay_decision_id']]
        assert not server.pending
    finally:
        await server.close()


async def test_native_permission_revoked_after_answer_cannot_release_allow(tmp_path):
    current = True
    cards = []
    async def authorize():
        return current
    async def observe(payload):
        pass
    async def publish(payload):
        cards.append(payload)
    spec = SimpleNamespace(run_id='run1', binding=SimpleNamespace(native_conversation='fixture-native'))
    server = ClaudeHookServer(tmp_path / 'hook', spec, authorize, publish, observe, accept_pid=lambda pid:pid==__import__('os').getpid())
    payload = {'session_id': 'fixture-native', 'prompt_id':'fixture-prompt', 'hook_event_name': 'PermissionRequest',
               'tool_name': 'Bash', 'tool_input': {'command': 'echo artificial'}}
    pending = None
    try:
        await server.start()
        await asyncio.to_thread(exchange, server.directory / 'owner-channel.json',
            {**payload,'hook_event_name':'PreToolUse','tool_use_id':'fixture-tool'})
        pending = asyncio.create_task(asyncio.to_thread(exchange, server.directory / 'owner-channel.json', payload))
        await until(lambda: bool(cards))
        async def verify():
            nonlocal current
            current = False  # verifier sampled true; authority changes before native delivery
            return True
        card = cards[0]
        result = await server.answer(card['decisionId'], card['actionDigest'], 'allow_once', verify)
        assert result == {'outcome': 'unknown'}
        assert await pending == {'behavior': 'deny'}
        assert not server.pending
    finally:
        await server.close()
        if pending:
            await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('mode', ['cloud', 'unknown', None])
def test_production_owner_refuses_out_of_scope_auth_before_any_placement(mode, tmp_path):
    from acc.broker.claude_owner import ClaudeOwnerFactory
    reviewed = SimpleNamespace(spec={'auth_mode': mode, 'run_dir': str(tmp_path / 'run')})
    with pytest.raises(p.RelayError) as error:
        ClaudeOwnerFactory(None, reviewed, {}, None)
    assert error.value.code is p.ErrorCode.POLICY_REFUSED
    assert not (tmp_path / 'run').exists()


async def test_cloud_mode_cannot_authorize_an_l1_handover(ownership_case):
    from acc.server.models import RelayRun
    case = ownership_case
    before = len(case.owned_factory.owners)
    @write_intent
    def set_mode(mode):
        with case.db() as db:
            run = db.get(RelayRun, case.launch.run_id)
            run.frozen_policy = {**run.frozen_policy, 'authMode': mode}
            db.commit()
    set_mode('cloud')
    try:
        with pytest.raises(p.RelayError) as error:
            await select_owner(case, 'terminal', 'out-of-scope-cloud')
        assert error.value.code is p.ErrorCode.POLICY_REFUSED
        assert len(case.owned_factory.owners) == before
    finally:
        set_mode('api_key')


async def test_completed_owner_cleanup_retains_conclusive_exit_after_proof_handles_close():
    from acc.broker.claude_owner import ClaudeOwnerFactory
    factory = object.__new__(ClaudeOwnerFactory)
    factory.close_complete = True
    factory.current = SimpleNamespace(process=SimpleNamespace(returncode=0))
    # A completed factory cannot launch again. No live descriptor or descendant
    # snapshot may replace its already verified whole-tree cleanup result.
    assert await factory.exited() is True
    factory.close_complete = False
    factory.exit_qualified = False
    assert await factory.exited() is False
