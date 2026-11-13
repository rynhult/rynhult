"""G1 application extension of g56 141ece973 OS-main restart worker (G5 harness)."""
import asyncio
import json
import os
import pwd
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import select, text

from acc.node.client import Relay
from acc.server.api.context import Context, Principal
from acc.server.broker_client import Brokers
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker, write_intent
from acc.server.ha import ADMITTED, Authority, Fenced
from acc.server.models import Node, RelayDecision, RelayOperation, RelaySession, Session
from acc.server.nodes import NodeLink
from acc.server.session_relay.hub import Hub
from acc.server.session_relay.maintenance import recover
from acc.server.session_relay.runtime import RuntimeManager
from acc.server.supervisor_common import LaunchRefused
from acc.session_relay import protocol as p

SID = 'abc123def456'
NEXT = 'def456abc123'
THIRD = '123abcdef456'
HOST = 'fixture-relay-node'


async def main():
    data = json.loads(Path('/evidence/placement-config.json').read_text())
    root = Path(data['main_root'])
    engine = make_engine(f'sqlite:///{root}/relay.db')
    dbs = make_sessionmaker(engine)
    authority = Authority(engine, 'fixture-main')
    authority.install_write_guard()
    account = pwd.getpwuid(os.getuid()).pw_name
    socket = Path('/evidence') / (account + '.sock')
    if not socket.exists():
        socket.symlink_to('owned-broker.sock')
    async def to_node(packet):
        if isinstance(packet, bytes):
            await relay.handle(packet)
    async def to_main(packet):
        link.receive(packet)
    link = NodeLink('fixture-node-id', HOST, to_node,
        {'capabilities': ['session-runtime-v1']}, [account])
    relay = Relay(SimpleNamespace(send=to_main), Path('/evidence'))
    async def close_wire():
        link.close()
        await relay.aclose()
        await link.wait_closed()
    brokers = Brokers(Path('/evidence/owned-broker.sock'), None, account, 'fixture-services',
        hub=SimpleNamespace(get=lambda host: link if host == HOST else None, close=close_wire), authority=authority)
    ctx = SimpleNamespace(db=dbs, broker=brokers,
        settings=Settings(data_dir=root / 'data', hook_path='/evidence/installed/relay-hook', feature_overrides={'session_relay': True}))
    fixture_env = json.loads(Path('/evidence/restart-env.json').read_text())
    delivery = SimpleNamespace(env={key: fixture_env[key] for key in ('ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL')}, catalog=None, check_env=lambda _: None)
    ctx.supervisor = SimpleNamespace(catalog=None, vault=SimpleNamespace(deliver=lambda *_a, **_k: delivery,
        redactor=lambda *_a: SimpleNamespace(text=lambda value: value)))
    ctx.session_for = lambda db, who, sid: Context.session_for(ctx, db, who, sid)
    ctx.owns = Context.owns
    who = Principal('alex', 'owner', 'fixture', 1)
    actor = p.Actor(p.ActorKind.PERSON, 'alex', p.Channel.CHAT)
    resolve = lambda: (who, actor)
    manager = RuntimeManager(ctx)
    ctx.relay_manager = manager
    @write_intent
    def reserve(identifier):
        launch = manager.new_exclusive_launch(resolve)
        with dbs() as db:
            if identifier != SID and db.get(Session, identifier) is None:
                previous = db.get(Session, SID)
                db.add(Session(id=identifier, provider='claude', claude_auth='api_key',
                    native_id='44444444-4444-4444-8444-444444444444' if identifier == NEXT else '55555555-5555-4555-8555-555555555555',
                    name='Reviewed after OS main crash',
                    tmux_name='acc-' + identifier, launch_spec_id=previous.launch_spec_id,
                    run_as=previous.run_as, owner=previous.owner, root_owner=previous.owner,
                    parent_chain=[], team_host=HOST,
                    workdir=previous.workdir, run_dir=str(Path(data['run_dir']) / identifier)))
                db.flush()
            launch.reserve(db, db.get(Session, identifier))
            db.commit()
        return launch
    @write_intent
    def issue(launch):
        params = {'sessionId': launch.session_id, 'expectedRunId': str(launch.run_id),
            'text': 'Never replay OS-main staged input', 'expectedControlRevision': 1,
            'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
        with dbs() as db:
            db.get(RelaySession, launch.session_id).capabilities = {'text': 'qualified'}
            db.commit()
        result = Hub(ctx).register(resolve, launch.session_id, str(launch.run_id), 'os-main-stage',
            'turn.submit', p.operation_digest('turn.submit', params))
        return launch.issue(resolve, result.operation_id, params), result.operation_id
    async def operation(method):
        if method == 'state':
            with dbs() as db:
                original = db.scalar(select(RelayOperation).where(RelayOperation.registration_key == 'restart-original-answer'))
                receipt = original.receipt if original else None
            return {'receipt': receipt, 'pid': os.getpid(), 'main': manager.incarnation, 'authority': authority.status(),
                'runs': sorted(manager.runs), 'channels': len(relay.channels)}
        if method in ('promote', 'promote_fenced'):
            # Fixture controller admits promote_fenced only after its own waitpid.
            authority.promote(expected_epoch=authority.status()['epoch'], old_main_fenced=method == 'promote_fenced')
            return {'authority': authority.status()}
        if method == 'recover':
            recover(ctx)
            return {'recovered': True}
        if method == 'stale_epoch_sql':
            token = ADMITTED.set(authority.epoch - 1)
            try:
                with engine.begin() as connection:
                    connection.execute(text("UPDATE setting SET key=key WHERE key='absent-fixture-row'"))
            finally:
                ADMITTED.reset(token)
            return {'written': True}
        if method == 'node_loss':
            await close_wire()
            await asyncio.gather(*tuple(manager.background), return_exceptions=True)
            return {'node_closed': link.closed}
        if method == 'sql_write':
            with engine.begin() as connection:
                connection.execute(text("UPDATE setting SET key=key WHERE key='absent-fixture-row'"))
            return {'written': True}
        if method in ('launch_first', 'launch_fresh', 'launch_replacement'):
            identifier = {'launch_first': SID, 'launch_fresh': NEXT, 'launch_replacement': THIRD}[method]
            launch = reserve(identifier)
            await launch.launch(identifier)
            return {'run': str(launch.run_id), 'main': manager.incarnation, 'fence': vars(launch.fence)}
        if method == 'submit_permission':
            launch = manager.runs[SID]
            Path('/evidence/request-native-permission').touch()
            Path('/evidence/request-native-public-permission').touch()
            params = {'sessionId': SID, 'expectedRunId': str(launch.run_id), 'text': 'First isolated fixture turn',
                'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
            @write_intent
            def register():
                with dbs() as db:
                    db.get(RelaySession, SID).capabilities = {'text': 'qualified', 'decision.tool_approval': 'qualified'}
                    db.commit()
                return Hub(ctx).register(resolve, SID, str(launch.run_id), 'restart-person-turn',
                    'turn.submit', p.operation_digest('turn.submit', params))
            registered = register()
            await manager.submit(resolve, SID, {**params, 'operationId': registered.operation_id})
            return {'submitted': True}
        if method == 'fresh_state':
            launch = manager.runs[NEXT]
            with dbs() as db:
                row = db.get(RelaySession, NEXT)
                record = db.get(__import__('acc.server.models', fromlist=['RelayRun']).RelayRun, launch.run_id)
                return {'session': NEXT, 'run': str(launch.run_id), 'native': launch.binding.native_conversation,
                    'owner': record.identity['processIdentity'], 'sealed': row.sealed, 'peer_closed': launch.peer.closed,
                    'rootOwner': db.get(Session, NEXT).root_owner, 'parentChain': db.get(Session, NEXT).parent_chain}
        if method == 'fresh_turn':
            launch = manager.runs[NEXT]
            params = {'sessionId': NEXT, 'expectedRunId': str(launch.run_id),
                'text': 'Person enables one new replacement native turn',
                'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
            @write_intent
            def register_fresh():
                with dbs() as db:
                    db.get(RelaySession, NEXT).capabilities = {'text': 'qualified'}
                    db.commit()
                return Hub(ctx).register(resolve, NEXT, str(launch.run_id), 'replacement-person-turn',
                    'turn.submit', p.operation_digest('turn.submit', params))
            registered = register_fresh()
            await manager.submit(resolve, NEXT, {**params, 'operationId': registered.operation_id})
            for _ in range(500):
                with dbs() as db:
                    receipt = dict(db.get(RelayOperation, registered.operation_id).receipt)
                    active = db.get(RelaySession, NEXT).active_operation_id
                if receipt['state'] == 'accepted' and active is None:
                    return {'receipt': receipt}
                await asyncio.sleep(.02)
            raise AssertionError('Replacement native turn did not conclusively complete')
        if method == 'old_answer_on_fresh':
            launch = manager.runs[NEXT]
            try:
                await launch.peer.call('owner.answer', json.loads(Path('/evidence/old-answer-grant.json').read_text()))
            except ConnectionError:
                return {'refused': True, 'peer_live': not launch.peer.closed}
            raise AssertionError('Old answer grant reached a replacement')
        if method == 'old_hooks_on_fresh':
            with dbs() as db:
                directory = Path(db.get(Session, NEXT).run_dir) / 'relay'
            old = json.loads(Path('/evidence/old-native-channel.json').read_text())
            fresh = json.loads((directory / 'owner-channel.json').read_text())
            assert old['token'] != fresh['token'] and old['runId'] != fresh['runId'] and old['nativeId'] != fresh['nativeId']
            denied = 0
            for field in ('token', 'runId', 'nativeId'):
                for event in ('SessionEnd', 'Stop', 'PermissionRequest'):
                    envelope = {'token': old['token'] if field == 'token' else fresh['token'],
                        'runId': old['runId'] if field == 'runId' else fresh['runId'],
                        'payload': {'session_id': old['nativeId'] if field == 'nativeId' else fresh['nativeId'],
                            'hook_event_name': event, 'tool_use_id': 'old-incarnation-tool',
                            'tool_name': 'Bash', 'tool_input': {'command': 'Never execute an old hook'}}}
                    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                    try:
                        reader, writer = await asyncio.open_unix_connection(f'/proc/self/fd/{directory_fd}/native-hook.sock')
                    finally:
                        os.close(directory_fd)
                    try:
                        writer.write(json.dumps(envelope).encode() + b'\n')
                        await writer.drain()
                        assert json.loads(await asyncio.wait_for(reader.readline(), 5)) == {'behavior': 'deny'}
                        denied += 1
                    finally:
                        writer.close()
                        await writer.wait_closed()
            return {'refused': denied}
        if method == 'pending':
            with dbs() as db:
                value = db.scalar(select(RelayDecision).where(RelayDecision.session_id == SID))
                return {'pending': value is not None and value.decision['status'] == 'pending'}
        if method == 'answer':
            launch = manager.runs[SID]
            with dbs() as db:
                decision = db.scalar(select(RelayDecision).where(RelayDecision.session_id == SID))
                if decision is None:
                    return {'pending': False}
                value = decision.decision
                params = {'sessionId': SID, 'expectedRunId': str(launch.run_id), 'decisionId': decision.id,
                    'choiceId': 'accept' if data.get('allow') else 'decline', 'expectedRevision': value['revision'],
                    'actionDigest': value['actionDigest']}
            registered = Hub(ctx).register(resolve, SID, str(launch.run_id), 'restart-original-answer',
                'decision.reply', p.operation_digest('decision.reply', params))
            original_call = launch.peer.call
            async def capture_answer(method, payload):
                if method == 'owner.answer':
                    path = Path('/evidence/old-answer-grant.json')
                    path.write_text(json.dumps(payload))
                    path.chmod(0o600)
                    with dbs() as db:
                        directory = Path(db.get(Session, SID).run_dir) / 'relay'
                    channel = Path('/evidence/old-native-channel.json')
                    channel.write_bytes((directory / 'owner-channel.json').read_bytes())
                    channel.chmod(0o600)
                return await original_call(method, payload)
            launch.peer.call = capture_answer
            result = await launch.answer(resolve, {**params, 'operationId': registered.operation_id})
            launch.peer.call = original_call
            return {'operation': registered.operation_id, 'outcome': result['outcome']}
        if method.startswith('replace_scope_'):
            kind = method.removeprefix('replace_scope_')
            @write_intent
            def replace_scope():
                with dbs() as db:
                    row = db.get(Session, SID)
                    if kind == 'account':
                        row.run_as = 'example-other-agent'
                    elif kind == 'directory':
                        row.run_dir = str(Path(row.run_dir).with_name('replacement-directory'))
                    elif kind == 'node-key':
                        db.scalar(select(Node).where(Node.name == row.host)).public_key = 'replacement-node-public-key'
                    else:
                        raise ValueError('Unknown placement scope fault')
                    db.commit()
            replace_scope()
            return {'replaced': kind}
        if method == 'recover_application':
            with dbs() as db:
                original = db.scalar(select(RelayOperation).where(RelayOperation.registration_key == 'restart-original-answer'))
                identifier = original.id
            recovered = await manager.recover_application(resolve, SID, identifier)
            return {'recovered': recovered}
        if method == 'stage':
            launch = manager.runs[SID]
            token, operation_id = issue(launch)
            operation = launch.grants[token].operation
            await launch.peer.call('stage', {'grant': token})
            Hub(ctx).queue(resolve, SID, operation_id, expected_run=str(launch.run_id), digest=operation.payload_digest)
            Hub(ctx).claim(resolve, SID, operation_id)
            # Private fixture journal; never a production capability source.
            Path('/evidence/main-old-grant.json').write_text(json.dumps({'token': token, 'operation': operation_id}))
            return {'operation': operation_id}
        if method == 'old_grant':
            token = json.loads(Path('/evidence/main-old-grant.json').read_text())['token']
            try:
                await manager.runs[NEXT].peer.call('dispatch', {'grant': token})
            except ConnectionError:
                return {'refused': True}
            raise AssertionError('Stale main grant admitted')
        if method == 'demote':
            # Existing HA runtime stop path, with only its worker dependencies
            # represented by empty fixture services; real relay/broker close.
            from acc.server.ha_runtime import Runtime
            ctx.supervisor.tools = SimpleNamespace(tasks=[])
            ctx.supervisor._backup_task = None
            ctx.supervisor.relay_manager = manager
            ctx.publisher = SimpleNamespace(tasks=[])
            runtime = Runtime(authority, ctx)
            epoch = authority.epoch
            await runtime.stop()
            authority.demote(expected_epoch=epoch)
            return {'authority': authority.status()}
        raise ValueError('Unknown private main operation')
    async def serve(reader, writer):
        try:
            method = json.loads(await asyncio.wait_for(reader.readline(), 5))['method']
            try:
                result = await operation(method)
            except Fenced:
                result = {'fenced': True}
            except (p.RelayError, LaunchRefused) as error:
                result = {'refused': True}
                if method == 'launch_fresh':
                    # Private synthetic launch diagnostics, never a public error
                    # or a reason to bypass the production enrollment checks.
                    result['reason'] = str(error)
                    import traceback
                    result['refusal'] = traceback.format_exc()
            writer.write(json.dumps(result).encode() + b'\n')
            await writer.drain()
        except Exception as error:
            writer.write(json.dumps({'error': repr(error)}).encode() + b'\n')
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
    Path('/evidence/main.sock').unlink(missing_ok=True)
    server = await asyncio.start_unix_server(serve, '/evidence/main.sock')
    try:
        await asyncio.Event().wait()
    finally:
        server.close()
        await server.wait_closed()
        await manager.close()
        await brokers.close()
        authority.release()
        engine.dispose()


if __name__ == '__main__':
    asyncio.run(main())
