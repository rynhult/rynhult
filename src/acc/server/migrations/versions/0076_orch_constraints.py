"""Lane D: person-owned constraints/requirements, run mode and dependent-work notices.

Based on main's 0073. Coordinator chains this after lane C's reserved 0075 when
combining the independently based lanes; no placeholder migrations are introduced.
"""
import sqlalchemy as sa
from alembic import op

revision = '0076'
down_revision = '0075'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('orchestration_plan') as b:
        b.add_column(sa.Column('run_mode', sa.String(16), nullable=False, server_default='build'))
        b.add_column(sa.Column('build_inputs', sa.JSON(), nullable=False, server_default='{}'))
    with op.batch_alter_table('plan_phase') as b:
        b.add_column(sa.Column('requirement_ids', sa.JSON(), nullable=False, server_default='[]'))
    with op.batch_alter_table('work_item') as b:
        b.add_column(sa.Column('blocks', sa.JSON(), nullable=False, server_default='[]'))
    for name, requirement in [('plan_constraint', False), ('plan_requirement', True)]:
        op.create_table(name,
            sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), primary_key=True),
            sa.Column('id', sa.String(16), primary_key=True),
            sa.Column('text', sa.Text(), nullable=False),
            *([sa.Column('acceptance', sa.Text(), nullable=False)] if requirement else []),
            sa.Column('source', sa.String(16), nullable=False),
            sa.Column('version', sa.Integer(), nullable=False),
            sa.Column('state', sa.String(24), nullable=False),
            sa.Column('brief_version', sa.Integer(), nullable=True))
    op.create_table('plan_proposal',
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('work_item.id'), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('target_id', sa.String(16), nullable=False),
        sa.Column('base_version', sa.Integer(), nullable=False),
        sa.Column('brief_version', sa.Integer(), nullable=True),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('acceptance', sa.Text(), nullable=False))
    op.create_index('ix_plan_proposal_team_id', 'plan_proposal', ['team_id'])
    op.create_table('plan_change_notice',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
        sa.Column('plan_version', sa.Integer(), nullable=False),
        sa.Column('assignment_id', sa.String(32), nullable=False),
        sa.Column('attempt_id', sa.String(32), nullable=False),
        sa.Column('session_id', sa.String(32), sa.ForeignKey('session.id'), nullable=True),
        sa.Column('fields', sa.JSON(), nullable=False),
        sa.Column('delivery_id', sa.String(32), sa.ForeignKey('message_delivery.id'), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('team_id', 'plan_version', 'assignment_id', 'attempt_id', name='uq_plan_notice'))
    op.create_index('ix_plan_change_notice_team_id', 'plan_change_notice', ['team_id'])


def downgrade():
    op.drop_table('plan_change_notice')
    op.drop_table('plan_proposal')
    op.drop_table('plan_requirement')
    op.drop_table('plan_constraint')
    for table, fields in [('work_item', ['blocks']), ('plan_phase', ['requirement_ids']),
                           ('orchestration_plan', ['build_inputs', 'run_mode'])]:
        with op.batch_alter_table(table) as b:
            for field in fields:
                b.drop_column(field)
