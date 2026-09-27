"""season_status_change — history of season status changes

2026-09-27, PLAN-CODE 0.2. `setSeasonStatus` and every other season status
write left no trace, so after the 09-06 → 09-26 confusion the user's own
season-level changes could not be told apart from automation's. Same shape
as `status_change` (show level), one row per actual change, with who made it.

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-09-27 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: str | Sequence[str] | None = "c1d2e3f4a5b6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE season_status_change (
            id               TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'j-'),
            season_id        TEXT NOT NULL REFERENCES season (id),
            show_id          TEXT NOT NULL REFERENCES show (id),
            previous_status  TEXT,
            new_status       TEXT,
            changed_at       TEXT NOT NULL,
            changed_by       TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_season_status_change_season ON season_status_change (season_id)")


def downgrade() -> None:
    op.execute("DROP TABLE season_status_change")
