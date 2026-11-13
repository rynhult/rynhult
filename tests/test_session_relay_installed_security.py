"""Installed native hooks through enrolled production owners, in offline labs."""
import importlib.util
import json
import shutil
import sysconfig
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.relay_native
@pytest.mark.parametrize('provider,tampered,owned_view', [
    ('codex', False, False), ('codex', True, False), ('codex', False, True),
    ('claude', False, False), ('claude', True, False)])
def test_enrolled_owner_consumes_installed_guard_and_frozen_policy(provider, tampered, owned_view, record_property):
    root = Path(__file__).resolve().parents[1]
    if not shutil.which('bwrap') or not Path('/usr/bin/' + provider).exists():
        pytest.skip('Installed bubblewrap and pinned native CLI required; no fallback')
    if not Path(sysconfig.get_path('purelib')).resolve().is_relative_to(root / '.acc'):
        pytest.skip('Self-contained locked project environment required; no host dependency mounts')
    spec = importlib.util.spec_from_file_location('installed_security_probe',
        root / 'scripts/session_relay_qualification/installed_security_probe.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result = module.capture_installed_security(provider, tampered, owned_view)
    for key in ('productionInstalledGuard', 'productionCompiledPolicy', 'nativeGuardDenied',
                'guardDenialConsumedByModel', 'targetUnchanged', 'productionNodeClient'):
        assert result[key] is True, key
    assert result['tamperedAdmissionRefused'] is tampered
    assert result['modelCalls'] == 2
    assert result['declaredCapabilities'] == {} and result['terminalMediated'] is False
    if provider == 'codex':
        assert len(result['refusedControls']) == 6
        assert result['productionOwnerStopped'] is True
    else:
        assert result['productionEmptyTreeStopped'] is True
    record_property('offline_installed_security', json.dumps(result, sort_keys=True))


@pytest.mark.parametrize('receipt', [False, True])
async def test_stopped_sdk_exit_needs_whole_tree_receipt_without_clearing_idle_ledgers(receipt):
    from acc.broker.claude_owner import ClaudeOwnerFactory
    from acc.session_relay.adapters.claude_agent import ClaudeAgentAdapter

    adapter = ClaudeAgentAdapter(policy=lambda *_: True, publish=None)
    adapter.process = SimpleNamespace(returncode=0)
    adapter.attestation = SimpleNamespace(process_identity='123:456', native_conversation='fixture-native')
    adapter.exit_verified = True
    adapter.native_tools.add('guard-denied-tool-without-post-event')
    factory = object.__new__(ClaudeOwnerFactory)
    factory.current, factory.exit_qualified = adapter, True
    factory.enclosure = SimpleNamespace(proof=lambda name, identity:
        receipt and name == 'exit.json' and identity == '123:456')
    assert await factory.exited() is receipt
    assert (await adapter.prepare_switch()).idle is False
    assert adapter.native_tools == {'guard-denied-tool-without-post-event'}


@pytest.mark.parametrize('owner_failure', [False, True])
async def test_exclusive_stop_eof_and_cancelled_waiter_release_real_resources_once(tmp_path, owner_failure):
    import asyncio
    import os
    import sqlite3

    from acc.broker.relay_ownership import ExclusiveRuntime
    from acc.broker.relay_runtime import RuntimeTransportFactory
    from acc.broker.session_relay import AdmissionJournal, PayloadStaging

    state = ExclusiveRuntime(RuntimeTransportFactory(SimpleNamespace(), owned_view_factory=None), 'fixture')
    state.key = ('fixture', 'run')
    state.factory.active[state.key] = state
    state.journal = AdmissionJournal(tmp_path / 'journal')
    state.staging = PayloadStaging(tmp_path / 'payloads')
    descriptors = state.journal.fd, state.staging.fd
    entered, release = asyncio.Event(), asyncio.Event()
    calls = []
    async def stop():
        calls.append('stop')
        entered.set()
        await release.wait()
        if owner_failure:
            raise RuntimeError('artificial owner close refusal')
    state.controller = SimpleNamespace(close=stop)
    first = asyncio.create_task(state.close())
    await entered.wait()
    second = asyncio.create_task(state.close())
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    release.set()
    if owner_failure:
        with pytest.raises(RuntimeError, match='artificial owner close refusal'):
            await second
    else:
        await second
        await state.close()
    assert calls == ['stop'] and not state.factory.active
    for fd in descriptors:
        with pytest.raises(OSError):
            os.fstat(fd)
    with pytest.raises(sqlite3.ProgrammingError):
        state.journal.db.execute('SELECT 1')
