"""season status column — per-season status tracking

Revision ID: b2c3d4e5f607
Revises: a1b2c3d4e5f6
Create Date: 2026-09-02

Adds a nullable status column to the season table, backfilled from the
parent show's current status.  show.status remains the canonical
show-level value for now — the derived/rollup logic comes in a later
migration step (2.1c).
"""

from alembic import op

# revision identifiers
revision = "b2c3d4e5f607"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE season ADD COLUMN status TEXT"
        " CHECK (status IS NULL OR status IN"
        " ('watching','completed','planned','paused','dropped'))"
    )
    # Backfill: each season inherits its show's current status.
    op.execute(
        "UPDATE season SET status = ("
        "  SELECT show.status FROM show WHERE show.id = season.show_id"
        ")"
    )


def downgrade() -> None:
    # SQLite < 3.35 doesn't support DROP COLUMN; accept the no-op.
    pass
