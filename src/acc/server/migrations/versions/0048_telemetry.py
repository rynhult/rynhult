"""Opt-in telemetry counters and immutable local queue/history."""
import sqlalchemy as sa
from alembic import op

revision = '0048'
down_revision = '0047'
branch_labels = depends_on = None


def upgrade():
    op.create_table('telemetry_counter',
                    sa.Column('id', sa.String(100), primary_key=True),
                    sa.Column('day', sa.String(10), nullable=False),
                    sa.Column('event', sa.String(40), nullable=False),
                    sa.Column('provider', sa.String(20), nullable=False),
                    sa.Column('count', sa.Integer(), nullable=False))
    op.create_index('ix_telemetry_counter_day', 'telemetry_counter', ['day'])
    op.create_table('telemetry_item',
                    sa.Column('id', sa.String(100), primary_key=True),
                    sa.Column('kind', sa.String(16), nullable=False),
                    sa.Column('key_id', sa.String(22), nullable=False),
                    sa.Column('body', sa.Text(), nullable=False),
                    sa.Column('state', sa.String(16), nullable=False),
                    sa.Column('created', sa.Float(), nullable=False),
                    sa.Column('due', sa.Float(), nullable=False),
                    sa.Column('attempts', sa.Integer(), nullable=False),
                    sa.Column('result', sa.String(32), nullable=False),
                    sa.Column('receipt', sa.String(36), nullable=False),
                    sa.Column('occurrences', sa.Integer(), nullable=False),
                    sa.Column('sent_at', sa.Float(), nullable=True))


def downgrade():
    op.drop_table('telemetry_item')
    op.drop_table('telemetry_counter')
