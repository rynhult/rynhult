"""Owner-run disposable-zone qualification. Never called by accd or ordinary test collection.

Credentials are private JSON files, never argv/environment values. Reports contain only fixed labels.
The state directory retains uncertain mutation intents for manual reconciliation.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import secrets
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .dns_credentials import PROVIDERS, caddy_provider, check_fields
from .server.db import make_engine, make_sessionmaker
from .server.dns_providers import DNSError, OwnedDNS, Record, make_provider
from .server.models import DNSOwnedRecord


def private_json(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or
                stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 16384):
            raise ValueError('unsafe_file')
        return json.loads(os.read(fd, 16385))
    finally:
        os.close(fd)


async def publishing_probe(provider, db, zone, zone_name):
    host = 'acc-dns-trial-' + secrets.token_hex(6) + '.' + zone_name
    owned = OwnedDNS(provider, db)
    results = []
    await owned.ensure_record(zone, host, 'A', '192.0.2.1', 'trial-create')
    records = await provider.list_records(zone, host)
    if len(records) != 1 or records[0].values != ('192.0.2.1',):
        raise DNSError('trial_create_failed')
    results.append('PASS publishing create')
    await owned.ensure_record(zone, host, 'A', '192.0.2.2', 'trial-update')
    if (await provider.list_records(zone, host))[0].values != ('192.0.2.2',):
        raise DNSError('trial_update_failed')
    results.append('PASS publishing update')
    # An independent ledger must refuse this already-present name without touching it.
    with tempfile.TemporaryDirectory(prefix='acc-dns-refusal-') as scratch:
        engine = make_engine('sqlite:///' + str(Path(scratch) / 'refusal.db'))
        DNSOwnedRecord.__table__.create(engine)
        try:
            try:
                await OwnedDNS(provider, make_sessionmaker(engine)).ensure_record(zone, host, 'A', '192.0.2.99', 'foreign')
            except DNSError as error:
                if str(error) != 'ownership_refused':
                    raise
            else:
                raise DNSError('trial_ownership_failed')
        finally:
            engine.dispose()
    results.append('PASS publishing ownership refusal')
    await owned.delete_owned_record(zone, host, 'trial-delete')
    if await provider.list_records(zone, host):
        raise DNSError('trial_delete_failed')
    results.append('PASS publishing delete')
    return results


async def seed_challenge_sentinel(provider, zone, host, state):
    if await provider.list_records(zone, host):
        raise DNSError('trial_challenge_not_empty')
    value = 'acc-trial-preserve-' + secrets.token_hex(8)
    if provider.name in ('route53', 'googleclouddns', 'hetzner'):
        value = json.dumps(value)  # These APIs use DNS wire TXT values, including quotes.
    record = Record('', host, 'TXT', (value,), marker='managed by ACC trial sentinel')
    intent = Path(state) / ('challenge-' + secrets.token_hex(8) + '.json')
    with intent.open('x') as stream:
        os.chmod(intent, 0o600)
        json.dump({'zone': zone, 'desired': record.snapshot(), 'state': 'pending'}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    created = await provider.write_record(zone, record, None)
    if created.name != host or created.type != 'TXT' or created.values != record.values:
        raise DNSError('trial_sentinel_uncertain')
    with intent.open('w') as stream:
        json.dump({'zone': zone, 'applied': created.snapshot(), 'state': 'owned'}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    return created, intent


async def cleanup_challenge_sentinel(provider, zone, sentinel, intent):
    records = await provider.list_records(zone, sentinel.name)
    if (len(records) != 1 or records[0].id != sentinel.id or records[0].type != 'TXT' or
            records[0].values != sentinel.values or records[0].ttl != sentinel.ttl):
        raise DNSError('trial_sentinel_changed')
    # Caddy deliberately added/removed a challenge next to our sentinel, so its ETag changed.
    # Require the same ID and exact original values, then use the fresh conditional version.
    await provider.remove_record(zone, records[0])
    if await provider.list_records(zone, sentinel.name):
        raise DNSError('trial_txt_cleanup_failed')
    intent.unlink()


async def certificate_probe(provider, fields, zone, zone_name, binary, state, *, renewal=False):
    """Staging issuance and optional forced renewal using the same Caddy storage.

    The second launch retains the issued certificate and forces it into Caddy's
    renewal window. A different serial AND fingerprint are required. This tests
    renewal machinery without claiming to test the production lifetime timer.
    """
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes
    host = 'acc-cert-trial-' + secrets.token_hex(6) + '.' + zone_name
    challenge = '_acme-challenge.' + host
    sentinel, intent = await seed_challenge_sentinel(provider, zone, challenge, state)
    before = [(sentinel.type, sentinel.values, sentinel.ttl)]
    with tempfile.TemporaryDirectory(prefix='certificate-', dir=state) as directory:
        provider_config = caddy_provider(provider.name, fields)
        if provider.name == 'googleclouddns':
            account_file = Path(directory) / 'service-account.json'
            with account_file.open('x') as stream:
                os.chmod(account_file, 0o600)
                stream.write(fields['service_account_json'])
            provider_config['gcp_application_default'] = str(account_file)
        policy = {'subjects': [host], 'issuers': [{'module': 'acme',
                  'ca': 'https://acme-staging-v02.api.letsencrypt.org/directory',
                  'challenges': {'dns': {'provider': provider_config}}}]}
        automation = {'policies': [policy]}
        config = {'admin': {'disabled': True, 'config': {'persist': False}},
                  'storage': {'module': 'file_system', 'root': directory},
                  'apps': {'tls': {'certificates': {'automate': [host]}, 'automation': automation}}}
        previous = None
        for cycle in range(2 if renewal else 1):
            if cycle:
                policy['renewal_window_ratio'] = 1
                automation['renew_interval'] = '10s'
            with tempfile.TemporaryFile() as output:
                def bounds():
                    import resource
                    resource.setrlimit(resource.RLIMIT_FSIZE, (2 * 1024 * 1024, 2 * 1024 * 1024))
                process = subprocess.Popen([binary, 'run', '--config', '-'], stdin=subprocess.PIPE,
                    stdout=output, stderr=output, env={'PATH': '/usr/bin:/bin', 'HOME': directory}, preexec_fn=bounds)
                try:
                    process.stdin.write(json.dumps(config).encode())
                    process.stdin.close()
                    deadline = time.monotonic() + 180
                    observed = None
                    while time.monotonic() < deadline:
                        # Logs are transient private input, never printed or included in reports.
                        # Read with pread: changing the shared file offset corrupts subprocess output.
                        for line in os.pread(output.fileno(), 2 * 1024 * 1024, 0).splitlines():
                            try:
                                event = json.loads(line)
                            except (ValueError, UnicodeError):
                                continue
                            if isinstance(event, dict) and event.get('level') in ('error', 'fatal', 'panic'):
                                raise DNSError('trial_caddy_failed')
                        if process.poll() is not None:
                            raise DNSError('trial_caddy_failed')
                        for path in Path(directory).rglob('*.crt'):
                            try:
                                cert = x509.load_pem_x509_certificate(path.read_bytes())
                            except ValueError:
                                continue  # writer may be replacing a certificate
                            sans = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                            if sans.get_values_for_type(x509.DNSName) != [host]:
                                continue
                            identity = (cert.serial_number, cert.fingerprint(hashes.SHA256()))
                            if previous and (identity[0] == previous[0] or identity[1] == previous[1]):
                                continue
                            after = [(r.type, r.values, r.ttl) for r in await provider.list_records(zone, challenge)]
                            if after == before:
                                observed = identity
                                break
                        if observed is not None:
                            break
                        await asyncio.sleep(.5)
                    if observed is None:
                        raise DNSError('trial_staging_renewal_failed' if cycle else 'trial_staging_issuance_failed')
                    previous = observed
                finally:
                    if process.poll() is None:
                        process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)
        await cleanup_challenge_sentinel(provider, zone, sentinel, intent)
        results = ['PASS certificate staging issuance', 'PASS certificate shared TXT preservation',
                   'PASS certificate TXT cleanup']
        if renewal:
            results.append('PASS certificate forced staging renewal')
        return results


def parser():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--provider', required=True, choices=list(PROVIDERS))
    p.add_argument('--disposable-zone', required=True)
    p.add_argument('--ack-disposable', action='store_true', required=True)
    p.add_argument('--publishing-credentials', type=Path, required=True)
    p.add_argument('--certificate-credentials', type=Path, required=True)
    p.add_argument('--state-dir', type=Path, required=True)
    p.add_argument('--caddy', type=Path, required=True)
    p.add_argument('--candidate-sha256', required=True, help='Independently reviewed candidate digest')
    return p


async def run(args):
    os.umask(0o077)
    args.state_dir.mkdir(mode=0o700, parents=False, exist_ok=True)
    st = args.state_dir.lstat()
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise ValueError('unsafe_directory')
    # Hash before executing the candidate. Never download a digest beside the executable.
    fd = os.open(args.caddy, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022 or st.st_size > 100 * 1024 * 1024:
            raise ValueError('unsafe_binary')
        candidate = os.read(fd, 100 * 1024 * 1024 + 1)
        if hashlib.sha256(candidate).hexdigest() != args.candidate_sha256:
            raise ValueError('unapproved_binary')
    finally:
        os.close(fd)
    # Execute exactly the bytes just hashed, from the private state directory: the candidate's own path may be
    # replaced between the check and execution by anyone who can write its directory (SRD2-04).
    verified = args.state_dir / ('caddy-verified-' + secrets.token_hex(8))
    with os.fdopen(os.open(verified, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700), 'wb') as stream:
        stream.write(candidate)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        await qualify(args, verified)
    finally:
        verified.unlink()


async def qualify(args, binary):
    pub = check_fields(args.provider, private_json(args.publishing_credentials))
    cert = check_fields(args.provider, private_json(args.certificate_credentials))
    if pub == cert:
        raise ValueError('separate_credentials_required')
    for fields in (pub, cert):
        if fields.get('zone_name', args.disposable_zone) != args.disposable_zone:
            raise ValueError('zone_mismatch')
    providers = [make_provider(args.provider, fields) for fields in (pub, cert)]
    engine = make_engine('sqlite:///' + str(args.state_dir / 'ledger.db'))
    DNSOwnedRecord.__table__.create(engine, checkfirst=True)
    try:
        async with asyncio.timeout(300):
            zones = [[z for z in await provider.zones() if z['name'] == args.disposable_zone] for provider in providers]
            if any(len(z) != 1 for z in zones) or zones[0][0]['id'] != zones[1][0]['id']:
                raise ValueError('explicit_trial_zone_required')
            zone = zones[0][0]['id']
            for result in await publishing_probe(providers[0], make_sessionmaker(engine), zone, args.disposable_zone):
                print(result, flush=True)
            for result in await certificate_probe(providers[1], cert, zone, args.disposable_zone,
                                                    str(binary), args.state_dir):
                print(result, flush=True)
        print('PASS automated probes; renewal, negative IAM and security review remain release gates', flush=True)
    finally:
        for provider in providers:
            await provider.close()
        engine.dispose()


def main():
    args = parser().parse_args()
    try:
        asyncio.run(run(args))
    except Exception:
        print('FAIL qualification incomplete; retain the private state directory and reconcile trial records')
        raise SystemExit(1) from None


if __name__ == '__main__':
    main()
