"""Purpose-bound infrastructure vault. No general vault resolver accepts these IDs."""
from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, select

from ..dns_credentials import PROVIDERS as PROVIDERS
from ..dns_credentials import check_fields as credential_fields
from .models import InfrastructureCredential, Setting, VaultKey, utcnow
from .vault import Redactor, VaultError


def qualification(provider):
    return 'qualified' if PROVIDERS.get(provider, {}).get('qualification') == 'qualified' else 'preview'


def provider_choices():
    return [{'id': provider, 'qualification': qualification(provider)} for provider in PROVIDERS]


def check_fields(provider, fields):
    try:
        if provider == 's3':
            from ..backup_s3 import fields as s3_fields
            return s3_fields(fields)
        return credential_fields(provider, fields)
    except ValueError:
        raise VaultError('Supply valid fixed provider credential fields') from None


def aad_for(provider, purpose, revision):
    return f"infrastructure:{provider}:{purpose}:{'api_token' if provider == 'cloudflare' else 'fields'}:{revision}"


def aad(key, revision=None):
    return aad_for(key.provider, key.purpose, revision or key.revision)


def seal(s, store, key, fields):
    fields = check_fields(key.provider, fields)
    empty = not s.scalar(select(func.count()).select_from(VaultKey)) and not s.scalar(
        select(func.count()).select_from(InfrastructureCredential))
    import json
    content = fields['api_token'] if key.provider == 'cloudflare' else json.dumps(fields, sort_keys=True)
    return store.cipher.seal(key.id, aad(key), content, create=empty)


def value(store, key):
    if key.disabled:
        raise VaultError("This infrastructure credential is disabled")
    return open_fields(store, key)


def open_fields(store, key, revision=None, ciphertext=None):
    import json
    content = store.cipher.open(key.id, aad(key, revision), ciphertext or key.ciphertext)
    return {'api_token': content} if key.provider == 'cloudflare' else check_fields(key.provider, json.loads(content))


def replace(s, store, key, fields):
    check_fields(key.provider, fields)
    now = utcnow()
    key.retired = [r for r in key.retired if r['expires'] > now.timestamp()] + [
        {'revision': key.revision, 'ciphertext': key.ciphertext,
         'expires': (now + timedelta(hours=24)).timestamp()}]
    key.revision += 1
    key.ciphertext = seal(s, store, key, fields)
    key.updated_at = now


def redactor(s, store):
    pairs = []
    for key in s.scalars(select(InfrastructureCredential)):
        pairs.extend((v, 'infrastructure') for v in open_fields(store, key).values())
        for old in key.retired:
            pairs.extend((v, 'infrastructure') for v in open_fields(store, key, old['revision'], old['ciphertext']).values())
    return Redactor(pairs)


def metadata(key):
    result = {k: getattr(key, k) for k in ('id', 'name', 'provider', 'purpose', 'revision', 'disabled', 'updated_at')}
    if key.provider in PROVIDERS:
        result['qualification'] = qualification(key.provider)
    return result


def binding_out(binding):
    if binding is None:
        return {'provider': 'internal', 'status': 'internal', 'revision': 0, 'applied_revision': None}
    return {**{k: getattr(binding, k) for k in ('provider', 'zone', 'certificate_id', 'publishing_id', 'revision',
                                              'applied_revision', 'status', 'generation')},
            'qualification': qualification(binding.provider)}


def legacy_publishing(settings):
    """Read accd's fixed legacy token, without following the secret directory or file."""
    import os
    import stat
    directory = os.open(settings.data_dir / 'secrets', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        st = os.fstat(directory)
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            raise VaultError('Unsafe legacy directory')
        fd = os.open('cloudflare_token', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        try:
            st = os.fstat(fd)
            if (st.st_uid != os.getuid() or not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or
                    stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 4097):
                raise VaultError('Unsafe legacy credential')
            return os.read(fd, 4098).decode().strip()
        finally:
            os.close(fd)
    finally:
        os.close(directory)


def purge_retired(s, *, now=None):
    """Bound rollback ciphertext retention even when no session ever held the credential."""
    cutoff = utcnow().timestamp() if now is None else now
    for key in s.scalars(select(InfrastructureCredential)):
        retained = [old for old in key.retired if old['expires'] > cutoff]
        if retained != key.retired:
            key.retired = retained


def publishing_source(s):
    row = s.get(Setting, 'dns.publishing_source')
    return row.value['source'] if row else 'vault'


def legacy_publishing_retired(s):
    row = s.get(Setting, 'dns.legacy_publishing')
    return bool(row and row.value.get('retired'))


def retire_legacy_publishing(s, key, username):
    """Removing, disabling or replacing the imported copy retires the legacy file too: rollback never revives it."""
    if key.migration_source != 'legacy-publishing' or legacy_publishing_retired(s):
        return
    row = s.get(Setting, 'dns.legacy_publishing')
    if row is None:
        row = Setting(key='dns.legacy_publishing', value={})
        s.add(row)
    # A selected legacy source stays selected and refuses jobs: switching to vault is the owner's choice.
    row.value, row.updated_by = {'retired': True}, username
