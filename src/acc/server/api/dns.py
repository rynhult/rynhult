"""Owner browser-only, write-only infrastructure Keys and DNS Settings."""
from __future__ import annotations

import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, SecretStr
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .. import auth, vault
from .. import infrastructure as infra
from ..models import AuditLog, DNSBinding, DNSOwnedRecord, InfrastructureCredential, PublishRequest, Setting, User
from .context import Context


class Authorized(BaseModel):
    model_config = ConfigDict(extra='forbid')
    password: SecretStr = Field(max_length=1024)


class CredentialIn(Authorized):
    name: str = Field(min_length=1, max_length=80)
    provider: str
    purpose: str
    fields: dict[str, SecretStr]


class Replacement(Authorized):
    fields: dict[str, SecretStr]


class BindingIn(Authorized):
    provider: str
    zone: str = Field(max_length=253)
    certificate_id: str
    publishing_id: str


class PublishingSourceIn(Authorized):
    source: str = Field(pattern='^(vault|legacy)$')


class AdoptIn(Authorized):
    request_id: str = Field(pattern=r'^pub-[a-zA-Z0-9-]{1,52}$')


class ApplyIn(Authorized):
    revision: int


def router(ctx: Context):
    api = APIRouter()
    store = ctx.supervisor.vault

    def owner(request: Request):
        who = ctx.owner(request)
        if who.user_id is None:
            raise HTTPException(403, 'Sign in as an owner in the browser to manage infrastructure credentials.')
        return who

    def authorize(body, who):
        throttle_key = 'dns:' + who.username
        if ctx.throttle.blocked(throttle_key):
            refused(who, 'dns.reauth', 'password', 'throttled')
            raise HTTPException(429, 'Too many attempts. Wait a few minutes and try again.')
        ctx.throttle.fail(throttle_key)
        with ctx.db() as s:
            user = s.get(User, who.user_id)
            valid = auth.password_ok(user.password_hash, body.password.get_secret_value())
        if not valid:
            refused(who, 'dns.reauth', 'password', 'refused')
            raise HTTPException(403, "That password didn't match. Enter your current password.")
        ctx.throttle.reset(throttle_key)

    def key_for(s, key_id):
        key = s.get(InfrastructureCredential, key_id)
        if key is None or key.provider == 's3':
            raise HTTPException(404, 'That credential was not found. Reload the page and try again.')
        return key

    def audit(s, who, action, target, **detail):
        s.add(AuditLog(actor=who.username, action=action, target=str(target), detail=detail))

    def refused(who, action, target, result):
        """Record a refused or failed attempt in its own transaction, before the error response."""
        with ctx.db() as s:
            audit(s, who, action, target, result=result)
            s.commit()

    def seal(s, key, fields):
        try:
            return infra.seal(s, store, key, fields)
        except vault.VaultError:
            raise HTTPException(409, "Rynhult can't unlock its saved DNS credentials. Restore the vault key from your backup, then try again.") from None

    def checked_fields(provider, fields):
        try:
            return infra.check_fields(provider, {k: v.get_secret_value() for k, v in fields.items()})
        except vault.VaultError:
            raise HTTPException(422, 'Fill in the fields shown for this provider, then try again.') from None

    @api.get('/api/dns/providers')
    def providers(who=Depends(owner)):
        return infra.provider_choices()

    @api.get('/api/dns/credentials')
    def credentials(who=Depends(owner)):
        with ctx.db() as s:
            return [infra.metadata(k) for k in s.scalars(select(InfrastructureCredential).where(InfrastructureCredential.provider != 's3'))]

    @api.post('/api/dns/credentials', status_code=201)
    def add(body: CredentialIn, who=Depends(owner)):
        authorize(body, who)
        fields = checked_fields(body.provider, body.fields)
        if body.provider not in infra.PROVIDERS or body.purpose not in ('certificates', 'publishing'):
            raise HTTPException(422, 'Choose Certificates or Publishing as the credential purpose.')
        with ctx.db() as s:
            key = InfrastructureCredential(id=vault.new_id(), name=body.name, provider=body.provider, purpose=body.purpose,
                                           owner=who.username, revision=1)
            key.ciphertext = seal(s, key, fields)
            s.add(key)
            audit(s, who, 'dns.credential.add', key.id, provider=key.provider, purpose=key.purpose, revision=1)
            s.commit()
            return infra.metadata(key)

    @api.post('/api/dns/credentials/{key_id}/replace')
    def replace(key_id: str, body: Replacement, who=Depends(owner)):
        authorize(body, who)
        with ctx.db() as s:
            key = key_for(s, key_id)
            fields = checked_fields(key.provider, body.fields)
            try:
                infra.replace(s, store, key, fields)
            except vault.VaultError:
                raise HTTPException(409, "Rynhult can't unlock its saved DNS credentials. Restore the vault key from your backup, then try again.") from None
            binding = s.get(DNSBinding, 1)
            if binding and key_id in (binding.certificate_id, binding.publishing_id):
                binding.revision += 1
                binding.status = 'saved'
            infra.retire_legacy_publishing(s, key, who.username)
            audit(s, who, 'dns.credential.replace', key.id, revision=key.revision)
            s.commit()
            return infra.metadata(key)

    @api.post('/api/dns/credentials/{key_id}/validate')
    async def validate(key_id: str, body: Authorized, who=Depends(owner)):
        authorize(body, who)
        from ..dns_providers import make_provider
        with ctx.db() as s:
            key = key_for(s, key_id)
            revision = key.revision
            try:
                fields = infra.value(store, key)
                provider_name = key.provider
                available = True
            except vault.VaultError:
                available = False
        if not available:
            refused(who, 'dns.validate', key_id, 'unavailable')
            raise HTTPException(409, "Rynhult can't unlock this credential. Restore the vault key from your backup, or replace the credential.")
        provider = make_provider(provider_name, fields)
        ok = False
        try:
            ok = await provider.validate_credentials()
        except Exception:
            pass  # fixed category only: provider messages may contain credentials
        finally:
            await provider.close()
        with ctx.db() as s:
            audit(s, who, 'dns.validate', key_id, revision=revision, result='readable' if ok else 'failed')
            s.commit()
        if not ok:
            raise HTTPException(409, "DNS validation failed: the provider didn't accept this credential. Check that it hasn't expired, covers this zone and can edit DNS records.")
        return {'status': 'readable', 'revision': revision, 'issuance': 'unverified'}

    @api.post('/api/dns/credentials/{key_id}/remove')
    def remove(key_id: str, body: Authorized, who=Depends(owner)):
        authorize(body, who)
        with ctx.db() as s:
            key = key_for(s, key_id)
            binding = s.get(DNSBinding, 1)
            if binding and key_id in (binding.certificate_id, binding.publishing_id):
                raise HTTPException(409, 'DNS and certificates use this credential. Choose a different one in Configure DNS first.')
            infra.retire_legacy_publishing(s, key, who.username)
            audit(s, who, 'dns.credential.delete', key.id)
            s.delete(key)
            s.commit()
        return {'deleted': key_id}

    @api.post('/api/dns/credentials/{key_id}/disable')
    def disable(key_id: str, body: Authorized, who=Depends(owner)):
        authorize(body, who)
        with ctx.db() as s:
            key = key_for(s, key_id)
            binding = s.get(DNSBinding, 1)
            if binding and key_id in (binding.certificate_id, binding.publishing_id):
                raise HTTPException(409, 'DNS and certificates use this credential, so disabling it would stop certificate renewal or Publishing. Choose a different one in Configure DNS first.')
            key.disabled = True
            infra.retire_legacy_publishing(s, key, who.username)
            audit(s, who, 'dns.credential.disable', key.id)
            s.commit()
        return {'disabled': key_id}

    @api.get('/api/dns/binding')
    def binding(who=Depends(owner)):
        with ctx.db() as s:
            return {**infra.binding_out(s.get(DNSBinding, 1)), 'publishing_source': infra.publishing_source(s),
                    'legacy_publishing': 'retired' if infra.legacy_publishing_retired(s) else 'retained'}

    @api.post('/api/dns/binding')
    def save(body: BindingIn, who=Depends(owner)):
        authorize(body, who)
        if body.provider not in infra.PROVIDERS or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}', body.zone):
            raise HTTPException(422, 'Choose a DNS provider and enter a valid zone name, such as example.com.')
        with ctx.db() as s:
            for key_id, purpose in ((body.certificate_id, 'certificates'), (body.publishing_id, 'publishing')):
                key = key_for(s, key_id)
                if key.disabled or key.provider != body.provider or key.purpose != purpose:
                    raise HTTPException(422, 'Choose an active credential from this provider for each purpose: one for Certificates and one for Publishing.')
                try:
                    zone_name = infra.value(store, key).get('zone_name', body.zone)
                except vault.VaultError:
                    raise HTTPException(409, "Rynhult can't unlock this credential. Restore the vault key from your backup, or replace the credential.") from None
                if zone_name != body.zone:
                    raise HTTPException(422, 'A chosen credential is for a different zone. Choose credentials made for this zone.')
            row = s.get(DNSBinding, 1)
            if row is None:
                row = DNSBinding(id=1, revision=0)
                s.add(row)
            row.provider, row.zone = body.provider, body.zone
            row.certificate_id, row.publishing_id = body.certificate_id, body.publishing_id
            row.revision += 1
            row.status = 'saved'
            source = s.get(Setting, 'dns.publishing_source')
            if source:
                source.value = {'source': 'vault'}
            audit(s, who, 'dns.binding.save', 1, revision=row.revision, zone=row.zone)
            s.commit()
            return infra.binding_out(row)

    @api.post('/api/dns/adopt')
    async def adopt(body: AdoptIn, who=Depends(owner)):
        """Explicit local history plus exact remote match; marker-only adoption is forbidden."""
        authorize(body, who)
        try:
            return await adopt_reviewed(body, who)
        except HTTPException as error:
            refused(who, 'dns.record.adopt', body.request_id, 'unavailable' if 'unavailable' in error.detail else 'refused')
            raise

    async def adopt_reviewed(body, who):
        with ctx.db() as s:
            published = s.get(PublishRequest, body.request_id)
            if published is None or published.status != 'applied' or published.action != 'publish':
                raise HTTPException(409, 'No applied publication has that ID. Enter the ID of a publication that was applied before.')
            host, zone, record_id, target = (published.host, published.zone_id, published.dns_record_id,
                                             (published.context or {}).get('target_ip'))
        from ..dns_providers import DNSError, OwnedDNS
        from ..publish import PublishRefused
        try:
            provider, selected = ctx.publisher.dns_selection()
        except PublishRefused:
            raise HTTPException(409, 'Choose the Publishing credential that made this record in Configure DNS, then adopt it.') from None
        try:
            zones = [z for z in await provider.zones() if z['id'] == zone and host.endswith('.' + z['name'])
                     and (not selected or selected == z['name'])]
            records = await provider.list_records(zone, host) if zones else []
            if (provider.name != 'cloudflare' or len(records) != 1 or records[0].id != record_id or
                    records[0].values != (target,) or records[0].type not in ('A', 'AAAA') or
                    not records[0].marker.startswith('managed by ACC') or '[proxied]' in records[0].marker):
                raise HTTPException(409, "The record at the DNS provider no longer matches that publication, so it wasn't adopted.")
            try:
                with ctx.db() as s:
                    # Re-check local history after network I/O, then insert under the unique ownership key.
                    published = s.get(PublishRequest, body.request_id)
                    if published is None or published.status != 'applied' or published.dns_record_id != record_id:
                        raise HTTPException(409, 'The publication changed. Reload the page and try again.')
                    owned = OwnedDNS(provider, ctx.db)
                    if owned.row(s, zone, host):
                        raise HTTPException(409, 'Rynhult already manages a DNS record for this host, so nothing was adopted.')
                    s.add(DNSOwnedRecord(provider=provider.name, zone_id=zone, host=host, state='owned',
                                         applied=records[0].snapshot(), request_id=body.request_id))
                    audit(s, who, 'dns.record.adopt', host, publication=body.request_id, zone=zone)
                    s.commit()
            except IntegrityError:
                raise HTTPException(409, 'Rynhult already manages a DNS record for this host. Reload the page to see it.') from None
        except DNSError:
            raise HTTPException(409, 'The DNS provider is unavailable, so nothing was adopted. Try again later.') from None
        finally:
            await provider.close()
        return {'status': 'owned', 'host': host}

    @api.post('/api/dns/publishing-source')
    def publishing_source(body: PublishingSourceIn, who=Depends(owner)):
        authorize(body, who)
        refusal = None
        with ctx.db() as s:
            row = s.get(DNSBinding, 1)
            if row is None or row.provider != 'cloudflare':
                refusal = 'refused', 'Save Cloudflare as the DNS provider in Configure DNS before choosing where Publishing gets its token.'
            elif body.source == 'legacy' and infra.legacy_publishing_retired(s):
                refusal = 'refused', "The legacy Publishing token was retired in Keys and can't be restored."
            elif body.source == 'legacy':
                try:
                    infra.check_fields('cloudflare', {'api_token': infra.legacy_publishing(ctx.settings)})
                except (OSError, vault.VaultError):
                    refusal = 'unavailable', "Rynhult can't read the legacy Publishing token. Choose Use vault for Publishing instead."
            if refusal is None:
                saved = s.get(Setting, 'dns.publishing_source')
                if saved is None:
                    saved = Setting(key='dns.publishing_source', value={})
                    s.add(saved)
                saved.value = {'source': body.source}
                audit(s, who, 'dns.publishing.source', row.publishing_id, source=body.source)
                s.commit()
                return {'publishing_source': body.source}
        # Outside the request's transaction, which holds the write lock (see refused).
        refused(who, 'dns.publishing.source', body.source, refusal[0])
        raise HTTPException(409, refusal[1])

    @api.post('/api/dns/apply')
    async def apply(body: ApplyIn, who=Depends(owner)):
        authorize(body, who)
        from ..dns_helper_client import request
        # Read and decrypt in a short transaction: the helper may take 30 seconds, and a mutating request's
        # transaction holds the database write lock for every other writer.
        with ctx.db() as s:
            row = s.get(DNSBinding, 1)
            if row is None or row.revision != body.revision:
                raise HTTPException(409, 'DNS settings changed. Reload the page and try again.')
            message = {'action': 'apply', 'provider': row.provider, 'zone': row.zone, 'revision': row.revision}
            try:
                message['fields'] = infra.value(store, key_for(s, row.certificate_id))
            except vault.VaultError:
                message = None
        try:
            if message is None:
                raise OSError('credential unavailable')
            result = await request(message)
        except OSError:
            refused(who, 'dns.apply', 1, 'unavailable')
            raise HTTPException(409, 'The DNS helper on the main machine is unavailable, so the saved settings were not applied. Try again later.') from None
        with ctx.db() as s:
            row = s.get(DNSBinding, 1)
            if result.get('ok'):
                row.applied_revision, row.generation = body.revision, result['generation']
            # A revision saved while the helper worked was not applied, whatever happened to this one.
            row.status = 'saved' if row.revision != body.revision else 'applied' if result.get('ok') else 'failed'
            audit(s, who, 'dns.apply', 1, revision=body.revision, zone=row.zone,
                  result='applied' if result.get('ok') else 'failed')
            s.commit()
            return infra.binding_out(row)

    @api.post('/api/dns/migrate')
    async def migrate(body: Authorized, who=Depends(owner)):
        authorize(body, who)
        from ..dns_helper_client import request
        from ..infrastructure import legacy_publishing
        def imported(s):
            return {k.migration_source: k.id for k in s.scalars(select(InfrastructureCredential).where(
                InfrastructureCredential.migration_source.is_not(None)))}
        with ctx.db() as s:
            migrated = imported(s)
        if not migrated:
            # The root helper call runs outside any transaction (see apply).
            try:
                response = await request({'action': 'import'})
                if not response.get('ok'):
                    raise OSError()
                certificate = infra.check_fields('cloudflare', response['fields'])
                publishing = infra.check_fields('cloudflare', {'api_token': legacy_publishing(ctx.settings)})
            except (OSError, ValueError, KeyError):
                refused(who, 'dns.migrate', 'legacy', 'refused')
                raise HTTPException(409, "Rynhult couldn't import the legacy Cloudflare credentials. Caddy and Publishing are unchanged.") from None
        with ctx.db() as s:
            migrated = imported(s)
            if not migrated:  # still none: a concurrent import would have made both
                for source, purpose, fields in [('legacy-caddy', 'certificates', certificate),
                                                ('legacy-publishing', 'publishing', publishing)]:
                    key = InfrastructureCredential(id=vault.new_id(), name='Imported Cloudflare ' + purpose,
                        provider='cloudflare', purpose=purpose, owner=who.username, revision=1, migration_source=source)
                    key.ciphertext = seal(s, key, fields)
                    s.add(key)
                    s.flush()
                    migrated[source] = key.id
                audit(s, who, 'dns.migrate', 'legacy', result='imported', rollback_hours=24)
                s.commit()
        return {'status': 'saved', 'certificate_id': migrated['legacy-caddy'],
                'publishing_id': migrated['legacy-publishing'], 'rollback_hours': 24}

    @api.post('/api/dns/rollback')
    async def rollback(body: Authorized, who=Depends(owner)):
        authorize(body, who)
        from ..dns_helper_client import request
        try:
            result = await request({'action': 'rollback'})
        except OSError:
            refused(who, 'dns.rollback', 1, 'unavailable')
            raise HTTPException(409, 'The DNS helper on the main machine is unavailable, so nothing was rolled back. Try again later.') from None
        with ctx.db() as s:
            row = s.get(DNSBinding, 1)
            if row and result.get('ok'):
                row.status, row.applied_revision, row.generation = 'saved', None, result.get('generation')
            audit(s, who, 'dns.rollback', 1, result='restored' if result.get('ok') else 'failed')
            s.commit()
        if not result.get('ok'):
            raise HTTPException(409, "Roll back didn't run: it was refused or its 24 hours have passed. Use the local recovery steps on the main machine.")
        return {'status': 'saved', 'issuance': 'unverified'}

    return api
