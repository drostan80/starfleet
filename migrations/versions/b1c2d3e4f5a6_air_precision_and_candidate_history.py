"""Air-date precision (time or date), the end-of-day "aired at", and candidate change history

2026-10-07 (user; RULEBOOK R1.0b/R1.0e). A date-only air date (AniDB, TVDB's list, TVmaze with no
air time, a series Sonarr gives no air time) is a calendar day, not an instant: `air_precision =
'date'`, `air_local_date`, and `air_aired_at` (the end of that day, when it counts as aired).
`air_date_utc` keeps what it held. NULL precision = a time. Sonarr's own raw date carries the same
(`air_raw_sonarr_*`), because its time is only real when the series has an air time.

`episode_air_candidate` gets the same, plus `first_air_date_utc` (the value the source first
reported, NULL until the first refresh after this migration seeds it) and `air_candidate_change`
logs each later change of a source's own value.

Revision ID: b1c2d3e4f5a6
Revises: a9b8c7d6e5f4
Create Date: 2026-10-07 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b1c2d3e4f5a6"
down_revision: str | Sequence[str] | None = "a9b8c7d6e5f4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_END_OF_DAY = (
    "CASE WHEN (SELECT tracking_space FROM show WHERE id = episode.show_id) = 'anime'"
    " THEN {d} || 'T15:00:00Z'"
    " ELSE strftime('%Y-%m-%dT%H:%M:%SZ', datetime({d}, '+1 day', '+8 hours')) END"
)


def upgrade() -> None:
    for column in (
        "air_precision", "air_local_date", "air_aired_at",
        "air_raw_sonarr_precision", "air_raw_sonarr_local_date",
    ):
        op.execute(f"ALTER TABLE episode ADD COLUMN {column} TEXT")
    op.execute("ALTER TABLE episode_air_candidate ADD COLUMN precision TEXT NOT NULL DEFAULT 'time'")
    op.execute("ALTER TABLE episode_air_candidate ADD COLUMN local_date TEXT")
    op.execute("ALTER TABLE episode_air_candidate ADD COLUMN first_air_date_utc TEXT")
    op.execute(
        """
        CREATE TABLE air_candidate_change (
            id                     TEXT PRIMARY KEY,
            episode_id             TEXT NOT NULL REFERENCES episode (id),
            source                 TEXT NOT NULL,
            channel                TEXT NOT NULL DEFAULT '',
            previous_air_date_utc  TEXT,
            new_air_date_utc       TEXT,
            seen_at                TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_air_candidate_change_episode ON air_candidate_change (episode_id)")

    # Backfill what is certain from the source alone (a calendar date is all AniDB and TVDB's own
    # list ever gave); Sonarr's and TVmaze's are classified by their next read.
    day = "substr(air_date_utc, 1, 10)"
    op.execute(
        f"""
        UPDATE episode SET air_precision = 'date', air_local_date = {day},
               air_aired_at = {_END_OF_DAY.format(d=day)}
        WHERE air_date_source IN ('tvdb', 'anidb') AND air_date_utc IS NOT NULL
        """
    )
    # TVmaze: with no air time its airstamp is a noon placeholder (or missing: the date fallback)
    op.execute(
        f"""
        UPDATE episode SET air_precision = 'date', air_local_date = te.airdate,
               air_aired_at = {_END_OF_DAY.format(d='te.airdate')}
        FROM show_external_id tm, tvmaze_episode te
        WHERE episode.air_date_source = 'tvmaze' AND episode.air_date_utc IS NOT NULL
          AND tm.show_id = episode.show_id AND tm.service = 'tvmaze'
          AND te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
          AND te.season = episode.sonarr_season AND te.episode = episode.sonarr_episode
          AND (te.airtime IS NULL OR te.airtime = '')
          AND te.airdate IS NOT NULL AND te.airdate != ''
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_air_candidate_change_episode")
    op.execute("DROP TABLE air_candidate_change")
    for column in ("first_air_date_utc", "local_date", "precision"):
        op.execute(f"ALTER TABLE episode_air_candidate DROP COLUMN {column}")
    for column in (
        "air_raw_sonarr_local_date", "air_raw_sonarr_precision",
        "air_aired_at", "air_local_date", "air_precision",
    ):
        op.execute(f"ALTER TABLE episode DROP COLUMN {column}")
