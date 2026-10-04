"""air-date candidates and per-season schedule choice

2026-10-04. Every source's schedule for an episode is kept as a *candidate* (source, channel,
date), so the user can see them all and pick which one a season follows (a posteriori choice,
user 2026-10-04). A season with a row in `season_air_choice` takes its dates from the chosen
source/channel only; without a row the existing rule stays (a different source wins only by an
earlier date). `syoboi_channel` holds Syoboi's station names so a station reads as "AT-X", not
"channel 20". Additive: three new tables, nothing existing is altered.

Revision ID: e5f6a7b8c9d1
Revises: d4e5f6a7b8c0
Create Date: 2026-10-04 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e5f6a7b8c9d1"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE episode_air_candidate (
            episode_id    TEXT NOT NULL REFERENCES episode (id),
            source        TEXT NOT NULL,
            channel       TEXT NOT NULL DEFAULT '',
            air_date_utc  TEXT NOT NULL,
            fetched_at    TEXT NOT NULL,
            PRIMARY KEY (episode_id, source, channel)
        )
    """)
    op.execute("CREATE INDEX ix_episode_air_candidate_episode ON episode_air_candidate (episode_id)")
    op.execute("""
        CREATE TABLE season_air_choice (
            season_id  TEXT PRIMARY KEY REFERENCES season (id),
            source     TEXT NOT NULL,
            channel    TEXT NOT NULL DEFAULT '',
            chosen_at  TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE syoboi_channel (
            chid        INTEGER PRIMARY KEY,
            name        TEXT NOT NULL,
            fetched_at  TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE syoboi_channel")
    op.execute("DROP TABLE season_air_choice")
    op.execute("DROP INDEX ix_episode_air_candidate_episode")
    op.execute("DROP TABLE episode_air_candidate")
