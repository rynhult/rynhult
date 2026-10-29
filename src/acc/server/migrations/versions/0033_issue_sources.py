"""Project issue-source connections and repository remotes."""
from alembic import op
import sqlalchemy as sa

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("project", sa.Column("remote_url", sa.Text(), nullable=True))
    op.add_column("project", sa.Column("issue_sources", sa.JSON(), nullable=True))
    op.add_column("session", sa.Column("issue_source", sa.JSON(), nullable=True))
    op.add_column("vault_grant", sa.Column("server_only", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    op.drop_column("vault_grant", "server_only")
    op.drop_column("session", "issue_source")
    op.drop_column("project", "issue_sources")
    op.drop_column("project", "remote_url")
