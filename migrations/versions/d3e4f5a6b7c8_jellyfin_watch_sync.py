"""What LCARS and Jellyfin last agreed on, per watched episode or movie

2026-10-10 (user). LCARS marks what you watched as played in Jellyfin (`jellyfin_sync.py`). This table
remembers, per episode or movie, the state both sides last agreed on, so that un-watching in LCARS later
un-marks it in Jellyfin (and a play that only exists in Jellyfin is never undone). New table only.

Revision ID: d3e4f5a6b7c8
Revises: c2d3e4f5a6b7
Create Date: 2026-10-10 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d3e4f5a6b7c8"
down_revision: str | Sequence[str] | None = "c2d3e4f5a6b7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jellyfin_watch_sync (
            entity_kind TEXT NOT NULL CHECK (entity_kind IN ('episode', 'movie')),
            entity_id TEXT NOT NULL,
            jellyfin_item_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK (state IN ('played', 'unplayed')),
            synced_at TEXT NOT NULL,
            PRIMARY KEY (entity_kind, entity_id)
        )
        """
    )
    op.execute("CREATE INDEX ix_jellyfin_watch_sync_state ON jellyfin_watch_sync (state)")


def downgrade() -> None:
    op.execute("DROP TABLE jellyfin_watch_sync")
