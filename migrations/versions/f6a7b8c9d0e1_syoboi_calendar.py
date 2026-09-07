"""Memory Alpha: Syoboi Calendar tables + expanded air_date_source (2026-09-07).

Creates:
  syoboi_program — per-episode broadcast events from Syoboi Calendar
  syoboi_title — title metadata

Also expands the air_date_source CHECK constraint on the episode table
to accept 'syoboi' as a valid source.  Same raw-DDL pattern as the
TVmaze migration (e5f6a7b8c9d0) because of the generated column.
"""

import sqlalchemy as sa
from alembic import op

revision = "f6a7b8c9d0e1"
down_revision = "e5f6a7b8c9d0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- syoboi_program table (broadcast events) --
    op.create_table(
        "syoboi_program",
        sa.Column("pid", sa.Integer, primary_key=True),
        sa.Column("tid", sa.Integer, nullable=False),
        sa.Column("chid", sa.Integer, nullable=False),
        sa.Column("count", sa.Integer),  # episode number (NULL = recap/special)
        sa.Column("st_time_jst", sa.Text),  # original JST: "2023-10-06 22:30:00"
        sa.Column("ed_time_jst", sa.Text),
        sa.Column("st_time_utc", sa.Text),  # derived UTC: "2023-10-06T13:30:00Z"
        sa.Column("ed_time_utc", sa.Text),
        sa.Column("subtitle", sa.Text),     # per-episode Japanese title
        sa.Column("flag", sa.Integer, server_default="0"),
        sa.Column("deleted", sa.Integer, server_default="0"),
        sa.Column("revision", sa.Text),
        sa.Column("last_update", sa.Text),
        sa.Column("fetched_at", sa.Text, nullable=False),
    )
    op.create_index(
        "ix_syoboi_program_tid_count",
        "syoboi_program",
        ["tid", "count"],
    )

    # -- syoboi_title table (title metadata) --
    op.create_table(
        "syoboi_title",
        sa.Column("tid", sa.Integer, primary_key=True),
        sa.Column("title", sa.Text),
        sa.Column("short_title", sa.Text),
        sa.Column("category", sa.Integer),
        sa.Column("first_ch", sa.Integer),
        sa.Column("first_ch_name", sa.Text),
        sa.Column("fetched_at", sa.Text, nullable=False),
    )

    # -- Expand air_date_source CHECK on episode table --
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
            air_date_source       TEXT CHECK (air_date_source IN ('sonarr', 'anilist', 'animeschedule', 'manual', 'tvmaze', 'anidb', 'syoboi')),
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

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.drop_table("syoboi_title")
    op.drop_table("syoboi_program")
