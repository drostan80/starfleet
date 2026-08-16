"""season.started_at / season.completed_at — write-mirror function set, todo.md

User's own call, 2026-08-15/16: "StartedAt and CompletedAt are great to
have... a definite must-have," and confirmed live against the user's real
1,057-entry completed AniList list that AniList does **not** reliably
auto-fill either field from progress/status (several fully-completed shows
had `startedAt: null`; four unrelated shows shared one bulk-import
`completedAt`) — so these need to be real LCARS-owned columns, captured
go-forward at write time, not derived from AniList's own behavior.

Both nullable, both TEXT (same ISO-8601 UTC convention every other
timestamp column in this schema already uses, not a new format).
`started_at` = the `watched_at` of this season's first-ever watch_event,
written once (`WHERE started_at IS NULL`) so a later delete/re-mark of that
same episode never moves the date. `completed_at` = the date `show.status`
transitioned to 'completed', written onto the show's highest-numbered
linked season only — mirrors watch_reconcile.py's own
`highest_season_number_by_show` convention for the same show-status/
per-season-entry asymmetry, so a status change doesn't stamp every linked
season including ones the user hasn't actually finished.

Every existing row gets NULL on both columns — deliberate, not a missed
backfill: pre-LCARS watch history has no local watch_event rows to derive
`started_at` from at all, and any already-completed show has no
status_change row from before LCARS existed either. That historical import
is its own separate, reverse-direction (read *from* AniList) piece of work
— overlaps BUILD_PLAN.md's already-parked PC.2, not folded into this
migration.

Revision ID: 8217a5e43344
Revises: de605a998eb9
Create Date: 2026-08-16 12:42:21.902842

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8217a5e43344"
down_revision: str | Sequence[str] | None = "de605a998eb9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE season ADD COLUMN started_at TEXT")
    op.execute("ALTER TABLE season ADD COLUMN completed_at TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE season DROP COLUMN started_at")
    op.execute("ALTER TABLE season DROP COLUMN completed_at")
