"""repair S1 source: fribb->manual promotion was a bug

_upsert_season ran on every refreshShowMetadata, silently converting
every fribb-matched S1 to source='manual', manual_override=1.  Fixed
in v0.1.71 (guard to addShow-only).  This migration repairs existing
rows: reset S1 to source='fribb', manual_override=0 where the season's
anilist_id matches the show-level AniList external ID (meaning it was
auto-set by _upsert_season from the show's own identity, not manually
overridden to a different AniList entry).

Revision ID: e5f6g7082930
Revises: d4e5f6071829
Create Date: 2026-09-02
"""

from alembic import op

revision = "e5f6g7082930"
down_revision = "d4e5f6071829"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Reset S1 from manual back to fribb where the anilist_id matches
    # the show's own AniList external_id -- these were auto-promoted by
    # the _upsert_season bug, not genuinely manual.
    op.execute(
        "UPDATE season SET source = 'fribb', manual_override = 0"
        " WHERE season_number = 1"
        "   AND source = 'manual'"
        "   AND manual_override = 1"
        "   AND anilist_id IS NOT NULL"
        "   AND CAST(anilist_id AS TEXT) IN ("
        "     SELECT external_id FROM show_external_id"
        "     WHERE show_external_id.show_id = season.show_id"
        "       AND show_external_id.service = 'anilist'"
        "   )"
    )


def downgrade() -> None:
    # Not reversible -- the original state was the bug.
    pass
