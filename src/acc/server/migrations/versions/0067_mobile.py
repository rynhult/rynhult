"""Native device pairing metadata and certificate-bound person sessions."""
import sqlalchemy as sa
from alembic import op

revision = '0067'
down_revision = '0066'  # coordinator: company sign-in follows group migrations 0063/0064
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('auth_session') as batch:
        batch.add_column(sa.Column('device_fingerprint', sa.String(64)))
        batch.add_column(sa.Column('device_bound_at', sa.DateTime()))
    for table in ('device', 'device_pairing'):
        with op.batch_alter_table(table) as batch:
            batch.add_column(sa.Column('installation_id', sa.String(32)))
            batch.add_column(sa.Column('platform', sa.String(16)))
            batch.add_column(sa.Column('key_hash', sa.String(64)))
    with op.batch_alter_table('device') as batch:
        batch.create_unique_constraint('uq_device_installation', ['person', 'kind', 'installation_id'])
    with op.batch_alter_table('device_pairing') as batch:
        batch.add_column(sa.Column('renew_fingerprint', sa.String(64)))
        batch.add_column(sa.Column('kind', sa.String(16), nullable=False, server_default='browser'))
        batch.add_column(sa.Column('csr_pem', sa.Text()))


def downgrade():
    # Older code knows neither: a phone-bound sign-in would become a cookie usable without the phone's key, and a live
    # phone pairing a browser pairing (security review 9, SR9-8). Both are short-lived; the phone signs in again.
    op.execute("DELETE FROM auth_session WHERE device_fingerprint IS NOT NULL")
    op.execute("DELETE FROM device_pairing WHERE kind != 'browser'")
    with op.batch_alter_table('device_pairing') as batch:
        batch.drop_column('renew_fingerprint')
        batch.drop_column('csr_pem')
        batch.drop_column('kind')
    with op.batch_alter_table('device') as batch:
        batch.drop_constraint('uq_device_installation', type_='unique')
    for table in ('device_pairing', 'device'):
        with op.batch_alter_table(table) as batch:
            for name in ('key_hash', 'platform', 'installation_id'):
                batch.drop_column(name)
    with op.batch_alter_table('auth_session') as batch:
        batch.drop_column('device_bound_at')
        batch.drop_column('device_fingerprint')
