"""season absolute-episode ranges + sub-season link + range-based external-id mapping

Slice 1 (inert foundation) of hierarchical season subdivision (NEXT_UP;
design note artifact 01f25b72). The chosen model: LCARS is source of truth
and subdivides its seasons to the *finest* linked source, reconciling all
sources by **absolute-episode range**. This migration adds the schema that
model needs and nothing else — every column is nullable, the new table
starts empty, and **no code reads any of it yet** (season.anilist_id/mal_id
stay the live fields; Slice 3 migrates the reconcile reads over). So this is
independently deployable and behaviourally inert.

Decisions this encodes (settled 2026-08-26, see NEXT_UP / the design note):
  - **D1** sub-season representation: adopt the finest source's own season
    numbering; where it can't map to a clean integer (season_number is
    INTEGER, UNIQUE(show_id, season_number)), fall back to
    `parent_season` + `sub_ordinal`, rendered "N.M".
  - **D3** reconcile by absolute range: `abs_start`/`abs_end` give each
    season its [start,end] in the show's absolute episode numbering, the
    join key across sources. `season_external_id` replaces the single-valued
    season.anilist_id/mal_id: one (season, service) row each — but
    `external_id` is deliberately **not** unique across seasons, so a coarse
    source whose single entry spans several fine seasons (its "season Z"
    covering eps x-y) links that one id to every fine season in the range.
    Reconciliation groups by external_id and applies across those seasons;
    the completion safeguard (never mark Z complete unless every component
    fine season is) lives in the reconcile slice, not here.

`parent_season` uses a NULL default so SQLite's ADD COLUMN accepts the
REFERENCES clause on a populated table.

Revision ID: c7a1e2f4b8d3
Revises: b3f9c2a7d1e4
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7a1e2f4b8d3"
down_revision: str | Sequence[str] | None = "b3f9c2a7d1e4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE season ADD COLUMN abs_start INTEGER")
    op.execute("ALTER TABLE season ADD COLUMN abs_end INTEGER")
    op.execute("ALTER TABLE season ADD COLUMN parent_season TEXT REFERENCES season (id)")
    op.execute("ALTER TABLE season ADD COLUMN sub_ordinal INTEGER")
    op.execute(
        """
        CREATE TABLE season_external_id (
            season_id   TEXT NOT NULL REFERENCES season (id),
            service     TEXT NOT NULL CHECK (service IN ('anilist', 'mal')),
            external_id INTEGER NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (season_id, service)
        )
        """
    )
    op.execute("CREATE INDEX ix_season_external_id_season ON season_external_id (season_id)")
    op.execute(
        "CREATE INDEX ix_season_external_id_lookup ON season_external_id (service, external_id)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_season_external_id_lookup")
    op.execute("DROP INDEX ix_season_external_id_season")
    op.execute("DROP TABLE season_external_id")
    op.execute("ALTER TABLE season DROP COLUMN sub_ordinal")
    op.execute("ALTER TABLE season DROP COLUMN parent_season")
    op.execute("ALTER TABLE season DROP COLUMN abs_end")
    op.execute("ALTER TABLE season DROP COLUMN abs_start")
