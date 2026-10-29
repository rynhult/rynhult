"""Local WebAuthn credentials, bounded persistent ceremonies and recovery policy."""
import sqlalchemy as sa
from alembic import op

revision = '0041'
down_revision = '0040'
branch_labels = depends_on = None


def upgrade():
    op.add_column('user', sa.Column('passkey_handle', sa.String(64)))
    op.create_index('ix_user_passkey_handle', 'user', ['passkey_handle'], unique=True)
    op.add_column('user', sa.Column('recovery_hash', sa.String(64)))
    op.add_column('auth_session', sa.Column('reauthenticated_at', sa.DateTime()))
    op.add_column('auth_session', sa.Column('passkey_verified', sa.Boolean(), nullable=False, server_default=sa.false()))
    op.create_table('passkey',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE'), nullable=False),
        sa.Column('credential_id', sa.String(1400), unique=True, nullable=False),
        sa.Column('public_key', sa.Text(), nullable=False),
        sa.Column('rp_id', sa.String(253), nullable=False),
        sa.Column('name', sa.String(100), nullable=False),
        sa.Column('sign_count', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('last_used_at', sa.DateTime()))
    op.create_index('ix_passkey_user_id', 'passkey', ['user_id'])
    op.create_table('passkey_challenge',
        sa.Column('id', sa.String(64), primary_key=True),
        sa.Column('challenge', sa.String(128), nullable=False),
        sa.Column('purpose', sa.String(16), nullable=False),
        sa.Column('origin', sa.String(300), nullable=False),
        sa.Column('client', sa.String(100), nullable=False),
        sa.Column('session_hash', sa.String(64)),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id', ondelete='CASCADE')),
        sa.Column('expires_at', sa.DateTime(), nullable=False))
    op.create_table('passkey_policy',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('owners', sa.String(16), nullable=False),
        sa.Column('members', sa.String(16), nullable=False))


def downgrade():
    op.drop_table('passkey_policy')
    op.drop_table('passkey_challenge')
    op.drop_table('passkey')
    op.drop_column('auth_session', 'passkey_verified')
    op.drop_column('auth_session', 'reauthenticated_at')
    op.drop_column('user', 'recovery_hash')
    op.drop_index('ix_user_passkey_handle', table_name='user')
    op.drop_column('user', 'passkey_handle')
