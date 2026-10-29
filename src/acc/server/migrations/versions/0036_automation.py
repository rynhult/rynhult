"""Durable session auto-resume and goal state."""
from alembic import op
import sqlalchemy as sa
revision = '0036'
down_revision = '0035'
branch_labels = depends_on = None


def upgrade():
    op.add_column('session', sa.Column('automation', sa.JSON()))


def downgrade():
    op.drop_column('session', 'automation')
