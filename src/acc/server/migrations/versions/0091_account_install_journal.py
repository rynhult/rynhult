"""Service-owned reviewed artifacts and account install admission/publication evidence."""
import sqlalchemy as sa
from alembic import op

revision = '0091'
down_revision = '0090'
branch_labels = depends_on = None


def upgrade():
    op.create_table('install_recipe',
        sa.Column('digest', sa.String(64), primary_key=True),
        sa.Column('recipe_id', sa.String(64), nullable=False),
        sa.Column('recipe', sa.JSON(), nullable=False),
        sa.Column('metadata_entry', sa.JSON(), nullable=False),
        sa.Column('artifact', sa.LargeBinary(), nullable=False),
        sa.Column('packument_digest', sa.String(64), nullable=False),
        sa.Column('reviewer', sa.String(64), nullable=False),
        sa.Column('approver', sa.String(64), nullable=False),
        sa.Column('withdrawn', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('account_install_journal',
        sa.Column('operation_id', sa.String(32), sa.ForeignKey('delegated_action.id'), primary_key=True),
        sa.Column('recipe_digest', sa.String(64), sa.ForeignKey('install_recipe.digest'), nullable=False),
        sa.Column('nonce', sa.String(32), nullable=False, unique=True),
        sa.Column('account_digest', sa.String(64), nullable=False),
        sa.Column('lease_deadline', sa.DateTime(timezone=True), nullable=False),
        sa.Column('dispatched_at', sa.DateTime(timezone=True)),
        sa.Column('before', sa.JSON()),
        sa.Column('envelope', sa.JSON(), nullable=False),
        sa.Column('staged', sa.JSON()),
        sa.Column('steps', sa.JSON(), nullable=False),
        sa.Column('cleanup', sa.String(32), nullable=False))
    op.create_index('ix_account_install_journal_account_digest', 'account_install_journal', ['account_digest'])


def downgrade():
    connection = op.get_bind()
    if (connection.scalar(sa.text('SELECT count(*) FROM account_install_journal'))
            or connection.scalar(sa.text('SELECT count(*) FROM install_recipe'))):
        raise RuntimeError('Install evidence exists; fence work and restore the pre-upgrade backup or forward-repair.')
    op.drop_table('account_install_journal')
    op.drop_table('install_recipe')
