"""Organizations phase 1: OIDC provider, external identities, SSO invites and sign-in flows; stable person IDs."""
import sqlalchemy as sa
from alembic import op

revision = '0053'
down_revision = '0052'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('person_id', sa.String(36)))
        batch.create_unique_constraint('uq_user_person_id', ['person_id'])
    with op.batch_alter_table('auth_session') as batch:
        batch.add_column(sa.Column('method', sa.String(16)))
        batch.add_column(sa.Column('step_up', sa.Boolean, nullable=False, server_default=sa.false()))
    op.create_table(
        'oidc_provider',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('issuer', sa.String(300), nullable=False),
        sa.Column('client_id', sa.String(200), nullable=False),
        sa.Column('secret_ciphertext', sa.Text),
        sa.Column('enabled', sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column('revision', sa.Integer, nullable=False, server_default='1'),
        sa.Column('session_hours', sa.Integer, nullable=False, server_default='12'),
        sa.Column('updated_by', sa.String(64)),
        sa.Column('updated_at', sa.DateTime, nullable=False),
    )
    op.create_table(
        'external_identity',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('organization', sa.String(64), nullable=False),
        sa.Column('issuer', sa.String(300), nullable=False),
        sa.Column('subject', sa.String(255), nullable=False),
        sa.Column('person_id', sa.String(36), sa.ForeignKey('user.person_id', ondelete='CASCADE'), nullable=False),
        sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('last_login_at', sa.DateTime),
        sa.UniqueConstraint('organization', 'issuer', 'subject', name='uq_external_identity'),
    )
    op.create_index('ix_external_identity_person_id', 'external_identity', ['person_id'])
    op.create_table(
        'sso_invite',
        sa.Column('id', sa.Integer, primary_key=True),
        sa.Column('token_hash', sa.String(64), nullable=False, unique=True),
        sa.Column('username', sa.String(64)),
        sa.Column('email', sa.String(320), nullable=False),
        sa.Column('account', sa.String(32)),
        sa.Column('user_id', sa.Integer, sa.ForeignKey('user.id', ondelete='CASCADE')),
        sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime, nullable=False),
        sa.Column('expires_at', sa.DateTime, nullable=False),
        sa.Column('used_at', sa.DateTime),
    )
    op.create_table(
        'oidc_flow',
        sa.Column('state_hash', sa.String(64), primary_key=True),
        sa.Column('binding_hash', sa.String(64), nullable=False),
        sa.Column('nonce', sa.String(64), nullable=False),
        sa.Column('verifier', sa.String(128), nullable=False),
        sa.Column('provider_revision', sa.Integer, nullable=False),
        sa.Column('invite_id', sa.Integer, sa.ForeignKey('sso_invite.id', ondelete='CASCADE')),
        sa.Column('session_hash', sa.String(64)),
        sa.Column('client', sa.String(64), nullable=False, server_default=''),
        sa.Column('expires_at', sa.DateTime, nullable=False),
    )
    op.create_index('ix_oidc_flow_client', 'oidc_flow', ['client'])


def downgrade():
    op.drop_table('oidc_flow')
    op.drop_table('sso_invite')
    op.drop_index('ix_external_identity_person_id', 'external_identity')
    op.drop_table('external_identity')
    op.drop_table('oidc_provider')
    with op.batch_alter_table('auth_session') as batch:
        batch.drop_column('step_up')
        batch.drop_column('method')
    with op.batch_alter_table('user') as batch:
        batch.drop_constraint('uq_user_person_id', type_='unique')
        batch.drop_column('person_id')
