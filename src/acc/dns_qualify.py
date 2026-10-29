"""Owner-only disposable DNS qualification: python -m acc.dns_qualify --help.

Secrets are accepted only through non-echoing interactive prompts. Reports are
fixed-label evidence, never an automatic production release approval.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import hashlib
import json
import os
import re
import secrets
import stat
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select

from .dns_credentials import PROVIDERS, check_fields
from .dns_live_test import certificate_probe, publishing_probe
from .server.db import make_engine, make_sessionmaker
from .server.dns_providers import DNSError, make_provider
from .server.models import DNSOwnedRecord


class QuietParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse normally reflects unknown arguments, including accidentally pasted secrets.
        self.exit(2, 'Invalid qualification arguments; use --help. No credential arguments are accepted.\n')


def parser():
    p = QuietParser(description=__doc__, allow_abbrev=False)
    p.add_argument('--provider', required=True, choices=list(PROVIDERS))
    p.add_argument('--disposable-zone', required=True)
    p.add_argument('--confirm-zone', required=True, help='Repeat the exact disposable zone name')
    p.add_argument('--state-dir', type=Path, required=True, help='New private directory; report.json is written here')
    p.add_argument('--caddy', type=Path, required=True)
    p.add_argument('--candidate-sha256', required=True, help='Digest supplied independently by the release reviewer')
    return p


def prompt_credentials(provider):
    if not sys.stdin.isatty():
        raise ValueError('interactive_terminal_required')
    results = []
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        for purpose in ('publishing', 'certificates'):
            fields = {}
            for field in PROVIDERS[provider]['fields']:
                fields[field] = getpass.getpass(f'{purpose} {field} (hidden): ')
            if provider == 'route53':
                token = getpass.getpass(f'{purpose} session_token (optional, hidden): ')
                if token:
                    fields['session_token'] = token
            results.append(check_fields(provider, fields))
    if results[0] == results[1]:
        raise ValueError('separate_credentials_required')
    return tuple(results)


class ScopedProvider:
    """Constrain the existing adapter to this run's zone and random trial names."""
    def __init__(self, provider, zone, hosts=None, *, zone_name=None):
        self.provider, self.zone, self.hosts = provider, zone, hosts
        self.zone_name = zone_name
        self.name = provider.name
        self.capabilities = provider.capabilities
        self.atomic_family_change = provider.atomic_family_change

    def check(self, zone, host):
        allowed = host in self.hosts if self.hosts is not None else bool(re.fullmatch(
            r'(?:acc-dns-trial-[0-9a-f]{12}|_acme-challenge\.acc-cert-trial-[0-9a-f]{12})\.'
            + re.escape(self.zone_name), host))
        if zone != self.zone or not allowed:
            raise DNSError('qualification_scope_refused')

    async def list_records(self, zone, host):
        self.check(zone, host)
        records = await self.provider.list_records(zone, host)
        if any(r.name != host for r in records):
            raise DNSError('qualification_scope_refused')
        return records

    async def write_record(self, zone, record, previous):
        self.check(zone, record.name)
        if previous is not None:
            self.check(zone, previous.name)
        result = await self.provider.write_record(zone, record, previous)
        self.check(zone, result.name)
        return result

    async def remove_record(self, zone, record):
        self.check(zone, record.name)
        return await self.provider.remove_record(zone, record)


def verified_copy(args):
    fd = os.open(args.caddy, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_mode & 0o022 or st.st_size > 100 * 1024 * 1024:
            raise ValueError('unsafe_binary')
        with os.fdopen(os.dup(fd), 'rb') as stream:
            candidate = stream.read(100 * 1024 * 1024 + 1)
        if hashlib.sha256(candidate).hexdigest() != args.candidate_sha256:
            raise ValueError('unapproved_binary')
    finally:
        os.close(fd)
    target = args.state_dir / ('caddy-verified-' + secrets.token_hex(8))
    with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o700), 'wb') as stream:
        stream.write(candidate)
        stream.flush()
        os.fsync(stream.fileno())
    return target


async def run(args):
    # Validate before any credential prompt, filesystem mutation, or provider request.
    if (args.provider not in PROVIDERS or not re.fullmatch(r'[0-9a-f]{64}', args.candidate_sha256) or
            args.disposable_zone != args.confirm_zone or
            not re.fullmatch(r'(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+'
                             r'[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?', args.disposable_zone)):
        return False
    try:
        args.state_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    except OSError:
        return False
    report = {'schema': 1, 'provider': args.provider, 'zone': args.disposable_zone,
              'status': 'failed', 'release_qualified': False, 'stages': [],
              'candidate_sha256': args.candidate_sha256, 'failed_stage': 'preflight',
              'started_at': datetime.now(timezone.utc).isoformat()}
    providers, engine, binary = [], None, None
    try:
        # No report path supplied by the caller; private fresh directory only.
        if args.provider == 'azure':
            report['error'] = 'azure_shared_txt_release_hold'
            raise ValueError('release_hold')
        binary = verified_copy(args)
        report['failed_stage'] = 'credentials'
        pub, cert = prompt_credentials(args.provider)
        pub, cert = check_fields(args.provider, pub), check_fields(args.provider, cert)
        if pub == cert or any(f.get('zone_name', args.disposable_zone) != args.disposable_zone for f in (pub, cert)):
            raise ValueError('credentials_or_zone_refused')
        for fields in (pub, cert):
            providers.append(make_provider(args.provider, fields))
        engine = make_engine('sqlite:///' + str(args.state_dir / 'ledger.db'))
        DNSOwnedRecord.__table__.create(engine)
        db = make_sessionmaker(engine)
        async with asyncio.timeout(600):
            report['failed_stage'] = 'zone_scope'
            zones = [await p.zones() for p in providers]
            # Credentials discovering extra zones do not meet this kit's one-zone contract.
            if any(len(z) != 1 or z[0]['name'] != args.disposable_zone for z in zones) or zones[0][0]['id'] != zones[1][0]['id']:
                raise ValueError('explicit_single_zone_required')
            zone = zones[0][0]['id']
            scoped = [ScopedProvider(p, zone, zone_name=args.disposable_zone) for p in providers]
            report['stages'].append('PASS explicit single zone')
            report['failed_stage'] = 'publishing'
            report['stages'].extend(await publishing_probe(scoped[0], db, zone, args.disposable_zone))
            report['failed_stage'] = 'certificate_issuance_renewal_cleanup'
            report['stages'].extend(await certificate_probe(scoped[1], cert, zone, args.disposable_zone,
                                                           str(binary), args.state_dir, renewal=True))
            report['failed_stage'] = 'ledger_check'
            with db() as session:
                if session.scalar(select(func.count()).select_from(DNSOwnedRecord)) != 0:
                    raise ValueError('ledger_not_empty')
            if list(args.state_dir.glob('challenge-*.json')):
                raise ValueError('challenge_intents_remain')
            report['stages'].append('PASS ledger empty')
            report['status'] = 'passed'
            report['failed_stage'] = None
    except (Exception, asyncio.CancelledError):
        # Neither provider exceptions nor Caddy output may reach stdout/report.
        report.setdefault('error', 'qualification_incomplete_reconcile_private_state')
    finally:
        for provider in providers:
            try:
                await provider.close()
            except Exception:
                report.update(status='failed', error='provider_close_failed')
        if engine is not None:
            engine.dispose()
        if binary is not None:
            binary.unlink(missing_ok=True)
        with os.fdopen(os.open(args.state_dir / 'report.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'w') as stream:
            json.dump(report, stream, indent=2)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
    return report['status'] == 'passed'


def main():
    args = parser().parse_args()
    try:
        passed = asyncio.run(run(args))
    except BaseException:
        passed = False
    print('PASS qualification evidence written; release review still required' if passed else
          'FAIL qualification incomplete; inspect report.json if created and reconcile private state')
    raise SystemExit(0 if passed else 1)


if __name__ == '__main__':
    main()
