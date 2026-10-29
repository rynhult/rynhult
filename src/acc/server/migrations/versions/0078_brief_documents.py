"""Private document evidence and proposed-version citations."""
import sqlalchemy as sa
from alembic import op

revision = '0078'
down_revision = '0077'
branch_labels = depends_on = None


def upgrade():
    op.add_column('project_brief_reference', sa.Column('document', sa.JSON(), nullable=True))
    op.add_column('project_brief_version', sa.Column('provenance', sa.JSON(), nullable=True))


def downgrade():
    op.drop_column('project_brief_version', 'provenance')
    op.drop_column('project_brief_reference', 'document')
