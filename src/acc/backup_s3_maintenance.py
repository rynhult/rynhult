"""Explicit, independent-host S3 retention extension. Never imported by accd.

Conservative inventory: all surviving restic object versions, not just today's
namespace. This includes every referenced version and may retain orphaned ones.
No repository password, deletion, credential vault, scheduler or bucket mutation.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import stat
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .backup_s3 import S3, fields, location, timestamp, xml
from .backup_server import Refused, canonical, private_write
from .maintenance import lease

DAY = 86400
GIB = 1 << 30
MAX_OBJECTS = 100000
OBJECT = re.compile(r'(?:config|(?:keys|index|snapshots)/[a-f0-9]{64}|data/[a-f0-9]{2}/[a-f0-9]{64})')


def private_json(path, limit=1 << 20):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > limit:
                raise Refused('Use a private owner-held regular file (0600)')
            return json.loads(file.read(limit + 1))
    except (OSError, ValueError):
        raise Refused('Cannot read private JSON file') from None


def credentials(path):
    value = private_json(path)
    if not isinstance(value, dict) or value.get('purpose') != 's3-retention-maintenance':
        raise Refused('An explicit maintenance purpose is required')
    return fields({k: v for k, v in value.items() if k != 'purpose'})


class Maintenance:
    def __init__(self, client, config, directory, *, now=time.time, sleep=time.sleep,
                 requests_per_second=2, max_extra_gib_days=1000, storage_price=0, request_price=0):
        self.client, self.config, self.directory = client, dict(config), Path(directory)
        self.now, self.sleep = now, sleep
        self.rate, self.max_extra_gib_days = requests_per_second, max_extra_gib_days
        self.storage_price, self.request_price = storage_price, request_price
        self._last_request = None
        self._validate_config()
        for value in (self.rate, self.max_extra_gib_days, storage_price, request_price):
            if not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise Refused('Use finite nonnegative rates and budgets')
        if not 0.1 <= self.rate <= 10 or self.max_extra_gib_days <= 0:
            raise Refused('Use 0.1–10 requests/second and a positive storage budget')
        if client.credentials['access_key_id'] in self.config['writer_access_key_ids']:
            raise Refused('The writer must never be used for maintenance, regardless of its permissions')

    def _validate_config(self):
        c = self.config
        if set(c) != {'endpoint', 'bucket', 'region', 'prefix', 'writer_access_key_ids'}:
            raise Refused('Use only endpoint, bucket, region, prefix and writer_access_key_ids in the target config')
        location(c['endpoint'], c['bucket'], c['region'])
        prefix, writers = c['prefix'], c['writer_access_key_ids']
        if (not isinstance(prefix, str) or not re.fullmatch(r'[A-Za-z0-9_-]+(?:/[A-Za-z0-9_-]+)+', prefix)
                or len(prefix) > 512 or not isinstance(writers, list) or not writers
                or any(not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key) for key in writers)):
            raise Refused('Use the exact repository prefix and all writer access key IDs')
        if any(self.client.target.get(k) != c[k] for k in ('endpoint', 'bucket', 'region', 'prefix')):
            raise Refused('S3 client target binding does not match')

    def _binding(self):
        return hashlib.sha256(canonical({'target': self.config,
            'maintenance_key_id': self.client.credentials['access_key_id']})).hexdigest()

    def _request(self, method, key='', query=None, **kw):
        if self._last_request is not None:
            delay = max(0, 1 / self.rate - (time.monotonic() - self._last_request))
            if delay:
                self.sleep(delay)
        self._last_request = time.monotonic()
        return self.client.request(method, key, query, **kw)

    def _retention(self, key, version):
        response = self._request('GET', key, {'retention': '', 'versionId': version})
        if response.status_code != 200:
            raise Refused('Version retention access refused')
        root = xml(response)
        value = root.findtext('RetainUntilDate')
        if root.findtext('Mode') != 'COMPLIANCE' or not value:
            raise Refused('Every repository version must have COMPLIANCE retention')
        try:
            if datetime.fromisoformat(value.replace('Z', '+00:00')).tzinfo is None:
                raise ValueError
        except ValueError:
            raise Refused('Retention deadlines must include a UTC offset') from None
        until = timestamp(value)
        if not math.isfinite(until):
            raise Refused('Invalid retention deadline')
        return until

    def _version(self, key, version):
        prefix = self.config['prefix'] + '/'
        if (not isinstance(key, str) or not key.startswith(prefix) or not OBJECT.fullmatch(key[len(prefix):])
                or not isinstance(version, str) or version == 'null' or not 1 <= len(version) <= 1024
                or any(ord(c) < 33 or ord(c) > 126 for c in version)):
            raise Refused('Unexpected repository object or version')

    def _inventory(self):
        prefix, marker, seen, items, rows = self.config['prefix'] + '/', None, set(), [], 0
        markers = set()
        for page in range(1, 1001):
            response = self._request('GET', query={'versions': '', 'prefix': prefix, 'max-keys': '1000',
                **({'key-marker': marker[0], 'version-id-marker': marker[1]} if marker else {})})
            if response.status_code != 200:
                raise Refused('Version listing refused')
            root = xml(response)
            for row in [*root.findall('Version'), *root.findall('DeleteMarker')]:
                rows += 1
                key, version = row.findtext('Key') or '', row.findtext('VersionId') or ''
                if rows > MAX_OBJECTS or not key.startswith(prefix):
                    raise Refused('Version listing exceeds limit or repository boundary')
                if row.tag == 'DeleteMarker' or key[len(prefix):].startswith('locks/'):
                    continue
                self._version(key, version)
                pair = key, version
                if pair in seen:
                    raise Refused('Duplicate version in listing')
                seen.add(pair)
                try:
                    size = int(row.findtext('Size'))
                    if not 0 <= size <= 5 * (1 << 40):
                        raise ValueError
                except (ValueError, TypeError):
                    raise Refused('Version listing has invalid sizes') from None
                items.append({'key': key, 'version': version, 'bytes': size})
            if root.findtext('IsTruncated') == 'false':
                if not any(item['key'] == prefix + 'config' for item in items):
                    raise Refused('Version listing contains no repository config')
                self._pages = page
                return sorted(items, key=lambda item: (item['key'], item['version']))
            after = root.findtext('NextKeyMarker'), root.findtext('NextVersionIdMarker')
            if not all(after) or after in markers:
                raise Refused('Incomplete version listing')
            markers.add(after)
            marker = after
        raise Refused('Version listing exceeds page limit')

    def _prepare_directory(self):
        if self.directory.is_symlink() or any(p.is_symlink() for p in self.directory.parents):
            raise Refused('Use an independent private job directory without symlinks')
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.directory.stat()
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refused('Use a private owner-held job directory (0700)')

    def _audit(self, job, event, **data):
        path = self.directory / 'audit.jsonl'
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
        with os.fdopen(fd, 'ab') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 128 << 20:
                raise Refused('Audit journal must be private and below 128 MiB; archive it independently')
            file.write(canonical({'job_id': job, 'maintenance_key_id': self.client.credentials['access_key_id'],
                                  'event': event, 'at': self.now(), **data}) + b'\n')
            file.flush()
            os.fsync(file.fileno())

    def _completed(self, job):
        path = self.directory / 'audit.jsonl'
        if not path.exists():
            return set()
        # Same private-file checks, with JSONL rather than JSON decoding.
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        done = set()
        with os.fdopen(fd, 'rb') as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 128 << 20:
                raise Refused('Invalid private audit journal')
            for line in file:
                try:
                    row = json.loads(line)
                    if row['job_id'] == job and row['event'] in ('extended', 'already_sufficient'):
                        done.add((row['key'], row['version']))
                except (ValueError, KeyError, TypeError):
                    raise Refused('Incomplete audit journal; review before resuming') from None
        return done

    def _report(self, plan, done, status, errors=0):
        items = plan['extend']
        extra = sum(item['bytes'] * max(0, plan['until'] - max(plan['created_at'], item['before'])) / GIB / DAY for item in items)
        requests = plan['versions'] + plan['pages_estimate'] + 3 * len(items)
        return {'job_id': plan['id'], 'status': status, 'versions': plan['versions'],
                'would_extend': len(items), 'remaining': sum((i['key'], i['version']) not in done for i in items),
                'errors': errors, 'until': plan['until'], 'inventory_at': plan['created_at'],
                'inventory_gib': plan['bytes'] / GIB, 'extra_gib_days': extra,
                'estimated_extra_storage_usd': extra * self.storage_price / 30 if self.storage_price else None,
                'estimated_current_monthly_storage_usd': plan['bytes'] / GIB * self.storage_price if self.storage_price else None,
                'estimated_requests': requests, 'estimated_request_usd': requests * self.request_price / 1000 if self.request_price else None,
                'pricing_supplied': bool(self.storage_price or self.request_price),
                'objects': items, 'audit': str(self.directory / 'audit.jsonl')}

    def run(self, *, apply=False, max_updates=1000):
        self._prepare_directory()
        with lease(self.directory / '.job', timeout=.1):
            if not apply:
                started = self.now()
                inventory = self._inventory()
                observed = [{**item, 'before': self._retention(item['key'], item['version'])} for item in inventory]
                at = self.now()
                if not math.isfinite(at) or not -60 <= at - started < DAY:
                    raise Refused('Inventory expired or local clock changed; make a new dry-run plan')
                extend = [item for item in observed if item['before'] < at + 35 * DAY]
                # Whole seconds survive provider precision; rounding up preserves the 60-day floor.
                plan = {'id': uuid.uuid4().hex, 'binding': self._binding(), 'created_at': at, 'until': math.ceil(at) + 60 * DAY,
                        'versions': len(inventory), 'bytes': sum(i['bytes'] for i in inventory),
                        'pages_estimate': self._pages, 'extend': extend}
                report = self._report(plan, set(), 'planned')
                if report['extra_gib_days'] > self.max_extra_gib_days:
                    report['status'] = 'budget_exceeded'
                    self._audit(plan['id'], 'budget_refused', extra_gib_days=report['extra_gib_days'], budget=self.max_extra_gib_days)
                    return report
                private_write(self.directory / 'job.json', canonical(plan))
                self._audit(plan['id'], 'planned', versions=len(inventory), would_extend=len(extend), extra_gib_days=report['extra_gib_days'])
                return report
            if type(max_updates) is not int or not 1 <= max_updates <= MAX_OBJECTS:
                raise Refused('Use a bounded positive update batch size')
            plan = private_json(self.directory / 'job.json', 64 << 20)
            if (not isinstance(plan, dict) or type(plan.get('created_at')) not in (int, float)
                    or not math.isfinite(plan['created_at']) or plan.get('until') != math.ceil(plan['created_at']) + 60 * DAY):
                raise Refused('Invalid reviewed plan retention horizon; make a new dry-run plan')
            if plan['binding'] != self._binding():
                raise Refused('Job target or identity binding changed; make a new dry-run plan')
            age = self.now() - plan['created_at']
            if age < -60:
                raise Refused('Local clock moved backwards; make a new dry-run plan')
            if age >= DAY:
                raise Refused('Reviewed plan expired; make a new dry-run plan')
            done = self._completed(plan['id'])
            if self._report(plan, done, 'planned')['extra_gib_days'] > self.max_extra_gib_days:
                raise Refused('Reviewed plan exceeds the current storage budget')
            attempts, errors = 0, 0
            for item in plan['extend']:
                key, version = item['key'], item['version']
                self._version(key, version)
                if (key, version) in done:
                    continue
                if attempts >= max_updates:
                    break
                if not -60 <= self.now() - plan['created_at'] < DAY:
                    raise Refused('Reviewed plan expired or clock changed during apply')
                attempts += 1
                try:
                    before = self._retention(key, version)
                    if before >= plan['until'] or before >= self.now() + 35 * DAY:
                        self._audit(plan['id'], 'already_sufficient', key=key, version=version)
                    else:
                        self._audit(plan['id'], 'extend_intent', key=key, version=version, before=before, until=plan['until'])
                        until = datetime.fromtimestamp(plan['until'], timezone.utc).isoformat().replace('+00:00', 'Z')
                        body = ('<Retention xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Mode>COMPLIANCE</Mode>'
                                '<RetainUntilDate>' + until + '</RetainUntilDate></Retention>').encode()
                        response = self._request('PUT', key, {'retention': '', 'versionId': version}, body=body,
                            headers={'content-md5': base64.b64encode(hashlib.md5(body).digest()).decode(), 'content-type': 'application/xml'})
                        if response.status_code != 200 or self._retention(key, version) < plan['until']:
                            raise Refused('Retention extension was not acknowledged and verified')
                        self._audit(plan['id'], 'extended', key=key, version=version, until=plan['until'])
                    done.add((key, version))
                except Refused:
                    errors += 1
                    self._audit(plan['id'], 'failed', key=key, version=version)
            if not -60 <= self.now() - plan['created_at'] < DAY:
                raise Refused('Reviewed plan expired or clock changed during apply')
            report = self._report(plan, done, 'complete' if len(done) == len(plan['extend']) else 'partial', errors)
            self._audit(plan['id'], report['status'], remaining=report['remaining'], errors=errors)
            return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Independent S3 COMPLIANCE retention maintenance; dry-run by default')
    parser.add_argument('--target', type=Path, required=True, help='Private owner-reviewed target JSON, outside the main')
    parser.add_argument('--credentials', type=Path, required=True, help='Private independent maintenance identity JSON')
    parser.add_argument('--state', type=Path, required=True, help='Private durable job directory on the independent host')
    parser.add_argument('--apply', action='store_true', help='Explicitly apply/resume the last reviewed dry-run plan')
    parser.add_argument('--max-updates', type=int, default=1000)
    parser.add_argument('--requests-per-second', type=float, default=2)
    parser.add_argument('--max-extra-gib-days', type=float, default=1000)
    parser.add_argument('--storage-usd-per-gib-month', type=float, default=0)
    parser.add_argument('--requests-usd-per-1000', type=float, default=0)
    args = parser.parse_args(argv)
    try:
        config, identity = private_json(args.target), credentials(args.credentials)
        config['endpoint'], config['bucket'], config['region'] = location(config['endpoint'], config['bucket'], config['region'])
        with S3(config, identity) as client:
            report = Maintenance(client, config, args.state, requests_per_second=args.requests_per_second,
                max_extra_gib_days=args.max_extra_gib_days, storage_price=args.storage_usd_per_gib_month,
                request_price=args.requests_usd_per_1000).run(apply=args.apply, max_updates=args.max_updates)
        print(json.dumps(report, allow_nan=False))
        return 0 if report['status'] in ('planned', 'complete') else 1 if report['status'] == 'budget_exceeded' else 2
    except (Refused, OSError, ValueError, KeyError, TypeError):
        print('Maintenance refused; nothing more was changed. Check that the target, credentials and state are private '
              '(files 0600, folder 0700), that the maintenance key is not the writer key, and the prefix, budget, clock '
              'and job journal. Then make a fresh dry-run plan after 24 hours. No credentials or provider messages are '
              'printed.',
              file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
