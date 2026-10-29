"""Discard reports produced by the pre-review scrubber without changing sent history."""
import sqlalchemy as sa
from alembic import op

revision = '0049'
down_revision = '0048'
branch_labels = depends_on = None


def upgrade():
    op.execute(sa.text("""
        UPDATE attention_item SET resolved_at = CURRENT_TIMESTAMP
        WHERE resolved_at IS NULL AND dedupe_key IN (
            SELECT 'telemetry:' || id FROM telemetry_item
            WHERE kind = 'reports' AND state != 'sent'
        )
    """))
    op.execute(sa.text("DELETE FROM telemetry_item WHERE kind = 'reports' AND state != 'sent'"))


def downgrade():
    pass
