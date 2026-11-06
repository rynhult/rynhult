"""Frozen P4.1 schema/backfill shared by migration 0084 and the compatibility writer.

Do not change this schema after release: add a new migration instead. No filesystem,
network, credentials, canonicalization or authority changes occur here.
"""
import uuid

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

TABLES = (
    'hosting_instance', 'source_owner', 'repository', 'repository_locator', 'source_connection',
    'connection_repository_access', 'source_execution_host', 'source_execution_account',
    'project_repository', 'working_location', 'location_registration', 'project_location',
    'project_launch_default', 'session_target',
)


def define(m):
    def table(name, *columns, **kw):
        return sa.Table(name, m, *columns, **kw)

    def ident():
        return sa.Column('id', sa.String(36), primary_key=True)

    def fk(name, target, nullable=True, ondelete=None):
        return sa.Column(name, sa.String(36), sa.ForeignKey(target, ondelete=ondelete), nullable=nullable)

    def project():
        return sa.Column('project_id', sa.Integer, sa.ForeignKey('project.id', ondelete='SET NULL'))

    def revision():
        return sa.Column('revision', sa.Integer, nullable=False, server_default='1')

    table('hosting_instance', ident(), sa.Column('provider', sa.String(40), nullable=False),
          sa.Column('api_origin', sa.Text, nullable=False), sa.UniqueConstraint('provider', 'api_origin'))
    table('source_owner', ident(), fk('instance_id', 'hosting_instance.id', False),
          sa.Column('provider_owner_id', sa.String(200), nullable=False), sa.Column('login', sa.Text),
          sa.Column('kind', sa.String(40)), sa.UniqueConstraint('instance_id', 'provider_owner_id'),
          sa.UniqueConstraint('id', 'instance_id'))
    table('repository', ident(), fk('instance_id', 'hosting_instance.id', False),
          sa.Column('provider_repository_id', sa.String(200), nullable=False), fk('owner_id', 'source_owner.id'),
          sa.Column('name', sa.Text), sa.Column('web_locator', sa.Text), fk('fork_parent_id', 'repository.id'),
          revision(), sa.UniqueConstraint('instance_id', 'provider_repository_id'),
          sa.ForeignKeyConstraint(['owner_id', 'instance_id'], ['source_owner.id', 'source_owner.instance_id']))
    table('repository_locator', ident(), sa.Column('authority', sa.String(120), nullable=False),
          sa.Column('locator', sa.Text, nullable=False), fk('repository_id', 'repository.id'),
          sa.Column('verification', sa.String(24), nullable=False, server_default='unresolved'),
          sa.Column('verified_at', sa.DateTime(timezone=True)))
    table('source_connection', ident(), sa.Column('principal', sa.String(120), nullable=False),
          fk('instance_id', 'hosting_instance.id', False), sa.Column('method', sa.String(40), nullable=False),
          sa.Column('verified_subject', sa.String(200)), sa.Column('secret_ref', sa.Text),
          fk('host_identity', 'source_execution_host.id'), fk('account_identity', 'source_execution_account.id'),
          sa.Column('credential_generation', sa.Integer, nullable=False),
          sa.Column('state', sa.String(24), nullable=False), sa.Column('checked_at', sa.DateTime(timezone=True)),
          sa.UniqueConstraint('id', 'credential_generation'),
          sa.CheckConstraint("method IN ('legacy_dashboard_pat', 'agent_gh')"),
          sa.CheckConstraint('credential_generation > 0'),
          sa.CheckConstraint("method <> 'agent_gh' OR (host_identity IS NOT NULL AND account_identity IS NOT NULL)"),
          sa.ForeignKeyConstraint(['account_identity', 'host_identity'],
                                  ['source_execution_account.id', 'source_execution_account.host_id']))
    table('connection_repository_access', fk('connection_id', 'source_connection.id', False),
          sa.Column('credential_generation', sa.Integer, nullable=False), fk('repository_id', 'repository.id', False),
          sa.Column('capabilities', sa.JSON, nullable=False), sa.Column('state', sa.String(24), nullable=False),
          sa.Column('error', sa.Text), sa.Column('checked_at', sa.DateTime(timezone=True)),
          sa.Column('expires_at', sa.DateTime(timezone=True)),
          sa.PrimaryKeyConstraint('connection_id', 'credential_generation', 'repository_id'),
          sa.ForeignKeyConstraint(['connection_id', 'credential_generation'],
                                  ['source_connection.id', 'source_connection.credential_generation']))
    table('source_execution_host', ident(), sa.Column('legacy_name', sa.String(64), nullable=False),
          sa.Column('node_id', sa.String(32), sa.ForeignKey('node.id', ondelete='SET NULL')),
          sa.Column('kind', sa.String(24), nullable=False),
          sa.Column('enrollment_fingerprint', sa.String(64)),
          # One installation-local identity; unknown node names are never coalesced.
          sa.Column('installation_key', sa.String(16), unique=True),
          sa.UniqueConstraint('node_id', 'enrollment_fingerprint'))
    table('source_execution_account', ident(), fk('host_id', 'source_execution_host.id', False),
          sa.Column('legacy_account', sa.String(32), nullable=False),
          sa.Column('machine_account_id', sa.String(32), sa.ForeignKey('group_machine_account.id', ondelete='SET NULL')),
          sa.Column('provisioning_generation', sa.String(64)),
          sa.Column('verification', sa.String(24), nullable=False, server_default='unverified'),
          sa.UniqueConstraint('id', 'host_id'))
    table('project_repository', ident(), project(), sa.Column('legacy_project_id', sa.Integer, nullable=False),
          fk('repository_id', 'repository.id'), fk('locator_id', 'repository_locator.id'),
          sa.Column('visibility_domain', sa.String(120), nullable=False),
          sa.Column('compatibility_primary', sa.Boolean, nullable=False, server_default=sa.false()), revision(),
          sa.CheckConstraint('(repository_id IS NULL) <> (locator_id IS NULL)', name='source_or_locator'),
          sa.UniqueConstraint('id', 'project_id'))
    pr = m.tables['project_repository']
    sa.Index('uq_project_compatibility_primary', pr.c.project_id, unique=True,
             sqlite_where=pr.c.compatibility_primary.is_(True), postgresql_where=pr.c.compatibility_primary.is_(True))
    table('working_location', ident(), sa.Column('authority', sa.String(120), nullable=False),
          fk('host_id', 'source_execution_host.id', False), fk('account_id', 'source_execution_account.id', False),
          sa.Column('recorded_path', sa.Text, nullable=False), sa.Column('canonical_path', sa.Text),
          sa.Column('kind', sa.String(24), nullable=False), fk('parent_id', 'working_location.id'), revision(),
          sa.Column('verification', sa.String(24), nullable=False, server_default='unverified'),
          sa.Column('verified_at', sa.DateTime(timezone=True)),
          sa.ForeignKeyConstraint(['account_id', 'host_id'], ['source_execution_account.id', 'source_execution_account.host_id']))
    table('location_registration', ident(), project(), sa.Column('legacy_project_id', sa.Integer, nullable=False),
          sa.Column('placement_id', sa.Integer, sa.ForeignKey('placement.id', ondelete='SET NULL')),
          sa.Column('legacy_placement_id', sa.Integer), fk('location_id', 'working_location.id', False),
          sa.Column('registration_key', sa.String(160), nullable=False, unique=True),
          sa.UniqueConstraint('project_id', 'placement_id'),
          sa.Column('provenance', sa.JSON, nullable=False), sa.UniqueConstraint('id', 'project_id', 'location_id'))
    table('project_location', ident(), project(), fk('membership_id', 'project_repository.id'),
          fk('location_id', 'working_location.id', False), fk('registration_id', 'location_registration.id', False),
          sa.Column('person', sa.String(64)), sa.Column('trust', sa.String(24), nullable=False),
          sa.Column('sync_status', sa.String(24), nullable=False),
          sa.UniqueConstraint('registration_id'), sa.UniqueConstraint('id', 'project_id'),
          sa.ForeignKeyConstraint(['membership_id', 'project_id'], ['project_repository.id', 'project_repository.project_id'], onupdate='CASCADE'),
          sa.ForeignKeyConstraint(['registration_id', 'project_id', 'location_id'],
                                  ['location_registration.id', 'location_registration.project_id', 'location_registration.location_id'], onupdate='CASCADE'))
    table('project_launch_default', project(), fk('project_location_id', 'project_location.id', False), revision(),
          sa.Column('legacy_project_id', sa.Integer, primary_key=True),
          sa.ForeignKeyConstraint(['project_location_id', 'project_id'], ['project_location.id', 'project_location.project_id'], onupdate='CASCADE'))
    table('session_target', sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id', ondelete='CASCADE'), primary_key=True),
          fk('membership_id', 'project_repository.id'), fk('location_id', 'working_location.id', False),
          fk('registration_id', 'location_registration.id', False), fk('connection_id', 'source_connection.id'),
          sa.Column('snapshot', sa.JSON, nullable=False))
    return [m.tables[name] for name in TABLES]


def backfill(db, project_ids=None, *, metadata=None):
    """Insert once per legacy registration. Never refresh frozen custody/identity facts."""
    if metadata is None:
        metadata = sa.MetaData()
        metadata.reflect(db, only=['project', 'placement', 'node', 'group_machine_account', *TABLES])
    t = metadata.tables
    projects = sa.select(t['project'])
    if project_ids is not None:
        projects = projects.where(t['project'].c.id.in_(project_ids))
    for p in db.execute(projects).mappings():
        authority = 'group:' + p['group_id'] if p['group_id'] else 'person:' + (p['owner'] or '@api')
        membership = db.execute(sa.select(t['project_repository'].c.id).where(
            t['project_repository'].c.project_id == p['id'],
            t['project_repository'].c.compatibility_primary.is_(True))).scalar()
        # Never invent a provider repository identity from a name or remote URL.
        remote = p['remote_url'] or p['repo']
        if remote and membership is None:
            locator, membership = str(uuid.uuid4()), str(uuid.uuid4())
            db.execute(t['repository_locator'].insert().values(id=locator, authority=authority,
                locator=sanitize_locator(remote)))
            db.execute(t['project_repository'].insert().values(id=membership, project_id=p['id'],
                legacy_project_id=p['id'], locator_id=locator, visibility_domain=authority, compatibility_primary=True))
        placements = list(db.execute(sa.select(t['placement']).where(t['placement'].c.project_id == p['id'])).mappings())
        registrations = []
        if p['workspace_path'] and p['account']:
            registrations.append((None, p['host'], p['account'], p['workspace_path'], p['owner'],
                                  'legacy' if p['trusted'] else 'pending', 'legacy', None))
        registrations.extend((q['id'], q['host'], q['account'], q['path'], q['person'], q['trust'],
                              q['sync_status'], q['machine_account_id']) for q in placements)
        for placement_id, host, account, path, person, trust, sync, machine_account in registrations:
            key = f'project:{p["id"]}'
            existing = (t['location_registration'].c.project_id == p['id']) & (
                t['location_registration'].c.placement_id == placement_id) if placement_id else (
                    t['location_registration'].c.registration_key == key)
            if db.execute(sa.select(t['location_registration'].c.id).where(existing)).scalar():
                continue
            if placement_id:
                # Placement integer IDs may be reused after deletion. Its detached
                # custody record must never become the new registration's identity.
                key = f'placement:{p["id"]}:{placement_id}:{uuid.uuid4()}'
            node_row = db.execute(sa.select(t['node']).where(t['node'].c.name == host)).mappings().first() if host != 'local' else None
            node = node_row['id'] if node_row else None
            import hashlib
            fingerprint = hashlib.sha256(node_row['public_key'].encode()).hexdigest() if node_row and node_row['public_key'] else None
            host_query = sa.select(t['source_execution_host'].c.id)
            if host == 'local':
                host_query = host_query.where(t['source_execution_host'].c.installation_key == 'local')
            elif node and fingerprint:
                host_query = host_query.where(t['source_execution_host'].c.node_id == node,
                    t['source_execution_host'].c.enrollment_fingerprint == fingerprint)
            else:
                host_query = None  # A lexical machine label is not a durable identity.
            host_id = db.execute(host_query).scalar() if host_query is not None else None
            if not host_id:
                host_id = str(uuid.uuid4())
                insert = sqlite_insert if db.dialect.name == 'sqlite' else pg_insert
                statement = insert(t['source_execution_host']).values(id=host_id, legacy_name=host,
                    node_id=node, enrollment_fingerprint=fingerprint, kind='local' if host == 'local' else 'enrolled' if node else 'unresolved',
                    installation_key='local' if host == 'local' else None)
                if host_query is not None:
                    # Different new projects/placements can register the same
                    # enrolled host concurrently. Do not fail or mint two IDs.
                    columns = ['installation_key'] if host == 'local' else ['node_id', 'enrollment_fingerprint']
                    statement = statement.on_conflict_do_nothing(index_elements=columns)
                db.execute(statement)
                if host_query is not None:
                    host_id = db.execute(host_query).scalar_one()
            account_id, location, registration, link = (str(uuid.uuid4()) for _ in range(4))
            db.execute(t['source_execution_account'].insert().values(id=account_id, host_id=host_id,
                legacy_account=account, machine_account_id=machine_account))
            db.execute(t['working_location'].insert().values(id=location, authority=authority, host_id=host_id,
                account_id=account_id, recorded_path=path, kind='checkout' if membership else 'local_folder'))
            provenance = {k: p[k] for k in ('slug', 'owner', 'group_id', 'remote_url', 'repo', 'import_mode',
                'managed_checkout', 'trusted', 'archived', 'deleting', 'private_workspace', 'builtin', 'backup_identity')}
            provenance['remote_url'] = sanitize_locator(p['remote_url']) if p['remote_url'] else None
            provenance['repo'] = sanitize_locator(p['repo']) if p['repo'] else None
            provenance.update(host=host, account=account, path=path, person=person,
                              machine_account_id=machine_account)
            if placement_id:
                # Project-root creation evidence does not grant custody of a
                # different Placement folder (even when its coordinates match).
                provenance['project_managed_checkout'] = p['managed_checkout']
                provenance['managed_checkout'] = False
                original = next(q for q in placements if q['id'] == placement_id)
                provenance.update(trust=trust, sync_status=sync, approved_by=original['approved_by'])
            db.execute(t['location_registration'].insert().values(id=registration, project_id=p['id'],
                legacy_project_id=p['id'], placement_id=placement_id, legacy_placement_id=placement_id,
                location_id=location, registration_key=key, provenance=provenance))
            db.execute(t['project_location'].insert().values(id=link, project_id=p['id'], membership_id=membership,
                location_id=location, registration_id=registration, person=person, trust=trust, sync_status=sync))
            if placement_id is None and not db.execute(sa.select(t['project_launch_default'].c.legacy_project_id).where(
                    t['project_launch_default'].c.legacy_project_id == p['id'])).scalar():
                db.execute(t['project_launch_default'].insert().values(project_id=p['id'], legacy_project_id=p['id'],
                    project_location_id=link))


def sanitize_locator(value):
    """Keep the legacy field intact; identity projections never expose URL credentials/query fragments."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        url = urlsplit(value)
        if url.scheme and url.netloc:
            host = url.hostname or ''
            if ':' in host:
                host = '[' + host + ']'
            if url.port:
                host += ':' + str(url.port)
            return urlunsplit((url.scheme, host, url.path, '', ''))
    except ValueError:
        return '[unresolved legacy locator]'
    # scp-like user@host:path: discard user info; never treat it as verified.
    return value.rsplit('@', 1)[-1].split('?', 1)[0].split('#', 1)[0]
