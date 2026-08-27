"""Drop season.parent_season and season.sub_ordinal — dead columns (2026-08-27).

These two columns were added in c7a1e2f4b8d3 (season-ranges slice) to support
a sub-season link design that was later superseded by the finest-source
subdivision model chosen at D1-D3 (2026-08-26). Zero rows have ever had either
column set — confirmed by direct inspection of the production DB — and no code
in lcars, Data, or any other client has ever read or written them.  They are
pure schema weight.

`parent_season` carries a REFERENCES season(id) self-referential FK, and other
tables (episode, season_external_id, …) also FK-reference season(id).  SQLite
3.26.0+ rewrites FK text in dependent tables when the referenced table is
renamed, so the naive rename-original→old approach leaves those FKs pointing
at the dropped `season_old` table.  Instead we:

  1. Create `season_new` with the trimmed schema.
  2. Copy all rows from `season` to `season_new`.
  3. DROP TABLE `season` (with foreign_keys=OFF so the FK check doesn't block).
  4. RENAME `season_new` → `season`.

Step 4 rewrites no FK text in other tables (nothing references `season_new`),
so dependent tables end up with their original `REFERENCES season(id)` intact
and pointing at the freshly renamed table.

The downgrade re-adds both columns as nullable via simple ALTER TABLE ADD
COLUMN (correct: no constraint on sub_ordinal; parent_season's self-referential
FK is re-added the same way c7a1e2f4b8d3 originally added it).
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "28cbe21e9b6e"
down_revision: str | None = "c7a1e2f4b8d3"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")
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
             abs_start, abs_end)
        SELECT  id, show_id, season_number, anilist_id, mal_id,
                source, matched, manual_override, last_reconciled_at,
                created_at, updated_at, score, started_at, completed_at,
                abs_start, abs_end
        FROM    season
        """
    )
    op.execute("DROP TABLE season")
    # Rename season_new → season.  No other table references season_new, so
    # SQLite does not rewrite any FK text — dependent tables keep their
    # original REFERENCES season(id) pointing at the newly renamed table.
    op.execute("ALTER TABLE season_new RENAME TO season")
    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    op.execute("ALTER TABLE season ADD COLUMN parent_season TEXT REFERENCES season (id)")
    op.execute("ALTER TABLE season ADD COLUMN sub_ordinal INTEGER")
