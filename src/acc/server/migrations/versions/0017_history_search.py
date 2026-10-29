"""history search: every session's conversation in a full-text index

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-22 01:30:00
"""
import sqlalchemy as sa
from alembic import op

import acc.server.models

revision = '0017'
down_revision = '0016'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('session') as batch:
        batch.add_column(sa.Column('transcript_stamp', sa.String(length=64), nullable=True))
        batch.add_column(sa.Column('indexed_at', acc.server.models.UTCDateTime(), nullable=True))
    # session_id/role/ts are stored but not tokenized; porter: "deploying" finds "deploy"
    if op.get_bind().dialect.name == "postgresql":
        op.create_table("transcript_fts", sa.Column("text", sa.Text(), nullable=False),
                        sa.Column("session_id", sa.String(32), nullable=False),
                        sa.Column("role", sa.Text()), sa.Column("ts", sa.Text()), sa.Column("seq", sa.Integer()))
        op.execute("CREATE INDEX transcript_fts_search ON transcript_fts USING gin "
                   "(to_tsvector('english', text))")
        op.create_index("transcript_fts_session", "transcript_fts", ["session_id"])
        return
    op.execute("CREATE VIRTUAL TABLE transcript_fts USING fts5(text, session_id UNINDEXED, role UNINDEXED, "
               "ts UNINDEXED, seq UNINDEXED, tokenize='porter unicode61')")


def downgrade() -> None:
    op.execute("DROP TABLE transcript_fts")
    with op.batch_alter_table('session') as batch:
        batch.drop_column('indexed_at')
        batch.drop_column('transcript_stamp')
