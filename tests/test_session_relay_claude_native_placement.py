"""Actual production broker/owned tmux/opener in an offline namespace."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_claude_registered_switch import registered_switch
from test_session_relay_ownership import SID, select_owner
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.broker_client import Brokers
from acc.server.db import write_intent
from acc.server.models import RelaySession, Session, SessionRun
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

NATIVE = '22222222-2222-4222-8222-222222222222'


pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('scenario', ['warm', 'empty-refused', 'native-busy', 'native-permission', 'native-background', 'registered-warm', 'registered-empty-refused', 'http-warm', 'http-withdrawal', 'http-lost-reply', 'http-post-stop', 'http-post-owned', 'http-stop', 'http-permission', 'http-read-work-race', 'http-read-work-success-race', 'http-input-revoke', 'http-input-late-revoke', 'http-submit-revoke', 'http-exit-sdk', 'http-exit-tui', 'http-exit-tui-busy', 'http-exit-sdk-busy', 'http-helper-sdk', 'http-helper-tui', 'http-partial-header', 'http-partial-body', 'http-read-eof', 'http-lost-input-ack', 'http-source-truncate', 'http-source-rewrite', 'http-source-replace', 'http-source-symlink', 'http-source-mode', 'http-source-epoch', 'http-capacity-owner', 'http-capacity-global', 'http-replaced-pane', 'http-replaced-server', 'http-sdk-partial', 'http-sdk-lost-dispatch-ack', 'http-sdk-allow-revoke', 'http-sdk-interrupt', 'http-sdk-interrupt-revoke', 'http-sdk-interrupt-lost', 'http-sdk-interrupt-recovery', 'http-old-run'])
async def test_production_claude_owned_sdk_tmux_sdk_native_input(runtime_case, monkeypatch, record_property, scenario):  # noqa: F811
    c = runtime_case
    permission_case = scenario in ('native-permission', 'http-permission', 'http-sdk-allow-revoke')
    http_case = scenario in ('http-warm', 'http-withdrawal', 'http-lost-reply', 'http-post-stop', 'http-post-owned', 'http-stop', 'http-permission', 'http-read-work-race', 'http-read-work-success-race', 'http-input-revoke', 'http-input-late-revoke', 'http-submit-revoke', 'http-exit-sdk', 'http-exit-tui', 'http-exit-tui-busy', 'http-exit-sdk-busy', 'http-helper-sdk', 'http-helper-tui', 'http-partial-header', 'http-partial-body', 'http-read-eof', 'http-lost-input-ack', 'http-source-truncate', 'http-source-rewrite', 'http-source-replace', 'http-source-symlink', 'http-source-mode', 'http-source-epoch', 'http-capacity-owner', 'http-capacity-global', 'http-replaced-pane', 'http-replaced-server', 'http-sdk-partial', 'http-sdk-lost-dispatch-ack', 'http-sdk-allow-revoke')
    http_case = http_case or scenario.startswith('http-sdk-interrupt') or scenario == 'http-old-run'
    async def switch(target, key):
        if scenario in ('registered-warm', 'registered-empty-refused'):
            return await registered_switch(c, target, key)
        return await select_owner(c, target, key)
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('claude_placement_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    output = None
    @write_intent
    def reset():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            db.flush()
            row = db.get(Session, SID)
            row.provider, row.native_id, row.claude_auth = 'claude', NATIVE, 'api_key'
            row.tmux_name = 'acc-' + SID
            row.launch_spec.provider = 'claude'
            row.launch_spec.inputs = {'provider': 'claude', 'model': 'claude-sonnet-4-5'}
            settings = {'disableAllHooks': False, 'hooks': {}}
            if permission_case:
                settings['permissions'] = {'ask': ['Bash']}
            if scenario == 'native-background':
                settings['permissions'] = {'allow': ['Bash']}
            row.launch_spec.files = {'settings.json': json.dumps(settings),
                'guardrails.json': json.dumps({'workspace': row.cwd})}
            db.commit()
    reset()
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'owned-inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 40 if method == 'observer_capacity' else 10))
            assert 'error' not in value, value
            return value
        finally:
            writer.close()
            await writer.wait_closed()
    def prepare(directory):
        nonlocal output
        output = directory
        with c.db() as db:
            row = db.get(Session, SID)
            config = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent), 'tmux_name': row.tmux_name, 'unknown_tree': scenario.startswith(('http-helper-', 'http-replaced-')), 'timeout': 220 if scenario.endswith('-recovery') or 'capacity' in scenario or getattr(c, 'native_observer_capacity', False) else 100}
        (directory / 'placement-config.json').write_text(json.dumps(config))
        return lambda: None
    task = asyncio.create_task(asyncio.to_thread(probe.capture_claude_resume, enclosure=True, tui=True, placement=prepare))
    brokers = None
    try:
        for _ in range(1000):
            if task.done():
                await task
            if output is not None and (output / 'owned-ready.json').exists():
                break
            await asyncio.sleep(.02)
        assert output is not None and (output / 'owned-ready.json').exists()
        endpoint = json.loads((output / 'owned-ready.json').read_text())['base_url']
        c.ctx.supervisor.vault.deliver = lambda *_args, **_kwargs: SimpleNamespace(
            env={'ANTHROPIC_API_KEY': 'relay-synthetic-fixture-only', 'ANTHROPIC_BASE_URL': endpoint}, catalog=None, check_env=lambda _: None)
        with c.db() as db:
            account = db.get(Session, SID).run_as
        brokers = Brokers(output / 'owned-broker.sock', None, account, 'fixture-services')
        c.ctx.broker = brokers
        c.launch = c.manager.new_exclusive_launch(c.resolve)
        @write_intent
        def reserve():
            with c.db() as db:
                c.launch.reserve(db, db.get(Session, SID))
                db.commit()
        reserve()
        main_errors = []
        original_handle = c.launch.handle
        async def diagnostic(method, params):
            try:
                value = await original_handle(method, params)
                if method == 'authority.owner' and value is not True:
                    with c.db() as db:
                        from acc.server.models import RelayOwnership
                        owner, row = db.get(RelayOwnership, SID), db.get(RelaySession, SID)
                        main_errors.append(json.dumps({'method': method, 'run': params.get('runId'),
                            'current': row.current_run_id, 'sealed': row.sealed, 'phase': owner.phase,
                            'target': owner.target_run_id}))
                return value
            except Exception:
                import traceback
                main_errors.append(traceback.format_exc())
                raise
        c.launch.handle = diagnostic
        try:
            await c.launch.launch(SID)
        except Exception as error:
            error.add_note(''.join(main_errors))
            log = output / 'placement-errors.log'
            if log.exists():
                error.add_note(log.read_text())
            raise
        @write_intent
        def qualify():
            with c.db() as db:
                db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified',
                    **({'switch.to_chat': 'qualified', 'switch.to_terminal': 'qualified'} if http_case else {}),
                    **({'decision.tool_approval': 'qualified'} if scenario in ('http-permission', 'http-sdk-allow-revoke') else {})}
                db.commit()
        qualify()
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        if http_case:
            monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_QUALIFIED', True)
        if scenario in ('http-permission', 'http-sdk-allow-revoke'):
            monkeypatch.setattr('acc.server.session_relay.claude_public.PUBLIC_CLAUDE_DECISIONS_QUALIFIED', True)
        if scenario in ('empty-refused', 'registered-empty-refused'):
            before = await inspect('state')
            initial_run = c.launch.run_id
            with pytest.raises((p.RelayError, ConnectionError)):
                await switch('terminal', 'empty-cannot-resume')
            after = await inspect('state')
            assert before['view'] == after['view'] == 'chat'
            assert before['owner_process'] == after['owner_process'] and after['owner_process']
            assert before['enclosures'] == after['enclosures'] == 1
            assert c.launch.run_id == initial_run
            with c.db() as db:
                from acc.server.models import RelayOwnership
                assert not db.get(RelaySession, SID).sealed
                assert db.get(RelayOwnership, SID).phase == 'owned'
            record_property('empty_first_scope', 'pre-stop refusal preserves original SDK process/run; first real person turn then permits same-id resume')
        # Actual cookie/CSRF-authenticated public input and observer, no HTTP route mocks.
        from dataclasses import replace

        import httpx
        from fastapi import FastAPI
        from test_session_relay_public_writer import attach, call

        from acc.server import auth
        from acc.server.api.context import Context
        from acc.server.api.session_relay import router
        from acc.server.models import User
        context = Context(replace(c.ctx.settings, cookie_secure=False, allowed_origins=('http://fixture',)),
            c.db, c.ctx.supervisor, c.ctx.broker, None, 'fixture-api-token')
        context.relay_runtime = c.manager
        c.manager.ctx = c.launch.ctx = c.launch.owner_hub.ctx = context
        c.ctx = context
        @write_intent
        def login():
            with c.db() as db:
                token, csrf = auth.create_session(db, db.get(User, 1), 'fixture')
                db.commit()
                return token, csrf
        token, csrf = login()
        app = FastAPI()
        app.include_router(router(context))
        params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'First isolated fixture turn',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
        async def unexpected_native_exit(client, view=None, control=None):
            before = await inspect('state')
            run_id = c.launch.run_id
            helper = scenario.startswith('http-helper-')
            assert (await inspect('kill_owner_supervisor' if helper else 'kill_native_owner'))['runId'] == str(run_id)
            for _ in range(500):
                exited = await inspect('owner_exit')
                if (not exited['root_live'] if helper else exited['empty'] and (view is not None or exited['done'])):
                    break
                await asyncio.sleep(.02)
            assert exited['empty'] is (not helper), exited
            if view is not None:
                import base64
                response = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                    'frameSeq': 3, 'data': base64.b64encode(b'Never replay after native death').decode()})
                assert 'error' in response, response
            else:
                params = {'sessionId': SID, 'expectedRunId': str(run_id), 'text': 'Never replay after native death',
                    'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
                registration = await call(client, 'operation.register', {'expectedRunId': str(run_id),
                    'registrationKey': 'after-native-death', 'method': 'turn.submit',
                    'payloadDigest': p.operation_digest('turn.submit', params)})
                assert 'result' in registration, registration
                await call(client, 'turn.submit', {**params, 'operationId': registration['result']['operationId']})
                with c.db() as db:
                    from acc.server.models import RelayOperation
                    assert db.get(RelayOperation, registration['result']['operationId']).receipt['state'] != 'accepted'
            after = await inspect('state')
            assert not (await inspect('permission_state'))['marker_exists']
            assert c.launch.run_id == run_id and after['view'] == before['view']
            assert after['owner_process'] == before['owner_process'] and after['enclosures'] == before['enclosures'] == 1
            assert after['active'] == 1  # no replacement enrollment or launch
            assert after['sdk_queries'] == before['sdk_queries']
            await inspect('release_model')
            await c.manager.close()
            await inspect('finish')
            result = await task
            expected_trees = 2 if view is not None else 1
            assert len(result['tui']['empty_tree_records']) == expected_trees - int(helper)
            assert result['tui']['unknown_tree_quarantined'] is helper
            assert all(row['empty'] for row in result['tui']['empty_tree_records'])
            requests = json.dumps(result['tui']['requests'])
            assert 'Never replay after native death' not in requests
            record_property('native_unexpected_exit', f'{scenario}: exact owned native child SIGKILL, supervisor empty-tree receipt; fresh input refuses/no provider replay or replacement; {expected_trees} empty trees')

        if scenario.startswith('http-sdk-interrupt'):
            from acc.server.models import RelayOperation
            monkeypatch.setattr('acc.server.session_relay.interrupts.PUBLIC_CLAUDE_INTERRUPTS_QUALIFIED', True)
            @write_intent
            def interrupt_capability():
                with c.db() as db:
                    row = db.get(RelaySession, SID)
                    row.capabilities = {**row.capabilities, 'interrupt': 'qualified'}
                    revision = row.control_revision
                    db.commit()
                    return revision
            revision = interrupt_capability()
            await inspect('hold_long_public_model')
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                await attach(client, 'native-sdk-interrupt', ['queue', 'writer'])
                params['expectedControlRevision'] = revision
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'native-sdk-active', 'method': 'turn.submit', 'payloadDigest': p.operation_digest('turn.submit', params)})
                active_id = registered['result']['operationId']
                first = await call(client, 'turn.submit', {**params, 'operationId': active_id})
                assert first['result']['state'] == 'uncertain', first
                for _ in range(500):
                    if (await inspect('state'))['model_held']:
                        break
                    await asyncio.sleep(.01)
                assert (await inspect('state'))['model_held']
                payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'turnId': active_id,
                    'expectedControlRevision': revision}
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'native-sdk-interrupt', 'method': 'turn.interrupt', 'payloadDigest': p.operation_digest('turn.interrupt', payload)})
                identifier = registered['result']['operationId']
                request = {**payload, 'operationId': identifier}
                if scenario.endswith('-revoke'):
                    await inspect('sdk_interrupt_lock')
                    pending = asyncio.create_task(call(client, 'turn.interrupt', request))
                    for _ in range(500):
                        if (await inspect('state'))['sdk_interrupts']:
                            break
                        await asyncio.sleep(.01)
                    assert (await inspect('state'))['sdk_interrupts']
                    @write_intent
                    def revoke_interrupt():
                        with c.db() as db:
                            db.get(RelaySession, SID).control_revision += 1
                            db.commit()
                    revoke_interrupt()
                    await inspect('sdk_interrupt_unlock')
                    reply = await pending
                    assert 'error' in reply or reply['result']['state'] == 'failed', reply
                else:
                    original_call = c.launch.peer.call
                    async def lost(method, values):
                        result = await original_call(method, values)
                        if method == 'dispatch':
                            raise ConnectionError('Artificial lost actual native broker answer')
                        return result
                    if scenario.endswith('-lost'):
                        c.launch.peer.call = lost
                    reply = await call(client, 'turn.interrupt', request)
                    assert reply['result']['state'] == ('uncertain' if scenario.endswith('-lost') else 'accepted'), reply
                    if scenario.endswith('-lost'):
                        observed = await client.get(f'/api/sessions/{SID}/relay/operations/{identifier}')
                        assert observed.status_code == 200 and observed.json()['state'] == 'accepted', observed.text
                        settled = observed.json()
                    else:
                        settled = reply['result']
                    assert settled['actor'] == first['result']['actor'] and settled['payloadDigest'] == p.operation_digest('turn.interrupt', payload)
                    assert (await call(client, 'turn.interrupt', request))['result'] == settled
                final = await inspect('state')
                writes = sum(record['writeCompleted'] for record in final['sdk_interrupts'])
                assert writes == (0 if scenario.endswith('-revoke') else 1), final['sdk_interrupts']
                assert final['sdk_queries'] == 1 and final['enclosures'] == 1
                with c.db() as db:
                    assert db.get(RelayOperation, identifier).receipt['state'] == ('failed' if scenario.endswith('-revoke') else 'accepted')
                await inspect('release_model')
                recovery = None
                if scenario.endswith('-recovery'):
                    from relay_sdk_recovery import fresh_session_after_interrupt
                    recovery = await fresh_session_after_interrupt(c, inspect, request, settled)
            await c.manager.close()
            await inspect('finish')
            result = await task
            assert len(result['tui']['empty_tree_records']) == (2 if recovery else 1)
            assert all(record['empty'] for record in result['tui']['empty_tree_records'])
            assert sum('First isolated fixture turn' in item.get('current_fixture_markers', []) for item in result['tui']['requests'] if item['tool_count'] > 0) == 1
            record_property('claude_native_interrupt', json.dumps({'scenario': scenario, 'writes': writes,
                'queryCount': final['sdk_queries'], 'oneOriginalToolBearingRequest': True, 'freshSessionRecovery': recovery,
                'zeroToolRequests': sum(item['tool_count'] == 0 for item in result['tui']['requests']), 'emptyTrees': 2 if recovery else 1,
                'cli_version': result['cli_version'], 'cli_digest': result['cli_digest'], 'terminalMediated': False}))
            return

        if http_case:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                await attach(client, 'actual-native-first-chat', ['queue'])
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'create-native-http-history', 'method': 'turn.submit',
                    'payloadDigest': p.operation_digest('turn.submit', params)})
                assert 'result' in registered, registered
                if scenario == 'http-sdk-allow-revoke':
                    if getattr(c, 'native_guard_exit', False):
                        await inspect('kill_next_installed_guard')
                    await inspect('request_public_permission')
                    if getattr(c, 'native_sdk_metadata_fault', None):
                        await inspect('sdk_metadata_' + c.native_sdk_metadata_fault)
                if scenario in ('http-sdk-partial', 'http-sdk-lost-dispatch-ack'):
                    before = await inspect('state')
                    original_call = c.launch.peer.call
                    dispatches = 0
                    if scenario == 'http-sdk-partial':
                        await inspect('sdk_partial_write')
                    else:
                        async def lose_dispatch(method, payload):
                            nonlocal dispatches
                            result = await original_call(method, payload)
                            if method == 'dispatch':
                                dispatches += 1
                                raise ConnectionError('Isolated SDK dispatch ACK loss')
                            return result
                        c.launch.peer.call = lose_dispatch
                    try:
                        submitted = await call(client, 'turn.submit', {**params, 'operationId': registered['result']['operationId']})
                    finally:
                        c.launch.peer.call = original_call
                    if scenario == 'http-sdk-lost-dispatch-ack':
                        for _ in range(500):
                            with c.db() as db:
                                from acc.server.models import RelayOperation
                                receipt = db.get(RelayOperation, registered['result']['operationId']).receipt
                            if receipt['state'] == 'accepted':
                                break
                            await asyncio.sleep(.02)
                        assert receipt['state'] == 'accepted' and dispatches == 1
                    else:
                        with c.db() as db:
                            from acc.server.models import RelayOperation
                            assert db.get(RelayOperation, registered['result']['operationId']).receipt['state'] != 'accepted'
                    after = await inspect('state')
                    assert after['sdk_queries'] == before['sdk_queries'] + 1
                    assert after['enclosures'] == 1 and after['owner_process'] == before['owner_process']
                    await call(client, 'turn.submit', {**params, 'operationId': registered['result']['operationId']})
                    assert (await inspect('state'))['sdk_queries'] == after['sdk_queries']
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 1
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    if scenario == 'http-sdk-partial':
                        assert not result['tui']['requests']
                    else:
                        assert sum('First isolated fixture turn' in row.get('current_fixture_markers', [])
                            for row in result['tui']['requests'] if row.get('tool_count')) == 1
                    record_property('native_sdk_transport', f'{scenario}: actual SDK stdin/dispatch ACK fault, original receipt truthful, duplicate cannot send again, same owner/one query attempt,1 empty tree')
                    return
                if scenario == 'http-exit-sdk-busy':
                    await inspect('hold_public_model')
                if scenario == 'http-submit-revoke':
                    before = await inspect('state')
                    original = c.launch.peer.handle
                    seen = False
                    async def revoke_submit(method, payload):
                        nonlocal seen
                        result = await original(method, payload)
                        if method == 'authority.verify' and not seen:
                            seen = True
                            @write_intent
                            def revoke():
                                with c.db() as db:
                                    auth.end_session(db, token)
                                    db.commit()
                            revoke()
                        return result
                    c.launch.peer.handle = revoke_submit
                    try:
                        submitted = await client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'revoked-sdk-submit', 'method': 'ry/turn.submit',
                            'params': {**params, 'operationId': registered['result']['operationId']}})
                        assert submitted.status_code in (200, 401, 403), submitted.text
                        assert submitted.status_code != 200 or 'error' in submitted.json(), submitted.text
                    finally:
                        c.launch.peer.handle = original
                    after = await inspect('state')
                    assert seen and after['owner_process'] == before['owner_process']
                    assert after['hook_events'] == before['hook_events']
                    with c.db() as db:
                        from acc.server.models import RelayOperation
                        receipt = db.get(RelayOperation, registered['result']['operationId']).receipt
                        assert receipt['state'] != 'accepted'
                    assert (await client.get(f'/api/sessions/{SID}/relay')).status_code == 401
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 1
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    assert not result['tui']['requests']
                    record_property('native_inflight_sdk_revocation', 'real cookie revoked after first exact dispatch authority verifier returns true; later checks refuse SDK send, no prompt hook/model request; same original SDK process, no accepted receipt,1 empty tree')
                    return
                submitted = await call(client, 'turn.submit', {**params, 'operationId': registered['result']['operationId']})
                assert 'result' in submitted, submitted
                if scenario == 'http-sdk-allow-revoke' and getattr(c, 'native_guard_exit', False):
                    for _ in range(500):
                        state = await inspect('state')
                        with c.db() as db:
                            active = db.get(RelaySession, SID).active_operation_id
                        if active is None and state['guard_helper_kills'] == 1:
                            break
                        await asyncio.sleep(.02)
                    assert active is None and state['guard_helper_kills'] == 1
                    assert not (await inspect('permission_state'))['marker_exists']
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 1
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    record_property('native_guard_helper_loss', 'actual broker-created installed guard SIGKILL before stdin; native SDK tool denied before permission/effect; one helper killed/one empty tree')
                    return
                if scenario == 'http-sdk-allow-revoke':
                    from sqlalchemy import select

                    from acc.server.models import RelayDecision, RelayOperation
                    for _ in range(500):
                        with c.db() as db:
                            pending = db.scalar(select(RelayDecision).where(RelayDecision.session_id == SID,
                                RelayDecision.run_id == c.launch.run_id))
                            decision = dict(pending.decision) if pending else None
                        if decision and decision['status'] == 'pending':
                            break
                        await asyncio.sleep(.02)
                    assert decision and decision['status'] == 'pending'
                    if getattr(c, 'native_pending_exit', False):
                        await unexpected_native_exit(client)
                        return
                    await attach(client, 'sdk-allow-reviewer', ['decisions'])
                    choice = next(choice['id'] for choice in decision['choices']
                        if choice['effect'] == getattr(c, 'native_decision_effect', 'allow_once'))
                    answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'decisionId': decision['decisionId'],
                        'choiceId': choice, 'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                    registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'sdk-revoked-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', answer)})
                    assert 'result' in registration, registration
                    before = await inspect('state')
                    original = c.launch.peer.handle
                    seen = False
                    decision_checks = 0
                    positive = getattr(c, 'native_decision_fault', None) == 'positive' and not getattr(c, 'native_sdk_metadata_fault', None)
                    async def revoke_allow(method, payload):
                        nonlocal seen, decision_checks
                        result = await original(method, payload)
                        if method == 'authority.answer_verify':
                            decision_checks += 1
                        if method == 'authority.answer_verify' and decision_checks == getattr(c, 'native_decision_verify_index', 1):
                            seen = True
                            @write_intent
                            def revoke():
                                with c.db() as db:
                                    if getattr(c, 'native_decision_fault', None) is None:
                                        auth.end_session(db, token)
                                        db.commit()
                                if getattr(c, 'native_decision_fault', None) is not None:
                                    from relay_decision_faults import change_decision_scope
                                    change_decision_scope(c, SID, decision['decisionId'], c.native_decision_fault)
                            revoke()
                        return result
                    c.launch.peer.handle = revoke_allow
                    try:
                        response = await client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'revoked-sdk-allow', 'method': 'ry/decision.reply',
                            'params': {**answer, 'operationId': registration['result']['operationId']}})
                        assert response.status_code in (200, 401, 403, 409), response.text
                        if not positive and response.status_code == 200:
                            value = response.json()
                            assert 'error' in value or value.get('result') == {'outcome': 'unknown'}, value
                    finally:
                        c.launch.peer.handle = original
                    assert seen
                    if getattr(c, 'native_decision_fault', None) is not None:
                        assert c.native_scope_changed == c.native_decision_fault
                    for _ in range(500 if positive else 1):
                        with c.db() as db:
                            state = db.get(RelayOperation, registration['result']['operationId']).receipt['state']
                        if state == 'accepted' or not positive:
                            break
                        await asyncio.sleep(.02)
                    assert (await inspect('permission_state'))['marker_exists'] is (positive and getattr(c, 'native_decision_effect', 'allow_once') == 'allow_once')
                    after = await inspect('state')
                    assert before['owner_process'] == after['owner_process'] and after['sdk_queries'] == 1
                    if getattr(c, 'native_sdk_metadata_fault', None):
                        assert after['sdk_metadata_changes'] == 1
                    with c.db() as db:
                        receipt = db.get(RelayOperation, registration['result']['operationId']).receipt
                        assert (receipt['state'] == 'accepted') is positive
                        assert receipt['actor']['principal'] == 'alex'
                    await c.manager.close()
                    assert (await inspect('permission_state'))['marker_exists'] is (positive and getattr(c, 'native_decision_effect', 'allow_once') == 'allow_once')
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 1
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    record_property('native_sdk_allow_revocation', f'{getattr(c, "native_decision_fault", "cookie")}: final reserved-answer checks; expected_native_allow={positive}, original actor/op retained, one query/one empty tree')
                    return
                if scenario == 'http-exit-sdk-busy':
                    for _ in range(500):
                        held = await inspect('state')
                        if held['model_held']:
                            break
                        await asyncio.sleep(.02)
                    assert held['model_held']
                    await unexpected_native_exit(client)
                    return
        else:
            registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'create-native-history',
                'turn.submit', p.operation_digest('turn.submit', params))
            await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
        for _ in range(500):
            with c.db() as db:
                active = db.get(RelaySession, SID).active_operation_id
            if active is None:
                break
            await asyncio.sleep(.03)
        assert active is None
        if getattr(c, 'native_history_fault', None):
            from relay_native_history_faults import qualify_history_fault
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                before = await inspect('state')
                await qualify_history_fault(c, client, SID, c.native_history_fault)
                after = await inspect('state')
                assert before['owner_process'] == after['owner_process'] and after['sdk_queries'] == 1
            await c.manager.close()
            await inspect('finish')
            result = await task
            assert len(result['tui']['empty_tree_records']) == 1 and all(row['empty'] for row in result['tui']['empty_tree_records'])
            record_property('native_history_recovery', f'{c.native_history_fault}: stale cursor/epoch refuses, fresh public read attachment recovers history without launch/input/receipt replay; same actual SDK owner')
            return
        if scenario in ('http-exit-sdk', 'http-helper-sdk'):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                await attach(client, 'native-exit-sdk-reader', ['queue'])
                await unexpected_native_exit(client)
            return
        async def owner_hint(client, attachment, *, idle, blocker=None):
            deadline = asyncio.get_running_loop().time() + 5
            while True:
                response = await client.get(f'/api/sessions/{SID}/relay/ownership', params={
                    'attachmentId': attachment, 'expectedRunId': str(c.launch.run_id)})
                assert response.status_code == 200, response.text
                result = response.json()
                if not idle or result['selector']['idle_verified'] or asyncio.get_running_loop().time() >= deadline:
                    break
                # Native idle can precede final hook/receipt reconciliation.
                # Start from public proven idle, never the internal hint alone.
                await asyncio.sleep(.01)
            assert result['selector']['idle_verified'] is idle, result
            if blocker:
                assert blocker in result['selector']['blockers']
            return result

        async def http_switch(client, target, key, *, expected='accepted'):
            attached = await attach(client, key + '-view', ['switch'])
            if scenario == 'http-warm':
                await owner_hint(client, attached['attachmentId'], idle=True)
            async def mutate(method, payload, suffix):
                params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), **payload}
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': key + suffix, 'method': method, 'payloadDigest': p.operation_digest(method, params)})
                assert 'result' in registered, registered
                reply = await call(client, method, {**params, 'operationId': registered['result']['operationId']})
                if expected != 'accepted' and method == 'switch.commit':
                    code = 'POLICY_REFUSED' if expected == 'failed' else 'OUTCOME_UNKNOWN'
                    assert reply['error']['data']['code'] == code, reply
                    retained = await client.get(f"/api/sessions/{SID}/relay/operations/{registered['result']['operationId']}")
                    assert retained.status_code == 200, retained.text
                    receipt = retained.json()
                    assert receipt['state'] == expected
                    assert receipt['operationId'] == registered['result']['operationId']
                    if expected == 'uncertain':
                        assert reply['error']['data']['receipt']['operationId'] == receipt['operationId']
                    return {'state': expected, 'operationId': receipt['operationId']}
                assert 'result' in reply, reply
                receipt = (reply['result']['receipt'] if method == 'switch.commit' else
                    (await call(client, 'operation.get', {'operationId': registered['result']['operationId']}))['result'])
                assert receipt['state'] == 'accepted'
                assert receipt['operationId'] == registered['result']['operationId']
                return reply['result']
            with c.db() as db:
                revision = db.get(RelaySession, SID).revision
            plan = await mutate('switch.prepare', {'target': target, 'expectedRevision': revision}, '-prepare')
            owned = await mutate('switch.commit', {'planId': plan['planId'], 'planDigest': plan['planDigest'],
                'expectedRevision': plan['revision']}, '-commit')
            if expected != 'accepted':
                assert owned['state'] == expected
                return {**owned, 'planId': plan['planId'], 'planDigest': plan['planDigest'], 'revision': plan['revision']}
            assert owned['view'] == target
            assert 'error' in await call(client, 'session.snapshot', {})
            return owned

        if http_case:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                if scenario in ('http-post-stop', 'http-post-owned'):
                    fault_phase = 'starting' if scenario == 'http-post-stop' else 'owned'
                    enclosures = 1 if scenario == 'http-post-stop' else 2
                    old_run = c.launch.run_id
                    original = c.launch.peer.handle
                    seen = False
                    async def lose_after_exit(method, params):
                        nonlocal seen
                        if method == 'authority.switch_checkpoint' and params['phase'] == fault_phase and not seen:
                            seen = True
                            if fault_phase == 'starting':
                                assert params['evidence']['exit'] == 'exited'
                            else:
                                assert params['evidence']['process_identity']
                                assert params['evidence']['run_id'] != str(old_run)
                            @write_intent
                            def clear():
                                with c.db() as db:
                                    db.get(RelaySession, SID).capabilities = {}
                                    db.commit()
                            clear()
                        return await original(method, params)
                    c.launch.peer.handle = lose_after_exit
                    try:
                        outcome = await http_switch(client, 'terminal', 'native-http-' + scenario, expected='uncertain')
                    finally:
                        c.launch.peer.handle = original
                    assert seen and c.launch.run_id == old_run
                    with c.db() as db:
                        from acc.server.models import RelayOwnership
                        assert db.get(RelaySession, SID).sealed
                        assert db.get(RelayOwnership, SID).phase == 'recovery_required'
                    assert c.launch.terminal_grants.record is None
                    # Same old attachment cannot retry the dispatched plan, take
                    # input, or synthesize a replacement after source exit.
                    assert 'error' in await call(client, 'switch.commit', {'expectedRunId': str(old_run),
                        'planId': outcome['planId'], 'planDigest': outcome['planDigest'],
                        'expectedRevision': outcome['revision'], 'operationId': outcome['operationId']})
                    state = await inspect('state')
                    # The factory retains only its current enclosure handle;
                    # persistent per-run exit records below count prior owners.
                    assert state['enclosures'] == 1
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == enclosures
                    assert all(record['empty'] is True for record in result['tui']['empty_tree_records'])
                    assert result['tui']['terminalMediated'] is False
                    record_property('native_post_stop_scope', f'actual SDK whole-tree exit; capability removed before {fault_phase} checkpoint; {enclosures} whole-tree exit receipts, sealed recovery_required, original uncertain HTTP receipt retained and exact retry refused; zero automatic resume')
                    return
                if scenario == 'http-withdrawal':
                    before = await inspect('state')
                    old_run = c.launch.run_id
                    original = c.launch.peer.handle
                    seen = False
                    async def withdraw(method, params):
                        nonlocal seen
                        if method == 'authority.switch_checkpoint' and params['phase'] == 'stopping' and not seen:
                            seen = True
                            @write_intent
                            def clear():
                                with c.db() as db:
                                    db.get(RelaySession, SID).capabilities = {}
                                    db.commit()
                            clear()
                        return await original(method, params)
                    c.launch.peer.handle = withdraw
                    try:
                        await http_switch(client, 'terminal', 'native-http-withdraw', expected='failed')
                    finally:
                        c.launch.peer.handle = original
                    after = await inspect('state')
                    assert seen and c.launch.run_id == old_run
                    assert before['owner_process'] == after['owner_process'] and after['owner_process']
                    assert before['enclosures'] == after['enclosures'] == 1
                    assert before['view'] == after['view'] == 'chat'
                    qualify()
                    record_property('native_post_await_withdrawal', 'switch capability removed after actual native idle preparation, before stopping checkpoint; failed exact receipt, original process/run/enclosure preserved; fresh later plan succeeds')
                if scenario == 'http-lost-reply':
                    original_call = c.launch.peer.call
                    dispatches = 0
                    async def lose(method, params):
                        nonlocal dispatches
                        result = await original_call(method, params)
                        if method == 'owner.select':
                            dispatches += 1
                            raise ConnectionError('Injected lost answer after real native owned checkpoint')
                        return result
                    c.launch.peer.call = lose
                    try:
                        await http_switch(client, 'terminal', 'native-http-terminal')
                    finally:
                        c.launch.peer.call = original_call
                    assert dispatches == 1
                    record_property('native_lost_commit_reply', 'real native owned checkpoint accepted; injected lost response observed through original receipt, one dispatch/no replay; same-ID Terminal input and later Chat continue')
                else:
                    await http_switch(client, 'terminal', 'native-http-terminal')
        else:
            await switch('terminal', 'native-production-terminal')
        qualify()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
            headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
            view = await attach(client, 'actual-native-public-writer', ['writer', 'terminal',
                *(['decisions'] if scenario == 'http-permission' else [])])
            payload = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                'attachmentId': view['attachmentId'], 'expectedRevision': view['snapshot']['controlRevision']}
            registered = (await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                'registrationKey': 'actual-native-public-claim', 'method': 'control.claim',
                'payloadDigest': p.operation_digest('control.claim', payload)}))['result']
            claimed = await call(client, 'control.claim', {**payload, 'operationId': registered['operationId']})
            assert 'result' in claimed, claimed
            control = claimed['result']
            assert control['receipt']['state'] == 'accepted'
            if getattr(c, 'native_observer_capacity', False):
                before = await inspect('state')
                stats = await inspect('observer_capacity')
                after = await inspect('state')
                assert stats['cap'] == 16 and stats['recovered']
                assert before['owner_process'] == after['owner_process'] and before['hook_events'] == after['hook_events']
                assert before['processed'] == after['processed'] == 0
                assert before['view'] == after['view'] == 'terminal'
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2 and all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_observer_capacity', json.dumps(stats))
                return
            if scenario == 'http-stop':
                from acc.server.session_relay.hub import seal_for_stop
                seal_for_stop(c.ctx, SID)
                assert await c.manager.stop(SID) is True
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(record['empty'] for record in result['tui']['empty_tree_records'])
                assert result['tui']['terminalMediated'] is False
                record_property('native_stop_scope', 'same-ID public HTTP Chat/Terminal and real claim; source/target whole-tree cleanup; broker Stop returns conclusive exited after proof descriptors close')
                return
            import base64
            if scenario in ('http-partial-header', 'http-partial-body', 'http-read-eof'):
                kind = scenario.removeprefix('http-')
                await inspect('transport_fault_' + kind)
                import base64
                frame = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                    'inputEpoch': control['inputEpoch'], 'frameSeq': 1,
                    'data': base64.b64encode(b'Never submit an incomplete frame').decode()}
                reply = await call(client, 'terminal.input', frame)
                assert 'error' in reply, reply
                with c.db() as db:
                    assert db.get(RelaySession, SID).sealed
                assert 'error' in await call(client, 'terminal.input', frame)
                assert 'error' in await call(client, 'terminal.drain', {
                    'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                state = await inspect('state')
                assert state['processed'] == 0
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                assert not any('Native TUI isolated fixture turn' in row.get('current_fixture_markers', [])
                    for row in result['tui']['requests'])
                record_property('native_partial_transport', f'{kind}: actual owned socket cut/EOF; sealed partial/unknown, unchanged owner until explicit EOF cleanup, old frame/drain refuses; no native submit/replay,2 empty trees')
                return
            if scenario in ('http-input-revoke', 'http-input-late-revoke'):
                before = await inspect('state')
                original = c.launch.peer.handle
                seen = False
                input_checks = 0
                async def revoke_after_check(method, params):
                    nonlocal seen, input_checks
                    result = await original(method, params)
                    if method == 'authority.terminal' and params.get('action') == 'input':
                        input_checks += 1
                    if method == 'authority.terminal' and params.get('action') == 'input' and input_checks == (3 if scenario == 'http-input-late-revoke' else 1):
                        seen = True
                        @write_intent
                        def revoke():
                            with c.db() as db:
                                auth.end_session(db, token)
                                db.commit()
                        revoke()
                    return result
                c.launch.peer.handle = revoke_after_check
                try:
                    response = await client.post(f'/api/sessions/{SID}/relay', json={
                        'jsonrpc': '2.0', 'id': 'revoked-native-frame', 'method': 'ry/terminal.input',
                        'params': {'sessionId': SID, 'attachmentId': view['attachmentId'],
                            'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                            'frameSeq': 1, 'data': base64.b64encode(b'Never admit revoked native input').decode()}})
                    assert response.status_code in (200, 401, 403), response.text
                    assert response.status_code != 200 or 'error' in response.json(), response.text
                finally:
                    c.launch.peer.handle = original
                after = await inspect('state')
                assert seen and before['owner_process'] == after['owner_process']
                assert after['processed'] == 0
                assert after['admitted'] == (1 if scenario == 'http-input-late-revoke' else 0)
                assert after['hook_events'] == before['hook_events']
                if scenario == 'http-input-revoke':
                    assert 'Never admit revoked native input' not in (await inspect('screen'))['text']
                assert (await client.get(f'/api/sessions/{SID}/relay')).status_code == 401
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_inflight_revocation', f'real cookie revoked after Terminal authority check {input_checks}; no accepted frame/execution; admitted{after["admitted"]}/processed0 truthfully retained, unchanged owner/hooks, EOF cleans2 trees')
                return
            if scenario in ('native-busy', 'http-exit-tui-busy'):
                await inspect('hold_model')
            if permission_case:
                assert not (await inspect('permission_state'))['marker_exists']
                if getattr(c, 'native_guard_exit', False):
                    await inspect('kill_next_installed_guard')
                await inspect('request_permission')
            if scenario == 'native-background':
                await inspect('request_background')
            for sequence, data in ((1, b'Native TUI isolated fixture turn'), (2, b'\r')):
                if scenario == 'http-lost-input-ack' and sequence == 2:
                    stream = c.launch.terminal_grants.record
                    await inspect('transport_fault_lost-ack')
                    frame = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                        'inputEpoch': control['inputEpoch'], 'frameSeq': sequence, 'data': base64.b64encode(data).decode()}
                    reply = await call(client, 'terminal.input', frame)
                    assert 'error' in reply, reply
                    assert c.launch.terminal_grants.record is None and stream.phase == 'revoked'
                    assert stream.processed == 2 and stream.native is not None
                    with c.db() as db:
                        from acc.server.models import RelayOperation
                        assert db.get(RelaySession, SID).sealed
                        receipt = db.get(RelayOperation, stream.native.operation_id).receipt
                        assert receipt['state'] == 'accepted' and receipt['actor']['principal'] == 'alex'
                    assert 'error' in await call(client, 'terminal.input', frame)
                    assert 'error' in await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'],
                        'inputGrant': control['inputGrant']})
                    await c.manager.close()
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 2
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    assert sum('Native TUI isolated fixture turn' in row.get('current_fixture_markers', [])
                        for row in result['tui']['requests']) == 1
                    record_property('native_lost_input_ack', 'real TUI provider admission/receipt/processed2 precedes deliberately lost Host ACK; original receipt remains accepted, stream sealed, old frame/drain refuses, exactly one native prompt,2 empty trees')
                    return
                reply = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                    'frameSeq': sequence, 'data': base64.b64encode(data).decode()})
                assert reply['result']['watermark']['admitted'] == sequence
                if sequence == 1:
                    drained = await call(client, 'terminal.drain', {
                        'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
                    assert drained['result'] == {'epoch': control['inputEpoch'], 'admitted': 1, 'processed': 0}
                    if scenario == 'http-warm':
                        await owner_hint(client, view['attachmentId'], idle=False, blocker='terminal_draft')
                    await asyncio.sleep(.7)
            if permission_case and getattr(c, 'native_guard_exit', False):
                for _ in range(500):
                    state = await inspect('state')
                    if state['processed'] == 2 and state['guard_helper_kills'] == 1:
                        break
                    await asyncio.sleep(.02)
                assert state['processed'] == 2 and state['idle'] and state['guard_helper_kills'] == 1
                assert not (await inspect('permission_state'))['marker_exists']
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_guard_helper_loss', 'actual broker-created installed guard SIGKILL before stdin; native TUI tool denied before permission/effect; one helper killed/two empty trees')
                return
            if permission_case:
                from sqlalchemy import select

                from acc.server.models import RelayDecision, RelayOperation
                for _ in range(400):
                    with c.db() as db:
                        pending = db.scalar(select(RelayDecision).where(RelayDecision.session_id == SID,
                            RelayDecision.run_id == c.launch.run_id))
                        decision = dict(pending.decision) if pending else None
                    if decision and decision['status'] == 'pending':
                        break
                    await asyncio.sleep(.02)
                assert decision and decision['status'] == 'pending', json.dumps(await inspect('state'))
                if getattr(c, 'native_pending_exit', False):
                    await unexpected_native_exit(client, view, control)
                    return
                if getattr(c, 'native_hook_exit', False):
                    before = await inspect('state')
                    assert (await inspect('kill_permission_hook'))['killed']
                    answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                        'decisionId': decision['decisionId'],
                        'choiceId': next(choice['id'] for choice in decision['choices'] if choice['effect'] == 'allow_once'),
                        'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                    registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'hook-lost-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', answer)})
                    assert 'result' in registration, registration
                    response = await call(client, 'decision.reply', {**answer, 'operationId': registration['result']['operationId']})
                    assert 'error' in response or response.get('result') == {'outcome': 'unknown'}, response
                    assert not (await inspect('permission_state'))['marker_exists']
                    assert 'error' in await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                        'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                        'frameSeq': 3, 'data': base64.b64encode(b'\r').decode()})
                    after = await inspect('state')
                    assert after['owner_process'] == before['owner_process'] and after['processed'] == 0
                    with c.db() as db:
                        receipt = db.get(RelayOperation, registration['result']['operationId']).receipt
                        assert receipt['state'] != 'accepted' and receipt['actor']['principal'] == 'alex'
                    await c.manager.close()
                    assert not (await inspect('permission_state'))['marker_exists']
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 2
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    record_property('native_permission_hook_loss', 'exact owned PermissionRequest helper SIGKILL; requested Allow stays unknown with no effect; input/Enter fenced while original ticket unresolved, no replay,2 empty trees')
                    return
                before_permission = await inspect('state')
                with pytest.raises((p.RelayError, ConnectionError)):
                    await switch('chat', 'native-permission-still-pending')
                assert before_permission['owner_process'] and (await inspect('state'))['owner_process'] == before_permission['owner_process']
                answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
                    'decisionId': decision['decisionId'], 'choiceId': 'decline',
                    'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
                if getattr(c, 'native_decision_fault', None) is not None:
                    from relay_decision_faults import change_decision_scope
                    answer['choiceId'] = next(choice['id'] for choice in decision['choices'] if choice['effect'] == getattr(c, 'native_decision_effect', 'allow_once'))
                    registration = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'native-await-scope-allow', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', answer)})
                    assert 'result' in registration, registration
                    positive = c.native_decision_fault == 'positive'
                    original = c.launch.peer.handle
                    seen = False
                    async def change_after_verify(method, payload):
                        nonlocal seen
                        result = await original(method, payload)
                        if method == 'authority.answer_verify' and not seen:
                            seen = True
                            change_decision_scope(c, SID, decision['decisionId'], c.native_decision_fault)
                        return result
                    c.launch.peer.handle = change_after_verify
                    try:
                        response = await client.post(f'/api/sessions/{SID}/relay', json={
                            'jsonrpc': '2.0', 'id': 'await-scope-allow', 'method': 'ry/decision.reply',
                            'params': {**answer, 'operationId': registration['result']['operationId']}})
                        assert response.status_code in (200, 401, 403, 409), response.text
                    finally:
                        c.launch.peer.handle = original
                    assert seen and c.native_scope_changed == c.native_decision_fault
                    for _ in range(500 if positive else 1):
                        with c.db() as db:
                            state = db.get(RelayOperation, registration['result']['operationId']).receipt['state']
                        if state == 'accepted' or not positive:
                            break
                        await asyncio.sleep(.02)
                    assert (await inspect('permission_state'))['marker_exists'] is (positive and getattr(c, 'native_decision_effect', 'allow_once') == 'allow_once')
                    with c.db() as db:
                        receipt = db.get(RelayOperation, registration['result']['operationId']).receipt
                        assert (receipt['state'] == 'accepted') is positive
                        assert receipt['actor']['principal'] == 'alex'
                    await c.manager.close()
                    assert (await inspect('permission_state'))['marker_exists'] is (positive and getattr(c, 'native_decision_effect', 'allow_once') == 'allow_once')
                    await inspect('finish')
                    result = await task
                    assert len(result['tui']['empty_tree_records']) == 2
                    assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                    record_property('native_decision_await_scope', f'{c.native_decision_fault}: expected_native_allow={positive}, reserved-answer checks after await, exact original actor/op and2 empty trees')
                    return
                if scenario == 'http-permission':
                    registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                        'registrationKey': 'native-public-person-decline', 'method': 'decision.reply',
                        'payloadDigest': p.operation_digest('decision.reply', answer)})
                    assert 'result' in registered, registered
                    operation_id = registered['result']['operationId']
                    replied = await call(client, 'decision.reply', {**answer, 'operationId': operation_id})
                    assert 'result' in replied, replied
                    outcome = replied['result']
                    record_property('native_public_decision', 'real cookie/CSRF attached public registration and one-shot decline reach actual TUI PermissionRequest; marked completed native denial settles original answer')
                else:
                    registration = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'native-person-decline',
                        'decision.reply', p.operation_digest('decision.reply', answer))
                    operation_id = registration.operation_id
                    outcome = await c.launch.answer(c.resolve, {**answer, 'operationId': operation_id})
                assert outcome == {'outcome': 'accepted'}  # exact marked completed native provider chain, not socket delivery
                with c.db() as db:
                    assert db.get(RelayOperation, operation_id).receipt['state'] == 'accepted'
                    assert db.get(RelayDecision, (SID, decision['decisionId'])).decision['status'] == 'denied'
                for _ in range(500):
                    stopped = await inspect('state')
                    if stopped['processed'] == 2 and stopped['idle']:
                        break
                    await asyncio.sleep(.02)
                assert stopped['processed'] == 2 and stopped['idle'], stopped
                assert not (await inspect('permission_state'))['marker_exists']
                # A confirmed denial no longer leaves an unresolved answer gate.
                # No new native input is sent by this proof.
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                hooks = result['tui']['hook_observations']
                assert any(event['event'] == 'PermissionRequest' for event in hooks)
                assert {event['native_id'] for event in hooks} == {NATIVE}
                assert result['tui']['terminalMediated'] is False
                record_property('native_permission_scope', 'actual TUI PermissionRequest blocks handover; person hub decline prevents marker write and real Stop/processing follows, exact marked completed native denial settles original answer; allow/SDK/lost-proof settlement remains unqualified')
                return
            if scenario == 'http-exit-tui-busy':
                for _ in range(500):
                    held = await inspect('state')
                    if held['model_held']:
                        break
                    await asyncio.sleep(.02)
                assert held['model_held'] and not held['idle']
                await unexpected_native_exit(client, view, control)
                return
            if scenario == 'native-busy':
                for _ in range(500):
                    before_busy = await inspect('state')
                    if before_busy['model_held']:
                        break
                    await asyncio.sleep(.02)
                assert before_busy['model_held'], before_busy
                held_run = str(c.launch.run_id)
                try:
                    with pytest.raises((p.RelayError, ConnectionError)):
                        await switch('chat', 'actual-native-work-still-active')
                    after_busy = await inspect('state')
                    assert before_busy['owner_process'] and before_busy['owner_process'] == after_busy['owner_process']
                    assert before_busy['enclosures'] == after_busy['enclosures'] == 1
                    assert after_busy['view'] == 'terminal' and after_busy['processed'] == 0
                    assert str(c.launch.run_id) == held_run
                    with c.db() as db:
                        from acc.server.models import RelayOwnership
                        assert not db.get(RelaySession, SID).sealed
                        assert db.get(RelayOwnership, SID).phase == 'owned'
                    record_property('native_busy_scope', 'real native model request held; Chat handover refuses without stopping or transferring the TUI; real Stop/drain then permits same-ID resume')
                finally:
                    await inspect('release_model')
            for _ in range(500):
                state = await inspect('state')
                if state['processed'] == 2:
                    break
                await asyncio.sleep(.03)
            assert state['processed'] == state['admitted'] == 2, state
            assert state['idle'] and not state['failed'] and state['native_transcript']
            if scenario.startswith('http-replaced-'):
                before = await inspect('state')
                old_run = c.launch.run_id
                await inspect('kill_native_owner')
                for _ in range(500):
                    if (await inspect('owner_exit'))['empty']:
                        break
                    await asyncio.sleep(.02)
                assert (await inspect('owner_exit'))['empty']
                await inspect('replace_owned_' + ('server' if scenario.endswith('server') else 'pane'))
                frame = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                    'inputEpoch': control['inputEpoch'], 'frameSeq': 3,
                    'data': base64.b64encode(b'Never write into replacement pane').decode()}
                assert 'error' in await call(client, 'terminal.input', frame)
                after = await inspect('state')
                assert after['owner_process'] == before['owner_process'] and c.launch.run_id == old_run
                assert after['admitted'] == after['processed'] == 2 and after['enclosures'] == 1
                assert (await inspect('replacement_state')) == {'live': True, 'forbidden_input': False}
                await c.manager.close()
                assert (await inspect('replacement_state')) == {'live': True, 'forbidden_input': False}
                await inspect('finish')
                result = await task
                assert result['tui']['unknown_tree_quarantined']
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_owner_replacement', f'{scenario}: old tree conclusively exits; actual tmux pane/server replaced under reused name, stale grant refuses; cleanup preserves replacement pane and retains old quarantine until isolated fixture cleanup')
                return
            if scenario.startswith('http-capacity-'):
                from relay_capacity_helpers import qualify_public_capacity
                before = await inspect('state')
                stats = await qualify_public_capacity(app, context.session_cookie, token, csrf, login,
                    call, SID, global_limit=scenario.endswith('global'), baseline=3)
                after = await inspect('state')
                assert before['owner_process'] == after['owner_process'] and after['enclosures'] == 1
                assert after['admitted'] == after['processed'] == 2
                response = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                    'frameSeq': 3, 'data': base64.b64encode(b'Visible capacity draft').decode()})
                assert response['result']['watermark'] == {'epoch': control['inputEpoch'], 'admitted': 3, 'processed': 2}
                await asyncio.sleep(.7)
                assert 'Visible capacity draft' in (await inspect('screen'))['text']
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                assert 'Visible capacity draft' not in json.dumps(result['tui']['requests'])
                record_property('native_public_capacity', json.dumps(stats))
                return
            if scenario.startswith('http-source-'):
                kind = scenario.removeprefix('http-source-')
                before = await inspect('state')
                operation = c.launch.terminal_grants.record.native.operation_id
                if kind == 'epoch':
                    @write_intent
                    def reset_epoch():
                        from acc.server.session_relay import history
                        with c.db() as db:
                            history.reset(db, db.get(RelaySession, SID))
                            db.commit()
                    reset_epoch()
                else:
                    await inspect('source_fault_' + kind)
                frame = {'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant'],
                    'inputEpoch': control['inputEpoch'], 'frameSeq': 3,
                    'data': base64.b64encode(b'Never write after source identity loss').decode()}
                response = await call(client, 'terminal.input', frame)
                assert 'error' in response, response
                after = await inspect('state')
                assert after['processed'] == after['admitted'] == 2
                assert before['owner_process'] == after['owner_process'] and after['view'] == 'terminal'
                with c.db() as db:
                    from acc.server.models import RelayOperation
                    assert db.get(RelayOperation, operation).receipt['state'] == 'accepted'
                assert 'error' in await call(client, 'terminal.input', frame)
                if kind == 'epoch':
                    assert 'error' in await call(client, 'session.snapshot', {})
                    reader = await attach(client, 'new-history-epoch-reader', ['terminal'])
                    screen = await client.get(f'/api/sessions/{SID}/relay/terminal', params={
                        'attachmentId': reader['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                    assert screen.status_code == 200, screen.text
                    assert 'Native TUI isolated fixture turn' in screen.json()['text']
                    assert 'error' in await call(client, 'terminal.drain', {'attachmentId': reader['attachmentId'],
                        'inputGrant': control['inputGrant']})
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                assert 'Never write after source identity loss' not in json.dumps(result['tui']['requests'])
                record_property('native_source_fault', f'{kind}: no next frame/native replay, prior accepted receipt preserved, original owner/run,2 empty trees; main epoch reset offers fresh read without borrowed input grant')
                return
            if scenario in ('http-exit-tui', 'http-helper-tui'):
                await unexpected_native_exit(client, view, control)
                return
            if scenario == 'http-read-work-success-race':
                await owner_hint(client, view['attachmentId'], idle=True)
                before = await inspect('state')
                await inspect('hold_model')
                import threading

                from acc.server.session_relay import claude_observer
                entered, release = asyncio.Event(), threading.Event()
                loop = asyncio.get_running_loop()
                original = claude_observer.verify
                checks = 0
                def pause_final_observation(run, token):
                    nonlocal checks
                    checks += 1
                    if checks == 3:
                        # Native RPC has completed both authority callbacks.
                        # Pause the main finishing worker before opening its
                        # DB scope, across the route's final to_thread await.
                        # Never turn the production five-second RPC bound into
                        # an artificial error by holding its callback for IO.
                        loop.call_soon_threadsafe(entered.set)
                        release.wait()  # the unconditional finally releases it
                    return original(run, token)
                monkeypatch.setattr(claude_observer, 'verify', pause_final_observation)
                read_task = asyncio.create_task(client.get(f'/api/sessions/{SID}/relay/ownership', params={
                    'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)}))
                try:
                    await asyncio.wait_for(entered.wait(), 4)
                    # One text-plus-Enter frame starts genuine native work.
                    # Production RPC/observation deadlines stay unchanged.
                    for sequence, data in ((3, b'Race turn\r'),):
                        response = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                            'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                            'frameSeq': sequence, 'data': base64.b64encode(data).decode()})
                        assert response['result']['watermark']['admitted'] == sequence
                    for _ in range(200):
                        busy = await inspect('state')
                        if not busy['idle']:
                            break
                        await asyncio.sleep(.01)
                    assert not busy['idle'] and busy['admitted'] == 3 and busy['processed'] == 2
                    release.set()
                    response = await read_task
                    assert response.status_code == 200, response.text
                    assert response.json()['selector']['idle_verified'] is False
                    assert before['owner_process'] == busy['owner_process'] and checks == 3
                    again = await client.get(f'/api/sessions/{SID}/relay/ownership', params={
                        'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
                    assert again.status_code == 200, again.text
                    assert again.json()['selector']['idle_verified'] is False
                finally:
                    release.set()
                    monkeypatch.setattr(claude_observer, 'verify', original)
                    await inspect('release_model')
                    await asyncio.gather(read_task, return_exceptions=True)
                for _ in range(500):
                    final = await inspect('state')
                    if final['processed'] == 3 and final['idle']:
                        break
                    await asyncio.sleep(.02)
                assert final['processed'] == final['admitted'] == 3 and final['idle']
                assert final['owner_process'] == before['owner_process']
                drained = await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant']})
                assert drained['result'] == {'epoch': control['inputEpoch'], 'admitted': 3, 'processed': 3}
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_final_await_work', 'positive idle read paused across final main worker await after native inspection; same person sends second real native turn; UserPromptSubmit changes ledger during await, idle false without revoking writer; actual completion/drain3 and2 empty trees')
                return
            if scenario == 'http-read-work-race':
                await owner_hint(client, view['attachmentId'], idle=True)
                before = await inspect('state')
                await inspect('hold_model')
                entered, release = asyncio.Event(), asyncio.Event()
                original = c.launch.peer.handle
                checks = 0
                async def pause_final_observation(method, params):
                    nonlocal checks
                    result = await original(method, params)
                    if method == 'authority.owner_observation':
                        checks += 1
                        if checks == 2:
                            entered.set()
                            await asyncio.wait_for(release.wait(), 4.5)
                    return result
                c.launch.peer.handle = pause_final_observation
                read_task = asyncio.create_task(client.get(f'/api/sessions/{SID}/relay/ownership', params={
                    'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)}))
                try:
                    await asyncio.wait_for(entered.wait(), 4)
                    with c.db() as db:
                        observed_revision = db.get(RelaySession, SID).revision
                    observed_sequence = c.launch.terminal_grants.record.sequence
                    for sequence, data in ((3, b'Race turn'), (4, b'\r')):
                        response = await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                            'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'],
                            'frameSeq': sequence, 'data': base64.b64encode(data).decode()})
                        assert response['result']['watermark']['admitted'] == sequence
                        if sequence == 3:
                            await asyncio.sleep(.7)
                    for _ in range(200):
                        busy = await inspect('state')
                        if not busy['idle']:
                            break
                        await asyncio.sleep(.01)
                    assert not busy['idle'] and busy['admitted'] == 4 and busy['processed'] == 2
                    with c.db() as db:
                        current_revision = db.get(RelaySession, SID).revision
                    record_property('observation_frame_race', json.dumps({
                        'observedRootRevision': observed_revision, 'currentRootRevision': current_revision,
                        'observedSequence': observed_sequence, 'currentSequence': c.launch.terminal_grants.record.sequence,
                        'processed': c.launch.terminal_grants.record.processed, 'nativeBusy': not busy['idle']}))
                    release.set()
                    response = await read_task
                    assert response.status_code == 200, response.text
                    assert response.json()['selector']['idle_verified'] is False
                    assert before['owner_process'] == busy['owner_process'] and checks == 2
                finally:
                    release.set()
                    c.launch.peer.handle = original
                    await inspect('release_model')
                    await asyncio.gather(read_task, return_exceptions=True)
                for _ in range(500):
                    final = await inspect('state')
                    if final['processed'] == 4 and final['idle']:
                        break
                    await asyncio.sleep(.02)
                assert final['processed'] == final['admitted'] == 4 and final['idle']
                assert final['owner_process'] == before['owner_process']
                drained = await call(client, 'terminal.drain', {'attachmentId': view['attachmentId'],
                    'inputGrant': control['inputGrant']})
                assert drained['result'] == {'epoch': control['inputEpoch'], 'admitted': 4, 'processed': 4}
                await c.manager.close()
                await inspect('finish')
                result = await task
                assert len(result['tui']['empty_tree_records']) == 2
                assert all(row['empty'] for row in result['tui']['empty_tree_records'])
                record_property('native_final_await_work', 'positive idle read paused at final authorization; same person sends second real native turn; UserPromptSubmit changes ledger during await, idle false without revoking writer; actual completion/drain4 and2 empty trees')
                return
            if scenario == 'native-background':
                assert not state['background_started'] and not state['background_finished'], state
                assert state['background'] == 0 and not state['background_results'], state
                record_property('native_background_scope', 'real native Bash run_in_background request denied before execution despite frozen allow; Stop/drain then permits same-ID handover; background completion remains unqualified')
            screen = await inspect('screen')
            assert 'Native TUI isolated fixture turn' in screen['text']
            drained = await call(client, 'terminal.drain', {
                'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
            assert drained['result'] == {'epoch': control['inputEpoch'], 'admitted': 2, 'processed': 2}
            writer_attachment = view['attachmentId']
            writer_connection = client.headers['X-ACC-Relay-Connection']
            terminal_run = str(c.launch.run_id)
            view = await attach(client, 'actual-native-public-observer')
            response = await client.get(f'/api/sessions/{SID}/relay/terminal', params={
                'attachmentId': view['attachmentId'], 'expectedRunId': str(c.launch.run_id)})
            assert response.status_code == 200, response.text
            reply = response.json()
            assert 'First isolated fixture turn' in reply['text']
            assert 'Native TUI isolated fixture turn' in reply['text']
            assert reply['runId'] == str(c.launch.run_id)
            assert (await call(client, 'session.snapshot', {}))['result']['terminalMediated'] is False
            # Same person, different read attachment: no borrowed writer authority.
            assert 'error' in await call(client, 'terminal.drain', {
                'attachmentId': view['attachmentId'], 'inputGrant': control['inputGrant']})
            assert 'error' in await call(client, 'terminal.input', {'attachmentId': view['attachmentId'],
                'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'], 'frameSeq': 3,
                'data': base64.b64encode(b'must not reach native').decode()})
            with c.db() as db:
                assert not db.get(RelaySession, SID).sealed
            if http_case:
                owned = await http_switch(client, 'chat', 'native-http-chat')
                assert owned['view'] == 'chat' and str(c.launch.run_id) != terminal_run
                assert 'error' in await call(client, 'session.snapshot', {})
                record_property('public_http_scope', 'cookie/CSRF registered native Chat-to-Terminal-to-Chat prepare/commit; same native ID; ended attachment rejected; new owner retains revocable cookie')
        assert not c.launch.terminal_reads.grants

        if not http_case:
            await switch('chat', 'native-production-chat')
        qualify()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
            headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf,
                'X-ACC-Relay-Connection': writer_connection}, cookies={context.session_cookie: token}) as old:
            assert str(c.launch.run_id) != terminal_run
            assert 'error' in await call(old, 'terminal.input', {'attachmentId': writer_attachment,
                'inputGrant': control['inputGrant'], 'inputEpoch': control['inputEpoch'], 'frameSeq': 3,
                'data': base64.b64encode(b'no old-run replay').decode()})
            assert 'error' in await call(old, 'terminal.drain', {
                'attachmentId': writer_attachment, 'inputGrant': control['inputGrant']})
            if scenario == 'http-old-run':
                before_mutations = await inspect('state')
                for method, values in (
                    ('terminal.resize', {'attachmentId': writer_attachment, 'connectionSeq': 3,
                        'geometryGrant': '0' * 64, 'cols': 80, 'rows': 24}),
                    ('terminal.attach', {'attachmentId': writer_attachment, 'connectionSeq': 4}),
                    ('session.attach', {'clientId': 'old-native-terminal', 'connectionSeq': 5}),
                    ('operation.register', {'expectedRunId': terminal_run, 'registrationKey': 'old-run-claim',
                        'method': 'control.claim', 'payloadDigest': p.sha256_digest(b'artificial stale claim')}),
                    ('operation.register', {'expectedRunId': terminal_run, 'registrationKey': 'old-run-take',
                        'method': 'writer.take', 'payloadDigest': p.sha256_digest(b'artificial stale take')})):
                    refused = await call(old, method, values)
                    assert 'error' in refused, (method, refused)
                after_mutations = await inspect('state')
                assert after_mutations['owner_process'] == before_mutations['owner_process']
                assert after_mutations['enclosures'] == before_mutations['enclosures'] == 1
                assert after_mutations['sdk_queries'] == before_mutations['sdk_queries']
                record_property('old_native_run_mutations', 'actual TUI exit and distinct SDK run: old input/drain/resize/attach/claim/take refuse; replacement process/enclosure/query counts unchanged; geometry remains unsupported')
        with c.db() as db:
            row = db.get(RelaySession, SID)
            params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'Second isolated fixture turn',
                'expectedConfigurationRevision': row.configuration_revision,
                'expectedControlRevision': row.control_revision, 'ifBusy': 'reject'}
        if http_case:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='http://fixture',
                headers={'Origin': 'http://fixture', 'X-ACC-CSRF': csrf}, cookies={context.session_cookie: token}) as client:
                await attach(client, 'actual-native-new-chat', ['queue', 'writer', 'terminal'] if scenario == 'http-old-run' else ['queue'])
                if scenario == 'http-old-run':
                    for method in ('turn.submit', 'control.claim', 'writer.take'):
                        refused = await call(client, 'operation.register', {'expectedRunId': terminal_run,
                            'registrationKey': 'fresh-person-old-run-' + method, 'method': method,
                            'payloadDigest': p.sha256_digest(b'artificial old-run payload')})
                        assert refused['error']['data']['code'] == 'STALE_RUN', refused
                registered = await call(client, 'operation.register', {'expectedRunId': str(c.launch.run_id),
                    'registrationKey': 'after-native-http-history', 'method': 'turn.submit',
                    'payloadDigest': p.operation_digest('turn.submit', params)})
                assert 'result' in registered, registered
                submitted = await call(client, 'turn.submit', {**params, 'operationId': registered['result']['operationId']})
                assert 'result' in submitted, submitted
        else:
            registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'after-native-history',
                'turn.submit', p.operation_digest('turn.submit', params))
            await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
        for _ in range(500):
            with c.db() as db:
                active = db.get(RelaySession, SID).active_operation_id
            if active is None:
                break
            await asyncio.sleep(.03)
        assert active is None
        await c.manager.close()
        await inspect('finish')
        result = await task
        assert result['tui']['production_placement'] and result['tui']['production_opener']
        assert len(result['tui']['empty_tree_records']) == 3
        model_requests = [r for r in result['tui']['requests'] if 'count_tokens' not in r['path']]
        assert len(model_requests) >= 3
        # Native CLI also issued a zero-tool first-prompt request. Preserve it
        # in evidence; placement is not an HTTP exactly-once/billing proof.
        tool_requests = [r for r in model_requests if r['tool_count'] > 0]
        assert (len(tool_requests) >= 3 if scenario == 'native-background' else len(tool_requests) == 3), model_requests
        expected_markers = ('First isolated fixture turn', 'Native TUI isolated fixture turn', 'Second isolated fixture turn')
        for marker in expected_markers:
            assert sum(marker in r['current_fixture_markers'] for r in tool_requests) == 1, model_requests
        history = json.dumps(next(r['messages'] for r in model_requests if 'Second isolated fixture turn' in r['current_fixture_markers']))
        record_property('native_model_requests_total', len(model_requests))
        record_property('unclassified_zero_tool_requests', len(model_requests) - len(tool_requests))
        assert all(text in history for text in expected_markers)
        assert all(row['empty'] for row in result['tui']['empty_tree_records'])
        hooks = result['tui']['hook_observations']
        assert hooks and {event['native_id'] for event in hooks} == {NATIVE}
        terminal_hooks = [event['event'] for event in hooks if str(event['run_id']) == terminal_run]
        assert terminal_hooks.index('SessionStart') < terminal_hooks.index('UserPromptSubmit') < terminal_hooks.index('Stop')
        assert terminal_hooks.count('UserPromptSubmit') == terminal_hooks.count('Stop') == 1
        assert terminal_hooks.index('Stop') < terminal_hooks.index('SessionEnd')
        prompt_runs = list(dict.fromkeys(event['run_id'] for event in hooks if event['event'] == 'UserPromptSubmit'))
        assert len(prompt_runs) == 3
        for old_run, next_run in zip(prompt_runs, prompt_runs[1:]):
            ended = next(i for i, event in enumerate(hooks) if event['run_id'] == old_run and event['event'] == 'SessionEnd')
            resumed = next(i for i, event in enumerate(hooks) if event['run_id'] == next_run and event['event'] == 'SessionStart')
            assert ended < resumed
        record_property('native_hook_observations', json.dumps(hooks))
        record_property('native_terminal_session_end_observed', 'SessionEnd' in terminal_hooks)
        if scenario in ('registered-warm', 'registered-empty-refused', 'http-warm', 'http-withdrawal', 'http-lost-reply'):
            from sqlalchemy import select

            from acc.server.models import RelayOperation
            with c.db() as db:
                operations = list(db.scalars(select(RelayOperation).where(RelayOperation.session_id == SID,
                    RelayOperation.receipt['method'].as_string().in_(('switch.prepare', 'switch.commit')))))
                prefix = 'native-http' if http_case else 'native-production'
                expected = {f'{prefix}-{view}-{stage}' for view in ('terminal', 'chat') for stage in ('prepare', 'commit')}
                if scenario == 'registered-empty-refused':
                    expected |= {'empty-cannot-resume-prepare', 'empty-cannot-resume-commit'}
                if scenario == 'http-withdrawal':
                    expected |= {'native-http-withdraw-prepare', 'native-http-withdraw-commit'}
                assert len(operations) == len(expected) and {op.registration_key for op in operations} == expected
                assert all(op.receipt['state'] == ('failed' if op.registration_key in ('empty-cannot-resume-commit', 'native-http-withdraw-commit') else 'accepted')
                    and op.receipt['actor']['principal'] == 'alex' for op in operations)
                record_property('registered_native_operations', json.dumps([{'id': op.id,
                    'key': op.registration_key, 'run_id': str(op.run_id), 'method': op.receipt['method']} for op in operations]))
        assert result['tui']['terminalMediated'] is False
        record_property('production_native_placement', result['tui']['scope'])
    except Exception as error:
        if 'model_requests' in locals():
            error.add_note(json.dumps(model_requests))
        if output is not None and (output / 'placement-errors.log').exists():
            error.add_note((output / 'placement-errors.log').read_text())
        error.add_note(''.join(locals().get('main_errors', [])))
        raise
    finally:
        await c.manager.close()
        if output is not None and (output / 'owned-inspect.sock').exists() and not task.done():
            await inspect('finish')
        if brokers:
            await brokers.close()
        await asyncio.gather(task, return_exceptions=True)
