"""show_relation: add relation_type column

Batch 4.1 — store AniList relationType (SEQUEL, PREQUEL, SIDE_STORY,
etc.) on the directed show_relation edge.  Nullable TEXT; existing rows
legitimately have no value and will be backfilled on the next metadata
refresh for each show.

Revision ID: d4e5f6071829
Revises: c3d4e5f60718
Create Date: 2026-09-02
"""

from alembic import op

revision = "d4e5f6071829"
down_revision = "c3d4e5f60718"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE show_relation ADD COLUMN relation_type TEXT")


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35.0; the column is
    # harmless if left in place.  For a real rollback, recreate the table.
    pass
