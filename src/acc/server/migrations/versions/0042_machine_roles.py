"""Machine roles and durable presence/maintenance state."""
import json
import sqlalchemy as sa
from alembic import op

revision = '0042'
down_revision = '0041'
branch_labels = depends_on = None


def upgrade():
    for column in (
        sa.Column('role', sa.String(16)),
        sa.Column('offline_threshold_seconds', sa.Integer(), nullable=False, server_default='600'),
        sa.Column('offline_since', sa.DateTime()),
        sa.Column('snoozed_until', sa.DateTime()),
        sa.Column('last_downtime_seconds', sa.Integer()),
    ):
        op.add_column('node', column)
    connection = op.get_bind()
    for row in connection.execute(sa.text('SELECT id, info, status FROM node')).mappings():
        if row['status'] == 'pending':
            continue  # enrollment will choose the OS default once it has OS metadata
        info = row['info'] or {}
        if isinstance(info, str):
            info = json.loads(info)
        text = ' '.join(str(info.get(k, '')) for k in ('os', 'release', 'platform')).lower()
        role = 'workstation' if str(info.get('wsl', '')).lower() in ('true', '1', '2') or any(s in text for s in ('darwin', 'mac', 'windows', 'wsl', 'microsoft')) else 'always_on'
        connection.execute(sa.text('UPDATE node SET role=:role WHERE id=:id'), {'role': role, 'id': row['id']})
    op.add_column('attention_item', sa.Column('snoozed_until', sa.DateTime()))


def downgrade():
    op.drop_column('attention_item', 'snoozed_until')
    for name in ('role', 'offline_threshold_seconds', 'offline_since', 'snoozed_until', 'last_downtime_seconds'):
        op.drop_column('node', name)
