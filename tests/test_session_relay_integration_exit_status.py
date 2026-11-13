"""Native status comes from exact whole-tree evidence, never helper exit encoding."""
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from acc.broker.relay_enclosure import Enclosure, _write
from acc.broker.relay_receipts import signature
from acc.broker.relay_runtime import BrokerRuntime, RuntimeTransportFactory
from acc.broker.session_relay import PrivateRuntime


def signed_exit(enclosure, record):
    # A test supervisor's memory-only key; exercise the real verification path.
    key = b'unit-supervisor-receipt-key-32byt'[:32].ljust(32, b'!')
    with enclosure.receipt_keys.lock:
        enclosure.receipt_keys.keys[record['pid'], record['started']] = key
    return {**record, 'hmac': signature(key, 'exit.json', record)}


@pytest.mark.parametrize('status', [-9, 0, 7, True, '0', None])
def test_native_status_requires_exact_exit_receipt_and_survives_fd_retirement(tmp_path, status):
    enclosure = Enclosure(tmp_path, SimpleNamespace(run_id='run'), '/bin/true')
    identity = '123:456:private-suffix'
    try:
        _write(enclosure.fd, 'exit.json', signed_exit(enclosure, {'runId': 'run', 'token': enclosure.token,
            'pid': 123, 'started': '456', 'subreaper': True, 'empty': True, 'status': status}))
        expected = status if type(status) is int else None
        assert enclosure.native_status(identity) == expected
        assert enclosure.native_status('124:456:private-suffix') is None
        runtime = PrivateRuntime.__new__(PrivateRuntime)
        runtime.enclosure, runtime.token = enclosure, identity
        assert runtime.native_exit_status() == expected
        runtime._native_status = runtime.native_exit_status()
        enclosure.close()
        assert runtime.native_exit_status() == expected
    finally:
        enclosure.close()


@pytest.mark.parametrize('native', [-9, 0, None])
async def test_native_exit_publication_does_not_infer_vendor_status_from_supervisor(native):
    state = BrokerRuntime(RuntimeTransportFactory(SimpleNamespace()), 'fixture-enrollment')
    state.owner = SimpleNamespace(runtime=SimpleNamespace(native_exit_status=lambda: native), stop=AsyncMock())
    observed = []
    async def publish(code):
        observed.append(code)
    state.publish_exit = publish
    await state.finish_exit(247)  # Python's encoding of a child SIGKILL is not exit247
    assert observed == [native]
    await state.close()


def test_native_status_uses_the_same_verified_record(tmp_path, monkeypatch):
    from acc.broker import relay_enclosure

    enclosure = Enclosure(tmp_path, SimpleNamespace(run_id='run'), '/bin/true')
    calls = []
    def read(directory, name):
        calls.append(name)
        return signed_exit(enclosure, {'runId': 'run', 'token': enclosure.token if len(calls) == 1 else 'foreign',
            'pid': 123, 'started': '456', 'subreaper': True, 'empty': True,
            'status': -9 if len(calls) == 1 else 0})
    monkeypatch.setattr(relay_enclosure, '_read', read)
    try:
        assert enclosure.native_status('123:456:private-suffix') == -9
        assert calls == ['exit.json']
    finally:
        enclosure.close()


def test_unsigned_and_modified_status_never_become_verified_exit(tmp_path):
    enclosure = Enclosure(tmp_path, SimpleNamespace(run_id='run'), '/bin/true')
    identity = '123:456:private-suffix'
    body = {'runId': 'run', 'token': enclosure.token, 'pid': 123, 'started': '456',
            'subreaper': True, 'empty': True, 'status': 0}
    try:
        _write(enclosure.fd, 'exit.json', body)
        assert enclosure.native_status(identity) is None
        signed = signed_exit(enclosure, body)
        (enclosure.path / 'exit.json').unlink()
        _write(enclosure.fd, 'exit.json', signed)
        assert enclosure.native_status(identity) == 0
        (enclosure.path / 'exit.json').unlink()
        _write(enclosure.fd, 'exit.json', {**signed, 'status': 7})
        assert enclosure.native_status(identity) is None
    finally:
        enclosure.close()
