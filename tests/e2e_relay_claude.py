"""Capability-protected real Claude browser fixture, mandatory offline namespace."""
import asyncio
import importlib.util
import json
import os
import pwd
import secrets
import uuid
from dataclasses import replace
from pathlib import Path

from starlette.responses import JSONResponse
from starlette.routing import Route

from acc import providers
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import Project, RelaySession, Session, VaultGrant
from acc.server.session_relay import claude_public, exclusive_public, writers
from acc.server.session_relay.exclusive_runtime import ExclusiveLaunch
from acc.server.vault import create_key


def install(app, capability):
    if os.environ.get('ACC_E2E_RELAY_NATIVE') != '1':
        return
    ctx = app.state.ctx
    state = ctx.settings.data_dir.parent.resolve()
    assert str(state).startswith('/tmp/relay-m3-browser-native/')
    active = {}
    import time

    rpc_times = {}
    rpc_failures = []
    def allowed(request):
        return secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability)
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(active['output'] / 'owned-inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 5))
            assert 'error' not in value, value
            return value
        finally:
            writer.close()
            await writer.wait_closed()
    @write_intent
    def qualify(identifier):
        with ctx.db() as db:
            session = db.get(Session, identifier)
            if not session or session.project_id != active['project']:
                return
            row = db.get(RelaySession, identifier)
            assert row and not row.sealed
            row.capabilities = {'text': 'qualified', 'terminal.input': 'qualified',
                'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified', 'decision.tool_approval': 'qualified', 'interrupt': 'qualified'}
            db.commit()
        active['session'] = identifier
    async def prepare(request):
        if not allowed(request):
            return JSONResponse({}, status_code=403)
        who = await asyncio.to_thread(ctx.principal, request)
        if active:
            return JSONResponse({}, status_code=409)
        workspace = ctx.settings.workspaces_root / ('native-claude-browser-' + uuid.uuid4().hex[:8])
        workspace.mkdir(mode=0o700)
        account = pwd.getpwuid(os.getuid()).pw_name
        @write_intent
        def project():
            with ctx.db() as db:
                project = Project(slug='native-claude-' + uuid.uuid4().hex[:8], name='Offline Claude relay fixture',
                    workspace_path=str(workspace), account=account, owner=who.username, trusted=True, import_mode='managed')
                db.add(project)
                db.flush()
                key = create_key(db, ctx.supervisor.vault, name='Offline fixture API', variable='ANTHROPIC_API_KEY',
                    value='relay-synthetic-fixture-only', owner=who.username)
                db.add(VaultGrant(key_id=key.id, project_id=project.id, given_by=who.username))
                db.commit()
                return project.id
        active['settings_restore'] = ctx.settings, ctx.supervisor.settings
        ctx.settings = replace(ctx.settings, hook_path='/evidence/installed/relay-hook', statusline_path='/evidence/installed/relay-statusline')
        ctx.supervisor.settings = replace(ctx.supervisor.settings, hook_path=ctx.settings.hook_path, statusline_path=ctx.settings.statusline_path)
        from acc.server.session_relay import interrupts
        active['interrupt_restore'] = interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED
        interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED = True
        active['project'] = await asyncio.to_thread(project)
        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location('browser_claude_probe', root / 'scripts/session_relay_qualification/probe.py')
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        def configured(output):
            active['output'] = output
            (state / 'claude-relay-evidence.json').write_text(json.dumps({'output': str(output)}))
            (output / 'placement-config.json').write_text(json.dumps({'workspace': str(workspace),
                'run_dir': str(ctx.settings.run_dir), 'tmux_name': 'unlaunched', 'timeout': 200}))
            return lambda: None
        active['task'] = asyncio.create_task(asyncio.to_thread(probe.capture_claude_resume,
            enclosure=True, tui=True, placement=configured, fixture_root=state))
        for _ in range(1500):
            if active['task'].done():
                await active['task']
            if active.get('output') and (active['output'] / 'owned-ready.json').exists():
                break
            await asyncio.sleep(.02)
        endpoint = json.loads((active['output'] / 'owned-ready.json').read_text())['base_url']
        native = Brokers(active['output'] / 'owned-broker.sock', None, account, 'fixture-services')
        active['native'] = native
        original_call, original_fresh, original_accounts = ctx.broker.call, ctx.broker.fresh, ctx.broker.accounts
        original_deliver = ctx.supervisor.vault.deliver
        original_launched, original_handle = ExclusiveLaunch.launched, ExclusiveLaunch.handle
        async def call(method, **params):
            if method == 'relay_probe' and params.get('provider') == 'claude' and params.get('account') == account:
                return await native.call(method, **params)
            return await original_call(method, **params)
        def fresh(selected, host=None, timeout=60):
            if selected == account and host in (None, 'local'):
                return native.fresh(selected, host, timeout)
            return original_fresh(selected, host, timeout)
        def deliver(db, project_id, *args, **kwargs):
            value = original_deliver(db, project_id, *args, **kwargs)
            if project_id == active['project']:
                value.env.update(ANTHROPIC_BASE_URL=endpoint)
            return value
        def launched(run, params):
            result = original_launched(run, params)
            qualify(run.session_id)
            return result
        async def handle(run, method, params):
            start = time.monotonic()
            try:
                result = await original_handle(run, method, params)
            except Exception as error:
                if len(rpc_failures) < 128:
                    rpc_failures.append({'method': method, 'kind': params.get('kind'), 'type': type(error).__name__,
                        'code': getattr(getattr(error, 'code', None), 'name', None)})
                raise
            finally:
                if method == 'publication' and params.get('kind') == 'decision':
                    active.setdefault('decision_sources', []).append(params.get('approval', {}).get('source'))
                count, total, longest = rpc_times.get(method, (0, 0, 0))
                elapsed = time.monotonic() - start
                rpc_times[method] = count + 1, total + elapsed, max(longest, elapsed)
            if method == 'authority.answer_verify' and result is True and active.pop('revoke_answer', False):
                original = run.answers[params['grant']]['params']
                active['revoked_answer'] = original['operationId'], original['decisionId']
                from acc.server import auth
                @write_intent
                def revoke():
                    with ctx.db() as db:
                        auth.end_session(db, active.pop('revoke_token'))
                        db.commit()
                await asyncio.to_thread(revoke)
                active['answer_revoked'] = True
            if method == 'authority.switch_checkpoint' and params['phase'] == 'owned':
                await asyncio.to_thread(qualify, run.session_id)
            return result
        active['restore'] = (original_call, original_fresh, original_accounts, original_deliver,
            original_launched, original_handle, writers.PUBLIC_WRITER_QUALIFIED,
            claude_public.PUBLIC_CLAUDE_QUALIFIED, claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED, dict(ctx.settings.feature_overrides), exclusive_public.CLAUDE_LAUNCH_QUALIFIED)
        provider = type(providers.get('claude'))
        original_compile = provider.compile
        def compile(provider, inputs, context, instructions):
            files = original_compile(provider, inputs, context, instructions)
            settings = json.loads(files['settings.json'])
            permissions = settings.setdefault('permissions', {})
            permissions['ask'] = sorted(set(permissions.get('ask', [])) | {'Bash'})
            return {**files, 'settings.json': json.dumps(settings)}
        active['provider_restore'] = provider, original_compile
        provider.compile = compile
        ctx.broker.call, ctx.broker.fresh = call, fresh
        ctx.broker.accounts = lambda: sorted(set(original_accounts()) | {account})
        ctx.supervisor.vault.deliver = deliver
        ExclusiveLaunch.launched, ExclusiveLaunch.handle = launched, handle
        ctx.settings.feature_overrides['session_relay'] = True
        exclusive_public.CLAUDE_LAUNCH_QUALIFIED = True
        writers.PUBLIC_WRITER_QUALIFIED = claude_public.PUBLIC_CLAUDE_QUALIFIED = claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED = True
        return JSONResponse({'projectId': active['project']})
    async def diagnostic(request):
        if not allowed(request):
            return JSONResponse({}, status_code=403)
        result = await inspect('state')
        result['screen'] = await inspect('screen')
        result['rpc_times'] = rpc_times
        result['main_rpc_failures'] = rpc_failures
        result['decision_sources'] = active.get('decision_sources', [])
        result['answer_revoked'] = active.get('answer_revoked', False)
        if active.get('revoked_answer'):
            from acc.server.models import RelayDecision, RelayOperation
            operation_id, decision_id = active['revoked_answer']
            with ctx.db() as db:
                result['revoked_receipt'] = db.get(RelayOperation, operation_id).receipt['state']
                result['revoked_decision'] = db.get(RelayDecision, (active['session'], decision_id)).decision['status']

        result['permission_marker'] = (await inspect('permission_state'))['marker_exists']
        result['decision_metadata'] = await inspect('decision_metadata')
        if active.get('session'):
            with ctx.db() as db:
                session = db.get(Session, active['session'])
                row = db.get(RelaySession, session.id)
                result.update(sessionId=session.id, nativeId=session.native_id, runId=str(row.current_run_id),
                    terminalMediated=False)
        return JSONResponse(result)
    async def observation_race(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        return JSONResponse(await inspect('arm_observation_race'))
    async def permission(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        assert (await inspect('state'))['view'] in ('chat', 'terminal')
        return JSONResponse(await inspect('request_public_permission'))
    async def guard(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        return JSONResponse(await inspect('request_guard'))
    async def model(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        value = await request.json()
        if value not in ({'held': True}, {'held': False}, {'held': True, 'long': True}):
            return JSONResponse({}, status_code=400)
        return JSONResponse(await inspect('hold_long_public_model' if value.get('long') else 'hold_public_model' if value['held'] else 'release_model'))
    async def revoke_after_answer(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        who = await asyncio.to_thread(ctx.principal, request)
        with ctx.db() as db:
            if db.get(Session, active['session']).owner != who.username:
                return JSONResponse({}, status_code=403)
        active['revoke_token'] = request.cookies[ctx.session_cookie]
        active['revoke_answer'] = True
        return JSONResponse({'armed': True})
    async def forget_answers(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        run = ctx.relay_runtime.runs.get(active['session'])
        if not isinstance(run, ExclusiveLaunch) or not run.answer_operations:
            return JSONResponse({}, status_code=409)
        identifiers = set(run.answer_operations)
        from acc.server.models import RelayRun
        from acc.server.session_relay.application_origins import KEY
        with ctx.db() as db:
            saved = db.get(RelayRun, run.run_id).identity.get(KEY, {})
            durable = len(identifiers & saved.keys())
        run.answer_operations.clear()
        return JSONResponse({'forgot': len(identifiers), 'remaining': len(run.answer_operations), 'durable': durable})
    async def exit_for_recovery(request):
        if not allowed(request) or 'session' not in active:
            return JSONResponse({}, status_code=403)
        # Actual peer EOF retires the owned SDK enclosure; no database end fact
        # is invented and no replacement is enrolled by this private control.
        await ctx.relay_runtime.close()
        async with asyncio.timeout(20):
            while (await inspect('state'))['active']:
                await asyncio.sleep(.02)
        return JSONResponse({'exited': True, 'active': 0})
    async def finish(request):
        if not allowed(request):
            return JSONResponse({}, status_code=403)
        if not active:
            return JSONResponse({'closed': True})
        try:
            runtime = getattr(ctx, 'relay_runtime', None)
            if runtime:
                await asyncio.wait_for(runtime.close(), 10)
            await active['native'].close()
            await inspect('finish')
            return JSONResponse(await active['task'])
        finally:
            if 'interrupt_restore' in active:
                from acc.server.session_relay import interrupts
                interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED = active['interrupt_restore']
            if 'restore' in active:
                call, fresh, accounts, deliver, launched, handle, writer, claude, decisions, features, launch_qualified = active['restore']
                ctx.broker.call, ctx.broker.fresh, ctx.broker.accounts = call, fresh, accounts
                ctx.supervisor.vault.deliver = deliver
                ExclusiveLaunch.launched, ExclusiveLaunch.handle = launched, handle
                writers.PUBLIC_WRITER_QUALIFIED, claude_public.PUBLIC_CLAUDE_QUALIFIED = writer, claude
                claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED = decisions
                exclusive_public.CLAUDE_LAUNCH_QUALIFIED = launch_qualified
                ctx.settings.feature_overrides.clear()
                ctx.settings.feature_overrides.update(features)
            if 'provider_restore' in active:
                provider, compile = active['provider_restore']
                provider.compile = compile
            if 'native' in active:
                await active['native'].close()
            if 'settings_restore' in active:
                ctx.settings, ctx.supervisor.settings = active['settings_restore']
            active.clear()
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/prepare', prepare, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/state', diagnostic, methods=['GET']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/observation-race', observation_race, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/permission', permission, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/guard', guard, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/model', model, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/revoke-answer', revoke_after_answer, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/forget-answers', forget_answers, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/exit-for-recovery', exit_for_recovery, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-claude/finish', finish, methods=['POST']))
