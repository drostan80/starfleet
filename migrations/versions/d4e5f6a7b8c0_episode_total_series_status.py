"""season.episode_total, show.series_status — a confirmed episode count (RULEBOOK R2.15a)

2026-09-29. A level completes by itself only when its episode count is confirmed:
`season.episode_total` is the total AniList (or MAL, when AniList has none) gives for the
level's entry — NULL while unknown; `show.series_status` is Sonarr's series status
(`continuing` / `ended`), NULL when no source has said.

Revision ID: d4e5f6a7b8c0
Revises: c3d4e5f6a7b9
Create Date: 2026-09-29 00:00:03.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d4e5f6a7b8c0"
down_revision: str | Sequence[str] | None = "c3d4e5f6a7b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE season ADD COLUMN episode_total INTEGER")
    op.execute("ALTER TABLE show ADD COLUMN series_status TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN series_status")
    op.execute("ALTER TABLE season DROP COLUMN episode_total")
