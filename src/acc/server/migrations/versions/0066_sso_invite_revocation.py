"""Retain withdrawn invitations so pending sign-ins can explain the refusal."""
import sqlalchemy as sa
from alembic import op

revision = "0066"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("sso_invite", sa.Column("withdrawn_at", sa.DateTime(), nullable=True))


def downgrade():
    # Losing the marker must not reactivate a withdrawn invitation.
    op.execute("UPDATE sso_invite SET expires_at = withdrawn_at "
               "WHERE withdrawn_at IS NOT NULL AND expires_at > withdrawn_at")
    with op.batch_alter_table("sso_invite") as batch:
        batch.drop_column("withdrawn_at")
