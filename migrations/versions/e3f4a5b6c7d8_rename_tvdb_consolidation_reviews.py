"""rename franchise_* review fields to tvdb_consolidation_*

2026-09-27, PLAN-CODE 0.3. The "franchise collision / franchise merge" code
never dealt with a franchise (RULEBOOK R1.20): it folds two LCARS show rows
sharing one TVDB id into one show. Renamed to "same-TVDB show consolidation";
the review field values already stored follow the new names so the review
page and resolveTvdbConsolidation keep finding them.

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-09-27 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: str | Sequence[str] | None = "d2e3f4a5b6c7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RENAMES = (
    ("franchise_auto_merge", "tvdb_consolidation_merge"),
    ("franchise_season_collision", "tvdb_consolidation_season_collision"),
)


def upgrade() -> None:
    for old, new in _RENAMES:
        op.execute(f"UPDATE pending_review SET field = '{new}' WHERE field = '{old}'")


def downgrade() -> None:
    for old, new in _RENAMES:
        op.execute(f"UPDATE pending_review SET field = '{old}' WHERE field = '{new}'")
