"""Person-issued scoped consent and exact approval admission records."""
import sqlalchemy as sa
from alembic import op

revision = '0083'
down_revision = '0082'
branch_labels = depends_on = None


def upgrade():
    op.create_table('approval_consent',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('parent_id', sa.String(32), sa.ForeignKey('approval_consent.id')),
        sa.Column('issuer', sa.String(64), nullable=False, index=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id')),
        sa.Column('team_id', sa.String(32), sa.ForeignKey('team.id'), nullable=False),
        sa.Column('action', sa.String(32), nullable=False), sa.Column('constraints', sa.JSON(), nullable=False),
        sa.Column('authority', sa.JSON(), nullable=False), sa.Column('duration', sa.String(16), nullable=False),
        sa.Column('request_digest', sa.String(64)), sa.Column('state', sa.String(16), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False), sa.Column('remaining', sa.Integer()),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True)), sa.Column('revoked_at', sa.DateTime(timezone=True)),
        sa.Column('last_used_at', sa.DateTime(timezone=True)), sa.Column('verification_ref', sa.String(64)),
        sa.UniqueConstraint('parent_id', 'team_id', name='approval_child_team'))
    op.create_table('approval_operation',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('request_id', sa.String(40), sa.ForeignKey('tool_request.id'), nullable=False, unique=True),
        sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('consent_id', sa.String(32), sa.ForeignKey('approval_consent.id'), nullable=False),
        sa.Column('consent_revision', sa.Integer(), nullable=False),
        sa.Column('lead_id', sa.String(32), sa.ForeignKey('session.id'), nullable=False),
        sa.Column('state', sa.String(16), nullable=False), sa.Column('reason', sa.String(100), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('approval_batch', sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('actor', sa.String(64), nullable=False), sa.Column('digest', sa.String(64), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False), sa.Column('security_revision', sa.Integer(), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=False), sa.Column('used', sa.Boolean(), nullable=False))
    # 0030 creates its tables from current model metadata on a fresh database.
    if 'decision' not in {c['name'] for c in sa.inspect(op.get_bind()).get_columns('tool_request')}:
        with op.batch_alter_table('tool_request') as batch:
            batch.add_column(sa.Column('decision', sa.JSON(), nullable=True))


def downgrade():
    # Retain both active authority and revoked/dispatched evidence. Older binaries
    # cannot enforce these capabilities; no translation into global guard Off.
    bind = op.get_bind()
    if bind.scalar(sa.text('SELECT count(*) FROM tool_request WHERE decision IS NOT NULL')) or any(
           bind.scalar(sa.text('SELECT count(*) FROM ' + name)) for name in
           ('approval_consent', 'approval_operation', 'approval_batch')):
        raise RuntimeError('Delegated approval records exist; fence affected work and restore the pre-upgrade backup or forward-repair.')
    with op.batch_alter_table('tool_request') as batch:
        batch.drop_column('decision')
    op.drop_table('approval_operation')
    op.drop_table('approval_batch')
    op.drop_table('approval_consent')
