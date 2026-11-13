"""Private durable fact bounds and file trust, never producer/query authority."""
import json
import os
import pwd
from types import SimpleNamespace

import pytest

from acc.broker import application_proofs as proofs
from acc.broker.config import BrokerConfig
from acc.session_relay import protocol as p


@pytest.fixture
def fact(tmp_path):
    workspace = tmp_path / 'workspace'
    workspace.mkdir(mode=0o700)
    run = tmp_path / 'runs' / 'abc123def456'
    store = proofs.ApplicationProofs(run / 'relay')
    binding = p.Binding('fixture-binding', 'fixture-install', 'fixture-node', pwd.getpwuid(os.getuid()).pw_name,
        str(workspace), 'claude', 'fixture-native', p.sha256_digest(b'guards'))
    operation = {'operationId': 'a' * 32, 'sessionId': run.name, 'runId': '1', 'method': 'decision.reply',
        'payloadDigest': p.sha256_digest(b'original'), 'actor': {'kind': 'person', 'principal': 'alex', 'channel': 'chat'},
        'fence': {'authority_epoch': 1, 'broker_incarnation': 'old-broker', 'hub_incarnation': 'old-main'}}
    application = {'decisionId': 'fixture-decision', 'effect': 'deny', 'actionDigest': p.sha256_digest(b'action'),
        'nativeRef': 'fixture-native:fixture-decision:fixture-tool'}
    scope = {'directory': str(run), 'binding': vars(binding), 'operation': operation}
    config = BrokerConfig(socket_path=tmp_path / 'unused.sock', run_dir=run.parent, allowed_roots=(workspace,))
    yield SimpleNamespace(store=store, binding=binding, operation=operation, application=application, scope=scope,
        config=config, file=store.root / proofs.filename(operation['operationId']))
    store.close()


def test_atomic_idempotent_facts_survive_reopen_without_replay(fact):
    c = fact
    c.store.remember(c.binding, {'operation': c.operation}, c.application)
    c.store.remember(c.binding, {'operation': c.operation}, c.application)
    assert len(list(c.store.root.iterdir())) == 1
    c.store.close()
    assert proofs.read(c.config, c.scope)['application'] == c.application


@pytest.mark.parametrize('fault', ['symlink', 'hardlink', 'mode', 'actor', 'digest', 'binding', 'version', 'oversize'])
def test_untrusted_or_substituted_fact_never_returns(fact, fault):
    c = fact
    c.store.remember(c.binding, {'operation': c.operation}, c.application)
    if fault == 'symlink':
        source = c.file.with_suffix('.old')
        c.file.rename(source)
        c.file.symlink_to(source)
    elif fault == 'hardlink':
        os.link(c.file, c.file.with_suffix('.copy'))
    elif fault == 'mode':
        c.file.chmod(0o644)
    elif fault == 'oversize':
        c.file.write_bytes(b'x' * (proofs.MAX_BYTES + 1))
    else:
        value = json.loads(c.file.read_text())
        if fault == 'actor':
            value['operation']['actor']['principal'] = 'another-person'
        elif fault == 'digest':
            value['operation']['payloadDigest'] = p.sha256_digest(b'changed')
        elif fault == 'binding':
            value['binding']['account'] = 'another-account'
        elif fault == 'version':
            value['version'] = True
        c.file.write_text(json.dumps(value))
    with pytest.raises((p.RelayError, OSError)):
        proofs.read(c.config, c.scope)


def test_capacity_and_conflicts_never_replace_existing_fact(fact, monkeypatch):
    c = fact
    monkeypatch.setattr(proofs, 'MAX_RECORDS', 1)
    c.store.remember(c.binding, {'operation': c.operation}, c.application)
    with pytest.raises(p.RelayError):
        c.store.remember(c.binding, {'operation': c.operation}, {**c.application, 'effect': 'allow_once'})
    with pytest.raises(p.RelayError) as limit:
        c.store.remember(c.binding, {'operation': {**c.operation, 'operationId': 'b' * 32}}, c.application)
    assert limit.value.code is p.ErrorCode.LIMIT
    assert proofs.read(c.config, c.scope)['application'] == c.application


def test_absent_fact_is_read_only_and_creates_nothing(fact):
    c = fact
    assert proofs.read(c.config, c.scope) is None
    assert list(c.store.root.iterdir()) == []


@pytest.mark.parametrize('fault', ['cancel', 'guard'])
async def test_post_write_cancellation_or_guard_loss_retires_unqualified_fact(fact, monkeypatch, fault):
    import asyncio
    import threading

    from acc.broker.relay_ownership import ExclusiveRuntime

    c = fact
    written, release = threading.Event(), threading.Event()
    original = c.store.remember
    def remember(*args):
        original(*args)
        written.set()
        assert release.wait(5)
    monkeypatch.setattr(c.store, 'remember', remember)
    checks = []
    def guard():
        checks.append(True)
        if fault == 'guard' and len(checks) == 2:
            raise p.RelayError(p.ErrorCode.INTEGRITY_FAILURE)
    runtime = object.__new__(ExclusiveRuntime)
    runtime.application_proofs, runtime.binding = c.store, c.binding
    runtime.application_records = {c.operation['operationId']: 'fixture'}
    runtime.guard_check = guard
    task = asyncio.create_task(runtime.persist_application({'operation': c.operation},
        {'outcome': 'accepted', 'application': c.application}))
    try:
        assert await asyncio.to_thread(written.wait, 5)
        if fault == 'cancel':
            task.cancel()
        release.set()
        with pytest.raises(asyncio.CancelledError if fault == 'cancel' else p.RelayError):
            await task
        assert not c.file.exists() and not runtime.application_records
        assert proofs.read(c.config, c.scope) is None
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)
