"""Advisory metadata, independent of attention state."""
import sqlalchemy as sa
from alembic import op

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("attention_item", sa.Column("risk", sa.JSON(), nullable=True))


def downgrade():
    op.drop_column("attention_item", "risk")
