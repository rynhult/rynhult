"""Opt-in group authority and legacy routing snapshots. No sharing or host changes."""
import sqlalchemy as sa
from alembic import op

revision = "0038"
down_revision = "0037"
branch_labels = depends_on = None


def upgrade():
    op.create_table('acc_group',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('name', sa.String(120), nullable=False),
        sa.Column('created_by', sa.String(64), nullable=False),
        sa.Column('home_site', sa.String(64), nullable=False),
        sa.Column('archived', sa.Boolean(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('paused', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('group_membership',
        sa.Column('group_id', sa.String(32), sa.ForeignKey('acc_group.id'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), primary_key=True),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('provenance', sa.String(24), nullable=False),
        sa.Column('external_id', sa.String(200)),
        sa.CheckConstraint("role IN ('maintainer', 'member', 'viewer')"),
        sa.CheckConstraint("provenance IN ('local', 'identity-provider')"))
    # SQLite batch recreation preserves legacy nullable owner rows without assigning a group.
    with op.batch_alter_table('project') as batch:
        batch.add_column(sa.Column('group_id', sa.String(32)))
        batch.create_foreign_key('fk_project_group', 'acc_group', ['group_id'], ['id'])
        batch.create_check_constraint('project_single_authority', 'group_id IS NULL OR owner IS NULL')
        batch.create_index('ix_project_group_id', ['group_id'])
    op.create_table('placement',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('person', sa.String(64)),
        sa.Column('host', sa.String(64), nullable=False),
        sa.Column('account', sa.String(32), nullable=False),
        sa.Column('path', sa.Text(), nullable=False),
        sa.Column('trust', sa.String(16), nullable=False),
        sa.Column('sync_status', sa.String(24), nullable=False),
        sa.Column('approved_by', sa.String(64)),
        sa.UniqueConstraint('project_id', 'person', 'host', 'account', 'path'))
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('placement_id', sa.Integer()))
        batch.create_foreign_key('fk_session_placement', 'placement', ['placement_id'], ['id'])
    op.create_table('group_project_owner',
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('user.id'), primary_key=True))
    op.create_table('group_merge_approval',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id'), nullable=False),
        sa.Column('approved_by', sa.String(64), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('branch', sa.String(200), nullable=False),
        sa.Column('old_sha', sa.String(64), nullable=False),
        sa.Column('new_sha', sa.String(64), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))
    op.create_table('group_key_grant',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('key_id', sa.String(32), sa.ForeignKey('vault_key.id', ondelete='CASCADE'), nullable=False),
        sa.Column('group_id', sa.String(32), sa.ForeignKey('acc_group.id'), nullable=False),
        sa.Column('project_id', sa.Integer(), sa.ForeignKey('project.id')),
        sa.Column('host', sa.String(64)),
        sa.Column('given_by', sa.String(64), nullable=False),
        sa.Column('given_at', sa.DateTime(), nullable=False))
    op.execute("""INSERT INTO placement (project_id, person, host, account, path, trust, sync_status)
        SELECT id, owner, COALESCE(host, 'local'), account, workspace_path,
               CASE WHEN trusted THEN 'legacy' ELSE 'pending' END, 'legacy' FROM project""")
    # A session may be a worktree or run under an older binding. Preserve its actual immutable routing.
    op.execute("""INSERT INTO placement (project_id, person, host, account, path, trust, sync_status)
        SELECT DISTINCT s.project_id, s.owner, COALESCE(s.team_host, project.host, 'local'), s.run_as, COALESCE(s.worktree, s.workdir, project.workspace_path), 'legacy', 'legacy'
        FROM session s JOIN project ON project.id=s.project_id WHERE NOT EXISTS (SELECT 1 FROM placement p WHERE p.project_id=s.project_id
        AND (p.person = s.owner OR (p.person IS NULL AND s.owner IS NULL)) AND p.host=COALESCE(s.team_host, project.host, 'local') AND p.account=s.run_as AND p.path=COALESCE(s.worktree, s.workdir, project.workspace_path))""")
    op.execute("""UPDATE session SET placement_id=(SELECT p.id FROM placement p WHERE p.project_id=session.project_id
        AND (p.person = session.owner OR (p.person IS NULL AND session.owner IS NULL)) AND p.host=COALESCE(session.team_host, (SELECT host FROM project WHERE id=session.project_id), 'local')
        AND p.account=session.run_as AND p.path=COALESCE(session.worktree, session.workdir, (SELECT workspace_path FROM project WHERE id=session.project_id)) LIMIT 1)""")


def downgrade():
    for name in ('group_key_grant', 'group_merge_approval', 'group_project_owner'):
        op.drop_table(name)
    with op.batch_alter_table('session') as batch:
        batch.drop_constraint('fk_session_placement', type_='foreignkey')
        batch.drop_column('placement_id')
    op.drop_table('placement')
    with op.batch_alter_table('project') as batch:
        batch.drop_index('ix_project_group_id')
        batch.drop_constraint('project_single_authority', type_='check')
        batch.drop_constraint('fk_project_group', type_='foreignkey')
        batch.drop_column('group_id')
    op.drop_table('group_membership')
    op.drop_table('acc_group')
