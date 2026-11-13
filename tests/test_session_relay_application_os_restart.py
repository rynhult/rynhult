"""Real G5-style OS main/broker SIGKILL + native Claude unpublished proof recovery."""
import asyncio
import importlib.util
import json
import shutil
from pathlib import Path

import pytest
from test_session_relay_node_runtime import SID, node_runtime  # noqa: F401
from test_session_relay_runtime import runtime_case  # noqa: F401

from acc.server.db import write_intent
from acc.server.models import RelayOperation, RelaySession, Session, SessionRun

pytestmark = pytest.mark.relay_native


@pytest.mark.parametrize('loss', ['main', 'broker', 'both', 'before-proof', 'proof-actor', 'proof-digest', 'proof-binding', 'proof-mode', 'scope-account', 'scope-directory', 'scope-node-key'])
@pytest.mark.parametrize('allow', [False, True])
async def test_native_unpublished_application_survives_os_restart(node_runtime, loss, allow, record_property):  # noqa: F811
    c = node_runtime
    conclusive = loss not in ('before-proof', 'proof-actor', 'proof-digest', 'proof-binding', 'proof-mode') and not loss.startswith('scope-')
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location('g1_restart_probe', root / 'scripts/session_relay_qualification/probe.py')
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    @write_intent
    def prepare_main():
        with c.db() as db:
            db.delete(db.get(RelaySession, SID))
            db.delete(db.get(SessionRun, c.launch.run_id))
            row = db.get(Session, SID)
            row.provider, row.native_id, row.claude_auth = 'claude', '22222222-2222-4222-8222-222222222222', 'api_key'
            row.launch_spec.provider = 'claude'
            row.launch_spec.inputs = {'provider': 'claude', 'model': 'claude-sonnet-4-5'}
            row.launch_spec.files = {'settings.json': json.dumps({'disableAllHooks': False, 'hooks': {}, 'permissions': {'ask': ['Bash']}}),
                'guardrails.json': json.dumps({'workspace': row.cwd})}
            db.commit()
    prepare_main()
    output = None
    def prepare(directory):
        nonlocal output
        output = directory
        with c.db() as db:
            row = db.get(Session, SID)
            data = {'workspace': row.cwd, 'run_dir': str(Path(row.run_dir).parent),
                'main_root': str(Path(c.engine.url.database).parent), 'allow': allow, 'before_proof': loss == 'before-proof', 'timeout': 180}
        (directory / 'placement-config.json').write_text(json.dumps(data))
        (directory / 'claude_placement_fixture.py').rename(directory / 'placement_broker.py')
        for name in ('application_restart_fixture.py', 'application_restart_worker.py'):
            shutil.copyfile(root / 'scripts/session_relay_qualification' / name, directory / name)
        shutil.copyfile(directory / 'application_restart_fixture.py', directory / 'claude_placement_fixture.py')
        return lambda: None
    task = asyncio.create_task(asyncio.to_thread(probe.capture_claude_resume, enclosure=True, tui=True, placement=prepare))
    async def inspect(method):
        reader, writer = await asyncio.open_unix_connection(str(output / 'restart-inspect.sock'))
        try:
            writer.write(json.dumps({'method': method}).encode() + b'\n')
            await writer.drain()
            value = json.loads(await asyncio.wait_for(reader.readline(), 35))
            assert 'error' not in value, value
            return value
        finally:
            writer.close()
            await writer.wait_closed()
    try:
        for _ in range(1500):
            if task.done():
                await task
            if output is not None and (output / 'restart-ready').exists():
                break
            await asyncio.sleep(.02)
        assert output is not None and (output / 'restart-ready').exists()
        await inspect('main_start')
        first = await inspect('main:state')
        assert (await inspect('main:promote'))['authority']['epoch'] == 1
        await inspect('main:launch_first')
        await inspect('main:submit_permission')
        for _ in range(500):
            if (await inspect('main:pending'))['pending']:
                break
            await asyncio.sleep(.02)
        assert (await inspect('main:pending'))['pending']
        answer_task = asyncio.create_task(inspect('main:answer'))
        for _ in range(500):
            before = await inspect('state')
            if before['application_seen'] if loss == 'before-proof' else before['proof_ready']:
                break
            await asyncio.sleep(.02)
        if loss != 'before-proof':
            answer = await answer_task
            assert answer['outcome'] == 'unknown' and before['proof_ready']
        else:
            assert before['application_seen'] and not before['proof_ready']
        assert before['answer_invocations'] == 1
        original = (await inspect('main:state'))['receipt']
        assert original['state'] in ('submitting', 'uncertain')
        killed = await inspect(('main' if loss in ('main', 'both') else 'broker') + '_kill')
        assert killed['returncode'] == -9
        if loss.startswith('proof-'):
            with c.db() as db:
                directory = Path(db.get(Session, SID).run_dir)
            fact_path = directory / 'relay' / 'application-proofs' / (original['operationId'] + '.json')
            fact = json.loads(fact_path.read_text())
            if loss == 'proof-actor':
                fact['operation']['actor']['principal'] = 'another-person'
            elif loss == 'proof-digest':
                fact['operation']['payloadDigest'] = 'sha256:' + 'f' * 64
            elif loss == 'proof-binding':
                fact['binding']['native_conversation'] = '33333333-3333-4333-8333-333333333333'
            if loss == 'proof-mode':
                fact_path.chmod(0o644)
            else:
                fact_path.write_text(json.dumps(fact))
        if loss == 'both':
            assert (await inspect('broker_kill'))['returncode'] == -9
            await inspect('broker_start')
        await inspect(('main' if loss in ('main', 'both') else 'broker') + '_start')
        if loss in ('main', 'both'):
            second = await inspect('main:state')
            assert second['pid'] != first['pid'] and second['main'] != first['main']
            assert (await inspect('main:recover_application'))['recovered'] is False  # standby: no proof authority
            assert (await inspect('main:promote_fenced'))['authority']['epoch'] == 2
        if loss == 'before-proof':
            assert (await answer_task)['outcome'] == 'unknown'
        await inspect('main:recover')
        for _ in range(500):
            empty = await inspect('state')
            if empty['active'] == 0 and not empty['old_execution']:
                break
            await asyncio.sleep(.02)
        assert empty['active'] == 0 and empty['old_execution'] == []
        if loss.startswith('scope-'):
            assert (await inspect('main:replace_scope_' + loss.removeprefix('scope-')))['replaced']
        replacement_fault = getattr(c, 'native_replacement_fault', None)
        fresh_identity = None
        expected_requests = before['model_requests']
        if replacement_fault:
            fresh = await inspect('main:launch_fresh')
            assert 'run' in fresh, fresh
            assert fresh['run'] != original['runId']
            fresh_identity = await inspect('main:fresh_state')
            assert fresh_identity['native'] == '44444444-4444-4444-8444-444444444444'
            assert not fresh_identity['sealed'] and not fresh_identity['peer_closed']
            assert fresh_identity['session'] != SID
            assert fresh_identity['rootOwner'] == original['actor']['principal'] and fresh_identity['parentChain'] == []
            assert (await inspect('main:fresh_turn'))['receipt']['state'] == 'accepted'
            fresh_counts = await inspect('state')
            assert fresh_counts['sdk_queries'] == 1
            assert fresh_counts['model_inferences'] > before['model_inferences']
            expected_requests = fresh_counts['model_requests']
            if replacement_fault == 'grant':
                refused = await inspect('main:old_answer_on_fresh')
                assert refused == {'refused': True, 'peer_live': True}
            elif replacement_fault == 'publication':
                refused = await inspect('broker:replay_old_publication')
                assert refused == {'refused': 2, 'peer_live': True}
            elif replacement_fault == 'hooks':
                assert (await inspect('main:old_hooks_on_fresh'))['refused'] == 9
            assert await inspect('main:fresh_state') == fresh_identity
        assert (await inspect('main:recover_application'))['recovered'] is conclusive
        recovered = (await inspect('main:state'))['receipt']
        assert recovered['state'] == ('accepted' if conclusive else 'uncertain')
        assert {key: recovered[key] for key in ('operationId', 'runId', 'actor', 'payloadDigest')} == {
            key: original[key] for key in ('operationId', 'runId', 'actor', 'payloadDigest')}
        final = await inspect('state')
        assert final['active'] == bool(replacement_fault) and final['old_execution'] == []
        assert final['answer_invocations'] == (2 if replacement_fault == 'grant' else 1)
        assert final['model_requests'] == expected_requests
        with c.db() as db:
            assert db.get(RelayOperation, original['operationId']).receipt['state'] == ('accepted' if conclusive else 'uncertain')
            assert db.get(RelaySession, SID).sealed
        assert (await inspect('main:state'))['runs'] == (['def456abc123'] if replacement_fault else [])
        if replacement_fault:
            assert await inspect('main:fresh_state') == fresh_identity
            assert final['sdk_queries'] == 1
        record_property('native_recovery', json.dumps({'loss': loss, 'allow': allow,
            'old_main': first['pid'], 'killed': killed['pid'], 'answers': 1, 'no_model_replay': True}))
    finally:
        if output is not None and (output / 'restart-inspect.sock').exists() and not task.done():
            await inspect('finish')
        await task
