"""Portable DNS mutation intent and ownership ledger."""
from alembic import op
from acc.server.models import DNSOwnedRecord

revision = '0043'
down_revision = '0042'
branch_labels = None
depends_on = None


def upgrade():
    DNSOwnedRecord.__table__.create(op.get_bind())


def downgrade():
    DNSOwnedRecord.__table__.drop(op.get_bind())
