"""Fixed provider registry and durable Publishing ownership.

No credential chain, caller endpoints, response bodies in errors or automatic mutation retries.
A committed pending intent is deliberately sticky after an ambiguous outcome, including process death.
"""
from __future__ import annotations

import asyncio
import ipaddress
from dataclasses import asdict, dataclass
from typing import Protocol

import httpx
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .ha import fence
from .models import DNSOwnedRecord, utcnow


class DNSError(ValueError):
    """Fixed categories only; credentials and remote errors never leave this boundary."""


@dataclass(frozen=True)
class Record:
    id: str
    name: str
    type: str
    values: tuple[str, ...]
    ttl: int = 300
    marker: str = ''
    version: str = ''

    def snapshot(self):
        return {**asdict(self), 'values': list(self.values)}


class DNSAdapter(Protocol):
    name: str
    capabilities: frozenset[str]
    atomic_family_change: bool

    async def validate_credentials(self) -> bool: ...
    async def zones(self) -> list[dict]: ...
    async def list_records(self, zone: str, host: str) -> list[Record]: ...
    async def write_record(self, zone: str, record: Record, previous: Record | None) -> Record: ...
    async def remove_record(self, zone: str, record: Record) -> None: ...
    async def close(self) -> None: ...


class DNSProvider(Protocol):
    capabilities: frozenset[str]

    async def validate_credentials(self) -> bool: ...
    async def zones(self) -> list[dict]: ...
    async def list_records(self, zone: str, host: str) -> list[Record]: ...
    async def ensure_record(self, zone: str, host: str, kind: str, value: str, request_id: str) -> Record: ...
    async def delete_owned_record(self, zone: str, host: str, request_id: str) -> None: ...
    async def close(self) -> None: ...


async def bounded_response_body(response):
    """Bound both decoded output and wire input, including a server ignoring Accept-Encoding: identity."""
    import zlib

    limit = 2 * 1024 * 1024
    if response.is_stream_consumed:  # buffered test/custom transports have already decoded their content
        if len(response.content) > limit:
            raise DNSError('provider_response_limit')
        return response.content
    encoding = response.headers.get('Content-Encoding', 'identity').strip().lower()
    if encoding not in ('identity', 'gzip', 'x-gzip', 'deflate'):
        raise DNSError('provider_response_invalid')
    decoder = None if encoding == 'identity' else zlib.decompressobj(
        zlib.MAX_WBITS if encoding == 'deflate' else zlib.MAX_WBITS | 16)
    body, prefix = bytearray(), bytearray()
    wire = 0
    raw_deflate = False
    try:
        async for chunk in response.aiter_raw(chunk_size=64 * 1024):
            wire += len(chunk)
            if wire > limit + 64 * 1024:  # include modest encoding overhead, not arbitrary zero-output input
                raise DNSError('provider_response_limit')
            if decoder is None:
                part = chunk
            else:
                if encoding == 'deflate' and not raw_deflate and not body:
                    prefix.extend(chunk)
                try:
                    part = decoder.decompress(chunk, limit - len(body) + 1)
                except zlib.error:
                    # HTTP deflate is also deployed without zlib framing. Retry only
                    # before any output, using the bounded prefix, never after accepting data.
                    if encoding != 'deflate' or raw_deflate or body:
                        raise
                    raw_deflate = True
                    decoder = zlib.decompressobj(-zlib.MAX_WBITS)
                    part = decoder.decompress(bytes(prefix), limit + 1)
                if part or raw_deflate:
                    prefix.clear()
                if decoder.unused_data:
                    raise DNSError('provider_response_invalid')
            body.extend(part)
            if len(body) > limit:
                raise DNSError('provider_response_limit')
        if decoder is not None and not decoder.eof:
            raise DNSError('provider_response_invalid')
    except zlib.error:
        raise DNSError('provider_response_invalid') from None
    return bytes(body)


class HTTPProvider:
    atomic_family_change = True
    capabilities = frozenset({'A', 'AAAA'})

    async def validate_credentials(self):
        return bool(await self.zones())

    async def close(self):
        if getattr(self, 'owns_client', True):
            await self.client.aclose()

    async def request(self, method, path, **kwargs):
        try:
            # Retry only a bounded read. A timed-out mutation is never evidence that nothing changed.
            # Uncompressed replies, as every other outbound client asks: the size limit then bounds what is decoded.
            kwargs['headers'] = {**(kwargs.get('headers') or {}), 'Accept-Encoding': 'identity'}
            async with asyncio.timeout(20):
                for attempt in range(3):
                    async with self.client.stream(method, path, **kwargs) as response:
                        body = await bounded_response_body(response)
                        if method == 'GET' and response.status_code == 429 and attempt < 2:
                            import random
                            try:
                                delay = float(response.headers.get('Retry-After', random.uniform(.05, .15) * 2 ** attempt))
                            except ValueError:
                                raise DNSError('provider_rate_limited') from None
                            if not 0 <= delay <= 2:
                                raise DNSError('provider_rate_limited')
                            await asyncio.sleep(delay)
                            continue
                        if response.status_code >= 300:
                            raise DNSError('provider_refused')
                        # The bounded reader already decoded the body; keeping Content-Encoding would decode it twice.
                        headers = [(k, v) for k, v in response.headers.multi_items()
                                   if k.lower() not in ('content-encoding', 'content-length', 'transfer-encoding')]
                        return httpx.Response(response.status_code, headers=headers, content=bytes(body))
        except (httpx.HTTPError, TimeoutError, ValueError) as error:
            if isinstance(error, DNSError):
                raise
            raise DNSError('provider_unavailable') from None


class CloudflareProvider(HTTPProvider):
    name = 'cloudflare'

    def __init__(self, token, *, transport=None):
        self.client = httpx.AsyncClient(base_url='https://api.cloudflare.com/client/v4',
            headers={'Authorization': f'Bearer {token}'}, timeout=20, transport=transport)

    async def call(self, method, path, **kwargs):
        try:
            data = (await self.request(method, path, **kwargs)).json()
            if not data.get('success'):
                raise DNSError('provider_refused')
            return data
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, DNSError):
                raise
            raise DNSError('provider_response_invalid') from None

    async def pages(self, path, **params):
        rows = []
        for page in range(1, 101):
            data = await self.call('GET', path, params={**params, 'page': page, 'per_page': 100})
            rows.extend(data['result'])
            if page >= data.get('result_info', {}).get('total_pages', 1):
                return rows
        raise DNSError('provider_page_limit')

    async def zones(self):
        return [{'id': z['id'], 'name': z['name']} for z in await self.pages('/zones')
                if z.get('status') == 'active' and '#dns_records:edit' in z.get('permissions', [])]

    @staticmethod
    def record(r):
        # Proxy state is part of ownership. A remotely enabled proxy must never be silently reset.
        return Record(r['id'], r['name'], r['type'], (r.get('content', ''),), r.get('ttl', 300),
                      (r.get('comment') or '') + (' [proxied]' if r.get('proxied') else ''))

    async def list_records(self, zone, host):
        return [self.record(r) for r in await self.pages(f'/zones/{zone}/dns_records', name=host)]

    async def write_record(self, zone, record, previous):
        body = dict(name=record.name, type=record.type, content=record.values[0], ttl=record.ttl,
                    comment=record.marker)
        if record.type in ('A', 'AAAA'):
            body['proxied'] = False
        path = f'/zones/{zone}/dns_records' + (f'/{previous.id}' if previous else '')
        data = await self.call('PUT' if previous else 'POST', path, json=body)
        return self.record(data['result'])

    async def remove_record(self, zone, record):
        await self.call('DELETE', f'/zones/{zone}/dns_records/{record.id}')


def restored(snapshot):
    return Record(**{**snapshot, 'values': tuple(snapshot['values'])})


class OwnedDNS:
    """Only a ledger row AND an exact remote snapshot authorize a mutation.

    Unique intents serialize even across processes; no DB transaction spans network I/O.
    The row records prior/applied state and request identity before making a write.
    """
    def __init__(self, provider: DNSAdapter, db):
        self.provider, self.db = provider, db
        self.capabilities = provider.capabilities

    async def validate_credentials(self):
        return await self.provider.validate_credentials()

    async def zones(self):
        return await self.provider.zones()

    async def list_records(self, zone, host):
        return await self.provider.list_records(zone, host)

    async def close(self):
        await self.provider.close()

    def row(self, s, zone, host):
        return s.scalar(select(DNSOwnedRecord).where(DNSOwnedRecord.provider == self.provider.name,
                                                    DNSOwnedRecord.zone_id == zone, DNSOwnedRecord.host == host))

    def claim(self, zone, host, request_id, desired=None):
        try:
            with self.db() as s:
                row = self.row(s, zone, host)
                if row is None:
                    row = DNSOwnedRecord(provider=self.provider.name, zone_id=zone, host=host,
                                         request_id=request_id, state='pending', desired=desired)
                    s.add(row)
                    prior = None
                else:
                    if row.state != 'owned':
                        raise DNSError('reconciliation_required')
                    # Compare-and-swap protects concurrent deferred transactions as well as API writers.
                    from sqlalchemy import update
                    result = s.execute(update(DNSOwnedRecord).where(DNSOwnedRecord.id == row.id,
                        DNSOwnedRecord.state == 'owned', DNSOwnedRecord.request_id == row.request_id).values(
                            state='pending', request_id=request_id, prior=row.applied, desired=desired, updated_at=utcnow()))
                    if result.rowcount != 1:
                        raise DNSError('reconciliation_required')
                    prior = row.applied
                s.commit()
                return prior
        except IntegrityError:
            raise DNSError('reconciliation_required') from None

    def finish(self, zone, host, state, applied):
        with self.db() as s:
            row = self.row(s, zone, host)
            row.state, row.applied, row.updated_at = state, applied, utcnow()
            if state == 'deleted':
                s.delete(row)
            s.commit()

    async def checked(self, zone, host, prior):
        records = await self.provider.list_records(zone, host)
        if prior is None:
            if records:
                raise DNSError('ownership_refused')
        elif len(records) != 1 or records[0].snapshot() != prior:
            raise DNSError('ownership_refused')
        return restored(prior) if prior else None

    async def ensure_record(self, zone, host, kind, value, request_id, *, restore=None):
        address = ipaddress.ip_address(value)
        if kind != ('AAAA' if address.version == 6 else 'A') or kind not in self.provider.capabilities:
            raise DNSError('record_type_refused')
        record = restore or Record('', host, kind, (str(address),), marker=f'managed by ACC ({request_id})')
        if record.name != host or record.type != kind or record.values != (str(address),):
            raise DNSError('record_type_refused')
        prior = self.claim(zone, host, request_id, record.snapshot())
        try:
            previous = await self.checked(zone, host, prior)
        except DNSError:
            # No mutation attempted: safe to release this intent, retaining an existing ownership claim.
            self.finish(zone, host, 'owned' if prior else 'deleted', prior)
            raise
        if previous and previous.type != record.type and not self.provider.atomic_family_change:
            self.finish(zone, host, 'owned', prior)
            raise DNSError('address_family_change_requires_unpublish')
        fence(self.db)  # checked() awaited a remote read after the intent commit
        try:
            result = await self.provider.write_record(zone, record, previous)
        except Exception:
            self.finish(zone, host, 'uncertain', prior)
            raise DNSError('mutation_uncertain_reconcile') from None
        if (result.name != host or result.type != kind or result.values != record.values or result.ttl != record.ttl):
            self.finish(zone, host, 'uncertain', prior)
            raise DNSError('mutation_uncertain_reconcile')
        self.finish(zone, host, 'owned', result.snapshot())
        return result

    async def delete_owned_record(self, zone, host, request_id):
        prior = self.claim(zone, host, request_id)
        try:
            previous = await self.checked(zone, host, prior)
            if previous is None:
                raise DNSError('ownership_refused')
        except DNSError:
            self.finish(zone, host, 'owned' if prior else 'deleted', prior)
            raise
        fence(self.db)  # refuse a stale epoch before the remote delete
        try:
            await self.provider.remove_record(zone, previous)
        except Exception:
            self.finish(zone, host, 'uncertain', prior)
            raise DNSError('mutation_uncertain_reconcile') from None
        self.finish(zone, host, 'deleted', None)


class AzureProvider(HTTPProvider):
    name = 'azure'
    atomic_family_change = False

    def __init__(self, fields, *, transport=None):
        self.fields = fields
        self.zone = (f"/subscriptions/{fields['subscription_id']}/resourceGroups/{fields['resource_group']}"
                     f"/providers/Microsoft.Network/dnsZones/{fields['zone_name']}")
        self.client = httpx.AsyncClient(base_url='https://management.azure.com', timeout=20, transport=transport)
        self.bearer = None

    async def call(self, method, path, **kwargs):
        if self.bearer is None:
            f = self.fields
            response = await self.request('POST', f"https://login.microsoftonline.com/{f['tenant_id']}/oauth2/v2.0/token",
                data={'grant_type': 'client_credentials', 'client_id': f['client_id'],
                      'client_secret': f['client_secret'], 'scope': 'https://management.azure.com/.default'})
            self.bearer = response.json()['access_token']
        headers = {'Authorization': 'Bearer ' + self.bearer, **kwargs.pop('headers', {})}
        return await self.request(method, path, headers=headers, **kwargs)

    async def zones(self):
        return [{'id': self.zone, 'name': self.fields['zone_name']}]

    async def validate_credentials(self):
        await self.list_records(self.zone, self.fields['zone_name'])
        return True

    def path(self, zone, host=None, kind=None):
        if zone != self.zone:
            raise DNSError('zone_refused')
        if host is None:
            return self.zone + '/recordsets?api-version=2018-05-01'
        name = '@' if host == self.fields['zone_name'] else host[:-(len(self.fields['zone_name']) + 1)]
        from urllib.parse import quote
        return self.zone + f'/{kind}/{quote(name, safe="")}?api-version=2018-05-01'

    def record(self, row):
        kind = row['type'].rsplit('/', 1)[-1].upper()
        props = row['properties']
        key = {'A': ('ARecords', 'ipv4Address'), 'AAAA': ('AAAARecords', 'ipv6Address')}.get(kind)
        values = tuple(sorted(r[key[1]] for r in props.get(key[0], []))) if key else ('unsupported',)
        if kind == 'TXT':
            values = tuple(sorted(''.join(r['value']) for r in props.get('TXTRecords', [])))
        host = self.fields['zone_name'] if row['name'] == '@' else row['name'] + '.' + self.fields['zone_name']
        return Record(row['id'], host, kind, values, props.get('TTL', 300), version=row.get('etag', ''))

    async def list_records(self, zone, host):
        path, records = self.path(zone), []
        for _ in range(100):
            data = (await self.call('GET', path)).json()
            records.extend(self.record(r) for r in data['value'] if
                           r['name'] == ('@' if host == self.fields['zone_name'] else host[:-(len(self.fields['zone_name']) + 1)]))
            path = data.get('nextLink')
            if not path:
                return records
            # nextLink is remote input: never send bearer credentials to another host, zone or endpoint.
            if not path.startswith('https://management.azure.com' + self.zone + '/recordsets?'):
                raise DNSError('provider_pagination_refused')
        raise DNSError('provider_page_limit')

    async def write_record(self, zone, record, previous):
        if previous and previous.type != record.type:
            raise DNSError('address_family_change_requires_unpublish')
        if previous and not previous.version:
            raise DNSError('conditional_write_unavailable')
        if record.type == 'TXT':
            properties = {'TTL': record.ttl, 'TXTRecords': [{'value': [v]} for v in record.values]}
        else:
            key, field = ('ARecords', 'ipv4Address') if record.type == 'A' else ('AAAARecords', 'ipv6Address')
            properties = {'TTL': record.ttl, key: [{field: record.values[0]}]}
        response = await self.call('PUT', self.path(zone, record.name, record.type),
            headers={'If-Match': previous.version} if previous else {'If-None-Match': '*'},
            json={'properties': properties})
        return self.record(response.json())

    async def remove_record(self, zone, record):
        if not record.version:
            raise DNSError('conditional_write_unavailable')
        await self.call('DELETE', self.path(zone, record.name, record.type), headers={'If-Match': record.version})


class Route53Provider(HTTPProvider):
    name = 'route53'

    def __init__(self, fields, *, transport=None):
        self.fields = fields
        self.client = httpx.AsyncClient(base_url='https://route53.amazonaws.com', timeout=20, transport=transport)

    async def call(self, method, path, *, content=b'', params=None):
        # Explicit static credentials only. No shared profiles, environment, STS or metadata lookup.
        import hashlib
        import hmac
        from datetime import datetime, timezone
        from urllib.parse import quote
        request = self.client.build_request(method, path, params=params, content=content)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        date = stamp[:8]
        headers = {'host': 'route53.amazonaws.com', 'x-amz-date': stamp}
        if self.fields.get('session_token'):
            headers['x-amz-security-token'] = self.fields['session_token']
        keys = sorted(headers)
        signed = ';'.join(keys)
        canonical_headers = ''.join(k + ':' + headers[k] + '\n' for k in keys)
        query = '&'.join(quote(k, safe='-_.~') + '=' + quote(v, safe='-_.~')
                         for k, v in sorted(request.url.params.multi_items()))
        canonical = '\n'.join([method, request.url.path, query, canonical_headers, signed,
                               hashlib.sha256(content).hexdigest()])
        scope = date + '/us-east-1/route53/aws4_request'
        to_sign = 'AWS4-HMAC-SHA256\n' + stamp + '\n' + scope + '\n' + hashlib.sha256(canonical.encode()).hexdigest()
        key = ('AWS4' + self.fields['secret_access_key']).encode()
        for part in (date, 'us-east-1', 'route53', 'aws4_request'):
            key = hmac.new(key, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
        headers['Authorization'] = ('AWS4-HMAC-SHA256 Credential=' + self.fields['access_key_id'] + '/' + scope +
                                    ', SignedHeaders=' + signed + ', Signature=' + signature)
        headers['Content-Type'] = 'application/xml'
        response = await self.request(method, path, params=params, content=content, headers=headers)
        import xml.etree.ElementTree as ET
        try:
            if b'<!DOCTYPE' in response.content or b'<!ENTITY' in response.content:
                raise ValueError()
            root = ET.fromstring(response.content)
            for element in root.iter():
                element.tag = element.tag.rsplit('}', 1)[-1]
            return root
        except ValueError:
            raise DNSError('provider_response_invalid') from None

    async def zones(self):
        # The ID and name are separate owner inputs. Route 53 accepts any name below a zone's apex, so an ID for a
        # parent zone would put "trial" records in it. Only the hosted zone's own SOA binds the two (SRD2-03).
        if not getattr(self, 'apex_verified', False):
            root = await self.call('GET', self.path(self.fields['hosted_zone_id']),
                                   params={'name': self.fields['zone_name'], 'type': 'SOA', 'maxitems': '1'})
            first = root.find('./ResourceRecordSets/ResourceRecordSet')
            if (first is None or first.findtext('Type') != 'SOA' or
                    first.findtext('Name', '').rstrip('.').lower() != self.fields['zone_name']):
                raise DNSError('zone_mismatch')
            self.apex_verified = True
        return [{'id': self.fields['hosted_zone_id'], 'name': self.fields['zone_name']}]

    async def validate_credentials(self):
        await self.zones()
        await self.list_records(self.fields['hosted_zone_id'], self.fields['zone_name'])
        return True

    def path(self, zone):
        if zone != self.fields['hosted_zone_id']:
            raise DNSError('zone_refused')
        return f'/2013-04-01/hostedzone/{zone}/rrset'

    async def list_records(self, zone, host):
        await self.zones()
        params = {'name': host, 'maxitems': '100'}
        records = []
        for _ in range(100):
            root = await self.call('GET', self.path(zone), params=params)
            for rr in root.findall('./ResourceRecordSets/ResourceRecordSet'):
                name = rr.findtext('Name', '').rstrip('.').lower()
                if name != host:
                    continue
                if any(child.tag not in ('Name', 'Type', 'TTL', 'ResourceRecords') for child in rr):
                    raise DNSError('shared_or_alias_rrset_refused')
                kind = rr.findtext('Type')
                records.append(Record(host + '/' + kind, name, kind,
                    tuple(sorted(r.text for r in rr.findall('./ResourceRecords/ResourceRecord/Value'))),
                    int(rr.findtext('TTL', '300'))))
            if root.findtext('IsTruncated') != 'true' or root.findtext('NextRecordName', '').rstrip('.') != host:
                return records
            params = {'name': root.findtext('NextRecordName'), 'type': root.findtext('NextRecordType'), 'maxitems': '100'}
            if root.findtext('NextRecordIdentifier'):
                params['identifier'] = root.findtext('NextRecordIdentifier')
        raise DNSError('provider_page_limit')

    async def change(self, zone, changes):
        import xml.etree.ElementTree as ET
        root = ET.Element('ChangeResourceRecordSetsRequest', xmlns='https://route53.amazonaws.com/doc/2013-04-01/')
        batch = ET.SubElement(ET.SubElement(root, 'ChangeBatch'), 'Changes')
        for action, record in changes:
            change = ET.SubElement(batch, 'Change')
            ET.SubElement(change, 'Action').text = action
            rr = ET.SubElement(change, 'ResourceRecordSet')
            for key, value in [('Name', record.name + '.'), ('Type', record.type), ('TTL', str(record.ttl))]:
                ET.SubElement(rr, key).text = value
            values = ET.SubElement(rr, 'ResourceRecords')
            for value in record.values:
                ET.SubElement(ET.SubElement(values, 'ResourceRecord'), 'Value').text = value
        result = await self.call('POST', self.path(zone), content=ET.tostring(root))
        change_id = result.findtext('./ChangeInfo/Id', '')
        import re
        if not re.fullmatch(r'/change/[A-Za-z0-9]+', change_id):
            raise DNSError('provider_response_invalid')
        async with asyncio.timeout(20):
            while True:
                state = await self.call('GET', '/2013-04-01' + change_id)
                if state.findtext('./ChangeInfo/Status') == 'INSYNC':
                    return
                await asyncio.sleep(.5)

    async def write_record(self, zone, record, previous):
        # A DELETE with the exact old set plus CREATE is atomic and rejects concurrent drift.
        await self.change(zone, ([('DELETE', previous)] if previous else []) + [('CREATE', record)])
        return Record(record.name + '/' + record.type, record.name, record.type, record.values, record.ttl)

    async def remove_record(self, zone, record):
        await self.change(zone, [('DELETE', record)])


class ZoneBoundProvider(HTTPProvider):
    """An explicit zone; names, IDs and pagination never supply credential destinations."""
    def check_zone(self, zone):
        if zone != self.zone:
            raise DNSError('zone_refused')

    def relative(self, host):
        apex = self.fields['zone_name']
        if host == apex:
            return '@'
        if not host.endswith('.' + apex):
            raise DNSError('zone_refused')
        return host[:-(len(apex) + 1)]

    async def json_call(self, method, path, **kwargs):
        return (await self.request(method, path, **kwargs)).json()


class GoogleCloudDNSProvider(ZoneBoundProvider):
    name = 'googleclouddns'

    def __init__(self, fields, *, transport=None):
        self.fields = fields
        self.zone = fields['managed_zone']
        self.base = f"/dns/v1/projects/{fields['project_id']}/managedZones/{self.zone}"
        self.zone = self.base
        self.client = httpx.AsyncClient(base_url='https://dns.googleapis.com', timeout=20, transport=transport)
        self.bearer = None
        self.expires = 0

    async def call(self, method, path, **kwargs):
        import base64
        import json
        import time

        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding
        if self.bearer is None or (self.expires and time.time() >= self.expires):
            account = json.loads(self.fields['service_account_json'])
            def encoded(data):
                return base64.urlsafe_b64encode(json.dumps(data, separators=(',', ':')).encode()).rstrip(b'=')
            now = int(time.time())
            assertion = encoded({'alg': 'RS256', 'typ': 'JWT'}) + b'.' + encoded({
                'iss': account['client_email'], 'scope': 'https://www.googleapis.com/auth/ndev.clouddns.readwrite',
                'aud': 'https://oauth2.googleapis.com/token', 'iat': now, 'exp': now + 3600})
            key = serialization.load_pem_private_key(account['private_key'].encode(), password=None)
            signature = key.sign(assertion, padding.PKCS1v15(), hashes.SHA256())
            assertion += b'.' + base64.urlsafe_b64encode(signature).rstrip(b'=')
            data = await self.json_call('POST', 'https://oauth2.googleapis.com/token', data={
                'grant_type': 'urn:ietf:params:oauth:grant-type:jwt-bearer', 'assertion': assertion.decode()})
            self.bearer = data['access_token']
            self.expires = now + min(int(data['expires_in']), 3600) - 60
        return await self.json_call(method, path, headers={'Authorization': 'Bearer ' + self.bearer}, **kwargs)

    async def zones(self):
        row = await self.call('GET', self.base)
        if (row['name'] != self.fields['managed_zone'] or row['dnsName'] != self.fields['zone_name'] + '.' or
                row.get('visibility') != 'public'):
            raise DNSError('zone_refused')
        self.verified = True
        return [{'id': self.zone, 'name': self.fields['zone_name']}]

    async def list_records(self, zone, host):
        self.check_zone(zone)
        self.relative(host)
        await self.verified_zone()
        params, rows = {'maxResults': 100}, []
        for _ in range(100):
            data = await self.call('GET', self.base + '/rrsets', params=params)
            for r in data.get('rrsets', []):
                if r['name'] == host + '.':
                    rows.append(Record(r['name'] + r['type'], host, r['type'],
                                       tuple(sorted(r.get('rrdatas', ['unsupported']))), r['ttl'],
                                       version='routing-policy' if r.get('routingPolicy') else ''))
            token = data.get('nextPageToken')
            if not token:
                return rows
            params['pageToken'] = token
        raise DNSError('provider_page_limit')

    async def verified_zone(self):
        if not getattr(self, 'verified', False):
            await self.zones()
            self.verified = True

    @staticmethod
    def wire(record):
        if record.version:
            raise DNSError('record_type_refused')
        return dict(name=record.name + '.', type=record.type, ttl=record.ttl, rrdatas=list(record.values))

    async def change(self, zone, record=None, previous=None):
        self.check_zone(zone)
        await self.verified_zone()
        body = {}
        if record:
            body['additions'] = [self.wire(record)]
        if previous:
            body['deletions'] = [self.wire(previous)]
        data = await self.call('POST', self.base + '/changes', json=body)
        for _ in range(40):
            if data['status'] == 'done':
                return
            import re
            if not re.fullmatch(r'[0-9]+', data['id']):
                raise DNSError('provider_response_invalid')
            await asyncio.sleep(.1)
            data = await self.call('GET', self.base + '/changes/' + data['id'])
        raise DNSError('provider_change_pending')

    async def write_record(self, zone, record, previous):
        self.relative(record.name)
        await self.change(zone, record, previous)
        return Record(record.name + '.' + record.type, record.name, record.type, record.values, record.ttl)

    async def remove_record(self, zone, record):
        self.relative(record.name)
        await self.change(zone, previous=record)


class DigitalOceanProvider(ZoneBoundProvider):
    name = 'digitalocean'

    def __init__(self, fields, *, transport=None):
        self.fields = fields
        self.zone = fields['zone_name']
        self.base = '/v2/domains/' + self.zone
        self.client = httpx.AsyncClient(base_url='https://api.digitalocean.com',
            headers={'Authorization': 'Bearer ' + fields['api_token']}, timeout=20, transport=transport)

    async def zones(self):
        row = (await self.json_call('GET', self.base))['domain']
        if row['name'] != self.zone:
            raise DNSError('zone_refused')
        return [{'id': self.zone, 'name': self.zone}]

    def record(self, row):
        host = self.zone if row['name'] == '@' else row['name'] + '.' + self.zone
        return Record(str(row['id']), host, row['type'], (row['data'],), row['ttl'])

    async def list_records(self, zone, host):
        self.check_zone(zone)
        relative = self.relative(host)
        rows = []
        for page in range(1, 101):
            data = await self.json_call('GET', self.base + '/records', params={'page': page, 'per_page': 100})
            rows.extend(self.record(r) for r in data['domain_records'] if r['name'] == relative)
            # Never follow provider-supplied URLs; pagination remains on the fixed endpoint.
            if not data.get('links', {}).get('pages', {}).get('next'):
                return rows
        raise DNSError('provider_page_limit')

    def path(self, record):
        import re
        if not re.fullmatch(r'[0-9]+', record.id):
            raise DNSError('provider_response_invalid')
        return self.base + '/records/' + record.id

    async def write_record(self, zone, record, previous):
        self.check_zone(zone)
        body = dict(type=record.type, name=self.relative(record.name), data=record.values[0], ttl=record.ttl)
        data = await self.json_call('PUT' if previous else 'POST',
            self.path(previous) if previous else self.base + '/records', json=body)
        return self.record(data['domain_record'])

    async def remove_record(self, zone, record):
        self.check_zone(zone)
        await self.request('DELETE', self.path(record))


class HetznerProvider(ZoneBoundProvider):
    name = 'hetzner'
    atomic_family_change = False

    def __init__(self, fields, *, transport=None):
        self.fields = fields
        self.zone = None
        self.client = httpx.AsyncClient(base_url='https://api.hetzner.cloud',
            headers={'Authorization': 'Bearer ' + fields['api_token']}, timeout=20, transport=transport)

    async def zones(self):
        row = (await self.json_call('GET', '/v1/zones/' + self.fields['zone_name']))['zone']
        if row['name'] != self.fields['zone_name'] or row['mode'] != 'primary' or type(row['id']) is not int:
            raise DNSError('zone_refused')
        self.zone, self.ttl = str(row['id']), row['ttl']
        return [{'id': self.zone, 'name': row['name']}]

    async def base(self, zone):
        if self.zone is None:
            await self.zones()
        self.check_zone(zone)
        return '/v1/zones/' + self.zone + '/rrsets'

    def record(self, row):
        host = self.fields['zone_name'] if row['name'] == '@' else row['name'] + '.' + self.fields['zone_name']
        import json
        return Record(row['name'] + '/' + row['type'], host, row['type'],
            tuple(sorted(r['value'] for r in row['records'])), row.get('ttl') or self.ttl,
            marker=json.dumps([r.get('comment', '') for r in row['records']]),
            version=json.dumps({'labels': row.get('labels', {}), 'protection': row.get('protection', {})}, sort_keys=True))

    async def list_records(self, zone, host):
        base = await self.base(zone)
        relative, rows = self.relative(host), []
        for page in range(1, 101):
            data = await self.json_call('GET', base, params={'page': page, 'per_page': 100})
            rows.extend(self.record(r) for r in data['rrsets'] if r['name'] == relative)
            if data['meta']['pagination']['next_page'] is None:
                return rows
        raise DNSError('provider_page_limit')

    async def action(self, method, path, **kwargs):
        action = (await self.json_call(method, path, **kwargs))['action']
        for _ in range(40):
            if action['status'] == 'success':
                return
            if action['status'] == 'error' or type(action['id']) is not int:
                raise DNSError('provider_refused')
            await asyncio.sleep(.1)
            action = (await self.json_call('GET', '/v1/actions/' + str(action['id'])))['action']
        raise DNSError('provider_change_pending')

    async def write_record(self, zone, record, previous):
        from urllib.parse import quote
        base = await self.base(zone)
        relative = self.relative(record.name)
        records = [{'value': v} for v in record.values]
        if previous:
            if previous.type != record.type or previous.ttl != record.ttl:
                raise DNSError('record_type_refused')
            await self.action('POST', base + '/' + quote(relative, safe='') + '/' + record.type + '/actions/set_records',
                              json={'records': records})
        else:
            await self.action('POST', base, json=dict(name=relative, type=record.type, ttl=record.ttl, records=records))
        found = await self.list_records(zone, record.name)
        if len(found) != 1:
            raise DNSError('provider_response_invalid')
        return found[0]

    async def remove_record(self, zone, record):
        from urllib.parse import quote
        base = await self.base(zone)
        await self.action('DELETE', base + '/' + quote(self.relative(record.name), safe='') + '/' + record.type)


def make_provider(name, fields, *, transport=None):
    from .infrastructure import check_fields
    fields = check_fields(name, fields)
    if name == 'cloudflare':
        return CloudflareProvider(fields['api_token'], transport=transport)
    cls = {'azure': AzureProvider, 'route53': Route53Provider, 'googleclouddns': GoogleCloudDNSProvider,
           'digitalocean': DigitalOceanProvider, 'hetzner': HetznerProvider}.get(name)
    if cls is None:
        raise DNSError('provider_refused')
    return cls(fields, transport=transport)


def bounded_provider_method(method):
    """Normalize parsing/transport failures as well as HTTP failures at each public adapter boundary."""
    from functools import wraps

    @wraps(method)
    async def bounded(*args, **kwargs):
        try:
            async with asyncio.timeout(20):
                return await method(*args, **kwargs)
        except DNSError:
            raise
        except Exception:
            raise DNSError('provider_unavailable') from None
    return bounded


for _provider in (CloudflareProvider, AzureProvider, Route53Provider, GoogleCloudDNSProvider, DigitalOceanProvider, HetznerProvider):
    for _method in ('zones', 'validate_credentials', 'list_records', 'write_record', 'remove_record'):
        setattr(_provider, _method, bounded_provider_method(getattr(_provider, _method)))
