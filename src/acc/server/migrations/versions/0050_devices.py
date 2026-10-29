"""Device registry and one-use LAN pairing capabilities (no private material)."""
import sqlalchemy as sa
from alembic import op

revision = '0050'
down_revision = '0049'
branch_labels = depends_on = None


def upgrade():
    op.create_table('device',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('person', sa.String(64), nullable=False, index=True),
        sa.Column('label', sa.String(100), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('certificate_pem', sa.Text(), nullable=False),
        sa.Column('fingerprint', sa.String(64), nullable=False, unique=True),
        sa.Column('serial', sa.String(64), nullable=False, unique=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('last_seen_at', sa.DateTime()),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('device_pairing',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('person', sa.String(64), nullable=False, index=True),
        sa.Column('label', sa.String(100), nullable=False),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('receipt_hash', sa.String(64), unique=True),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('device_id', sa.String(32)),
        sa.Column('renew_id', sa.String(32)),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))


def downgrade():
    op.drop_table('device_pairing')
    op.drop_table('device')
