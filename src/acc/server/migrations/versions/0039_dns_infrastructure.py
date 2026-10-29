"""Owner-only infrastructure vault and desired/applied DNS binding."""
from alembic import op
from acc.server.models import DNSBinding, InfrastructureCredential

revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade():
    InfrastructureCredential.__table__.create(op.get_bind())
    DNSBinding.__table__.create(op.get_bind())


def downgrade():
    DNSBinding.__table__.drop(op.get_bind())
    InfrastructureCredential.__table__.drop(op.get_bind())
