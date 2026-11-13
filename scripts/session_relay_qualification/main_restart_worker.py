"""Disposable OS main: real runtime, simulated node wire, production HA fencing."""
import asyncio
import json
import os
import pwd
from pathlib import Path
from types import SimpleNamespace

from sqlalchemy import text

from acc.node.client import Relay
from acc.server import child_caps  # noqa: F401 - register immutable lineage for newly seeded sessions
from acc.server.api.context import Context, Principal
from acc.server.broker_client import Brokers
from acc.server.config import Settings
from acc.server.db import make_engine, make_sessionmaker, write_intent
from acc.server.ha import ADMITTED, Authority, Fenced
from acc.server.models import RelaySession, Session
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
    data = json.loads(Path('/evidence/config.json').read_text())
    root = Path(data['main_root'])
    engine = make_engine(f'sqlite:///{root}/relay.db')
    dbs = make_sessionmaker(engine)
    authority = Authority(engine, 'fixture-main')
    authority.install_write_guard()
    account = pwd.getpwuid(os.getuid()).pw_name
    socket = Path('/evidence') / (account + '.sock')
    if not socket.exists():
        socket.symlink_to('broker.sock')
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
    brokers = Brokers(Path('/evidence/broker.sock'), None, account, 'fixture-services',
        hub=SimpleNamespace(get=lambda host: link if host == HOST else None, close=close_wire), authority=authority)
    ctx = SimpleNamespace(db=dbs, broker=brokers,
        settings=Settings(data_dir=root / 'data', feature_overrides={'session_relay': True}))
    delivery = SimpleNamespace(env={}, catalog=None, check_env=lambda _: None)
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
        launch = manager.new_launch(resolve)
        with dbs() as db:
            if identifier != SID and db.get(Session, identifier) is None:
                previous = db.get(Session, SID)
                db.add(Session(id=identifier, provider='codex', name='Reviewed after OS main crash',
                    tmux_name='acc-' + identifier, launch_spec_id=previous.launch_spec_id,
                    run_as=previous.run_as, owner=previous.owner, team_host=HOST,
                    workdir=previous.workdir, run_dir=str(Path(data['run_dir']) / identifier)))
                db.flush()
            launch.reserve(db, db.get(Session, identifier))
            db.commit()
        return launch
    @write_intent
    def issue(launch, *, key='os-main-stage', text='Never replay OS-main staged input'):
        params = {'sessionId': launch.session_id, 'expectedRunId': str(launch.run_id),
            'text': text, 'expectedControlRevision': 1,
            'expectedConfigurationRevision': 0, 'ifBusy': 'reject'}
        with dbs() as db:
            db.get(RelaySession, launch.session_id).capabilities = {'text': 'qualified'}
            db.commit()
        result = Hub(ctx).register(resolve, launch.session_id, str(launch.run_id), key,
            'turn.submit', p.operation_digest('turn.submit', params))
        return launch.issue(resolve, result.operation_id, params), result.operation_id
    async def operation(method):
        if method == 'state':
            return {'pid': os.getpid(), 'main': manager.incarnation, 'authority': authority.status(),
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
        if method == 'complete_turn':
            launch = manager.runs[SID]
            token, operation_id = issue(launch, key='os-main-completed', text='Retain completed native output across OS-main crash')
            grant = launch.grants.pop(token)
            # Normal main submit path; the earlier issue has not staged or sent.
            receipt = await manager.submit(resolve, SID, {**grant.operation.params, 'operationId': operation_id})
            assert receipt.state is p.ReceiptState.ACCEPTED
            return {'operation': operation_id, 'run': str(launch.run_id)}
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
            except (p.RelayError, LaunchRefused):
                result = {'refused': True}
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
