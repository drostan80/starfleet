"""anilist_activity_checkpoint table — B.5.3, BUILD_PLAN.md Phase B.5

New: `anilist_activity_checkpoint`, a true singleton (`id INTEGER PRIMARY
KEY CHECK (id = 1)`) tracking the last-seen AniList activity-feed entry
(`Page.activities`, `type: ANIME_LIST`) `poll_anilist_activity()`
(watch_reconcile.py) checkpoints against, so a repeat poll only asks
AniList for what's new since last time rather than walking this
account's entire activity history (confirmed live 2026-08-13: 5000+
entries on a real, years-old account) on every tick.

Two columns, not one, by design — confirmed live the same day that
several real activities can share one `createdAt` (whole-second
resolution), so a `createdAt`-only watermark risks either skipping
same-second entries (if advanced past them) or reprocessing them (if
held back a defensive second) every single poll. `last_activity_id` is
the real, exact cursor (AniList's activity ids are confirmed
monotonically increasing with creation order under `sort: ID`);
`last_activity_created_at` exists so the next poll's own
`createdAt_greater` query argument can stay a cheap server-side filter
(minus a 1-second safety margin) rather than fetching everything since
account creation and filtering every row by id client-side.

Global, not per-show — one shared personal activity feed, same
"global singleton, no id prefix" reasoning `availability_poll_checkpoint`
(B.3) already established for `sonarr`/`radarr`'s own per-service rows,
generalized here to the one-row-total case since there's only ever one
AniList account. Not exposed via GraphQL — pure internal polling
bookkeeping, same treatment `availability_poll_checkpoint` already gets.

Revision ID: de605a998eb9
Revises: fe1f556c14f9
Create Date: 2026-08-13 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "de605a998eb9"
down_revision: str | Sequence[str] | None = "fe1f556c14f9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE anilist_activity_checkpoint (
            id                        INTEGER PRIMARY KEY CHECK (id = 1),
            last_activity_id          INTEGER,
            last_activity_created_at  INTEGER,
            updated_at                TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE anilist_activity_checkpoint")
