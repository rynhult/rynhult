"""Add source/location identities without rewriting legacy routing or custody."""
import sqlalchemy as sa
from alembic import op

from acc.server.source_schema_v1 import backfill, define

revision = '0084'
down_revision = '0083'
branch_labels = depends_on = None


def upgrade():
    db = op.get_bind()
    metadata = sa.MetaData()
    metadata.reflect(db, only=['project', 'placement', 'node', 'group_machine_account', 'session'])
    tables = define(metadata)
    metadata.create_all(db, tables=tables, checkfirst=True)
    backfill(db)


def downgrade():
    db = op.get_bind()
    # Retain additive compatibility data for old readers and an idempotent re-upgrade.
    # New-only state cannot be represented by legacy binaries: never discard it.
    for table in ('repository', 'source_connection', 'source_owner', 'connection_repository_access', 'hosting_instance'):
        if db.scalar(sa.text(f'SELECT count(*) FROM {table}')):
            raise RuntimeError('Source-only state exists; restore the pre-upgrade backup before downgrading.')
    if db.scalar(sa.text('SELECT count(*) FROM project_repository WHERE NOT compatibility_primary')):
        raise RuntimeError('Multiple repositories exist; restore the pre-upgrade backup before downgrading.')
    for query in (
        "SELECT count(*) FROM project_repository WHERE revision <> 1",
        "SELECT count(*) FROM working_location WHERE revision <> 1 OR parent_id IS NOT NULL "
        "OR canonical_path IS NOT NULL OR verification <> 'unverified'",
        "SELECT count(*) FROM source_execution_account WHERE provisioning_generation IS NOT NULL "
        "OR verification <> 'unverified'",
        "SELECT count(*) FROM working_location w WHERE NOT EXISTS "
        "(SELECT 1 FROM location_registration r WHERE r.location_id = w.id)",
        "SELECT count(*) FROM project_launch_default d JOIN project_location l ON l.id = d.project_location_id "
        "JOIN location_registration r ON r.id = l.registration_id "
        "WHERE d.revision <> 1 OR r.legacy_placement_id IS NOT NULL",
    ):
        if db.scalar(sa.text(query)):
            raise RuntimeError('New location/default state exists; restore the pre-upgrade backup before downgrading.')
