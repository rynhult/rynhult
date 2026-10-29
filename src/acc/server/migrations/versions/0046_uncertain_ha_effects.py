"""Quarantine uncertain external effects across manual authority handover."""
import sqlalchemy as sa
from alembic import op

revision = '0046'
down_revision = '0045'
branch_labels = depends_on = None


def upgrade():
    op.add_column('ha_authority', sa.Column('uncertain', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    op.drop_column('ha_authority', 'uncertain')
