"""Actual production broker/owned tmux/opener in an offline namespace."""
import asyncio
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_claude_terminal_grants import Attached, claim
from test_session_relay_ownership import SID, select_owner
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.broker_client import Brokers
from acc.server.db import write_in_thread, write_intent
from acc.server.models import RelaySession, Session, SessionRun
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

NATIVE = '22222222-2222-4222-8222-222222222222'


pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('cycles', [2, 17, pytest.param(0, id='background')])
async def test_production_claude_owner_hook_churn(runtime_case, monkeypatch, record_property, cycles, *, proof_budget=1200, background_mode='stop'):  # noqa: F811
    c = runtime_case
    path = Path(__file__).resolve().parents[1] / 'scripts/session_relay_qualification/probe.py'
    spec = importlib.util.spec_from_file_location('claude_placement_probe', path)
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    import subprocess
    from types import SimpleNamespace as Namespace
    original_run = subprocess.run
    def bounded_run(*args, **kwargs):
        if args and args[0][-1] == '/evidence/driver.py':
            kwargs['timeout'] = proof_budget
        return original_run(*args, **kwargs)
    probe.subprocess = Namespace(**{**vars(subprocess), 'run': bounded_run})
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
            row.launch_spec.files = {'settings.json': '{"disableAllHooks":false,"hooks":{}}',
                'guardrails.json': json.dumps({'workspace': row.cwd})}
            db.commit()
    reset()
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'owned-inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 10))
            assert 'error' not in value, value
            return value
        finally:
            writer.close()
            await writer.wait_closed()
    def prepare(directory):
        nonlocal output
        output = directory
        (directory / 'claude_placement_fixture.py').write_text(
            (path.parent / 'claude_churn_fixture.py').read_text())
        with c.db() as db:
            row = db.get(Session, SID)
            config = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent), 'tmux_name': row.tmux_name, 'proof_budget': proof_budget}
        (directory / 'placement-config.json').write_text(json.dumps(config))
        if cycles == 0:
            (Path(config['workspace']) / '.relay-background.py').write_text(
                "import os,time\nfrom pathlib import Path\n"
                f"Path({str(Path(config['workspace']) / '.relay-background.pid')!r}).write_text(str(os.getpid()))\n"
                + (f"while not Path({str(Path(config['workspace']) / '.relay-background.release')!r}).exists(): time.sleep(.1)\n"
                 if background_mode == 'complete' else "while True: time.sleep(1)\n"))
            driver = directory / 'driver.py'
            source = driver.read_text()
            boundary = "            data = ''.join('event: ' + name"
            assert source.count(boundary) == 1
            tool_response = """            if ('Background isolated fixture turn' in json.dumps(body.get('messages', [{}])[-1].get('content', ''))
                    and body.get('tools') and not any(r.get('background_tool') for r in requests)):
                requests[-1]['background_tool'] = True
                tool = {'type': 'tool_use', 'id': 'tool_background_fixture', 'name': 'Bash', 'input': {}}
                events[1] = ('content_block_start', {'type': 'content_block_start', 'index': 0, 'content_block': tool})
                events[2] = ('content_block_delta', {'type': 'content_block_delta', 'index': 0, 'delta': {
                    'type': 'input_json_delta', 'partial_json': json.dumps({
                        'command': '/usr/bin/python3 /evidence/background.py', 'run_in_background': True,
                        'description': 'Run the disposable isolated background lifetime probe'})}})
                events[4][1]['delta']['stop_reason'] = 'tool_use'
"""
            driver.write_text(source.replace(boundary, tool_response.replace(
                repr('/usr/bin/python3 /evidence/background.py'),
                repr('/usr/bin/python3 ' + __import__('shlex').quote(str(Path(config['workspace']) / '.relay-background.py')))) + boundary))
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
                db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
                db.commit()
        await write_in_thread(qualify)
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'First isolated fixture turn',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
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
        if cycles == 0:
            await background_turn(c, inspect, record_property, background_mode)
        phases = []
        for cycle in range(cycles):
            cycle_start = time.monotonic()
            await select_owner(c, 'terminal', f'native-production-terminal-{cycle}')
            terminal_ready = time.monotonic()
            await write_in_thread(qualify)
            c.attached = Attached(c.resolve)
            control = await claim(c, key=f'person-claim-{cycle}')
            import base64
            for sequence, data in ((1, b'Native TUI isolated fixture turn'), (2, b'\r')):
                await c.manager.input_terminal(c.attached, SID, {'inputGrant': control.input_grant,
                    'inputEpoch': control.input_epoch, 'frameSeq': sequence, 'data': base64.b64encode(data).decode()})
                if sequence == 1:
                    await asyncio.sleep(.7)
            for _ in range(500):
                state = await inspect('state')
                if state['processed'] == 2:
                    break
                await asyncio.sleep(.03)
            assert state['processed'] == state['admitted'] == 2, state
            assert state['idle'] and not state['failed'] and state['native_transcript']
            screen = await inspect('screen')
            assert 'Native TUI isolated fixture turn' in screen['text']
            # Host processed input establishes native admission. Durable turn
            # completion is a separate publication; wait for it before switching.
            for _ in range(500):
                with c.db() as db:
                    active = db.get(RelaySession, SID).active_operation_id
                if active is None:
                    break
                await asyncio.sleep(.03)
            assert active is None
            terminal_done = time.monotonic()
            await select_owner(c, 'chat', f'native-production-chat-{cycle}')
            chat_ready = time.monotonic()
            await write_in_thread(qualify)
            with c.db() as db:
                row = db.get(RelaySession, SID)
                params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'Second isolated fixture turn',
                    'expectedConfigurationRevision': row.configuration_revision,
                    'expectedControlRevision': row.control_revision, 'ifBusy': 'reject'}
            registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), f'after-native-history-{cycle}',
                'turn.submit', p.operation_digest('turn.submit', params))
            await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
            for _ in range(500):
                with c.db() as db:
                    active = db.get(RelaySession, SID).active_operation_id
                if active is None:
                    break
                await asyncio.sleep(.03)
            assert active is None
            sample = await inspect('sample')
            assert sample['retired'] == 2 * (cycle + 1), sample
            phases.append({'cycle': cycle, 'to_terminal': terminal_ready - cycle_start,
                'native_turn': terminal_done - terminal_ready, 'to_chat': chat_ready - terminal_done,
                'sdk_turn': time.monotonic() - chat_ready})
            Path(f'/tmp/relay-g56-claude-profile-{cycles}.json').write_text(json.dumps(
                {'budget_seconds': proof_budget, 'phases': phases, 'broker': sample}, indent=2) + '\n')
        await c.manager.close()
        await inspect('finish')
        result = await task
        assert result['tui']['production_placement'] and result['tui']['production_opener']
        assert len(result['tui']['empty_tree_records']) == 1 + 2 * cycles
        assert len(result['tui']['retired']) == 2 * cycles
        samples = result['tui']['samples']
        if samples:
            assert max(x['fds'] for x in samples) - min(x['fds'] for x in samples) <= 4, samples
            assert max(x['tasks'] for x in samples) - min(x['tasks'] for x in samples) <= 4, samples
        record_property('cycle_phases', json.dumps(phases))
        record_property('broker_timings', json.dumps(result['tui']['timings']))
        record_property('native_owner_lifetimes', 1 + 2 * cycles)
        record_property('cleanup_samples', json.dumps(samples))
        model_requests = [r for r in result['tui']['requests'] if 'count_tokens' not in r['path']]
        assert len(model_requests) >= 1 + 2 * cycles
        # Native CLI also issued a zero-tool first-prompt request. Preserve it
        # in evidence; placement is not an HTTP exactly-once/billing proof.
        tool_requests = [r for r in model_requests if r['tool_count'] > 0]
        if cycles == 0:
            assert sum(r.get('background_tool', False) for r in model_requests) == 1
            return
        assert len(tool_requests) == 1 + 2 * cycles, model_requests
        for marker in ('First isolated fixture turn', 'Native TUI isolated fixture turn', 'Second isolated fixture turn'):
            assert sum(marker in r['current_fixture_markers'] for r in tool_requests) == (1 if marker == 'First isolated fixture turn' else cycles), model_requests
        history = json.dumps(next(r['messages'] for r in model_requests if 'Second isolated fixture turn' in r['current_fixture_markers']))
        record_property('native_model_requests_total', len(model_requests))
        record_property('unclassified_zero_tool_requests', len(model_requests) - len(tool_requests))
        assert all(text in history for text in ('First isolated fixture turn', 'Native TUI isolated fixture turn',
                                               'Second isolated fixture turn'))
        assert all(row['empty'] for row in result['tui']['empty_tree_records'])
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
        # The native subprocess deadline can close the inspector first. Preserve
        # that primary error and retrieve the capture task's timeout exception.
        primary = sys.exception()
        cleanup_errors = []
        try:
            await c.manager.close()
            if output is not None and (output / 'owned-inspect.sock').exists() and not task.done():
                await inspect('finish')
        except Exception as error:
            cleanup_errors.append(error)
        finally:
            if brokers:
                await brokers.close()
            outcome = await asyncio.gather(task, return_exceptions=True)
        cleanup_errors.extend(error for error in outcome if isinstance(error, BaseException))
        if primary is not None:
            for error in cleanup_errors:
                primary.add_note('Native fixture/cleanup: ' + repr(error))
        elif cleanup_errors:
            raise cleanup_errors[0]


async def lose_permission_application_evidence(c, inspect):
    # New primary code can prove native application. Exercise the old unknown
    # boundary with explicit transport loss, never by discarding valid evidence
    # inside the production settlement function or weakening its assertions.
    assert await inspect('lose_application_evidence') == {'applicationEvidenceLoss': True}
    original = c.launch.peer.call
    async def lost_answer(method, params):
        result = await original(method, params)
        if method == 'owner.answer' and result.get('outcome') == 'accepted':
            raise ConnectionError('Artificial answer loss after actual native application')
        return result
    c.launch.peer.call = lost_answer


async def background_turn(c, inspect, record_property, mode='stop'):
    await lose_permission_application_evidence(c, inspect)
    record_property('permission_application_evidence_loss', True)
    from acc.server.models import RelayDecision
    with c.db() as db:
        row = db.get(RelaySession, SID)
        params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
            'text': 'Background isolated fixture turn',
            'expectedConfigurationRevision': row.configuration_revision,
            'expectedControlRevision': row.control_revision, 'ifBusy': 'reject'}
    registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'native-background-turn',
        'turn.submit', p.operation_digest('turn.submit', params))
    await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
    answered = set()
    for _ in range(1000):
        with c.db() as db:
            from sqlalchemy import select
            decisions = [dict(x.decision) for x in db.scalars(select(RelayDecision).where(
                RelayDecision.session_id == SID))]
        for decision in decisions:
            identifier = decision['decisionId']
            if identifier in answered or decision['status'] != 'pending':
                continue
            answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'decisionId': identifier,
                'choiceId': 'accept', 'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
            op = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'answer-' + identifier,
                'decision.reply', p.operation_digest('decision.reply', answer))
            assert await c.launch.answer(c.resolve, {**answer, 'operationId': op.operation_id}) == {'outcome': 'unknown'}
            answered.add(identifier)
        state = await inspect('background')
        if state['running'] and state['background'] and not state['active_turn']:
            break
        await asyncio.sleep(.03)
    assert state['running'] and state['background'] and not state['active_turn'], state
    assert answered, 'Native background tool must pass through the real permission decision path'
    with pytest.raises(p.RelayError):
        await select_owner(c, 'terminal', 'background-must-refuse')
    if mode == 'complete':
        await inspect('release_background')
        for _ in range(500):
            state = await inspect('background')
            if state['exited'] and not state['background']:
                break
            await asyncio.sleep(.03)
        assert state['exited'] and not state['background'], state
        # Native completion is not permission-receipt evidence. Preserve the
        # unknown decisions and the refusal of a new ownership plan.
        with c.db() as db:
            assert all(db.get(RelayDecision, (SID, identifier)).decision['status'] == 'outcome_unknown'
                       for identifier in answered)
        with pytest.raises(p.RelayError):
            await select_owner(c, 'terminal', 'unknown-permission-still-refuses')
        record_property('real_background_completion_observed', True)
    await c.manager.close()
    for _ in range(500):
        state = await inspect('background')
        if state['exited'] and state['active'] == 0:
            break
        await asyncio.sleep(.03)
    assert state['exited'] and state['active'] == 0, state
    record_property('real_background_process_reaped', True)
    record_property('permission_replies_unknown', len(answered))
