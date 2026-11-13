"""Explicit, capability-protected offline native relay browser fixture.

No production installer or account service. Native code only runs in the existing
mandatory unshare-all/clearenv probe, with a local artificial Responses server.
The test controls qualification in this disposable process; never an attestation.
"""
import asyncio
import importlib.util
import json
import os
import pwd
import secrets
import threading
import uuid
from pathlib import Path
from types import SimpleNamespace

from starlette.responses import JSONResponse
from starlette.routing import Route

from acc import providers
from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import Project, RelayOperation, RelaySession, Session
from acc.server.session_relay import writers


def install(app, capability):
    if os.environ.get('ACC_E2E_RELAY_NATIVE') != '1':
        return
    ctx = app.state.ctx
    state = ctx.settings.data_dir.parent.resolve()
    assert str(state).startswith('/tmp/relay-m3-browser-native/')
    active = {}

    async def inspect(method, *, timeout=5):
        reader, writer = await asyncio.open_unix_connection(str(active['output'] / 'inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), timeout))
            assert 'error' not in value, value
            return value
        finally:
            writer.close()
            await writer.wait_closed()

    def allowed(request):
        return secrets.compare_digest(request.headers.get('x-e2e-isolation', ''), capability)

    async def prepare(request):
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        who = await asyncio.to_thread(ctx.principal, request)
        if active:
            return JSONResponse({'detail': 'Native fixture already active.'}, status_code=409)
        from e2e_relay_input_profile import install as profile_input
        active['input_profile'], active['profile_close'] = profile_input(ctx)
        root = Path(__file__).resolve().parents[1]
        path = root / 'scripts/session_relay_qualification/codex_placement_probe.py'
        spec = importlib.util.spec_from_file_location('browser_native_probe', path)
        probe = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(probe)
        workspace = ctx.settings.workspaces_root / ('native-relay-browser-' + uuid.uuid4().hex[:8])
        workspace.mkdir(mode=0o700)
        account = pwd.getpwuid(os.getuid()).pw_name
        @write_intent
        def project():
            with ctx.db() as db:
                value = Project(slug='native-relay-' + uuid.uuid4().hex[:8], name='Native relay fixture', workspace_path=str(workspace),
                    account=account, owner=who.username, trusted=True, import_mode='managed')
                db.add(value)
                db.commit()
                return value.id
        identifier = await asyncio.to_thread(project)
        def configured(output):
            active['output'] = output
            (state / 'native-relay-evidence.json').write_text(json.dumps({'output': str(output)}))
            (output / 'config.json').write_text(json.dumps({
                'workspace': str(workspace), 'run_dir': str(ctx.settings.run_dir), 'timeout': 100}))
        active['task'] = asyncio.create_task(asyncio.to_thread(probe.capture, configured, fixture_root=state))
        for _ in range(1000):
            if active['task'].done():
                await active['task']
            if active.get('output') and (active['output'] / 'ready.json').exists():
                break
            await asyncio.sleep(.02)
        endpoint = json.loads((active['output'] / 'ready.json').read_text())['base_url']
        native = Brokers(active['output'] / 'broker.sock', None, account, 'fixture-services')
        active['native'] = native
        original_call, original_fresh, original_accounts = ctx.broker.call, ctx.broker.fresh, ctx.broker.accounts
        async def call(method, **params):
            if method == 'relay_probe' and params.get('account') == account:
                return await native.call(method, **params)
            return await original_call(method, **params)
        def fresh(selected, host=None, timeout=60):
            if selected == account and host in (None, 'local'):
                return native.fresh(selected, host, timeout)
            return original_fresh(selected, host, timeout)
        provider = type(providers.get('codex'))
        original_compile = provider.compile
        def compile(self, inputs, context, instructions):
            files = original_compile(self, inputs, context, instructions)
            # Freeze the artificial local provider through the ordinary compiler
            # result, before public launch persists/reviews its launch spec.
            return {**files, 'codex-overrides.json': json.dumps([
                'model_provider="fixture"', 'model="fixture-model"',
                'model_providers.fixture={name="Offline fixture",base_url="' + endpoint
                + '",wire_api="responses",requires_openai_auth=false,request_max_retries=0,stream_max_retries=0}'])}
        active['restore'] = (original_call, original_fresh, original_accounts, provider, original_compile,
            writers.PUBLIC_WRITER_QUALIFIED, dict(ctx.settings.feature_overrides))
        ctx.broker.call, ctx.broker.fresh, provider.compile = call, fresh, compile
        ctx.broker.accounts = lambda: sorted(set(original_accounts()) | {account})
        ctx.settings.feature_overrides['session_relay'] = True
        writers.PUBLIC_WRITER_QUALIFIED = True
        active['project'] = identifier
        return JSONResponse({'projectId': identifier})

    async def qualify(request):
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        identifier = request.path_params['session']
        @write_intent
        def update():
            with ctx.db() as db:
                session = db.get(Session, identifier)
                assert session.project_id == active['project']
                row = db.get(RelaySession, identifier)
                assert row and row.current_run_id and not row.sealed
                row.capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
                db.commit()
        await inspect('qualify')
        await asyncio.to_thread(update)
        active['session'] = identifier
        return JSONResponse(await inspect('clients'))

    async def end_owner(request):
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        kind = request.path_params['kind']
        methods = {'normal': 'end_owner', 'abnormal': 'crash_owner', 'unknown': 'lose_authority'}
        if kind not in methods or not active.get('session'):
            return JSONResponse({'detail': 'Unknown owned fixture end.'}, status_code=409)
        who = await asyncio.to_thread(ctx.principal, request)
        with ctx.db() as db:
            ctx.session_for(db, who, active['session'])
        return JSONResponse(await inspect(methods[kind]))

    async def diagnostic(request):
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        native = await inspect('state')
        return JSONResponse({'native': native, 'clients': await inspect('clients') if native['active'] else None,
            'interrupt': await inspect('interrupt_state') if active.get('interrupt') else None})

    async def interrupt_control(request):
        if not allowed(request) or not active.get('session'):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        kind = request.path_params['kind']
        if kind == 'slow-reader':
            return JSONResponse(await inspect('slow_native_reader', timeout=15))
        if kind == 'release':
            return JSONResponse(await inspect('release_model'))
        controls = {'hold-write': 'hold_native_write', 'write-state': 'native_write_state', 'release-write': 'release_native_write'}
        if kind in controls and active.get('interrupt'):
            return JSONResponse(await inspect(controls[kind]))
        if kind == 'receipt-state' and active.get('interrupt'):
            identifier = request.query_params.get('operationId', '')
            def receipt():
                with ctx.db() as db:
                    record = db.get(RelayOperation, identifier)
                    if record is None or record.session_id != active['session']:
                        return None
                    runtime = getattr(ctx, 'relay_runtime', None)
                    run = runtime.runs.get(active['session']) if runtime else None
                    return {'state': record.receipt['state'], 'payloadDigest': record.receipt['payloadDigest'],
                        'remainingGrants': len(run.grants) if run else 0}
            value = await asyncio.to_thread(receipt)
            return JSONResponse(value or {'detail': 'No fixture receipt.'}, status_code=200 if value else 404)

        if kind not in ('prepare', 'prepare-lost', 'prepare-native-lost') or active.get('interrupt'):
            return JSONResponse({'detail': 'Unknown fixture interrupt control.'}, status_code=409)
        await inspect({'prepare-lost': 'lose_broker_interrupt_answer', 'prepare-native-lost': 'lose_interrupt_answer'}.get(kind, 'qualify_interrupt'))
        @write_intent
        def update():
            with ctx.db() as db:
                row = db.get(RelaySession, active['session'])
                row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
                db.commit()
        await asyncio.to_thread(update)
        active['interrupt'] = True
        return JSONResponse(await inspect('hold_model'))

    cold_calls = []

    async def cold_observer(request):
        """Assert a fresh process serves persisted reads without runtime creation."""
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        await asyncio.to_thread(ctx.principal, request)
        if request.method == 'POST':
            if active or getattr(ctx, 'relay_runtime', None) is not None:
                return JSONResponse({'detail': 'Fresh process required.'}, status_code=409)
            from acc.server.session_relay import runtime

            def forbidden_manager(_ctx):
                cold_calls.append('runtime.manager')
                raise AssertionError('Persisted observer must not allocate a runtime')

            runtime.manager = forbidden_manager
            ctx.settings.feature_overrides['session_relay'] = True
        return JSONResponse({'pid': os.getpid(), 'state': str(state),
            'runtimePresent': getattr(ctx, 'relay_runtime', None) is not None,
            'nativeFixtureActive': bool(active), 'runtimeAllocations': list(cold_calls),
            'publicWriterQualified': writers.PUBLIC_WRITER_QUALIFIED})

    async def retained_control(request):
        if not allowed(request) or not active.get('session'):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        from acc.server.session_relay import final_screen, history
        action = request.path_params['action']
        control = active.get('retained_control')
        if action in ('drop-runtime', 'runtime-state'):
            native = await inspect('state')
            if action == 'drop-runtime':
                if native['active'] or (control and control['entered'].is_set() and not control['release'].is_set()):
                    return JSONResponse({'detail': 'Native owner or captured read still active.'}, status_code=409)
                runtime = getattr(ctx, 'relay_runtime', None)
                if runtime:
                    await runtime.close()
                ctx.relay_runtime = None
                native = await inspect('state')
            return JSONResponse({'runtimePresent': getattr(ctx, 'relay_runtime', None) is not None,
                'nativeActive': native['active'], 'nativeRequests': native['requests']})
        if action == 'hold':
            if control is None:
                original_read, original_mask = final_screen.read, ctx.supervisor.vault.redactor
                control = {'entered': threading.Event(), 'release': threading.Event(), 'lock': threading.Lock(), 'armed': False}
                active['retained_control'] = control
                active['retained_restore'] = (original_read, original_mask)
                def held_read(*args):
                    result = original_read(*args)
                    resolver = args[1]
                    if getattr(resolver, 'request', None) is not None and resolver.request.headers.get('x-e2e-retained-proof') == capability:
                        with control['lock']:
                            hold = control['armed']
                            control['armed'] = False
                        if hold:
                            control['entered'].set()
                            if not control['release'].wait(10):
                                raise TimeoutError('Bounded retained-read fixture timeout')
                    return result
                final_screen.read = held_read
            control['entered'].clear(); control['release'].clear()
            control['armed'] = True
            return JSONResponse({'armed': True})
        if control is None:
            return JSONResponse({'detail': 'No retained-read fixture.'}, status_code=409)
        if action == 'state':
            native = await inspect('state')
            return JSONResponse({'held': control['entered'].is_set() and not control['release'].is_set(),
                'nativeActive': native['active'], 'nativeRequests': native['requests']})
        if action in ('release', 'redact-release', 'epoch-release') and (not control['entered'].is_set() or control['release'].is_set()):
            return JSONResponse({'detail': 'No captured read is held.'}, status_code=409)
        if action == 'redact-release':
            original_mask = active['retained_restore'][1]
            def mask(*args, **kwargs):
                redactor = original_mask(*args, **kwargs)
                return SimpleNamespace(text=lambda text: redactor.text(text).replace('First production Chat', '[REDACTED]'))
            ctx.supervisor.vault.redactor = mask
        elif action == 'epoch-release':
            @write_intent
            def reset():
                with ctx.db() as db:
                    history.reset(db, db.get(RelaySession, active['session']))
                    db.commit()
            await asyncio.to_thread(reset)
        elif action == 'release':
            pass  # The public caller, not this fixture control, revoked attachment authority.
        else:
            return JSONResponse({'detail': 'Unknown retained-read fixture action.'}, status_code=409)
        control['release'].set()
        return JSONResponse({'released': True})

    async def finish(request):
        if not allowed(request):
            return JSONResponse({'detail': 'Fixture capability required.'}, status_code=403)
        if not active:
            return JSONResponse({'closed': True})
        try:
            (state / 'native-relay-cleanup.txt').write_text('closing runtime')
            runtime = getattr(ctx, 'relay_runtime', None)
            try:
                if runtime:
                    await asyncio.wait_for(runtime.close(), 10)
            finally:
                # Public preview owns a cached relay_probe connection. Close it
                # before the probe's asyncio.Server.wait_closed(), which waits
                # for all clients; collecting first deadlocks the two owners.
                await active['native'].close()
                (state / 'native-relay-cleanup.txt').write_text('finishing native probe')
                await inspect('finish')
            (state / 'native-relay-cleanup.txt').write_text('collecting native probe')
            result = await active['task']
            (state / 'native-relay-cleanup.txt').write_text('native probe closed')
            result['mainInputProfile'] = active['input_profile']
            return JSONResponse(result)
        finally:
            if 'profile_close' in active:
                active['profile_close']()
            if 'retained_restore' in active:
                from acc.server.session_relay import final_screen
                active['retained_control']['release'].set()
                final_screen.read, ctx.supervisor.vault.redactor = active['retained_restore']
            if 'restore' in active:
                original_call, original_fresh, original_accounts, provider, original_compile, qualified, features = active['restore']
                ctx.broker.call, ctx.broker.fresh, ctx.broker.accounts, provider.compile = original_call, original_fresh, original_accounts, original_compile
                writers.PUBLIC_WRITER_QUALIFIED = qualified
                ctx.settings.feature_overrides.clear()
                ctx.settings.feature_overrides.update(features)
            if 'native' in active:
                await active['native'].close()
            active.clear()

    app.router.routes.insert(0, Route('/e2e-test/relay-native/cold-observer', cold_observer, methods=['GET', 'POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/prepare', prepare, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/qualify/{session}', qualify, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/end/{kind}', end_owner, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/state', diagnostic, methods=['GET']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/interrupt/{kind}', interrupt_control, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/retained/{action}', retained_control, methods=['POST']))
    app.router.routes.insert(0, Route('/e2e-test/relay-native/finish', finish, methods=['POST']))
