"""air_date_source 'tvdb' — episode lists read straight from TVDB (R1.2e)

2026-09-29. Episodes of shows that Sonarr doesn't hold are read from TVDB
directly (rebuild stage 4); their date-only air dates need an honest source
label. Widens the CHECK on episode.air_date_source and on air_date_change's
two source columns. SQLite table rebuild (can't ALTER a CHECK); same raw-DDL
pattern as the syoboi migration because of the generated column.

Revision ID: b2c3d4e5f6a8
Revises: a1b2c3d4e5f7
Create Date: 2026-09-29 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b2c3d4e5f6a8"
down_revision: str | Sequence[str] | None = "a1b2c3d4e5f7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "('sonarr', 'anilist', 'animeschedule', 'manual', 'tvmaze', 'anidb', 'syoboi')"
_NEW = "('sonarr', 'anilist', 'animeschedule', 'manual', 'tvmaze', 'anidb', 'syoboi', 'tvdb')"

_EPISODE_COLUMNS = (
    "id, show_id, season, episode, kind, absolute_number, air_date_utc, air_date_source,"
    " air_date_raw_sonarr, available_checked_at, runtime_minutes, state, created_at,"
    " updated_at, season_id, available_via_sonarr, available_via_radarr, file_path_sonarr,"
    " file_path_radarr, title, sonarr_season, sonarr_episode, synopsis, tvdb_absolute"
)


def _rebuild(sources: str) -> None:
    op.execute("PRAGMA foreign_keys = OFF")
    op.execute(f"""
        CREATE TABLE episode_new (
            id                    TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'e-'),
            show_id               TEXT NOT NULL REFERENCES show (id),
            season                INTEGER NOT NULL,
            episode               INTEGER NOT NULL,
            kind                  TEXT NOT NULL CHECK (kind IN ('regular', 'special', 'ova', 'bonus_movie')),
            absolute_number       REAL,
            air_date_utc          TEXT,
            air_date_source       TEXT CHECK (air_date_source IN {sources}),
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
            synopsis              TEXT, tvdb_absolute INTEGER,
            UNIQUE (show_id, season, episode)
        )
    """)
    op.execute(f"INSERT INTO episode_new ({_EPISODE_COLUMNS}) SELECT {_EPISODE_COLUMNS} FROM episode")
    op.execute("DROP TABLE episode")
    op.execute("ALTER TABLE episode_new RENAME TO episode")
    op.execute("CREATE INDEX ix_episode_show_id ON episode (show_id)")
    op.execute("CREATE INDEX ix_episode_air_date_utc ON episode (air_date_utc)")
    op.execute("CREATE INDEX ix_episode_state ON episode (state)")
    op.execute(
        "CREATE INDEX ix_episode_sonarr_numbering ON episode (show_id, sonarr_season, sonarr_episode)"
    )

    op.execute(f"""
        CREATE TABLE air_date_change_new (
            id                     TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'g-'),
            episode_id             TEXT NOT NULL REFERENCES episode (id),
            previous_air_date_utc  TEXT,
            new_air_date_utc       TEXT NOT NULL,
            previous_source        TEXT CHECK (previous_source IN {sources}),
            new_source             TEXT NOT NULL CHECK (new_source IN {sources}),
            changed_at             TEXT NOT NULL,
            changed_by             TEXT NOT NULL
        )
    """)
    op.execute("INSERT INTO air_date_change_new SELECT * FROM air_date_change")
    op.execute("DROP TABLE air_date_change")
    op.execute("ALTER TABLE air_date_change_new RENAME TO air_date_change")
    op.execute("CREATE INDEX ix_air_date_change_episode_id ON air_date_change (episode_id)")
    op.execute("PRAGMA foreign_keys = ON")


def upgrade() -> None:
    _rebuild(_NEW)


def downgrade() -> None:
    op.execute("UPDATE episode SET air_date_source = NULL WHERE air_date_source = 'tvdb'")
    op.execute("DELETE FROM air_date_change WHERE new_source = 'tvdb' OR previous_source = 'tvdb'")
    _rebuild(_OLD)
