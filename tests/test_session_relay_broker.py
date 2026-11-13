"""Focused relay journal/fencing tests; no live broker, account or vendor CLI."""
import asyncio
import os
import sqlite3
from dataclasses import replace

import pytest

from acc.broker.session_relay import AdmissionBroker, AdmissionJournal, PrivateRuntime
from acc.session_relay.adapters.base import AdapterOperation, AdmissionResult, Outcome, ReviewedSpec
from acc.session_relay.protocol import (
    Actor,
    ActorKind,
    Binding,
    Channel,
    ErrorCode,
    Evidence,
    EvidenceKind,
    Fence,
    RelayError,
    operation_digest,
)

ACTOR = Actor(ActorKind.PERSON, 'person-a', Channel.CHAT)


def operation(fence=None, **changes):
    params = {'text': 'private text'}
    op = AdapterOperation('op1', 'session1', 'run1', 'turn.submit', operation_digest('turn.submit', params),
                          ACTOR, fence or Fence(1, 'broker1', 'hub1'), 2000, params)
    return replace(op, **changes)


def accepted(op):
    return AdmissionResult(Outcome.ACCEPTED, Evidence(EvidenceKind.PROVIDER_ADMISSION, op.operation_id, op.run_id, 'native1'), 'turn1')


def test_claim_survives_restart_and_never_returns_fresh(tmp_path):
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    op = operation()
    journal.enroll(op.fence, 'link1')
    assert journal.claim(op, 'link1') == (True, 'uncertain', None)
    journal.close()
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    assert journal.claim(op, 'link1') == (False, 'uncertain', None)
    assert 'private text' not in (tmp_path / 'private/admission.sqlite3').read_bytes().decode(errors='ignore')
    journal.settle(op, accepted(op))
    assert journal.claim(op, 'link1')[1] == 'accepted'
    assert (tmp_path / 'private').stat().st_mode & 0o777 == 0o700
    assert (tmp_path / 'private/admission.sqlite3').stat().st_mode & 0o777 == 0o600
    journal.close()


def test_capacity_and_retention_preserve_unknown_and_block_expired_replay(tmp_path):
    now = [1000]
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: now[0], capacity=1, retention_ms=10)
    op = operation()
    journal.enroll(op.fence, 'link1')
    journal.claim(op, 'link1')
    with pytest.raises(RelayError) as error:
        journal.claim(operation(operation_id='op2'), 'link1')
    assert error.value.code is ErrorCode.LIMIT
    now[0] = 3000
    with pytest.raises(RelayError):
        journal.claim(operation(operation_id='op2', deadline_ms=4000), 'link1')
    journal.settle(op, accepted(op))
    now[0] = 3100
    assert journal.claim(operation(operation_id='op2', deadline_ms=4000), 'link1')[0]
    with pytest.raises(RelayError):
        journal.claim(op, 'link1')
    journal.close()


def test_atomic_claim_across_connections_and_changed_digest(tmp_path):
    first = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    second = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    op = operation()
    first.enroll(op.fence, 'link1')
    assert first.claim(op, 'link1')[0]
    assert not second.claim(op, 'link1')[0]
    with pytest.raises(RelayError) as error:
        second.claim(operation(payload_digest='sha256:' + '0' * 64), 'link1')
    assert error.value.code is ErrorCode.INTEGRITY_FAILURE
    first.close()
    second.close()


@pytest.mark.parametrize('attack', ['symlink', 'mode', 'schema', 'swap'])
def test_private_journal_refuses_untrusted_storage(tmp_path, attack):
    root = tmp_path / 'private'
    if attack == 'symlink':
        target = tmp_path / 'elsewhere'
        target.mkdir(mode=0o700)
        root.symlink_to(target)
        with pytest.raises(OSError):
            AdmissionJournal(root)
        return
    journal = AdmissionJournal(root)
    if attack == 'swap':
        root.rename(tmp_path / 'old')
        root.mkdir(mode=0o700)
        with pytest.raises(RelayError):
            journal.enroll(operation().fence, 'link1')
        journal.close()
        return
    journal.close()
    if attack == 'mode':
        root.chmod(0o755)
    else:
        db = sqlite3.connect(root / 'admission.sqlite3')
        db.execute('PRAGMA user_version=99')
        db.close()
    with pytest.raises(RelayError):
        AdmissionJournal(root)


async def test_post_await_disconnect_fences_dispatch_and_fresh_enrollment(tmp_path):
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    checking, proceed = asyncio.Event(), asyncio.Event()
    async def verify(op):
        checking.set()
        await proceed.wait()
        return True
    broker = AdmissionBroker(journal, verify)
    fence = Fence(1, broker.incarnation, 'hub1')
    await broker.enroll(fence, 'link1')
    calls = []
    async def send(op):
        calls.append(op)
        return accepted(op)
    task = asyncio.create_task(broker.dispatch(operation(fence), send))
    await checking.wait()
    await broker.disconnect('link1')
    proceed.set()
    with pytest.raises(RelayError):
        await task
    assert not calls
    await broker.enroll(replace(fence, authority_epoch=2), 'link2')
    op = operation(replace(fence, authority_epoch=2))
    assert (await broker.dispatch(op, send)).outcome is Outcome.ACCEPTED
    assert (await broker.dispatch(op, send)).outcome is Outcome.ACCEPTED
    assert len(calls) == 1
    await broker.disconnect('link1')  # old EOF cannot remove new enrollment
    assert broker.link[1] == 'link2'
    journal.close()


async def test_crash_after_claim_stays_unknown(tmp_path):
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    async def verify(op):
        return True
    broker = AdmissionBroker(journal, verify)
    fence = Fence(1, broker.incarnation, 'hub1')
    await broker.enroll(fence, 'link1')
    calls = []
    async def send(op):
        calls.append(op)
        raise asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await broker.dispatch(operation(fence), send)
    assert (await broker.dispatch(operation(fence), send)).outcome is Outcome.UNKNOWN
    assert len(calls) == 1
    journal.close()


async def test_private_owner_lock_and_no_launch_on_construction(tmp_path):
    binding = Binding('binding1', 'installation1', 'node1', 'account1', str(tmp_path), 'codex', 'native1', 'digest')
    runtime = PrivateRuntime(tmp_path / 'runtimes', ReviewedSpec(binding, 'run1', {'guard_digest': 'digest'}))
    assert runtime.process is None
    with pytest.raises(RelayError):
        PrivateRuntime(tmp_path / 'runtimes', runtime.spec)
    other_binding = replace(binding, binding_id='different-binding')
    with pytest.raises(RelayError):
        PrivateRuntime(tmp_path / 'runtimes', ReviewedSpec(other_binding, 'other-run', {}))
    await runtime.stop()


async def test_broker_restart_requires_new_persisted_authority_epoch(tmp_path):
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    async def verify(op):
        return True
    first = AdmissionBroker(journal, verify)
    await first.enroll(Fence(3, first.incarnation, 'hub1'), 'link1')
    second = AdmissionBroker(journal, verify)
    with pytest.raises(RelayError):
        await second.enroll(Fence(3, second.incarnation, 'hub1'), 'link2')
    await second.enroll(Fence(4, second.incarnation, 'hub2'), 'link2')
    with pytest.raises(RelayError):
        journal.claim(operation(Fence(3, first.incarnation, 'hub1')), 'link1')
    journal.close()


async def test_runtime_transport_enrollment_dispatch_and_eof_fence(tmp_path):
    import json

    from acc.broker.session_relay import RuntimeTransport
    from acc.server.broker_client import BrokerClient
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    async def verify(op):
        return True
    broker = AdmissionBroker(journal, verify)
    fence = Fence(1, broker.incarnation, 'hub1')
    async def enroll(params, incarnation, link):
        assert params == {'credential': 'fixture'} and incarnation == broker.incarnation
        return fence
    async def resolve(grant, received_fence, link):
        assert grant == 'single-operation-fixture' and received_fence == fence
        return operation(fence)
    sent = []
    async def dispatch(op):
        sent.append(op)
        return accepted(op)
    async def receipt(op_id, fence, link):
        return {'state': journal.lookup(op_id)[0]}
    transport = RuntimeTransport(broker, enroll, resolve, dispatch, receipt)
    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        await transport.serve(request['id'], request['params'], reader, writer)
    server = await asyncio.start_unix_server(handle, path=str(tmp_path / 'broker.sock'))
    async with server:
        client = BrokerClient(tmp_path / 'broker.sock')
        reader, writer = await client.session_runtime({'credential': 'fixture'})
        for request_id in (2, 3):
            writer.write(json.dumps({'id': request_id, 'version': 1, 'method': 'dispatch',
                                    'params': {'grant': 'single-operation-fixture'}}).encode() + b'\n')
            await writer.drain()
            response = json.loads(await reader.readline())
            assert response['ok'] and response['result']['outcome'] == 'accepted'
            assert 'native1' not in json.dumps(response)
        assert len(sent) == 1
        writer.close()
        await writer.wait_closed()
        for _ in range(100):
            if broker.link is None:
                break
            await asyncio.sleep(.001)
        assert broker.link is None
    journal.close()


def test_private_staging_exact_bytes_quota_and_no_unknown_eviction(tmp_path):
    from acc.broker.session_relay import PayloadStaging
    staging = PayloadStaging(tmp_path / 'payloads', capacity_bytes=8)
    data = 'å\nhello'.encode()
    reference, digest = staging.stage('op1', data)
    assert staging.read(reference, digest) == data
    assert (tmp_path / 'payloads' / reference).stat().st_mode & 0o777 == 0o600
    with pytest.raises(RelayError):
        staging.stage('op2', b'new')
    with pytest.raises(RelayError):
        staging.read('../elsewhere', digest)
    with pytest.raises(RelayError):
        staging.remove(reference, settled=False)
    staging.remove(reference, settled=True)
    staging.stage('op2', b'new')
    staging.close()


def test_staging_rejects_symlink_and_mutated_payload(tmp_path):
    from acc.broker.session_relay import PayloadStaging
    staging = PayloadStaging(tmp_path / 'payloads')
    reference, digest = staging.stage('op1', b'hello')
    target = tmp_path / 'payloads' / reference
    target.write_bytes(b'other')
    with pytest.raises(RelayError):
        staging.read(reference, digest)
    target.unlink()
    target.symlink_to(tmp_path / 'foreign')
    with pytest.raises(OSError):
        staging.read(reference, digest)
    staging.close()


async def test_supervised_owner_uses_private_stdio_and_never_restarts(tmp_path):
    import sys

    from acc.broker.session_relay import CodexRuntimeOwner
    # Entirely local fake executable; no installed CLI, account, hooks or network.
    executable = tmp_path / 'fake-codex'
    executable.write_text('#!' + sys.executable + '\n' + '''import json, sys
for line in sys.stdin:
    request = json.loads(line)
    if 'id' in request:
        print(json.dumps({'id': request['id'], 'result': {'fixture': True}}), flush=True)
''')
    executable.chmod(0o700)
    home = tmp_path / 'home'
    home.mkdir(mode=0o700)
    binding = Binding('binding1', 'install1', 'node1', 'account1', str(tmp_path), 'codex', 'native1', 'digest')
    spec = ReviewedSpec(binding, 'run1', {'guard_digest': 'digest', 'app_server_args': ['-c', 'features.hooks=true']})
    async def observe(message):
        raise AssertionError('unexpected fake notification')
    owner = await CodexRuntimeOwner.start(tmp_path / 'runtimes', spec, str(executable),
                                         {'HOME': str(home), 'CODEX_HOME': str(home / 'codex')}, observe)
    assert await owner.request('initialize', {}) == {'fixture': True}
    assert '--listen' not in str(owner.attestation)
    await owner.stop()
    assert owner.runtime.process.returncode is not None
    runtime = PrivateRuntime(tmp_path / 'runtimes', spec)
    with pytest.raises(FileExistsError):
        await runtime.start(str(executable), {'HOME': str(home)})
    await runtime.stop()


def test_private_socket_rejects_symlink_mode_and_foreign_peer(tmp_path, monkeypatch):
    import socket

    from acc import peercred
    from acc.broker.session_relay import private_dir, verify_socket
    root = tmp_path / 'sockets'
    directory = private_dir(root)
    path = root / 'native.sock'
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(str(path))
    path.chmod(0o600)
    class Writer:
        def get_extra_info(self, name):
            return sock
    writer = Writer()
    try:
        monkeypatch.setattr(peercred, 'peer_uid', lambda sock: os.getuid())
        verify_socket(path, directory, writer)
        path.chmod(0o660)
        with pytest.raises(RelayError):
            verify_socket(path, directory, writer)
        path.chmod(0o600)
        monkeypatch.setattr(peercred, 'peer_uid', lambda sock: os.getuid() + 1)
        with pytest.raises(RelayError):
            verify_socket(path, directory, writer)
        alias = root / 'alias.sock'
        alias.symlink_to(path)
        with pytest.raises(RelayError):
            verify_socket(alias, directory, writer)
    finally:
        sock.close()
        os.close(directory)


async def test_authority_is_rechecked_after_waiting_for_final_barrier(tmp_path):
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: 1000)
    admitted = []
    current = [True]
    checks = []
    async def verify(op):
        checks.append(current[0])
        return current[0]
    broker = AdmissionBroker(journal, verify)
    op = operation(fence=Fence(1, broker.incarnation, 'hub'))
    await broker.enroll(op.fence, 'link')
    async def send(op):
        admitted.append(op.operation_id)
        return accepted(op)
    try:
        async with broker.lock:
            task = asyncio.create_task(broker.dispatch(op, send))
            await asyncio.sleep(0)
            assert checks == [True]
            current[0] = False
        with pytest.raises(RelayError):
            await task
        assert checks == [True, False]
        assert admitted == []
        assert journal.lookup(op.operation_id) is None
    finally:
        journal.close()


def test_lifetime_receipt_churn_above_2048_reclaims_without_expired_replay(tmp_path):
    now = [1000]
    journal = AdmissionJournal(tmp_path / 'private', clock=lambda: now[0], capacity=2, retention_ms=1)
    fence = Fence(1, 'broker1', 'hub1')
    journal.enroll(fence, 'link1')
    first = None
    try:
        for index in range(2050):
            op = operation(fence, operation_id=f'op{index}', session_id=f'session{index}',
                           run_id=f'run{index}', deadline_ms=now[0] + 5)
            first = first or op
            assert journal.claim(op, 'link1')[0]
            journal.settle(op, accepted(op))
            now[0] += 10
        assert journal.db.execute('SELECT COUNT(*) FROM admission').fetchone()[0] <= 2
        assert journal.lookup(first.operation_id) is None
        with pytest.raises(RelayError) as error:
            journal.claim(first, 'link1')
        assert error.value.code is ErrorCode.STALE_AUTHORITY
    finally:
        journal.close()


def test_129_main_incarnations_keep_constant_persisted_fence_and_refuse_old_hubs(tmp_path):
    root = tmp_path / 'private'
    for epoch in range(1, 130):
        journal = AdmissionJournal(root, clock=lambda: 1000)
        try:
            fence = Fence(epoch, 'broker1', f'hub{epoch}')
            journal.enroll(fence, f'link{epoch}')
            if epoch > 1:
                with pytest.raises(RelayError):
                    journal.enroll(Fence(epoch - 1, 'broker1', f'hub{epoch-1}'), f'link{epoch-1}')
                with pytest.raises(RelayError):
                    journal.enroll(Fence(epoch, 'broker1', 'competing-hub'), 'competing-link')
            with pytest.raises(RelayError):
                journal.claim(operation(fence, deadline_ms=999), f'link{epoch}')
            assert journal.db.execute('SELECT COUNT(*) FROM meta').fetchone()[0] == 1
        finally:
            journal.close()
