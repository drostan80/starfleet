"""Art asset table — multi-source artwork storage (2026-08-30).

Replaces the single-scalar `show.poster_url`/`show.banner_url` approach
with a proper candidate table. Each row stores one art URL from one
source (anilist, tvdb, tmdb, mal, …) for one show or season, typed by
kind (poster, banner, background). A `selected` flag marks the user's
chosen art per slot; a partial unique index enforces at most one
selected asset per (show, season, kind) combination.

Existing `show.poster_url` and `show.banner_url` columns are kept as
the migration-era fallback — resolvers read the art_asset table first,
then fall back to the existing columns for shows that haven't had their
art assets populated yet. No data is lost or moved during upgrade.

The downgrade simply drops the table — art_asset rows are ephemeral
metadata re-fetchable from upstream services, so no data-preservation
concern.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7ac07268877"
down_revision: str | None = "28cbe21e9b6e"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE art_asset (
            id          TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'h-'),
            show_id     TEXT NOT NULL REFERENCES show (id),
            season_id   TEXT REFERENCES season (id),
            kind        TEXT NOT NULL CHECK (kind IN ('poster', 'banner', 'background')),
            source      TEXT NOT NULL,
            url         TEXT NOT NULL,
            width       INTEGER,
            height      INTEGER,
            language    TEXT,
            source_score INTEGER,
            selected    INTEGER NOT NULL DEFAULT 0 CHECK (selected IN (0, 1)),
            created_at  TEXT NOT NULL,
            UNIQUE (show_id, season_id, kind, source, url)
        )
        """
    )
    # At most one selected asset per (show, season, kind) slot.
    # season_id is nullable (show-level vs season-level), so two
    # indexes are needed: one for season_id IS NULL, one for non-null.
    op.execute(
        """
        CREATE UNIQUE INDEX ix_art_asset_selected_show
        ON art_asset (show_id, kind)
        WHERE selected = 1 AND season_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_art_asset_selected_season
        ON art_asset (show_id, season_id, kind)
        WHERE selected = 1 AND season_id IS NOT NULL
        """
    )
    # Fast lookup by show (all art for a show page).
    op.execute(
        "CREATE INDEX ix_art_asset_show ON art_asset (show_id)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS art_asset")
