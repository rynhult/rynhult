"""Publishing: agent/dashboard requests -> the owner's approval -> Cloudflare DNS + Caddy (via the root helper).

The root helper (deploy/publish/acc-publish) renders Caddy config from fixed templates; accd only
ever hands it {host, port, access}. DNS changes are limited to records Rynhult created.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import ssl
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from . import attention, infrastructure, publish_addresses
from . import dns_providers as dns
from .cloudflare import Cloudflare, CloudflareError
from .config import Settings
from .ha import fence
from .models import (
    AuditLog,
    DNSBinding,
    DNSOwnedRecord,
    InfrastructureCredential,
    PortReservation,
    Project,
    PublishRequest,
    Session,
    User,
)
from .node_tasks import NodeTasks
from .vault import Vault, VaultError
from .workspace_operations import WorkspaceOperations

log = logging.getLogger("acc.publish")
HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ACTIVE = ("approved", "applying", "applied")


class PublishRefused(Exception):
    pass


def now() -> datetime:
    return datetime.now(timezone.utc)


def check_request(host: str, port: int | None, access: str | None, action: str, settings: Settings | None = None) -> str:
    """The root helper applies the same rules; checking here too means a refused name never reaches DNS."""
    host = (host or "").strip().lower().rstrip(".")
    if not HOST_RE.fullmatch(host) or host.startswith(("*", "preview-")):
        raise PublishRefused(f"{host!r} isn't a valid hostname. Use a name like app.example.com.")
    if settings is not None:
        if host in settings.reserved_hosts:
            raise PublishRefused(f"{host} is reserved for Rynhult. Choose another name.")
        zones = settings.publish_zones or ((settings.wildcard_zone,) if settings.wildcard_zone else ())
        if zones and not any(host.endswith("." + zone) for zone in zones):
            raise PublishRefused(f"{host} is not in a zone Rynhult publishes to. Choose a name ending in {' or '.join(zones)}.")
    if action == "publish":
        if not isinstance(port, int) or not 8000 <= port <= 8199:
            raise PublishRefused("Choose a port from 8000 to 8199 (the dev and prod ranges).")
        if access not in ("public", "internal"):
            raise PublishRefused("Choose public or internal access.")
    return host


def describe_port(port: int | None, owner: dict | None) -> str:
    """Whose port a request publishes, for the approver."""
    if owner is None:
        return f"Port {port} is not reserved for any project"
    if owner.get("project"):
        return f"Port {port} is reserved for project {owner['project']}"
    return f"Port {port} is reserved for {owner.get('label') or 'an outside service'}"


class Publisher:
    def __init__(self, settings: Settings, db: sessionmaker, cloudflare: Cloudflare | None = None,
                 https_check=None):
        self.settings = settings
        self.db = db
        self.workspace_operations = WorkspaceOperations()
        self._cloudflare = cloudflare
        self.https_check = https_check or self._https_check
        self.work = NodeTasks("publishing")
        self.tasks = self.work.tasks
        # Zone listings are reachable by any signed-in user: cache them per credential selection, as the legacy
        # client did, so listings can't spend the Publishing credential's provider rate limit (SRD2-02).
        self._zones: tuple | None = None
        self._zones_lock = asyncio.Lock()

    def dns_selection(self):
        """Fresh purpose-bound decryption per job; a selected vault key never falls back to legacy."""
        with self.db() as s:
            binding = s.get(DNSBinding, 1)
            if binding and infrastructure.publishing_source(s) != 'legacy':
                if infrastructure.qualification(binding.provider) != 'qualified':
                    raise PublishRefused('This DNS provider is in preview: it isn\'t qualified for publishing in this release yet. '
                                         'An owner can choose a qualified provider in Settings → DNS.')
                key = s.get(InfrastructureCredential, binding.publishing_id)
                if key is None or key.purpose != 'publishing' or key.provider != binding.provider:
                    raise PublishRefused('The Publishing credential is missing. An owner can choose one in Settings → DNS.')
                try:
                    fields = infrastructure.value(Vault(self.settings.vault_key_path), key)
                except (VaultError, OSError):
                    raise PublishRefused('The Publishing credential can\'t be read. An owner can restore or replace it in Keys.') from None
                if fields.get('zone_name', binding.zone) != binding.zone:
                    raise PublishRefused('The Publishing credential is for a different zone than the one set up. An owner can choose the right one in Settings → DNS.')
                return self._provider(binding.provider, fields), binding.zone
            # Rollback restores the old token, not its old reach: a binding keeps legacy to the bound zone (SRD2-01).
            selected = binding.zone if binding else None
            if infrastructure.legacy_publishing_retired(s):
                raise PublishRefused('The old Publishing token was retired in Keys. An owner can choose Use vault for Publishing in Settings → DNS.')
        if self._cloudflare is not None:
            # Backwards-compatible dependency injection; borrowed clients remain owned by their caller.
            provider = dns.CloudflareProvider.__new__(dns.CloudflareProvider)
            provider.client, provider.owns_client = self._cloudflare.client, False
            return provider, selected
        try:
            token = infrastructure.legacy_publishing(self.settings)
        except (OSError, VaultError):
            raise PublishRefused('Publishing isn\'t set up yet. An owner can choose a Publishing credential in Settings → DNS.') from None
        return self._provider('cloudflare', {'api_token': token}), selected

    @staticmethod
    def _provider(name, fields):
        try:
            return dns.make_provider(name, fields)
        except VaultError:  # a malformed legacy file; execute() reports refusals, not arbitrary exceptions
            raise PublishRefused('The Publishing credential can\'t be read. An owner can replace it or choose another in Settings → DNS.') from None

    def _selection_identity(self):
        """What a cached zone list depends on, read without decrypting anything."""
        with self.db() as s:
            binding = s.get(DNSBinding, 1)
            key = s.get(InfrastructureCredential, binding.publishing_id) if binding else None
            identity = (infrastructure.publishing_source(s), infrastructure.legacy_publishing_retired(s), id(self._cloudflare),
                        binding and (binding.provider, binding.zone, binding.publishing_id, binding.revision),
                        key and (key.revision, key.disabled, key.updated_at))
        try:
            st = os.stat(self.settings.data_dir / 'secrets' / 'cloudflare_token', follow_symlinks=False)
            return identity + ((st.st_ino, st.st_size, st.st_mtime_ns),)
        except OSError:
            return identity + (None,)

    async def zones(self):
        identity = self._selection_identity()
        async with self._zones_lock:  # concurrent listings share one provider call
            if self._zones and self._zones[0] == identity and time.monotonic() - self._zones[1] < 300:
                return [dict(z) for z in self._zones[2]]
            provider, selected = self.dns_selection()
            try:
                zones = [z for z in await provider.zones() if not selected or z['name'] == selected]
            finally:
                await provider.close()
            self._zones = (identity, time.monotonic(), zones)
            return [dict(z) for z in zones]

    # ------------------------------------------------------------ requests

    def request(self, *, action: str, host: str, port: int | None, access: str | None, requested_by: str,
                project_id: int | None = None, session_id: str | None = None, reason: str | None = None,
                approved_by: str | None = None, owner: str | None = None, replace: bool = False, target_ip: str | None = None) -> str:
        """`owner`: whose request it is (defaults to the session's or project's owner). Approval stays with owners.

        D18: a request from a session (an agent) only publishes its own project's reserved ports and only unpublishes
        its own project's sites, whoever's session it is. A publish of a host that is live already records the site it
        would retarget, and approving it needs explicit confirmation (`replace` here for a pre-approved one)."""
        from .admin_features import enabled
        if not enabled(self.settings, 'publishing'):
            raise PublishRefused('Publishing is turned off in Admin features.')
        host = check_request(host, port, access, action, self.settings)
        with self.db() as db:
            if action == 'publish' and session_id:
                session = db.get(Session, session_id)
                if session and session.project_id is None:
                    raise PublishRefused('This session belongs to a deleted project; its history is read-only.')
            if owner is None and session_id and (session := db.get(Session, session_id)) is not None:
                owner = session.owner
            if owner is None and project_id and (project := db.get(Project, project_id)) is not None:
                owner = project.owner
            reservation = db.get(PortReservation, port) if action == "publish" and isinstance(port, int) else None
            member = owner and db.scalar(select(User.role).where(User.username == owner)) == "member"
            if action == "publish" and (member or session_id is not None):
                # Members and every agent publish only ports reserved for that project (the port registry).
                if project_id is None or reservation is None or reservation.project_id != project_id:
                    raise PublishRefused(f"Port {port} isn't reserved for this project. Reserve it under Ports on the project's page first.")
            if action == 'publish' and reservation is not None and reservation.project_id is not None:
                # A reserved port is authoritative even when the owner form sends
                # no project ID. Do not let an explicit conflicting ID relabel it.
                if project_id is not None and project_id != reservation.project_id:
                    raise PublishRefused(f"Port {port} isn't reserved for this project. Choose its owning project.")
                project_id = reservation.project_id
            if action == 'publish' and project_id is not None:
                project = db.get(Project, project_id)
                if project is None or project.deleting:
                    raise PublishRefused('This project is being deleted or no longer exists; new sites cannot be published for it.')
                if self.workspace_operations.busy(project.workspace_path, project.host or 'local'):
                    raise PublishRefused('This project has a workspace operation in progress. Wait for it to finish before publishing.')
                if owner is None:
                    owner = project.owner
            live = db.scalar(select(PublishRequest).where(PublishRequest.host == host, PublishRequest.action == "publish",
                                                          PublishRequest.status == "applied"))
            if action == "unpublish":
                if live is None:
                    raise PublishRefused(f"{host} is not published by Rynhult, so there's nothing to remove.")
                if session_id is not None and live.project_id != project_id:
                    raise PublishRefused(f"{host} is another project's site, so this session can't remove it.")
                port, access, project_id = live.port, live.access, live.project_id
            pending = db.scalar(select(PublishRequest).where(PublishRequest.host == host,
                                                             PublishRequest.status.in_(("pending_approval", "approved", "applying"))))
            if pending is not None:  # its id only to the person whose request it is
                raise PublishRefused(f"{host} already has a request in progress"
                                     + (f" ({pending.id})" if owner is None or pending.owner == owner else "")
                                     + ". Wait for it to finish, then try again.")
            names = {p.id: p.slug for p in db.scalars(select(Project).where(Project.id.in_(
                {i for i in (project_id, live.project_id if live else None, reservation.project_id if reservation else None)
                 if i is not None})))}
            context = {}
            if action == 'publish':
                try:
                    context['target_ip'] = publish_addresses.choose(
                        publish_addresses.inventory(db, self.settings), access, target_ip)
                except ValueError as error:
                    raise PublishRefused(str(error)) from None
            if action == "publish":
                context["port_owner"] = ({"project": names.get(reservation.project_id), "label": reservation.label}
                                         if reservation is not None else None)
                if live is not None:
                    context["replaces"] = {"id": live.id, "port": live.port, "access": live.access,
                                           "project": names.get(live.project_id), "decided_by": live.decided_by}
                    if approved_by is not None and not replace:
                        raise PublishRefused(f"{host} is live, serving port {live.port}"
                                             f"{f' ({names[live.project_id]})' if live.project_id in names else ''}. "
                                             "Confirm replacing it.")
            request = PublishRequest(id=f"pub-{uuid.uuid4().hex[:12]}", action=action, host=host, port=port, access=access,
                                     project_id=project_id, session_id=session_id, requested_by=requested_by[:80],
                                     reason=(reason or "")[:500] or None, owner=owner, context=context or None)
            db.add(request)
            db.add(AuditLog(actor=requested_by[:64], action=f"publish.request_{action}", target=host,
                            detail={"id": request.id, "port": port, "access": access,
                                    "target_ip": context.get("target_ip"),
                                    **({"replaces": context["replaces"]} if "replaces" in context else {})}))
            if approved_by is None:
                where = "public internet" if access == "public" else "private networks only"
                title = (f"Publish {host} → port {port} ({where})" if action == "publish" else f"Unpublish {host}")
                severity = "warn"
                if "replaces" in context:
                    old = context["replaces"]
                    was = f" ({old['project']})" if old["project"] else ""
                    title = f"Retarget live site {host}: port {old['port']}{was} → port {port} ({where})"
                    severity = "critical"
                detail = f"{describe_port(port, context.get('port_owner'))}. " if action == "publish" else ""
                if project_id in names:
                    detail += f"Asked for project {names[project_id]}. "
                if context.get('target_ip'):
                    detail += f"DNS target: {context['target_ip']}. "
                attention.raise_item(db, f"publish:{request.id}", kind="publish_request", severity=severity,
                                     source=requested_by[:32], session_id=session_id, title=title,
                                     detail=detail + (reason or "Approve to change DNS and Caddy."),
                                     owner="")  # "": owners only, they approve (the requester follows it on Publishing)
            db.commit()
            request_id = request.id
        if approved_by is not None:
            self.approve(request_id, approved_by, confirm_replace=True)
        return request_id

    def approve(self, request_id: str, actor: str, confirm_replace: bool = False) -> None:
        """Approving a request that retargets a live site needs `confirm_replace` (the approver saw what changes)."""
        self._decide(request_id, actor, approve=True, confirm_replace=confirm_replace)
        self.work.start(self.execute(request_id), f"publish:{request_id}")

    def reject(self, request_id: str, actor: str) -> None:
        self._decide(request_id, actor, approve=False)

    def _decide(self, request_id: str, actor: str, approve: bool, confirm_replace: bool = False) -> None:
        from .admin_features import enabled
        if approve and not enabled(self.settings, 'publishing'):
            raise PublishRefused('Publishing is turned off in Admin features.')
        with self.db() as db:
            request = db.get(PublishRequest, request_id)
            if request is None:
                raise PublishRefused("That request wasn't found.")
            if request.status != "pending_approval":
                raise PublishRefused(f"This request is already {request.status.replace('_', ' ')}, so it can't be decided again.")
            replaces = (request.context or {}).get("replaces")
            if approve and replaces and not confirm_replace:
                raise PublishRefused(f"This retargets the live site {request.host} (now port {replaces['port']}). "
                                     "Confirm replacing it.")
            request.status = "approved" if approve else "rejected"
            request.decided_by, request.decided_at = actor, now()
            attention.resolve(db, f"publish:{request_id}")
            db.add(AuditLog(actor=actor, action="publish.approve" if approve else "publish.reject", target=request.host,
                            detail={"id": request_id, "action": request.action}))
            db.commit()

    # ------------------------------------------------------------ execution

    def _update(self, request_id: str, **fields) -> None:
        with self.db() as db:
            request = db.get(PublishRequest, request_id)
            if 'outcome' in fields:
                request.context = {**(request.context or {}), 'outcome': fields.pop('outcome')}
            for key, value in fields.items():
                setattr(request, key, value)
            db.commit()

    async def execute(self, request_id: str) -> None:
        # Approval accepted before Off may finish; new requests and approvals are gated.
        with self.db() as db:
            r = db.get(PublishRequest, request_id)
            action, host, port, access = r.action, r.host, r.port, r.access
        self._update(request_id, status="applying")
        try:
            if action == "publish":
                outcome = await self._publish(request_id, host, port, access)
                self._update(request_id, status="applied", detail="Published successfully; HTTPS check completed.",
                             outcome=outcome, completed_at=now())
                with self.db() as db:  # a retargeted site: the request it replaced no longer is what's live
                    for old in db.scalars(select(PublishRequest).where(PublishRequest.host == host,
                                                                       PublishRequest.action == "publish",
                                                                       PublishRequest.status == "applied",
                                                                       PublishRequest.id != request_id)):
                        old.status, old.detail = "removed", f"replaced by {request_id} (port {port})"
                    db.commit()
            else:
                outcome = await self._unpublish(request_id, host)
                detail = "Site and DNS record removed." if outcome["dns_removed"] else "Site removed."
                self._update(request_id, status="removed", detail=detail, outcome=outcome, completed_at=now())
                with self.db() as db:
                    for old in db.scalars(select(PublishRequest).where(PublishRequest.host == host,
                                                                       PublishRequest.action == "publish",
                                                                       PublishRequest.status == "applied")):
                        old.status = "removed"
                    db.commit()
        except (PublishRefused, CloudflareError, dns.DNSError, OSError, TimeoutError) as error:
            # Provider exceptions and OS diagnostics may carry secrets/paths. Only our fixed DNS categories escape.
            if isinstance(error, (CloudflareError, OSError, TimeoutError)):
                error = PublishRefused('A service publishing relies on didn\'t answer. An owner can check Settings → DNS and the Caddy service, then try again.')
            log.warning("publish %s failed: %s", request_id, error)
            self._update(request_id, status="failed", detail=str(error)[:500], completed_at=now())
            with self.db() as db:
                attention.raise_item(db, f"publish_failed:{request_id}", kind="publish_failed", severity="warn",
                                     source="publisher", title=f"Couldn't {action} {host}", detail=str(error)[:500])
                db.commit()

    def _route_uncertain(self, request_id, host, zone_id, *, removing=False):
        """A queued root job can complete after accd stops hearing from it. Freeze further DNS changes."""
        with self.db() as s:
            for row in s.scalars(select(DNSOwnedRecord).where(DNSOwnedRecord.zone_id == zone_id,
                                                             DNSOwnedRecord.host == host)):
                row.state, row.request_id, row.updated_at = 'uncertain', request_id, now()
                if removing:
                    row.prior, row.desired = row.applied, None
            s.commit()

    async def _publish(self, request_id: str, host: str, port: int, access: str) -> dict:
        with self.db() as db:
            request = db.get(PublishRequest, request_id)
            try:
                ip = publish_addresses.choose(publish_addresses.inventory(db, self.settings), access,
                                              (request.context or {}).get('target_ip'))
            except ValueError as error:
                raise PublishRefused(str(error)) from None
            db.commit()
        fence(self.db)
        provider, selected = self.dns_selection()
        try:
            async with asyncio.timeout(60):
                zones = [z for z in await provider.zones() if (not selected or z['name'] == selected) and
                         host.endswith('.' + z['name'])]
                fence(self.db)  # zone lookup awaited the provider
                if not zones:
                    raise PublishRefused('No DNS zone set up in Settings → DNS contains this host. Choose a name in one of its zones.')
                zone = max(zones, key=lambda z: len(z['name']))
                owned = dns.OwnedDNS(provider, self.db)
                with self.db() as db:
                    old = owned.row(db, zone['id'], host)
                    prior = old.applied if old else None
                fence(self.db)
                record = await owned.ensure_record(zone['id'], host, 'AAAA' if ':' in ip else 'A', ip, request_id)
                fence(self.db)
                self._update(request_id, zone_id=zone['id'], zone_name=zone['name'], dns_record_id=record.id)
                try:
                    result = await self._run_helper(request_id, {"action": "apply", "host": host, "port": port, "access": access})
                except asyncio.CancelledError:
                    self._route_uncertain(request_id, host, zone['id'])
                    raise
                except (OSError, TimeoutError):
                    self._route_uncertain(request_id, host, zone['id'])
                    raise dns.DNSError('publication_uncertain_reconcile') from None
                fence(self.db)  # helper completion may follow an authority change
                if not result['ok']:
                    fence(self.db)
                    if prior:
                        await owned.ensure_record(zone['id'], host, prior['type'], prior['values'][0], request_id + '-rollback',
                                                  restore=dns.restored(prior))
                    else:
                        await owned.delete_owned_record(zone['id'], host, request_id + '-rollback')
                    self._update(request_id, dns_record_id=None)
                    raise PublishRefused('Caddy refused the route, so the site isn\'t live. Rynhult undid its DNS change where it still could.')
                https = await self.https_check(host, ip)
                return {'operation': 'publish', 'route_applied': True, 'route_detail': result['detail'],
                        'dns': {'host': host, 'target_ip': ip, 'dns_only': True},
                        'https': {'checked': True, 'detail': https}}
        finally:
            await provider.close()

    async def _unpublish(self, request_id: str, host: str) -> dict:
        with self.db() as db:
            live = db.scalar(select(PublishRequest).where(PublishRequest.host == host, PublishRequest.action == "publish",
                                                          PublishRequest.status == "applied"))
            zone_id, record_id, zone_name = (live.zone_id, live.dns_record_id, live.zone_name) if live else (None, None, None)
        try:
            result = await self._run_helper(request_id, {"action": "remove", "host": host})
        except asyncio.CancelledError:
            self._route_uncertain(request_id, host, zone_id, removing=True)
            raise
        except (OSError, TimeoutError):
            self._route_uncertain(request_id, host, zone_id, removing=True)
            raise dns.DNSError('publication_uncertain_reconcile') from None
        if not result["ok"]:
            raise PublishRefused(f"Caddy refused to remove the route: {result['detail']}")
        if zone_id and record_id:
            provider, selected = self.dns_selection()
            try:
                if selected and selected != zone_name:
                    raise PublishRefused('This site was published with a different DNS zone. Choose that zone again in Settings → DNS to remove it.')
                async with asyncio.timeout(60):
                    fence(self.db)
                    await dns.OwnedDNS(provider, self.db).delete_owned_record(zone_id, host, request_id)
            finally:
                await provider.close()
        return {'operation': 'unpublish', 'route_removed': True, 'dns_removed': bool(record_id),
                'route_detail': result['detail']}

    async def _run_helper(self, request_id: str, payload: dict) -> dict:
        root = self.settings.publish_dir
        with self.db() as db:
            actor = db.get(PublishRequest, request_id).decided_by or ""
        fence(self.db)  # a root helper applies this to Caddy: it cannot be retracted
        body = json.dumps({"id": request_id, "actor": actor, **payload})
        tmp = root / "requests" / f".{request_id}.tmp"
        tmp.write_text(body)
        os.chmod(tmp, 0o640)
        tmp.rename(root / "requests" / f"{request_id}.json")  # the path unit only watches *.json
        result_path = root / "results" / f"{request_id}.json"
        for _ in range(int(self.settings.publish_timeout / 0.25)):
            if result_path.exists():
                return json.loads(result_path.read_text())
            await asyncio.sleep(0.25)
        raise TimeoutError("acc-publish did not answer (is acc-publish.path enabled?)")

    @staticmethod
    async def _https_check(host: str, ip: str) -> str:
        """Connect to the origin with the real hostname; report the certificate and status line."""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(ip, 443, ssl=ssl.create_default_context(), server_hostname=host), 20)
            writer.write(f"HEAD / HTTP/1.1\r\nHost: {host}\r\nConnection: close\r\n\r\n".encode())
            await writer.drain()
            status = (await asyncio.wait_for(reader.readline(), 20)).decode().strip()
            writer.close()
            return status or "connected"
        except (OSError, ssl.SSLError, asyncio.TimeoutError) as error:
            return f"not yet ({type(error).__name__}: {str(error)[:120]}); certificates for new non-wildcard names can take a minute"
