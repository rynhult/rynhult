"""Real Claude broker SIGKILL with the model/controller namespace still alive."""
import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_session_relay_claude_terminal_grants import Attached, claim
from test_session_relay_ownership import SID, select_owner
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.broker_client import Brokers
from acc.server.db import write_in_thread, write_intent
from acc.server.models import RelayDecision, RelayOperation, RelaySession, Session, SessionRun
from acc.server.session_relay.hub import Hub
from acc.session_relay import protocol as p

NATIVE = '22222222-2222-4222-8222-222222222222'
NEXT = 'def456abc123'


pytestmark = pytest.mark.relay_native


async def test_claude_broker_death_stops_tui_before_fresh_review(runtime_case, monkeypatch, record_property):  # noqa: F811
    await process_death(runtime_case, monkeypatch, record_property)


async def process_death(case, monkeypatch, record_property, *, background=False):
    proof_budget = 120
    c = case
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
    async def control(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'broker-control.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            result = json.loads(await asyncio.wait_for(reader.readline(), 25))
            assert 'error' not in result, result
            return result
        finally:
            writer.close()
            await writer.wait_closed()
    def prepare(directory):
        nonlocal output
        output = directory
        (directory / 'claude_placement_fixture.py').write_text(
            (path.parent / 'claude_process_fixture.py').read_text())
        (directory / 'claude_churn_fixture.py').write_text((path.parent / 'claude_churn_fixture.py').read_text())
        (directory / 'claude_broker_worker.py').write_text(
            "import asyncio,json\nfrom claude_churn_fixture import serve\n"
            "print(json.dumps(asyncio.run(serve({'ANTHROPIC_BASE_URL': __import__('os').environ['ANTHROPIC_BASE_URL']}))))\n")
        with c.db() as db:
            row = db.get(Session, SID)
            config = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent), 'tmux_name': row.tmux_name, 'proof_budget': proof_budget}
        (directory / 'placement-config.json').write_text(json.dumps(config))
        if background:
            (Path(config['workspace']) / '.relay-background.py').write_text(
                "import os,time\nfrom pathlib import Path\n"
                f"Path({str(Path(config['workspace']) / '.relay-background.pid.tmp')!r}).write_text(str(os.getpid()))\n"
                f"Path({str(Path(config['workspace']) / '.relay-background.pid.tmp')!r}).replace({str(Path(config['workspace']) / '.relay-background.pid')!r})\n"
                "while True: time.sleep(1)\n")
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
        await c.launch.launch(SID)
        @write_intent
        def qualify():
            with c.db() as db:
                db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
                db.commit()
        await write_in_thread(qualify)
        monkeypatch.setattr('acc.server.session_relay.writers.PUBLIC_WRITER_QUALIFIED', True)
        params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'text': 'First isolated fixture turn',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
        registered = Hub(c.ctx).register(c.resolve, SID, str(c.launch.run_id), 'native-history',
            'turn.submit', p.operation_digest('turn.submit', params))
        await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
        for _ in range(500):
            with c.db() as db:
                active = db.get(RelaySession, SID).active_operation_id
            if active is None:
                break
            await asyncio.sleep(.03)
        assert active is None
        decisions = set()
        stale_operation = stale_token = None
        if background:
            stale_operation, stale_token, decisions = await start_background(c, inspect)
        else:
            await select_owner(c, 'terminal', 'before-broker-death')
            await write_in_thread(qualify)
            c.attached = Attached(c.resolve)
            grant = await claim(c)
        old_peer = c.launch.peer
        await control('track')
        killed = await control('kill')
        assert killed['returncode'] == -9
        for _ in range(300):
            trees = await control('trees')
            if trees['empty'] and trees['tracked_dead']:
                break
            await asyncio.sleep(.02)
        assert trees['empty'] and trees['tracked_dead'], trees
        assert old_peer.closed
        if background:
            with c.db() as db:
                assert db.get(RelayOperation, stale_operation).receipt['state'] == 'accepted'
                assert all(db.get(RelayDecision, (SID, identifier)).decision['status'] == 'outcome_unknown'
                           for identifier in decisions)
        restarted = await control('start')
        assert restarted['pid'] != killed['pid'] and restarted['generation'] == 2
        assert (await inspect('state'))['active'] == 0
        fresh = c.manager.new_exclusive_launch(c.resolve)
        @write_intent
        def reserve_fresh():
            with c.db() as db:
                previous = db.get(Session, SID)
                db.add(Session(id=NEXT, provider='claude', name='Reviewed after broker crash',
                    native_id='33333333-3333-4333-8333-333333333333', claude_auth='api_key',
                    tmux_name='acc-' + NEXT, launch_spec_id=previous.launch_spec_id,
                    run_as=previous.run_as, owner=previous.owner, workdir=previous.workdir,
                    run_dir=str(Path(previous.run_dir).parent / NEXT)))
                db.flush()
                fresh.reserve(db, db.get(Session, NEXT))
                db.commit()
        await write_in_thread(reserve_fresh)
        await fresh.launch(NEXT)
        assert fresh.binding.native_conversation != NATIVE
        @write_intent
        def qualify_fresh():
            with c.db() as db:
                db.get(RelaySession, NEXT).capabilities = {'text': 'qualified', 'terminal.input': 'qualified'}
                db.commit()
        await write_in_thread(qualify_fresh)
        params = {'sessionId': NEXT, 'expectedRunId': str(fresh.run_id), 'text': 'Second isolated fixture turn',
            'expectedConfigurationRevision': 0, 'expectedControlRevision': 1, 'ifBusy': 'reject'}
        registered = Hub(c.ctx).register(c.resolve, NEXT, str(fresh.run_id), 'fresh-history',
            'turn.submit', p.operation_digest('turn.submit', params))
        await c.manager.submit(c.resolve, NEXT, {**params, 'operationId': registered.operation_id})
        for _ in range(500):
            with c.db() as db:
                active = db.get(RelaySession, NEXT).active_operation_id
            if active is None:
                break
            await asyncio.sleep(.03)
        assert active is None
        if background:
            # Replay onto the matching SDK writer path, before switching it;
            # a view mismatch must not be the reason this old grant refuses.
            with pytest.raises((p.RelayError, ConnectionError)):
                await fresh.peer.call('dispatch', {'grant': stale_token})
        with c.db() as db:
            revision = db.get(RelaySession, NEXT).revision
        await c.manager.select_owner(c.resolve, NEXT, target='terminal', revision=revision, request_key='fresh-terminal')
        assert (await inspect('state'))['view'] == 'terminal'
        if not background:
            with pytest.raises((p.RelayError, ConnectionError)):
                await fresh.peer.call('terminal.input', {'grant': grant.input_grant, 'sequence': 1, 'data': 'eA=='})
        await write_in_thread(qualify_fresh)
        attached = Attached(c.resolve, attachment='fresh-reviewed-terminal')
        with c.db() as db:
            revision = db.get(RelaySession, NEXT).control_revision
        params = {'sessionId': NEXT, 'expectedRunId': str(fresh.run_id),
            'attachmentId': attached.attachment_id, 'expectedRevision': revision}
        registered = Hub(c.ctx).register(attached, NEXT, str(fresh.run_id), 'fresh-input',
            'control.claim', p.operation_digest('control.claim', params))
        current = await c.manager.claim_terminal(attached, NEXT, {**params, 'operationId': registered.operation_id})
        assert current.input_grant != (stale_token if background else grant.input_grant)
        reply = await c.manager.input_terminal(attached, NEXT, {'inputGrant': current.input_grant,
            'inputEpoch': current.input_epoch, 'frameSeq': 1, 'data': 'RnJlc2ggZHJhZnQ='})
        assert reply['watermark']['admitted'] == 1 and reply['watermark']['processed'] == 0
        await c.manager.close()
        for _ in range(500):
            if (await inspect('state'))['active'] == 0:
                break
            await asyncio.sleep(.02)
        assert (await inspect('state'))['active'] == 0
        if background:
            with c.db() as db:
                assert db.get(RelayOperation, stale_operation).receipt['state'] == 'accepted'
                assert all(db.get(RelayDecision, (SID, identifier)).decision['status'] == 'outcome_unknown'
                           for identifier in decisions)
        await control('finish')
        result = await task
        assert result['tui']['os_process'] and result['tui']['broker_generations'] == 2
        assert len(result['tui']['empty_tree_records']) == (3 if background else 4)
        assert all(r['empty'] for r in result['tui']['empty_tree_records'])
        requests = [r for r in result['tui']['requests'] if 'count_tokens' not in r['path']]
        if background:
            assert sum(r.get('background_tool', False) for r in requests) == 1
            for marker in ('First isolated fixture turn', 'Second isolated fixture turn'):
                assert sum(marker in r['current_fixture_markers'] for r in requests if r['tool_count'] > 0) == 1
            record_property('active_background_broker_death', True)
        else:
            assert sum(r['tool_count'] > 0 for r in requests) == 2, requests
        record_property('broker_death_returncode', -9)
        record_property('fresh_review_no_replay', True)
    finally:
        await c.manager.close()
        if output is not None and (output / 'broker-control.sock').exists() and not task.done():
            await control('finish')
        if brokers:
            await brokers.close()
        await asyncio.gather(task, return_exceptions=True)


async def start_background(c, inspect):
    from test_session_relay_claude_churn import lose_permission_application_evidence
    await lose_permission_application_evidence(c, inspect)
    from sqlalchemy import select
    with c.db() as db:
        row = db.get(RelaySession, SID)
        params = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id),
            'text': 'Background isolated fixture turn',
            'expectedConfigurationRevision': row.configuration_revision,
            'expectedControlRevision': row.control_revision, 'ifBusy': 'reject'}
    hub = Hub(c.ctx)
    registered = hub.register(c.resolve, SID, str(c.launch.run_id), 'background-before-crash',
        'turn.submit', p.operation_digest('turn.submit', params))
    # Diagnostic interception preserves the actual Main issue/result unchanged.
    issue = c.launch.issue
    tokens = []
    def capture(*args):
        token = issue(*args)
        if isinstance(token, str):
            tokens.append(token)
        return token
    c.launch.issue = capture
    await c.manager.submit(c.resolve, SID, {**params, 'operationId': registered.operation_id})
    answered = set()
    for _ in range(1000):
        with c.db() as db:
            values = [dict(x.decision) for x in db.scalars(select(RelayDecision).where(RelayDecision.session_id == SID))]
        for decision in values:
            identifier = decision['decisionId']
            if identifier in answered or decision['status'] != 'pending':
                continue
            answer = {'sessionId': SID, 'expectedRunId': str(c.launch.run_id), 'decisionId': identifier,
                'choiceId': 'accept', 'expectedRevision': decision['revision'], 'actionDigest': decision['actionDigest']}
            operation = hub.register(c.resolve, SID, str(c.launch.run_id), 'allow-' + identifier,
                'decision.reply', p.operation_digest('decision.reply', answer))
            assert await c.launch.answer(c.resolve, {**answer, 'operationId': operation.operation_id}) == {'outcome': 'unknown'}
            answered.add(identifier)
        state = await inspect('background')
        with c.db() as db:
            active = db.get(RelaySession, SID).active_operation_id
        if state['running'] and state['background'] and not state['active_turn'] and active is None:
            break
        await asyncio.sleep(.03)
    assert state['running'] and state['background'] and not state['active_turn'], state
    assert answered and len(tokens) == 1
    return registered.operation_id, tokens[0], answered
