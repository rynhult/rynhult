"""Group checkout receipts, work board and launch policy."""
import sqlalchemy as sa
from alembic import op

revision = '0047'
down_revision = '0046'
branch_labels = depends_on = None


def upgrade():
    for name, default in [('board_revision', '1'), ('concurrent', '4'), ('daily_launches', '50'), ('concurrent_ceiling', '4'), ('daily_ceiling', '50')]:
        op.add_column('acc_group', sa.Column(name, sa.Integer(), nullable=False, server_default=default))
    op.add_column('acc_group', sa.Column('usage_warning_usd', sa.Float()))
    op.add_column('acc_group', sa.Column('override_until', sa.DateTime()))
    from acc.server.models import GroupCheckout, GroupClaim, GroupLaunch, GroupOperation, GroupWork
    for model in (GroupCheckout, GroupWork, GroupClaim, GroupOperation, GroupLaunch):
        model.__table__.create(op.get_bind())


def downgrade():
    for table in ('group_launch', 'group_operation', 'group_claim', 'group_work', 'group_checkout'):
        op.drop_table(table)
    for column in ('usage_warning_usd', 'daily_ceiling', 'concurrent_ceiling', 'override_until', 'daily_launches', 'concurrent', 'board_revision'):
        op.drop_column('acc_group', column)
