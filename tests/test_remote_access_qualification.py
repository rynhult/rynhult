"""Real installed-Caddy boundary tests; a removed TLS check must expose the stub."""
import ssl
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from remote_access_qualification.harness import CADDY, Lab  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(CADDY).exists(), reason='qualifies the installed Caddy; none on this machine')


@pytest.fixture
def lab():
    with Lab() as instance:
        yield instance


def test_valid_device_and_private_recovery_reach_stub(lab):
    assert lab.request('active') == 200
    assert lab.request(None, sni='acc.example.lan', host='acc.example.lan') == 200
    assert lab.count == 2


@pytest.mark.parametrize('identity', [None, 'revoked', 'expired', 'wrong_ca'])
@pytest.mark.parametrize('tls', ['TLSv1_2', 'TLSv1_3'])
def test_invalid_device_never_reaches_upstream(lab, identity, tls):
    # expired and wrong_ca ARE listed: membership must not override chain checks.
    assert lab.request(identity, tls=tls) is None
    assert lab.count == 0


def test_empty_allow_list_denies_even_valid_chain(lab):
    lab.reload([])
    for identity in [None, 'active', 'revoked', 'expired', 'wrong_ca']:
        assert lab.request(identity) is None
    assert lab.count == 0


@pytest.mark.parametrize('sni,host,identity', [
    ('acc.example.lan', 'remote.example.com', None),
    ('remote.example.com', 'acc.example.lan', 'active'),
    ('remote.example.com', 'unknown.example.com', 'active'),
    ('unknown.example.com', 'remote.example.com', 'active'),
    ('', 'remote.example.com', 'active'),
])
def test_sni_host_confusion_never_reaches_upstream(lab, sni, host, identity):
    assert lab.request(identity, sni=sni, host=host) in (None, 421)
    assert lab.count == 0


@pytest.mark.parametrize('sni,identity,target', [
    ('remote.example.com', 'active', 'acc.example.lan'),
    ('acc.example.lan', None, 'remote.example.com'),
])
def test_http2_coalescing_does_not_cross_gate(lab, sni, identity, target):
    lab.coalescing_probe(sni, identity, target)


@pytest.mark.parametrize('tls', ['TLSv1_2', 'TLSv1_3'])
def test_reload_revokes_new_connections_and_session_reuse(lab, tls):
    lab.revocation_probe(tls)


@pytest.mark.parametrize('tls', ['TLSv1_2', 'TLSv1_3'])
def test_preexisting_real_ticket_cannot_survive_revocation_reload(lab, tls):
    lab.old_ticket_probe(tls)


def test_harness_cleans_its_process_and_scratch_folder():
    with Lab() as instance:
        process, root = instance.process, instance.root
        assert instance.request('active') == 200
    assert process.poll() is not None
    assert not root.exists()


def test_counter_detects_unsafe_deprecated_empty_leaf_policy(lab):
    # Negative control, NOT a candidate: legacy [] omits leaf enforcement.
    config = lab.config([])
    auth = config['apps']['http']['servers']['qualification']['tls_connection_policies'][0]['client_authentication']
    del auth['verifiers']
    auth['trusted_leaf_certs'] = []
    lab._admin('POST', '/load', config)
    assert lab.request('revoked') == 200
    assert lab.count == 1  # Instrumentation actually detects a gate bypass.


@pytest.mark.parametrize('identity,reason', [
    ('expired', 'certificate expired'),
    ('wrong_ca', 'unknown ca'),
])
@pytest.mark.parametrize('tls', ['TLSv1_2', 'TLSv1_3'])
def test_listed_invalid_leaf_is_rejected_for_chain_reason(lab, identity, reason, tls):
    # Distinguish chain rejection from a client withholding its certificate.
    # These leaves are listed, so a membership rejection is insufficient proof.
    with pytest.raises(ssl.SSLError, match=reason):
        with lab.connect(lab.context(identity, tls)) as sock:
            sock.sendall(b'GET / HTTP/1.1\r\nHost: remote.example.com\r\n\r\n')
            sock.recv(4096)
    assert lab.count == 0
