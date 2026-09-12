"""Episode-first cross-database identity — season subdivision + episode external IDs.

Three schema changes that complete the episode-first hierarchy:

1. **season gains part_number + label**: allows multiple subdivisions within
   one broadcast season (e.g., "Season 3 Part 1" and "Season 3 Part 2"
   sharing season_number=3 but with part_number 1 and 2). The UNIQUE
   constraint widens from (show_id, season_number) to
   (show_id, season_number, part_number). Existing rows get part_number=1.

2. **season_external_id gains name + url, drops service CHECK**: each
   source database can name the same subdivision differently (AniList
   "Mushoku Tensei II" vs MAL "Mushoku Tensei Part 2"). The restrictive
   CHECK (anilist/mal only) is replaced with an open-ended TEXT column,
   matching show_external_id's design. external_id widened to TEXT
   (was INTEGER) for consistency with show_external_id.

3. **episode_external_id table**: per-episode, per-database coordinates.
   The episode is the atomic source of truth — a season mapping
   immediately resolves into per-episode rows. Stores each database's
   own season/episode numbering for that episode (AniList's E2 vs
   TVDB's S02E14 for the same episode).

Revision ID: a8b9c0d1e2f3
Revises: f6a7b8c9d0e1
Create Date: 2026-09-12
"""

from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f6a7b8c9d0e1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------
    # 1. season: add part_number + label, widen UNIQUE constraint
    # ---------------------------------------------------------------
    # SQLite can't ALTER a UNIQUE constraint, so we rebuild the table.
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute("""
        CREATE TABLE season_new (
            id                  TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id             TEXT NOT NULL REFERENCES show (id),
            season_number       INTEGER NOT NULL,
            part_number         INTEGER NOT NULL DEFAULT 1,
            label               TEXT,
            anilist_id          INTEGER,
            mal_id              INTEGER,
            source              TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched')),
            matched             INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override     INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            last_reconciled_at  TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            score               REAL,
            started_at          TEXT,
            completed_at        TEXT,
            abs_start           INTEGER,
            abs_end             INTEGER,
            status              TEXT CHECK (status IS NULL OR status IN ('watching','completed','planned','paused','dropped','skipped')),
            UNIQUE (show_id, season_number, part_number)
        )
    """)
    op.execute("""
        INSERT INTO season_new
            (id, show_id, season_number, part_number, label,
             anilist_id, mal_id, source, matched, manual_override,
             last_reconciled_at, created_at, updated_at, score,
             started_at, completed_at, abs_start, abs_end, status)
        SELECT  id, show_id, season_number, 1, NULL,
                anilist_id, mal_id, source, matched, manual_override,
                last_reconciled_at, created_at, updated_at, score,
                started_at, completed_at, abs_start, abs_end, status
        FROM    season
    """)
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_new RENAME TO season")

    # Indexes for season
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")

    # ---------------------------------------------------------------
    # 2. season_external_id: add name + url, widen service + external_id
    # ---------------------------------------------------------------
    op.execute("""
        CREATE TABLE season_external_id_new (
            season_id   TEXT NOT NULL REFERENCES season (id),
            service     TEXT NOT NULL,
            external_id TEXT NOT NULL,
            name        TEXT,
            url         TEXT,
            created_at  TEXT NOT NULL,
            UNIQUE (season_id, service)
        )
    """)
    op.execute("""
        INSERT INTO season_external_id_new (season_id, service, external_id, name, url, created_at)
        SELECT season_id, service, CAST(external_id AS TEXT), NULL, NULL, created_at
        FROM season_external_id
    """)
    op.execute("DROP TABLE season_external_id")
    op.execute("ALTER TABLE season_external_id_new RENAME TO season_external_id")

    # Indexes for season_external_id
    op.execute("CREATE INDEX ix_season_external_id_season ON season_external_id (season_id)")
    op.execute("CREATE INDEX ix_season_external_id_lookup ON season_external_id (service, external_id)")

    # ---------------------------------------------------------------
    # 3. episode_external_id: per-episode cross-database identity
    # ---------------------------------------------------------------
    op.execute("""
        CREATE TABLE episode_external_id (
            episode_id      TEXT NOT NULL REFERENCES episode (id),
            service         TEXT NOT NULL,
            external_id     TEXT NOT NULL,
            season_number   INTEGER,
            episode_number  INTEGER,
            created_at      TEXT NOT NULL,
            PRIMARY KEY (episode_id, service)
        )
    """)
    op.execute("CREATE INDEX ix_episode_external_id_lookup ON episode_external_id (service, external_id)")

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")

    # 3. Drop episode_external_id
    op.execute("DROP INDEX IF EXISTS ix_episode_external_id_lookup")
    op.execute("DROP TABLE episode_external_id")

    # 2. Restore season_external_id with CHECK constraint
    op.execute("""
        CREATE TABLE season_external_id_old (
            season_id   TEXT NOT NULL REFERENCES season (id),
            service     TEXT NOT NULL CHECK (service IN ('anilist', 'mal')),
            external_id INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (season_id, service)
        )
    """)
    op.execute("""
        INSERT INTO season_external_id_old (season_id, service, external_id, created_at)
        SELECT season_id, service, CAST(external_id AS INTEGER), created_at
        FROM season_external_id
        WHERE service IN ('anilist', 'mal')
    """)
    op.execute("DROP TABLE season_external_id")
    op.execute("ALTER TABLE season_external_id_old RENAME TO season_external_id")
    op.execute("CREATE INDEX ix_season_external_id_season ON season_external_id (season_id)")
    op.execute("CREATE INDEX ix_season_external_id_lookup ON season_external_id (service, external_id)")

    # 1. Restore season without part_number/label
    op.execute("""
        CREATE TABLE season_old (
            id                  TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id             TEXT NOT NULL REFERENCES show (id),
            season_number       INTEGER NOT NULL,
            anilist_id          INTEGER,
            mal_id              INTEGER,
            source              TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched')),
            matched             INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override     INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            last_reconciled_at  TEXT,
            created_at          TEXT NOT NULL,
            updated_at          TEXT NOT NULL,
            score               REAL,
            started_at          TEXT,
            completed_at        TEXT,
            abs_start           INTEGER,
            abs_end             INTEGER,
            status              TEXT CHECK (status IS NULL OR status IN ('watching','completed','planned','paused','dropped','skipped')),
            UNIQUE (show_id, season_number)
        )
    """)
    op.execute("""
        INSERT INTO season_old
            (id, show_id, season_number, anilist_id, mal_id,
             source, matched, manual_override, last_reconciled_at,
             created_at, updated_at, score, started_at, completed_at,
             abs_start, abs_end, status)
        SELECT  id, show_id, season_number, anilist_id, mal_id,
                source, matched, manual_override, last_reconciled_at,
                created_at, updated_at, score, started_at, completed_at,
                abs_start, abs_end, status
        FROM    season
    """)
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_old RENAME TO season")
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")

    op.execute("PRAGMA foreign_keys = ON")
