"""Operator qualification boundary; no live credentials or provider requests."""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from acc import dns_qualify as kit
from acc.server.dns_providers import DNSError, Record


class Provider:
    name = 'cloudflare'
    capabilities = {'A', 'AAAA'}
    atomic_family_change = True

    def __init__(self):
        self.records = {}
        self.closed = False

    async def zones(self):
        return [{'id': 'z1', 'name': 'example.com'}]

    async def list_records(self, zone, host):
        return list(self.records.get(host, []))

    async def write_record(self, zone, record, previous):
        from dataclasses import replace
        record = replace(record, id='r1')
        self.records[record.name] = [record]
        return record

    async def remove_record(self, zone, record):
        del self.records[record.name]

    async def close(self):
        self.closed = True


def args(tmp_path):
    binary = tmp_path / 'candidate'
    binary.write_bytes(b'reviewed test executable')
    binary.chmod(0o700)
    return argparse.Namespace(provider='cloudflare', disposable_zone='example.com',
        confirm_zone='example.com', state_dir=tmp_path / 'state', caddy=binary,
        candidate_sha256=hashlib.sha256(binary.read_bytes()).hexdigest())


def test_qualification_real_ledger_lifecycle_and_private_report(tmp_path, monkeypatch):
    options = args(tmp_path)
    providers = [Provider(), Provider()]
    monkeypatch.setattr(kit, 'prompt_credentials', lambda name: ({'api_token': 'publishing-token'}, {'api_token': 'certificate-token'}))
    iterator = iter(providers)
    monkeypatch.setattr(kit, 'make_provider', lambda *a: next(iterator))
    async def certificate(provider, fields, zone, name, binary, state, *, renewal):
        assert renewal is True
        assert Path(binary).read_bytes() == b'reviewed test executable'
        assert zone == 'z1'
        return ['PASS certificate staging issuance', 'PASS certificate forced staging renewal']
    monkeypatch.setattr(kit, 'certificate_probe', certificate)
    assert asyncio.run(kit.run(options)) is True
    report = json.loads((options.state_dir / 'report.json').read_text())
    assert report['status'] == 'passed'
    assert report['zone'] == 'example.com'
    assert report['release_qualified'] is False
    assert report['stages'][-1] == 'PASS ledger empty'
    assert all(p.closed and not p.records for p in providers)
    assert (options.state_dir / 'report.json').stat().st_mode & 0o777 == 0o600
    assert not list(options.state_dir.glob('caddy-verified-*'))
    assert asyncio.run(kit.run(options)) is False  # interrupted/existing state never reused


def test_mismatched_confirmation_never_prompts_or_creates_state(tmp_path, monkeypatch):
    options = args(tmp_path)
    options.confirm_zone = 'other.example.com'
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: pytest.fail('prompted'))
    assert asyncio.run(kit.run(options)) is False
    assert not options.state_dir.exists()


def test_scope_refuses_foreign_zone_host_and_record():
    provider = kit.ScopedProvider(Provider(), 'z1', {'trial.example.com'})
    for zone, host in [('other', 'trial.example.com'), ('z1', 'other.example.com')]:
        with pytest.raises(DNSError):
            asyncio.run(provider.list_records(zone, host))
    with pytest.raises(DNSError):
        asyncio.run(provider.remove_record('z1', Record('r', 'foreign.example.com', 'A', ('192.0.2.1',))))


def test_first_error_stops_redacts_and_retains_state(tmp_path, monkeypatch):
    options = args(tmp_path)
    providers = [Provider(), Provider()]
    async def broken(zone, host):
        raise RuntimeError('SECRET_CANARY')
    providers[0].list_records = broken
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: ({'api_token': 'publishing-token'}, {'api_token': 'certificate-token'}))
    iterator = iter(providers)
    monkeypatch.setattr(kit, 'make_provider', lambda *a: next(iterator))
    async def forbidden(*a, **kw):
        pytest.fail('continued after error')
    monkeypatch.setattr(kit, 'certificate_probe', forbidden)
    assert asyncio.run(kit.run(options)) is False
    report = (options.state_dir / 'report.json').read_text()
    assert 'SECRET_CANARY' not in report
    assert json.loads(report)['failed_stage'] == 'publishing'
    assert all(p.closed for p in providers)


def test_no_noninteractive_secret_fallback(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    with pytest.raises(ValueError, match='interactive'):
        kit.prompt_credentials('cloudflare')


def test_parser_does_not_echo_unknown_secret(capsys):
    with pytest.raises(SystemExit):
        kit.parser().parse_args(['--api-token', 'SECRET_CANARY'])
    assert 'SECRET_CANARY' not in capsys.readouterr().err


def fake_caddy(tmp_path, *, renew=True, error=False):
    import sys
    binary = tmp_path / 'fake-caddy'
    binary.write_text('#!' + sys.executable + '\n' + '''
import json, pathlib, sys, time
from datetime import datetime, timedelta, timezone
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
config = json.load(sys.stdin)
root = pathlib.Path(config['storage']['root'])
policy = config['apps']['tls']['automation']['policies'][0]
renewing = policy.get('renewal_window_ratio') == 1
(root / ('renew-config.json' if renewing else 'issue-config.json')).write_text(json.dumps(config))
if ERROR:
    print(json.dumps({'level': 'error', 'msg': 'SECRET_CANARY'}), file=sys.stderr, flush=True)
elif not renewing or RENEW:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'test')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(2 if renewing else 1).not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(policy['subjects'][0])]), critical=False)
        .sign(key, hashes.SHA256()))
    (root / 'test.crt').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
time.sleep(60)
'''.replace('ERROR', repr(error)).replace('RENEW', repr(renew)))
    binary.chmod(0o700)
    return binary


def test_end_to_end_fake_cli_issues_renews_cleans_and_checks_ledger(tmp_path, monkeypatch):
    import httpx
    from test_publish import FakeCloudflareAPI

    from acc.server.dns_providers import CloudflareProvider
    options = args(tmp_path)
    options.caddy = fake_caddy(tmp_path)
    options.candidate_sha256 = hashlib.sha256(options.caddy.read_bytes()).hexdigest()
    remote = FakeCloudflareAPI()
    remote.zones = remote.zones[:1]
    monkeypatch.setattr(kit, 'make_provider', lambda name, fields:
        CloudflareProvider(fields['api_token'], transport=httpx.MockTransport(remote.handler)))
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: ({'api_token': 'publishing-token'}, {'api_token': 'certificate-token'}))
    assert asyncio.run(kit.run(options)) is True
    report = json.loads((options.state_dir / 'report.json').read_text())
    assert 'PASS certificate forced staging renewal' in report['stages']
    assert remote.records == {}
    assert not list(options.state_dir.glob('challenge-*.json'))


def test_caddy_error_stops_without_exposing_log_and_retains_sentinel(tmp_path):
    from acc.dns_live_test import certificate_probe
    provider = Provider()
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    with pytest.raises(DNSError, match='trial_caddy_failed'):
        asyncio.run(certificate_probe(provider, {'api_token': 'certificate-token'}, 'z1', 'example.com',
                                     str(fake_caddy(tmp_path, error=True)), state, renewal=True))
    assert len(provider.records) == 1
    assert len(list(state.glob('challenge-*.json'))) == 1


@pytest.mark.parametrize('fault', ['extra_zone', 'credential_zone', 'bad_hash', 'azure_hold'])
def test_preflight_refuses_before_remote_mutation(tmp_path, monkeypatch, fault):
    options = args(tmp_path)
    provider = Provider()
    async def zones():
        return [{'id': 'z1', 'name': 'example.com'}, {'id': 'z2', 'name': 'other.example.com'}]
    async def forbidden(*a):
        pytest.fail('remote mutation attempted')
    provider.write_record = forbidden
    monkeypatch.setattr(kit, 'make_provider', lambda *a: provider)
    pub, cert = {'api_token': 'publishing-token'}, {'api_token': 'certificate-token'}
    if fault == 'extra_zone':
        provider.zones = zones
    if fault == 'credential_zone':
        options.provider = 'digitalocean'
        pub['zone_name'] = cert['zone_name'] = 'other.example.com'
    if fault == 'bad_hash':
        options.candidate_sha256 = '0' * 64
    if fault == 'azure_hold':
        options.provider = 'azure'
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: (pub, cert))
    assert asyncio.run(kit.run(options)) is False
    report = json.loads((options.state_dir / 'report.json').read_text())
    assert report['status'] == 'failed'
    if fault == 'azure_hold':
        assert report['error'] == 'azure_shared_txt_release_hold'


def test_unapproved_binary_never_executes(tmp_path, monkeypatch):
    options = args(tmp_path)
    options.caddy.write_text('#!/bin/sh\ntouch ' + str(tmp_path / 'executed') + '\n')
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: pytest.fail('prompted before hash verification'))
    assert asyncio.run(kit.run(options)) is False
    assert not (tmp_path / 'executed').exists()


def test_prompt_uses_hidden_input_for_every_field(monkeypatch):
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    values = iter(['publishing-token', 'certificate-token'])
    monkeypatch.setattr('getpass.getpass', lambda _: next(values))
    assert kit.prompt_credentials('cloudflare') == ({'api_token': 'publishing-token'}, {'api_token': 'certificate-token'})


def test_echoing_getpass_fallback_refused(monkeypatch):
    import getpass
    import warnings
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    def fallback(_):
        warnings.warn('cannot hide input', getpass.GetPassWarning)
        pytest.fail('must not read echoed input')
    monkeypatch.setattr('getpass.getpass', fallback)
    with pytest.raises(getpass.GetPassWarning):
        kit.prompt_credentials('cloudflare')


def test_sentinel_cleanup_does_not_forget_unverified_delete(tmp_path):
    from acc.dns_live_test import cleanup_challenge_sentinel, seed_challenge_sentinel
    provider = Provider()
    async def scenario():
        sentinel, intent = await seed_challenge_sentinel(provider, 'z1', '_acme-challenge.trial.example.com', tmp_path)
        async def ambiguous(*a):
            pass  # provider acknowledged deletion but its read still shows the record
        provider.remove_record = ambiguous
        with pytest.raises(DNSError, match='trial_txt_cleanup_failed'):
            await cleanup_challenge_sentinel(provider, 'z1', sentinel, intent)
        assert intent.exists()
    asyncio.run(scenario())


def test_state_symlink_is_never_followed(tmp_path, monkeypatch):
    options = args(tmp_path)
    target = tmp_path / 'other'
    target.mkdir()
    options.state_dir.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: pytest.fail('prompted'))
    assert asyncio.run(kit.run(options)) is False
    assert list(target.iterdir()) == []


def test_certificate_unchanged_on_second_launch_is_not_renewal(tmp_path):
    from acc.dns_live_test import certificate_probe
    binary = fake_caddy(tmp_path, renew=False)
    # The second process exits after leaving the original certificate untouched.
    binary.write_text(binary.read_text().replace('time.sleep(60)', 'time.sleep(1 if renewing else 60)'))
    state = tmp_path / 'state'
    state.mkdir(mode=0o700)
    with pytest.raises(DNSError):
        asyncio.run(certificate_probe(Provider(), {'api_token': 'certificate-token'}, 'z1', 'example.com',
                                     str(binary), state, renewal=True))
    assert list(state.glob('challenge-*.json'))


def test_uncertain_write_stops_and_keeps_ledger_and_remote_record(tmp_path, monkeypatch):
    import httpx
    from sqlalchemy import select
    from test_publish import FakeCloudflareAPI

    from acc.server.db import make_engine, make_sessionmaker
    from acc.server.dns_providers import CloudflareProvider
    from acc.server.models import DNSOwnedRecord
    options = args(tmp_path)
    remote = FakeCloudflareAPI()
    remote.zones = remote.zones[:1]
    def handler(request):
        response = remote.handler(request)
        if request.method == 'POST':
            return httpx.Response(500, json={'error': 'SECRET_CANARY'})
        return response
    monkeypatch.setattr(kit, 'make_provider', lambda name, fields:
        CloudflareProvider(fields['api_token'], transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(kit, 'prompt_credentials', lambda _: ({'api_token': 'publishing-token'}, {'api_token': 'certificate-token'}))
    async def forbidden(*a, **kw):
        pytest.fail('continued after uncertain write')
    monkeypatch.setattr(kit, 'certificate_probe', forbidden)
    assert asyncio.run(kit.run(options)) is False
    assert len(remote.records) == 1
    report = (options.state_dir / 'report.json').read_text()
    assert 'SECRET_CANARY' not in report
    assert json.loads(report)['failed_stage'] == 'publishing'
    engine = make_engine('sqlite:///' + str(options.state_dir / 'ledger.db'))
    try:
        with make_sessionmaker(engine)() as session:
            row = session.scalar(select(DNSOwnedRecord))
            assert row.state == 'uncertain'
    finally:
        engine.dispose()
