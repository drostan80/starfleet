"""art_asset.episode_kind — a distinct cover for special/OVA/bonus-movie
episodes, shared across a whole show/season rather than per-individual-
episode (user's own scope choice, 2026-09-22).

Every existing row gets 'regular' (the DEFAULT backfills it) — correct,
since every art_asset row that already exists genuinely IS the normal
season/show art. The two "at most one selected asset per slot" partial
unique indexes are recreated to include episode_kind, so a selected
special-episode poster and a selected regular poster can coexist in the
same (show, season, kind) slot without colliding — they're different
slots now.

Manual-only feature (no automated fetch source knows about "the cover
for specials" as a concept) — `addManualArtUrl` gains an optional
`episodeKind` argument; `auto_select_best`/the negative-cache check stay
scoped to 'regular' so they're unaffected by this new dimension.

Revision ID: df50e70a1faa
Revises: 45c08e4d9cff
Create Date: 2026-09-22 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "df50e70a1faa"
down_revision: str | Sequence[str] | None = "45c08e4d9cff"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE art_asset ADD COLUMN episode_kind TEXT NOT NULL DEFAULT 'regular'"
        " CHECK (episode_kind IN ('regular', 'special', 'ova', 'bonus_movie'))"
    )
    op.execute("DROP INDEX ix_art_asset_selected_show")
    op.execute("DROP INDEX ix_art_asset_selected_season")
    op.execute(
        """
        CREATE UNIQUE INDEX ix_art_asset_selected_show
        ON art_asset (show_id, kind, episode_kind)
        WHERE selected = 1 AND season_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX ix_art_asset_selected_season
        ON art_asset (show_id, season_id, kind, episode_kind)
        WHERE selected = 1 AND season_id IS NOT NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_art_asset_selected_show")
    op.execute("DROP INDEX ix_art_asset_selected_season")
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
    op.execute("ALTER TABLE art_asset DROP COLUMN episode_kind")
