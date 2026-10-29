"""M16 tools: durable approvals and installed version pins."""
from alembic import op

from acc.server.models import InstalledTool, ToolRequest

revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


def upgrade():
    ToolRequest.__table__.create(op.get_bind())
    InstalledTool.__table__.create(op.get_bind())


def downgrade():
    InstalledTool.__table__.drop(op.get_bind())
    ToolRequest.__table__.drop(op.get_bind())
