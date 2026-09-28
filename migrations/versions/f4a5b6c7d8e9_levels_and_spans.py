"""levels and spans — season_span, season.kind/parent_id, decimal season number

2026-09-28, PLAN-CODE phase 2 (RULEBOOK R1.9a, R1.10–R1.13, R2.18, R3.6c).
Additive: the old columns stay and existing code keeps working; each reader
moves to the new structure in the phase that rewrites it, and the old
columns go in one step before cutover.

- `season_span(season_id, abs_from, abs_to)`: a season is one or more spans
  of absolute numbers (R1.11, R1.12). Filled with one span per season from
  `abs_start/abs_end` where both are set.
- `season.kind` (`tvdb_season` | `part` | `special`) and `parent_id`: a part
  (AniList/MAL cour) sits under its TVDB season (R1.10, R2.18); a special run
  with its own id is its own level (R1.13). `parent_id` is containment only:
  a side piece between S2 and S3 has no parent, its place is its decimal
  season number (2.5).
- `season.decimal_season_number`: the season number as shown and sorted —
  TVDB's number for TVDB seasons and their parts, N.5 / N.1, N.2… for side
  pieces between TVDB seasons (R1.9a). `season_number` stays TVDB's whole
  number and is NULL for side pieces.
- `season.show_id` nullable: an individual season (kind `individual_season`,
  R3.6d) has no show yet (R3.6c); it becomes a `tvdb_season` when it joins one.
- The unique key gains `kind`, so a TVDB season and its part 1 can share
  (show, season number, part number); old code only makes TVDB seasons.
- `show.status_before_pause` dropped (PLAN-CODE 2.6, user 2026-09-28).

Transition triggers keep the new columns filled while old code still writes
`abs_start/abs_end` and `season_number`; they go with those columns.

Mechanical only: every existing row becomes a `tvdb_season` (the data has no
part rows — the migration stops if it finds any rather than guess a parent).

Revision ID: f4a5b6c7d8e9
Revises: e3f4a5b6c7d8
Create Date: 2026-09-28 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "f4a5b6c7d8e9"
down_revision: str | Sequence[str] | None = "e3f4a5b6c7d8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD_COLUMNS = (
    "id, show_id, season_number, part_number, label, anilist_id, mal_id,"
    " source, matched, manual_override, last_reconciled_at, created_at, updated_at,"
    " score, started_at, completed_at, abs_start, abs_end, status, list_sync"
)

_SEASON_COMMON = """
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
            list_sync           INTEGER NOT NULL DEFAULT 1 CHECK (list_sync IN (0, 1)),"""

_TRIGGERS = (
    # Transition: old code writes abs_start/abs_end; the span follows.
    """
    CREATE TRIGGER season_span_from_abs_insert AFTER INSERT ON season
    WHEN NEW.abs_start IS NOT NULL AND NEW.abs_end IS NOT NULL
    BEGIN
        INSERT INTO season_span (season_id, abs_from, abs_to)
        VALUES (NEW.id, NEW.abs_start, NEW.abs_end);
    END
    """,
    """
    CREATE TRIGGER season_span_from_abs_update AFTER UPDATE OF abs_start, abs_end ON season
    BEGIN
        DELETE FROM season_span WHERE season_id = NEW.id;
        INSERT INTO season_span (season_id, abs_from, abs_to)
        SELECT NEW.id, NEW.abs_start, NEW.abs_end
        WHERE NEW.abs_start IS NOT NULL AND NEW.abs_end IS NOT NULL;
    END
    """,
    # Transition: old code writes season_number only; the decimal one follows
    # (side pieces set their own).
    """
    CREATE TRIGGER season_decimal_number_insert AFTER INSERT ON season
    WHEN NEW.decimal_season_number IS NULL AND NEW.season_number IS NOT NULL
         AND NEW.kind <> 'special'
    BEGIN
        UPDATE season SET decimal_season_number = NEW.season_number WHERE id = NEW.id;
    END
    """,
    """
    CREATE TRIGGER season_decimal_number_update AFTER UPDATE OF season_number ON season
    WHEN NEW.season_number IS NOT NULL AND NEW.kind <> 'special'
    BEGIN
        UPDATE season SET decimal_season_number = NEW.season_number WHERE id = NEW.id;
    END
    """,
)

_TRIGGER_NAMES = (
    "season_span_from_abs_insert",
    "season_span_from_abs_update",
    "season_decimal_number_insert",
    "season_decimal_number_update",
)


def upgrade() -> None:
    conn = op.get_bind()
    parts = conn.execute(text("SELECT COUNT(*) FROM season WHERE part_number <> 1")).scalar()
    if parts:
        raise RuntimeError(
            f"{parts} season rows have part_number <> 1: parts need a TVDB-season"
            " parent, which this migration does not invent — decide them first"
        )

    op.execute("PRAGMA foreign_keys = OFF")
    op.execute(
        f"""
        CREATE TABLE season_new (
            id                  TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id             TEXT REFERENCES show (id),
            season_number       INTEGER,
            part_number         INTEGER NOT NULL DEFAULT 1,{_SEASON_COMMON}
            kind                TEXT NOT NULL DEFAULT 'tvdb_season'
                                CHECK (kind IN ('tvdb_season', 'part', 'special', 'individual_season')),
            parent_id           TEXT REFERENCES season (id),
            decimal_season_number REAL,
            CHECK (kind <> 'part' OR parent_id IS NOT NULL),
            CHECK (kind <> 'tvdb_season'
                   OR (parent_id IS NULL AND show_id IS NOT NULL AND season_number IS NOT NULL)),
            CHECK (kind <> 'individual_season' OR (parent_id IS NULL AND show_id IS NULL)),
            UNIQUE (show_id, season_number, part_number, kind)
        )
        """
    )
    op.execute(
        f"INSERT INTO season_new ({_OLD_COLUMNS}, kind, decimal_season_number)"
        f" SELECT {_OLD_COLUMNS}, 'tvdb_season', season_number FROM season"
    )
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_new RENAME TO season")
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")
    op.execute("CREATE INDEX ix_season_parent_id ON season (parent_id)")

    op.execute(
        """
        CREATE TABLE season_span (
            season_id  TEXT NOT NULL REFERENCES season (id) ON DELETE CASCADE,
            abs_from   REAL NOT NULL,
            abs_to     REAL NOT NULL,
            CHECK (abs_to >= abs_from),
            UNIQUE (season_id, abs_from)
        )
        """
    )
    op.execute(
        "INSERT INTO season_span (season_id, abs_from, abs_to)"
        " SELECT id, abs_start, abs_end FROM season"
        " WHERE abs_start IS NOT NULL AND abs_end IS NOT NULL"
    )
    for trigger in _TRIGGERS:
        op.execute(trigger)

    op.execute("ALTER TABLE show DROP COLUMN status_before_pause")
    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    conn = op.get_bind()
    blocked = conn.execute(
        text(
            "SELECT COUNT(*) FROM season WHERE kind <> 'tvdb_season'"
            " OR show_id IS NULL OR season_number IS NULL"
        )
    ).scalar()
    if blocked:
        raise RuntimeError(f"{blocked} season rows don't fit the old layout (parts, side pieces, individual seasons)")

    op.execute("PRAGMA foreign_keys = OFF")
    op.execute("ALTER TABLE show ADD COLUMN status_before_pause TEXT")
    for name in _TRIGGER_NAMES:
        op.execute(f"DROP TRIGGER {name}")
    op.execute("DROP TABLE season_span")
    op.execute(
        f"""
        CREATE TABLE season_old (
            id                  TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'z-'),
            show_id             TEXT NOT NULL REFERENCES show (id),
            season_number       INTEGER NOT NULL,
            part_number         INTEGER NOT NULL DEFAULT 1,{_SEASON_COMMON}
            UNIQUE (show_id, season_number, part_number)
        )
        """
    )
    op.execute(f"INSERT INTO season_old ({_OLD_COLUMNS}) SELECT {_OLD_COLUMNS} FROM season")
    op.execute("DROP TABLE season")
    op.execute("ALTER TABLE season_old RENAME TO season")
    op.execute("CREATE INDEX ix_season_show_id ON season (show_id)")
    op.execute("PRAGMA foreign_keys = ON")
