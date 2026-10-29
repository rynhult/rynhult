"""Individual local-only development workspaces.

Revision ID: 0031
Revises: 0030
"""
import sqlalchemy as sa
from alembic import op

revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("project") as batch:
        batch.add_column(sa.Column("backup_identity", sa.String(120), nullable=True))
        batch.add_column(sa.Column("private_workspace", sa.Boolean(), nullable=False, server_default="0"))


def downgrade():
    with op.batch_alter_table("project") as batch:
        batch.drop_column("private_workspace")
        batch.drop_column("backup_identity")
