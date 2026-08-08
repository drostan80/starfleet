"""season.score — A.9's per-season score/AniList-push granularity

A.9 (§6.1) needed to answer a question A.4's season split had never
worked through: `setScore`/`setStatus` operate on `show` as a whole,
but AniList tracks each season as its own separate list entry — so
which entry receives a push once a show has more than one season?
Resolved directly with the user (not guessed, given real live-AniList-
data consequences): score becomes genuinely per-season, not just a
push-routing question. `season.score` holds this season's own personal
score (same 0-20 quarter-point scale as `show.score`, §6.1); the push
to a given season's AniList entry reads `season.score` when set,
falling back to `show.score` otherwise — the common single-season-show
case is unaffected either way. `show.score` itself is unchanged/kept
(still the field a client sets when a show has no per-season nuance
worth tracking, and still what a freshly-added single-season show
effectively displays via the fallback).

Revision ID: 1168ad1ebaa0
Revises: 2b9d7beb777c
Create Date: 2026-08-08 17:00:57.064690

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '1168ad1ebaa0'
down_revision: str | Sequence[str] | None = '2b9d7beb777c'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE season ADD COLUMN score REAL")


def downgrade() -> None:
    op.execute("ALTER TABLE season DROP COLUMN score")
