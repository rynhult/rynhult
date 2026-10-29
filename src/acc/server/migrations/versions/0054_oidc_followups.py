"""Organizations phase 1 follow-ups (decision 0061): owner-named MFA acr values, passkey enrollment after an MFA-proven
company sign-in, and which members have no local credentials."""
import sqlalchemy as sa
from alembic import op

revision = '0054'
down_revision = '0053'
branch_labels = depends_on = None


def upgrade():
    with op.batch_alter_table('oidc_provider') as batch:
        batch.add_column(sa.Column('mfa_acr', sa.JSON, nullable=False, server_default='[]'))
        batch.add_column(sa.Column('passkey_enrollment', sa.Boolean, nullable=False, server_default=sa.true()))
    with op.batch_alter_table('user') as batch:
        batch.add_column(sa.Column('company_only', sa.Boolean, nullable=False, server_default=sa.false()))
    with op.batch_alter_table('auth_session') as batch:
        batch.add_column(sa.Column('passkey_enroll_until', sa.DateTime))
    # Members that company sign-in created (pre-provisioned or invited) never had local credentials. Linked members
    # ("linked") keep theirs. The audit log is the only record of how each identity was bound.
    bind = op.get_bind()
    audit = sa.table('audit_log', sa.column('action', sa.String), sa.column('target', sa.String),
                     sa.column('detail', sa.JSON))
    user = sa.table('user', sa.column('username', sa.String), sa.column('company_only', sa.Boolean))
    created = {row.target for row in bind.execute(sa.select(audit.c.target, audit.c.detail)
                                                  .where(audit.c.action == 'organizations.identity_bound'))
               if isinstance(row.detail, dict) and row.detail.get('how') in ('preprovisioned', 'invited')}
    for username in sorted(created):
        bind.execute(user.update().where(user.c.username == username).values(company_only=True))


def downgrade():
    bind = op.get_bind()
    user = sa.table('user', sa.column('id', sa.Integer), sa.column('company_only', sa.Boolean))
    passkey = sa.table('passkey', sa.column('user_id', sa.Integer))
    if bind.execute(sa.select(passkey.c.user_id).join(user, user.c.id == passkey.c.user_id)
                    .where(user.c.company_only.is_(True)).limit(1)).first() is not None:
        raise RuntimeError('Cannot downgrade 0054 while company-only accounts have passkeys. Remove those passkeys '
                           'or accounts first; otherwise their passkeys could become standalone local sign-ins.')
    with op.batch_alter_table('auth_session') as batch:
        batch.drop_column('passkey_enroll_until')
    with op.batch_alter_table('user') as batch:
        batch.drop_column('company_only')
    with op.batch_alter_table('oidc_provider') as batch:
        batch.drop_column('passkey_enrollment')
        batch.drop_column('mfa_acr')
