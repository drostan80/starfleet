"""Add 'skipped' to show.status and season.status CHECK constraints (2026-09-04).

SKIP is a lightweight tombstone meaning "seen, not interested" — used by
the browse UI to suppress titles the user has already dismissed.  Unlike
every other status value, it carries no AniList/MAL equivalent and is
never pushed to external services.  Skipped shows have tracked = 0.

SQLite can't ALTER a CHECK constraint, so both tables are recreated using
the same drop-new/insert-select/drop-old/rename pattern established in
28cbe21e9b6e.  The `show` table has a GENERATED column (`available_locally`)
which must be omitted from INSERT … SELECT (SQLite regenerates it) and a
multi-line trailing CHECK that must be preserved verbatim.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3b7c9e1f042"
down_revision: str | None = "d160fe2b6843"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")

    # ── show table ─────────────────────────────────────────
    op.execute(
        """
        CREATE TABLE show_new (
            id                TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 's-'),
            media_shape       TEXT NOT NULL CHECK (media_shape IN ('episodic', 'movie')),
            tracking_space    TEXT NOT NULL CHECK (tracking_space IN ('tv', 'anime')),
            title_romaji      TEXT,
            title_english     TEXT,
            title_native      TEXT,
            primary_title     TEXT NOT NULL CHECK (primary_title IN ('romaji', 'english', 'native')),
            status            TEXT NOT NULL CHECK (status IN ('watching', 'planned', 'paused', 'completed', 'dropped', 'skipped')),
            tracked           INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1)),
            score             REAL,
            total_episodes    INTEGER,
            duration_minutes  INTEGER,
            poster_url        TEXT,
            banner_url        TEXT,
            genres_raw        TEXT,
            synopsis          TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            available_checked_at TEXT,
            hard_delete_requested_at TEXT,
            paced_cadence_days INTEGER CHECK (paced_cadence_days IS NULL OR paced_cadence_days > 0),
            metadata_last_refreshed_at TEXT,
            available_via_radarr TEXT NOT NULL DEFAULT 'unavailable' CHECK (available_via_radarr IN ('unavailable', 'downloading', 'available')),
            file_path_radarr TEXT,
            available_locally INTEGER GENERATED ALWAYS AS (
                CASE WHEN available_via_radarr = 'available' THEN 1 ELSE 0 END
            ) STORED,
            display_title_override TEXT,
            CHECK (
                (primary_title = 'romaji' AND title_romaji IS NOT NULL) OR
                (primary_title = 'english' AND title_english IS NOT NULL) OR
                (primary_title = 'native' AND title_native IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO show_new
            (id, media_shape, tracking_space,
             title_romaji, title_english, title_native, primary_title,
             status, tracked, score, total_episodes, duration_minutes,
             poster_url, banner_url, genres_raw, synopsis,
             created_at, updated_at, available_checked_at,
             hard_delete_requested_at, paced_cadence_days,
             metadata_last_refreshed_at,
             available_via_radarr, file_path_radarr,
             display_title_override)
        SELECT  id, media_shape, tracking_space,
                title_romaji, title_english, title_native, primary_title,
                status, tracked, score, total_episodes, duration_minutes,
                poster_url, banner_url, genres_raw, synopsis,
                created_at, updated_at, available_checked_at,
                hard_delete_requested_at, paced_cadence_days,
                metadata_last_refreshed_at,
                available_via_radarr, file_path_radarr,
                display_title_override
        FROM    show
        """
    )
    op.execute("DROP TABLE show")
    op.execute("ALTER TABLE show_new RENAME TO show")

    # Re-create indexes that DROP TABLE destroyed
    op.execute("CREATE INDEX ix_show_status ON show (status)")
    op.execute("CREATE INDEX ix_show_tracking_space ON show (tracking_space)")

    # ── season table ───────────────────────────────────────
    op.execute(
        """
        CREATE TABLE season_new (
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
        """
    )
    op.execute(
        """
        INSERT INTO season_new
            (id, show_id, season_number, anilist_id, mal_id,
             source, matched, manual_override, last_reconciled_at,
             created_at, updated_at, score, started_at, completed_at,
             abs_start, abs_end, status)
        SELECT  id, show_id, season_number, anilist_id, mal_id,
                source, matched, manual_override, last_reconciled_at,
                created_at, updated_at, score, started_at, completed_at,
                abs_start, abs_end, status
        FROM    season
        """
    )
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_new RENAME TO season")

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    # Downgrade: recreate both tables without 'skipped' in the CHECK.
    # Any rows currently set to 'skipped' are changed to 'dropped' first.
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute("UPDATE show SET status = 'dropped' WHERE status = 'skipped'")
    op.execute("UPDATE season SET status = 'dropped' WHERE status = 'skipped'")

    op.execute(
        """
        CREATE TABLE show_old (
            id                TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 's-'),
            media_shape       TEXT NOT NULL CHECK (media_shape IN ('episodic', 'movie')),
            tracking_space    TEXT NOT NULL CHECK (tracking_space IN ('tv', 'anime')),
            title_romaji      TEXT,
            title_english     TEXT,
            title_native      TEXT,
            primary_title     TEXT NOT NULL CHECK (primary_title IN ('romaji', 'english', 'native')),
            status            TEXT NOT NULL CHECK (status IN ('watching', 'planned', 'paused', 'completed', 'dropped')),
            tracked           INTEGER NOT NULL DEFAULT 1 CHECK (tracked IN (0, 1)),
            score             REAL,
            total_episodes    INTEGER,
            duration_minutes  INTEGER,
            poster_url        TEXT,
            banner_url        TEXT,
            genres_raw        TEXT,
            synopsis          TEXT,
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL,
            available_checked_at TEXT,
            hard_delete_requested_at TEXT,
            paced_cadence_days INTEGER CHECK (paced_cadence_days IS NULL OR paced_cadence_days > 0),
            metadata_last_refreshed_at TEXT,
            available_via_radarr TEXT NOT NULL DEFAULT 'unavailable' CHECK (available_via_radarr IN ('unavailable', 'downloading', 'available')),
            file_path_radarr TEXT,
            available_locally INTEGER GENERATED ALWAYS AS (
                CASE WHEN available_via_radarr = 'available' THEN 1 ELSE 0 END
            ) STORED,
            display_title_override TEXT,
            CHECK (
                (primary_title = 'romaji' AND title_romaji IS NOT NULL) OR
                (primary_title = 'english' AND title_english IS NOT NULL) OR
                (primary_title = 'native' AND title_native IS NOT NULL)
            )
        )
        """
    )
    op.execute(
        """
        INSERT INTO show_old
            (id, media_shape, tracking_space,
             title_romaji, title_english, title_native, primary_title,
             status, tracked, score, total_episodes, duration_minutes,
             poster_url, banner_url, genres_raw, synopsis,
             created_at, updated_at, available_checked_at,
             hard_delete_requested_at, paced_cadence_days,
             metadata_last_refreshed_at,
             available_via_radarr, file_path_radarr,
             display_title_override)
        SELECT  id, media_shape, tracking_space,
                title_romaji, title_english, title_native, primary_title,
                status, tracked, score, total_episodes, duration_minutes,
                poster_url, banner_url, genres_raw, synopsis,
                created_at, updated_at, available_checked_at,
                hard_delete_requested_at, paced_cadence_days,
                metadata_last_refreshed_at,
                available_via_radarr, file_path_radarr,
                display_title_override
        FROM    show
        """
    )
    op.execute("DROP TABLE show")
    op.execute("ALTER TABLE show_old RENAME TO show")

    op.execute("CREATE INDEX ix_show_status ON show (status)")
    op.execute("CREATE INDEX ix_show_tracking_space ON show (tracking_space)")

    op.execute(
        """
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
            status              TEXT CHECK (status IS NULL OR status IN ('watching','completed','planned','paused','dropped')),
            UNIQUE (show_id, season_number)
        )
        """
    )
    op.execute(
        """
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
        """
    )
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_old RENAME TO season")

    op.execute("PRAGMA foreign_keys = ON")
