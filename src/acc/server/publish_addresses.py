"""Owner-managed DNS targets, shared by every Publishing provider.

Blocks are never expanded: an owner picks one host address inside them.
Legacy environment values are grandfathered only by the one-time bootstrap.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
import uuid

import httpx
import psutil
from sqlalchemy import select

from .issue_sources import IssueSourceError, PublicIssueTransport
from .models import AuditLog, PublishRequest, Setting

KEY = 'publishing.addresses'
SERVICES = ('https://api.ipify.org', 'https://checkip.amazonaws.com')
DETECT_TIMEOUT = 4.0  # total DNS/connect/TLS/body deadline for each fixed service
MAX_DETECT_RESPONSE = 128


def validate(value: str, access: str) -> str:
    if access not in ('public', 'internal') or '%' in value:
        raise ValueError('Choose public or internal and an IP without a scope identifier')
    net = ipaddress.ip_network(value.strip(), strict=True)
    if net.prefixlen < (16 if net.version == 4 else 48):
        raise ValueError('Blocks must be /16 or smaller for IPv4, /48 or smaller for IPv6')
    if access == 'public':
        special = ('192.0.0.0/24', '192.88.99.0/24', '2001::/23', '2002::/16', '3fff::/20')
        if any(net.overlaps(block) for value in special
               if (block := ipaddress.ip_network(value)).version == net.version):
            raise ValueError('Public targets cannot use special-purpose or documentation ranges')
        # is_global alone includes multicast on some supported Python versions.
        for ip in (net.network_address, net.broadcast_address):
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
                raise ValueError('Public targets must be globally routable unicast addresses')
        if net.version == 6 and not net.subnet_of(ipaddress.ip_network('2000::/3')):
            raise ValueError('Public IPv6 targets must be global unicast')
    elif any(ip.is_multicast or ip.is_unspecified for ip in (net.network_address, net.broadcast_address)):
        raise ValueError('Internal targets must be unicast addresses')
    return str(net) if '/' in value else str(net.network_address)


def choose(rows: list[dict], access: str, value: str | None) -> str:
    available = [r for r in rows if r['access'] == access]
    if not value:
        row = next((r for r in available if r['default']), None)
        if row is None:
            raise ValueError(f'Add a default {access} address in Settings → Publishing')
        net = ipaddress.ip_network(row['address'])
        value = str(next(net.hosts())) if net.num_addresses > 1 else str(net.network_address)
    ip = ipaddress.ip_address(value)
    for row in available:
        net = ipaddress.ip_network(row['address'])
        if ip in net:
            if net.version == 4 and net.num_addresses > 2 and ip in (net.network_address, net.broadcast_address):
                raise ValueError('Pick a host address, not the network or broadcast address')
            return str(ip)
    raise ValueError('The chosen address is no longer in the owner’s current address list')


def inventory(db, settings) -> list[dict]:
    saved = db.get(Setting, KEY)
    if saved is None:
        rows = []
        for access, value in [('public', settings.origin_public_ip), ('internal', settings.origin_internal_ip)]:
            if value:
                rows.append(dict(id=uuid.uuid4().hex, address=str(ipaddress.ip_address(value)),
                                 label=f'Environment {access}', access=access, default=True, source='environment'))
        # Pin pre-inventory requests before owners can change the default.
        for site in db.scalars(select(PublishRequest).where(PublishRequest.action == 'publish')):
            if not (site.context or {}).get('target_ip'):
                value = settings.origin_public_ip if site.access == 'public' else settings.origin_internal_ip
                if value:
                    site.context = {**(site.context or {}), 'target_ip': str(ipaddress.ip_address(value))}
        saved = Setting(key=KEY, value={'entries': rows})
        db.add(saved)
        db.add(AuditLog(actor='system', action='publish.address_bootstrap', target=KEY,
                        detail={'entries': rows}))
        db.flush()
    return saved.value['entries']


def change(db, settings, actor: str, *, entry: dict | None = None, remove: str | None = None,
           default: str | None = None) -> list[dict]:
    rows = [dict(r) for r in inventory(db, settings)]
    if entry:
        address = validate(entry['address'], entry['access'])
        net = ipaddress.ip_network(address)
        if len(rows) >= 64:
            raise ValueError('At most 64 address entries are allowed')
        if any(net.overlaps(ipaddress.ip_network(r['address'])) for r in rows
               if ipaddress.ip_network(r['address']).version == net.version):
            raise ValueError('Address overlaps an existing entry')
        entry = dict(entry, address=address, id=uuid.uuid4().hex, source='owner',
                     default=not any(r['access'] == entry['access'] for r in rows))
        rows.append(entry)
        action, detail = 'add', entry
    else:
        row = next((r for r in rows if r['id'] == (remove or default)), None)
        if row is None:
            raise ValueError('No such address entry')
        if remove:
            net = ipaddress.ip_network(row['address'])
            for site in db.scalars(select(PublishRequest).where(PublishRequest.action == 'publish',
                                                               PublishRequest.status.in_(('approved', 'applying', 'applied', 'failed')))):
                if site.status == 'failed':
                    # A failed Caddy update can leave an existing DNS record changed.
                    # Reserve it until a later successful operation reconciles the host.
                    if not site.dns_record_id:
                        continue
                    reconciled = db.scalar(select(PublishRequest.id).where(
                        PublishRequest.host == site.host,
                        PublishRequest.status.in_(('applied', 'removed')),
                        PublishRequest.completed_at > site.completed_at))
                    if reconciled:
                        continue
                target = (site.context or {}).get('target_ip')
                if site.access == row['access'] and (target is None or ipaddress.ip_address(target) in net):
                    raise ValueError('Address is in use; move or unpublish those sites first')
            rows.remove(row)
            if row['default']:
                replacement = next((r for r in rows if r['access'] == row['access']), None)
                if replacement:
                    replacement['default'] = True
        else:
            for r in rows:
                if r['access'] == row['access']:
                    r['default'] = r['id'] == default
        action, detail = ('remove' if remove else 'default'), row
    db.get(Setting, KEY).value = {'entries': rows}
    db.add(AuditLog(actor=actor, action=f'publish.address_{action}', target=KEY, detail=detail))
    return rows


async def detect(*, transport=None, interfaces=None) -> dict:
    suggestions, failures = [], []
    async def probe(url):
        try:
            # Keep a distinct pool/TLS context per service: the pinned IP must never merge two
            # original hostnames into one reusable HTTP connection.
            async with asyncio.timeout(DETECT_TIMEOUT), httpx.AsyncClient(
                    timeout=3, follow_redirects=False, trust_env=False,
                    transport=transport if transport is not None else PublicIssueTransport(),
                    headers={'Accept-Encoding': 'identity'}) as client:
                async with client.stream('GET', url) as response:
                    response.raise_for_status()
                    if response.headers.get('content-encoding', 'identity').lower() != 'identity':
                        raise ValueError('compressed detection response')
                    content = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(content) + len(chunk) > MAX_DETECT_RESPONSE:
                            raise ValueError('oversized detection response')
                        content.extend(chunk)
                    value = validate(content.decode('ascii').strip(), 'public')
                    suggestions.append({'address': value, 'source': url, 'access': 'public'})
        except (httpx.HTTPError, ValueError, TimeoutError, IssueSourceError):
            failures.append(url)
    await asyncio.gather(*(probe(url) for url in SERVICES))
    external = [r['address'] for r in suggestions]
    for name, addrs in (psutil.net_if_addrs() if interfaces is None else interfaces).items():
        for addr in addrs:
            if addr.family not in (socket.AF_INET, socket.AF_INET6):
                continue
            value = addr.address.split('%')[0]
            try:
                access = 'public'
                validate(value, access)
            except ValueError:
                access = 'internal'
            try:
                validate(value, access)
                suggestions.append({'address': value, 'source': f'Interface {name}', 'access': access})
            except ValueError:
                pass
    return {'suggestions': suggestions, 'failures': failures,
            'agreement': len(external) == len(SERVICES) and len(set(external)) == 1}
