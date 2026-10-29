"""Manual control-plane authority; a single monotonically increasing epoch."""
import sqlalchemy as sa
from alembic import op

revision = '0045'
down_revision = '0044'
branch_labels = depends_on = None


def upgrade():
    op.create_table('ha_authority',
                           sa.Column('id', sa.Integer(), primary_key=True),
                           sa.Column('epoch', sa.BigInteger(), nullable=False),
                           sa.Column('holder', sa.String(64)),
                           sa.Column('incarnation', sa.String(64)),
                           sa.CheckConstraint('id = 1', name='ha_singleton'))


def downgrade():
    op.drop_table('ha_authority')
