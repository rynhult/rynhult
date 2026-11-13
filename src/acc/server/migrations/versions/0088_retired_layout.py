"""Remove retired per-person layout choices and admin rollout choice."""
import sqlalchemy as sa
from alembic import op

revision = '0088'
down_revision = '0087'
branch_labels = None
depends_on = None


def upgrade():
    settings = sa.table('setting', sa.column('key', sa.String), sa.column('value', sa.JSON))
    bind = op.get_bind()
    bind.execute(settings.delete().where(settings.c.key.startswith('ui_shell.', autoescape=True)))
    row = bind.execute(sa.select(settings.c.value).where(settings.c.key == 'admin_features')).first()
    if row and isinstance(row.value, dict) and 'new_shell' in row.value.get('choices', {}):
        value = {**row.value, 'choices': {k: v for k, v in row.value['choices'].items() if k != 'new_shell'}}
        bind.execute(settings.update().where(settings.c.key == 'admin_features').values(value=value))


def downgrade():
    # Retired presentation choices cannot be recovered; older releases use defaults.
    pass
