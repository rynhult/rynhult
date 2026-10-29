"""S3 conformance observations. Fixed HTTPS origin, SigV4, bounded XML, no redirects.

The production path writes probes through restic only. It never changes policy or
bucket configuration and accepts no maintenance identity.
"""
from __future__ import annotations

import hashlib
import hmac
import math
import re
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import quote, urlencode, urlsplit

import httpx

from .backup_server import LocalTarget, Refused

WINDOW = 30 * 86400
MAX_PAGES = 1000


def retention_runway(receipt, now):
    """Time left before the last version-bound inventory falls below 30 days.

    This is a projection from an observation, not a new provider check or credit
    for an independent maintenance job. Unknown/future evidence stays unknown.
    """
    empty = {'retention_runway_days': None, 'retention_warning': False, 'retention_lapsed': None}
    until, checked = receipt.get('immutable_until'), receipt.get('immutability_checked_at')
    if (receipt.get('version_bound_retention') is not True
            or any(type(value) not in (int, float) or not math.isfinite(value) for value in (until, checked, now))
            or checked > now):
        return empty
    seconds = until - now - WINDOW
    return {'retention_runway_days': max(0, seconds / 86400), 'retention_warning': seconds <= 7 * 86400,
            'retention_lapsed': seconds < 0}


def fields(values):
    if not isinstance(values, dict) or not {'access_key_id', 'secret_access_key'} <= values.keys() or values.keys() - {
            'access_key_id', 'secret_access_key', 'session_token'}:
        raise ValueError('Enter the access key ID and the secret access key.')
    if any(not isinstance(v, str) or not 1 <= len(v) <= 4096 or not v.isascii() or
           any(ord(c) < 33 or ord(c) == 127 for c in v) for v in values.values()):
        raise ValueError("The S3 access key or secret contains characters that aren't allowed. Copy it again from your provider.")
    return dict(values)


def location(endpoint, bucket, region):
    try:
        parts = urlsplit(endpoint)
        if (parts.scheme != 'https' or not parts.hostname or parts.username is not None or parts.password is not None
                or parts.path not in ('', '/') or parts.query or parts.fragment or parts.port == 0
                or not re.fullmatch(r'[a-zA-Z0-9.\-\[\]:]+', parts.netloc)):
            raise ValueError
        if not re.fullmatch(r'[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]', bucket) or '..' in bucket:
            raise ValueError
        if not re.fullmatch(r'[a-z0-9-]{1,64}', region):
            raise ValueError
    except (TypeError, ValueError):
        raise Refused('Use an HTTPS endpoint with no path or credentials in it (for example https://s3.eu-west-1.amazonaws.com), '
                      'plus a bucket name and region.') from None
    return endpoint.rstrip('/'), bucket, region


def xml(response):
    try:
        root = ET.fromstring(response.content)
        for element in root.iter():
            element.tag = element.tag.split('}')[-1]
        return root
    except ET.ParseError:
        raise Refused("The S3 provider sent an answer Rynhult couldn't read. Check the endpoint address.") from None


def timestamp(value):
    try:
        deadline = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if deadline.tzinfo is None:
            raise ValueError
        return deadline.timestamp()
    except (ValueError, TypeError, AttributeError):
        raise Refused("The S3 provider didn't report a valid retention deadline for a backup object.") from None


class S3:
    def __init__(self, target, credentials, *, transport=None):
        self.target, self.credentials = target, fields(credentials)
        self.client = httpx.Client(timeout=30, follow_redirects=False, trust_env=False, transport=transport,
                                   headers={'Accept-Encoding': 'identity'})

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.client.close()

    def request(self, method, key='', query=None, body=b'', headers=None):
        t, creds = self.target, self.credentials
        path = '/' + t['bucket'] + ('/' + quote(key, safe='/~') if key else '')
        query_string = urlencode(sorted((query or {}).items()), quote_via=quote, safe='~')
        now = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        digest = hashlib.sha256(body).hexdigest()
        signed = {'host': urlsplit(t['endpoint']).netloc, 'x-amz-date': now, 'x-amz-content-sha256': digest,
                  **(headers or {})}
        if creds.get('session_token'):
            signed['x-amz-security-token'] = creds['session_token']
        names = ';'.join(sorted(signed))
        canonical = '\n'.join([method, path, query_string,
            ''.join(k + ':' + signed[k].strip() + '\n' for k in sorted(signed)), names, digest])
        scope = now[:8] + '/' + t['region'] + '/s3/aws4_request'
        message = 'AWS4-HMAC-SHA256\n' + now + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
        signing = ('AWS4' + creds['secret_access_key']).encode()
        for part in (now[:8], t['region'], 's3', 'aws4_request'):
            signing = hmac.new(signing, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(signing, message.encode(), hashlib.sha256).hexdigest()
        signed['authorization'] = f"AWS4-HMAC-SHA256 Credential={creds['access_key_id']}/{scope}, SignedHeaders={names}, Signature={signature}"
        # Bound reads before XML parsing, including errors. Never emit provider messages or request headers.
        try:
            with self.client.stream(method, t['endpoint'] + path + ('?' + query_string if query_string else ''),
                                    headers=signed, content=body) as response:
                if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                    raise Refused("Compressed answers from the S3 provider aren't accepted. Check the endpoint address.")
                content, deadline = bytearray(), time.monotonic() + 60
                for chunk in response.iter_bytes():
                    if time.monotonic() > deadline:
                        raise Refused('The S3 provider took longer than 60 seconds to answer. Try again later.')
                    content.extend(chunk)
                    if len(content) > 2 << 20:
                        raise Refused("The S3 provider's answer was too large (over 2 MiB). Check the endpoint address.")
                result = httpx.Response(response.status_code, headers=response.headers, content=bytes(content))
            if 300 <= result.status_code < 400:
                raise Refused("The S3 provider redirected Rynhult to another address. Use the bucket's regional endpoint instead.")
            return result
        except httpx.HTTPError:
            raise Refused("Rynhult couldn't reach the S3 provider. Check the endpoint address and its HTTPS certificate.") from None

    def get_xml(self, key='', query=None):
        response = self.request('GET', key, query)
        if response.status_code != 200:
            raise Refused('The S3 provider refused a read. Check that the writer key may list and read objects, and read the bucket\'s '
                          'versioning, Object Lock and retention settings.')
        return xml(response)

    def capabilities(self):
        response = self.request('GET', query={'versioning': ''})
        versioning = response.status_code == 200 and xml(response).findtext('Status') == 'Enabled'
        response = self.request('GET', query={'object-lock': ''})
        mode, days = None, 0
        if response.status_code == 200:
            root = xml(response)
            if root.findtext('ObjectLockEnabled') == 'Enabled':
                mode = root.findtext('Rule/DefaultRetention/Mode')
                try:
                    days = int(root.findtext('Rule/DefaultRetention/Days') or '0')
                    days += int(root.findtext('Rule/DefaultRetention/Years') or '0') * 365
                except ValueError:
                    raise Refused("The bucket's Object Lock settings couldn't be read: its retention period isn't a number.") from None
        return {'versioning': versioning, 'mode': mode, 'default_days': days}

    def retention(self, key, version=None):
        root = self.get_xml(key, {'retention': '', **({'versionId': version} if version else {})})
        if root.findtext('Mode') != 'COMPLIANCE':
            raise Refused("Backup objects aren't under COMPLIANCE Object Lock, so Rynhult can't verify they are protected. Turn on "
                          'default COMPLIANCE Object Lock for this bucket.')
        return timestamp(root.findtext('RetainUntilDate'))

    def probe(self, snapshot):
        evidence = self.capabilities()
        if not evidence['versioning'] or evidence['mode'] != 'COMPLIANCE' or evidence['default_days'] < 30:
            raise Refused("Turn on versioning and a default COMPLIANCE Object Lock of at least 30 days for this bucket; versioning "
                          "alone doesn't protect backups.")
        if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
            raise Refused("The backup tool didn't report a valid test snapshot. Run Check setup again.")
        key = self.target['prefix'] + '/snapshots/' + snapshot
        # A disposable VALID encrypted restic snapshot, under the actual snapshots policy.
        # Never attempt deletion against a retained ACC generation.
        response = self.request('GET', key)
        version = response.headers.get('x-amz-version-id')
        if response.status_code != 200 or not version or version == 'null':
            raise Refused("The S3 provider didn't return a versioned copy of the test object. Turn on versioning for this bucket.")
        until = self.retention(key, version)
        if until < time.time() + WINDOW - 60:
            raise Refused("The test object is kept for less than 30 days. Set the bucket's default Object Lock to at least 30 days.")
        for query, headers in (({}, {}), ({'versionId': version}, {}),
                               ({'versionId': version}, {'x-amz-bypass-governance-retention': 'true'})):
            denied = self.request('DELETE', key, query, headers=headers)
            if denied.status_code != 403 or xml(denied).findtext('Code') != 'AccessDenied':
                raise Refused('The writer key can delete backups or bypass retention. Change its policy to deny object and version '
                                      'deletion (except DeleteObject on locks/*).')
        return {**evidence, 'checked_at': time.time()}

    def retained_until(self):
        return self.retained_versions()['until']

    def retained_versions(self):
        """Inventory explicit retained versions, including old deduplicated chunks.

        A bounded complete listing is required. No claim based solely on bucket defaults.
        Transient locks are excluded; valid encrypted probe snapshots count too.
        """
        prefix, token, count, earliest = self.target['prefix'] + '/', None, 0, float('inf')
        versions = {}
        pages, deadline = 0, time.monotonic() + 7200
        while True:
            pages += 1
            if pages > MAX_PAGES or time.monotonic() > deadline:
                raise Refused('The repository has too many objects to check their retention within the time limit.')
            root = self.get_xml(query={'list-type': '2', 'prefix': prefix,
                                      **({'continuation-token': token} if token else {})})
            for item in root.findall('Contents'):
                key = item.findtext('Key') or ''
                if not key.startswith(prefix):
                    raise Refused('The S3 provider listed an object outside the backup prefix. Check the endpoint and bucket.')
                relative = key[len(prefix):]
                if relative.startswith('locks/'):
                    continue
                count += 1
                if time.monotonic() > deadline or count > 100000:
                    raise Refused('The repository has more objects than Rynhult can check for retention (100,000).')
                response = self.request('HEAD', key)
                version = response.headers.get('x-amz-version-id')
                if response.status_code != 200 or not version or version == 'null' or key in versions:
                    raise Refused("The S3 provider didn't return a versioned copy of every backup object. Check that versioning is on.")
                versions[key] = version
                earliest = min(earliest, self.retention(key, version))
            if root.findtext('IsTruncated') == 'false':
                break
            next_token = root.findtext('NextContinuationToken')
            if not next_token or next_token == token:
                raise Refused("The S3 provider's object list ended early. Try again later.")
            token = next_token
        if not count or earliest == float('inf'):
            raise Refused('No retained backup objects were found in the repository. Run a backup, then check again.')
        return {'versions': versions, 'until': earliest}


class S3Target(LocalTarget):
    """Native restic S3 with no ambient identity or proxy fallback.

    Pinned restic 0.19.1 uses minio-go v7.1.0, whose client refuses HTTP
    redirects (api.go CheckRedirect). Region and path lookup are explicit.
    """
    def __init__(self, target, password_file, credential_file, binary):
        from pathlib import Path
        super().__init__(Path('/nonexistent'), password_file, binary)
        self.repository = 's3:' + target['endpoint'] + '/' + target['bucket'] + '/' + target['prefix']
        self.credential_file, self.region, self.target = credential_file, target['region'], target

    def delete_lock(self, identity):
        """One lock object (a delete marker under versioning): the writer may delete what restic itself removes."""
        import json
        key = '/'.join(part for part in (self.target['prefix'].strip('/'), 'locks', identity) if part)
        with S3(self.target, json.loads(self.credential_file.read_bytes())) as client:
            status = client.request('DELETE', key).status_code
        if status not in (200, 204, 404):
            raise Refused(f'The S3 provider refused to remove a stale backup lock (HTTP {status}). Check that the writer key may '
                          'delete objects under locks/.')

    def env(self):
        import json
        creds = json.loads(self.credential_file.read_bytes())
        return {**super().env(), 'AWS_ACCESS_KEY_ID':creds['access_key_id'],
                'AWS_SECRET_ACCESS_KEY':creds['secret_access_key'],
                'AWS_SESSION_TOKEN':creds.get('session_token', ''), 'AWS_DEFAULT_REGION':self.region}

    def repo_args(self):
        return [*super().repo_args(), '--option', 's3.bucket-lookup=path', '--option', 's3.connections=1',
                '--option', 's3.region=' + self.region]


def encrypted_probe(backend):
    import json
    import os
    import tempfile
    with tempfile.TemporaryFile() as payload:
        payload.write(os.urandom(32))
        payload.seek(0)
        lines = backend.command('backup', '--json', '--tag', 'acc-setup-probe', '--stdin',
                                '--stdin-filename', 'setup-probe', input_file=payload).splitlines()
    summary = next(json.loads(line) for line in reversed(lines) if json.loads(line).get('message_type') == 'summary')
    snapshot = summary['snapshot_id']
    if not re.fullmatch(r'[0-9a-f]{64}', snapshot):
        raise Refused("The backup tool didn't report a valid test snapshot. Run Check setup again.")
    return snapshot
