"""Memory Alpha: TVmaze episode table + expanded air_date_source CHECK (2026-09-07).

Creates:
  tvmaze_episode — per-episode data from TVmaze API (titles, airdates,
  runtimes), keyed by TVmaze show ID + season + episode.

Also expands the air_date_source CHECK constraint on the episode table
to accept 'tvmaze' and 'anidb' as valid sources. SQLite requires full
table recreation to alter a CHECK constraint — raw DDL, not batch mode,
because the generated column (available_locally) breaks alembic's
batch INSERT ... SELECT.
"""

import sqlalchemy as sa
from alembic import op

revision = "e5f6a7b8c9d0"
down_revision = "d4e5f6a7b8c9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- tvmaze_episode table --
    op.create_table(
        "tvmaze_episode",
        sa.Column("tvmaze_show_id", sa.Integer, nullable=False),
        sa.Column("season", sa.Integer, nullable=False),
        sa.Column("episode", sa.Integer, nullable=False),
        sa.Column("title", sa.Text),
        sa.Column("airdate", sa.Text),   # YYYY-MM-DD (local broadcast)
        sa.Column("airstamp", sa.Text),  # ISO 8601 UTC (e.g. 2013-06-25T02:00:00Z)
        sa.Column("airtime", sa.Text),   # HH:MM
        sa.Column("runtime_minutes", sa.Integer),
        sa.Column("fetched_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("tvmaze_show_id", "season", "episode"),
    )
    op.create_index(
        "ix_tvmaze_episode_show_id",
        "tvmaze_episode",
        ["tvmaze_show_id"],
    )

    # -- Expand air_date_source CHECK on episode table --
    # Raw DDL: create new table, copy data (excluding generated column),
    # drop old, rename. Can't use batch_alter_table because it tries to
    # INSERT into the generated column available_locally.

    # Disable FK checks during table swap
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute("""
        CREATE TABLE episode_new (
            id                    TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'e-'),
            show_id               TEXT NOT NULL REFERENCES show (id),
            season                INTEGER NOT NULL,
            episode               INTEGER NOT NULL,
            kind                  TEXT NOT NULL CHECK (kind IN ('regular', 'special', 'ova', 'bonus_movie')),
            absolute_number       REAL,
            air_date_utc          TEXT,
            air_date_source       TEXT CHECK (air_date_source IN ('sonarr', 'anilist', 'animeschedule', 'manual', 'tvmaze', 'anidb')),
            air_date_raw_sonarr   TEXT,
            available_checked_at  TEXT,
            runtime_minutes       INTEGER,
            state                 TEXT NOT NULL DEFAULT 'unwatched' CHECK (state IN ('unwatched', 'watched', 'skipped')),
            created_at            TEXT NOT NULL,
            updated_at            TEXT NOT NULL,
            season_id             TEXT REFERENCES season (id),
            available_via_sonarr  TEXT NOT NULL DEFAULT 'unavailable' CHECK (available_via_sonarr IN ('unavailable', 'downloading', 'available')),
            available_via_radarr  TEXT NOT NULL DEFAULT 'unavailable' CHECK (available_via_radarr IN ('unavailable', 'downloading', 'available')),
            file_path_sonarr      TEXT,
            file_path_radarr      TEXT,
            available_locally     INTEGER GENERATED ALWAYS AS (
                CASE WHEN available_via_sonarr = 'available' OR available_via_radarr = 'available'
                     THEN 1 ELSE 0 END
            ) STORED,
            title                 TEXT,
            sonarr_season         INTEGER,
            sonarr_episode        INTEGER,
            synopsis              TEXT,
            UNIQUE (show_id, season, episode)
        )
    """)

    # Copy all data — exclude the generated column
    op.execute("""
        INSERT INTO episode_new (
            id, show_id, season, episode, kind, absolute_number,
            air_date_utc, air_date_source, air_date_raw_sonarr,
            available_checked_at, runtime_minutes, state,
            created_at, updated_at, season_id,
            available_via_sonarr, available_via_radarr,
            file_path_sonarr, file_path_radarr,
            title, sonarr_season, sonarr_episode, synopsis
        )
        SELECT
            id, show_id, season, episode, kind, absolute_number,
            air_date_utc, air_date_source, air_date_raw_sonarr,
            available_checked_at, runtime_minutes, state,
            created_at, updated_at, season_id,
            available_via_sonarr, available_via_radarr,
            file_path_sonarr, file_path_radarr,
            title, sonarr_season, sonarr_episode, synopsis
        FROM episode
    """)

    op.execute("DROP TABLE episode")
    op.execute("ALTER TABLE episode_new RENAME TO episode")

    # Recreate indexes
    op.execute("CREATE INDEX ix_episode_show_id ON episode (show_id)")
    op.execute("CREATE INDEX ix_episode_air_date_utc ON episode (air_date_utc)")
    op.execute("CREATE INDEX ix_episode_state ON episode (state)")
    op.execute("CREATE INDEX ix_episode_sonarr_numbering ON episode (show_id, sonarr_season, sonarr_episode)")

    # Re-enable FK checks
    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.drop_table("tvmaze_episode")
    # CHECK constraint rollback would require another table recreation;
    # not worth it for a dev migration.
