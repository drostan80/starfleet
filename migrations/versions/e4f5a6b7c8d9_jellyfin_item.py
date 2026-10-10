"""Which Jellyfin item is which episode / show, for the Jellyfin links in the UI

2026-10-10 (user): a link to Jellyfin next to the link to mpv. The Jellyfin sync already matches
every episode, show and movie it can; it now remembers the Jellyfin item id of each (`kind` 'show' =
the series, or the movie; 'episode'), so the UI can open the right page. New table only.

Revision ID: e4f5a6b7c8d9
Revises: d3e4f5a6b7c8
Create Date: 2026-10-10 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e4f5a6b7c8d9"
down_revision: str | Sequence[str] | None = "d3e4f5a6b7c8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE jellyfin_item (
            kind TEXT NOT NULL CHECK (kind IN ('show', 'episode')),
            entity_id TEXT NOT NULL,
            jellyfin_item_id TEXT NOT NULL,
            seen_at TEXT NOT NULL,
            PRIMARY KEY (kind, entity_id)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE jellyfin_item")
