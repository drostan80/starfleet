"""season entity — corrects show_id_mapping's flawed one-anilist-id-per-show assumption

A real modeling gap found starting A.4, not caught by the earlier
audit pass: `show_id_mapping` (§5.5, A.1) assumed one `show` maps to
exactly one AniList entry — but the real, working aniq/Data
`mapping.py` this project is meant to formalize resolves AniList ids
by *(tvdb_id, season_number) together*, because TVDB groups a
franchise's seasons under one series id while AniList splits each
season into its own entry. A single `anilist_id` column on
`show_id_mapping` can't represent "season 1 -> AniList X, season 2 ->
AniList Y" at once.

Resolved directly with the user (not guessed): `show`/`season`/
`episode` are three independently-identified, independently-mappable
levels. `season` is new here — one row per season of a show, holding
the per-season cross-service identity (`anilist_id`/`mal_id` — both
split by season) that `show_id_mapping` incorrectly tried to hold on
`show` directly. `show_id_mapping` is dropped entirely; nothing has
ever been deployed with it (pre-release, no real data), so a clean
drop-and-replace beats carrying deprecated cruft forward. `tvdb_id`
stays exactly where it already was, on `show` via `show_external_id`
— TVDB doesn't split by season, it's one series id with
`season_number` as a sub-key, so no separate `tvdb_id` column is
needed on `season` itself. `episode.season_id` is added (nullable —
existing rows/A.8's future fetch logic populate it, nothing backfills
it automatically here) alongside the existing integer `season` column,
which stays for raw Sonarr-numbering compatibility exactly as before.
`franchise`/`franchise_member` (§5.9) are unrelated and untouched —
confirmed with the user this is a distinct, coexisting concept
(a movie needs to map to *both* its franchise position *and* its
season, not one or the other).

Revision ID: 2b9d7beb777c
Revises: 4509892cd91b
Create Date: 2026-08-08 14:56:58.055287

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '2b9d7beb777c'
down_revision: str | Sequence[str] | None = '4509892cd91b'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP TABLE show_id_mapping")

    op.execute("""
        CREATE TABLE season (
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
            UNIQUE (show_id, season_number)
        )
    """)

    op.execute("ALTER TABLE episode ADD COLUMN season_id TEXT REFERENCES season (id)")


def downgrade() -> None:
    op.execute("ALTER TABLE episode DROP COLUMN season_id")
    op.execute("DROP TABLE season")

    op.execute("""
        CREATE TABLE show_id_mapping (
            id                   TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'x-'),
            show_id              TEXT NOT NULL UNIQUE REFERENCES show (id),
            tvdb_id              INTEGER,
            anilist_id           INTEGER,
            source               TEXT NOT NULL CHECK (source IN ('fribb', 'manual', 'unmatched')),
            matched              INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override      INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            last_reconciled_at   TEXT,
            created_at           TEXT NOT NULL,
            updated_at           TEXT NOT NULL
        )
    """)
