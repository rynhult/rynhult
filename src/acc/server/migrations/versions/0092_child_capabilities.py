"""Immutable managed lineage and per-session requested/effective capability sets.

0092 follows 0091; 0093 is reserved for the relay migration.
"""
import sqlalchemy as sa
from alembic import op

revision = '0092'
down_revision = '0091'
branch_labels = depends_on = None


def upgrade():
    op.add_column('session', sa.Column('parent_chain', sa.JSON(), nullable=True))
    op.add_column('session', sa.Column('root_owner', sa.String(64), nullable=True))
    for name in ('cap_requested', 'cap_effective', 'cap_limit'):
        op.add_column('session', sa.Column(name, sa.JSON(), nullable=True))
    connection = op.get_bind()
    # Older installs used NULL for the installation principal; normalize before lineage/triggers.
    connection.execute(sa.text("UPDATE session SET owner='@api' WHERE owner IS NULL"))
    rows = {row.id: row for row in connection.execute(sa.text('SELECT id, parent_session_id, owner FROM session'))}
    sessions = sa.table('session', sa.column('id', sa.String()), sa.column('parent_chain', sa.JSON()),
                        sa.column('root_owner', sa.String()))
    for row in rows.values():
        parents, seen, current = [], {row.id}, row
        while current.parent_session_id:
            parent_id = current.parent_session_id
            if parent_id in seen or parent_id not in rows:
                raise RuntimeError('Invalid retained parent chain; repair lineage before upgrading.')
            seen.add(parent_id)
            parents.append(parent_id)
            current = rows[parent_id]
        connection.execute(sessions.update().where(sessions.c.id == row.id)
                           .values(parent_chain=list(reversed(parents)), root_owner=current.owner or '@api'))
    # SQLite table rebuilds drop triggers: the head migration test must keep checking these.
    if connection.dialect.name == 'sqlite':
        op.execute('''CREATE TRIGGER session_parent_immutable BEFORE UPDATE OF parent_session_id, parent_chain ON session
            BEGIN SELECT RAISE(ABORT, 'parent session chain is immutable'); END''')
        op.execute('''CREATE TRIGGER session_root_owner_transfer BEFORE UPDATE OF root_owner ON session
            WHEN NOT (NEW.owner IS NOT NULL AND NEW.root_owner IS NOT NULL
                      AND NEW.owner IS NOT OLD.owner AND NEW.root_owner IS NEW.owner)
            BEGIN SELECT RAISE(ABORT, 'parent session chain is immutable'); END''')
        op.execute('''CREATE TRIGGER session_owner_nonnull BEFORE UPDATE OF owner ON session
            WHEN NEW.owner IS NULL OR NEW.root_owner IS NULL
            BEGIN SELECT RAISE(ABORT, 'parent session chain is immutable'); END''')
    else:
        op.execute('''CREATE OR REPLACE FUNCTION acc_session_parent_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              RAISE EXCEPTION 'parent session chain is immutable';
            END $$''')
        op.execute('''CREATE TRIGGER session_parent_immutable BEFORE UPDATE OF parent_session_id, parent_chain
            ON session FOR EACH ROW EXECUTE FUNCTION acc_session_parent_immutable()''')

        op.execute('''CREATE OR REPLACE FUNCTION acc_session_root_owner_transfer() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.owner IS NULL OR NEW.root_owner IS NULL
                 OR NEW.owner IS NOT DISTINCT FROM OLD.owner OR NEW.root_owner IS DISTINCT FROM NEW.owner THEN
                RAISE EXCEPTION 'parent session chain is immutable';
              END IF;
              RETURN NEW;
            END $$''')
        op.execute('''CREATE TRIGGER session_root_owner_transfer BEFORE UPDATE OF root_owner
            ON session FOR EACH ROW EXECUTE FUNCTION acc_session_root_owner_transfer()''')

        op.execute('''CREATE OR REPLACE FUNCTION acc_session_owner_nonnull() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
              IF NEW.owner IS NULL OR NEW.root_owner IS NULL THEN
                RAISE EXCEPTION 'parent session chain is immutable';
              END IF;
              RETURN NEW;
            END $$''')
        op.execute('''CREATE TRIGGER session_owner_nonnull BEFORE UPDATE OF owner
            ON session FOR EACH ROW EXECUTE FUNCTION acc_session_owner_nonnull()''')


def downgrade():
    connection = op.get_bind()
    if connection.scalar(sa.text('SELECT count(*) FROM session WHERE cap_requested IS NOT NULL')):
        raise RuntimeError('Session capability evidence exists; restore the pre-upgrade backup or forward-repair.')
    if connection.dialect.name == 'sqlite':
        op.execute('DROP TRIGGER session_parent_immutable')
        op.execute('DROP TRIGGER session_root_owner_transfer')
        op.execute('DROP TRIGGER session_owner_nonnull')
    else:
        op.execute('DROP TRIGGER session_parent_immutable ON session')
        op.execute('DROP TRIGGER session_root_owner_transfer ON session')
        op.execute('DROP TRIGGER session_owner_nonnull ON session')
        op.execute('DROP FUNCTION acc_session_owner_nonnull()')
        op.execute('DROP FUNCTION acc_session_root_owner_transfer()')
        op.execute('DROP FUNCTION acc_session_parent_immutable()')
    for name in ('cap_limit', 'cap_effective', 'cap_requested', 'root_owner', 'parent_chain'):
        op.drop_column('session', name)
