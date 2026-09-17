"""season_source_add_auto

Add 'auto' to the season.source CHECK constraint for W3 auto-attached
sequel seasons.

Revision ID: 1b2164585c12
Revises: b9c0d1e2f3a4
Create Date: 2026-09-17 09:00:51.472161

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1b2164585c12'
down_revision: str | Sequence[str] | None = 'b9c0d1e2f3a4'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")
    op.execute(
        """
        CREATE TABLE season_new (
            id                  TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id             TEXT NOT NULL REFERENCES show (id),
            season_number       INTEGER NOT NULL,
            part_number         INTEGER NOT NULL DEFAULT 1,
            label               TEXT,
            anilist_id          INTEGER,
            mal_id              INTEGER,
            source              TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched', 'auto')),
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
        """
    )
    op.execute(
        """
        INSERT INTO season_new
        SELECT id, show_id, season_number, part_number, label,
               anilist_id, mal_id, source, matched, manual_override,
               last_reconciled_at, created_at, updated_at, score,
               started_at, completed_at, abs_start, abs_end, status
        FROM season
        """
    )
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_new RENAME TO season")
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")
    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")
    op.execute(
        """
        CREATE TABLE season_old (
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
        """
    )
    op.execute(
        "INSERT INTO season_old SELECT * FROM season WHERE source != 'auto'"
    )
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_old RENAME TO season")
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")
    op.execute("PRAGMA foreign_keys = ON")
