"""episode.tvdb_absolute — TVDB's absolute number as a mapping only

2026-09-28, PLAN-CODE phase 3.1 (RULEBOOK R1.2a, R1.2c). LCARS keeps its own
absolute numbering (Memory Alpha sets it); TVDB's absolute number — what
Sonarr reports as `absoluteEpisodeNumber` — is kept only as a mapping, beside
the TVDB season/episode already captured in `sonarr_season/sonarr_episode`.
Sonarr lookups key on these, never on `episode.absolute_number`.

Left empty: the next Sonarr read fills it (a stored `absolute_number` can't be
told apart from a Sonarr-reported one).

Revision ID: a5b6c7d8e9f0
Revises: f4a5b6c7d8e9
Create Date: 2026-09-28 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a5b6c7d8e9f0"
down_revision: str | Sequence[str] | None = "f4a5b6c7d8e9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE episode ADD COLUMN tvdb_absolute INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE episode DROP COLUMN tvdb_absolute")
