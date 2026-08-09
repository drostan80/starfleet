"""show.metadata_last_refreshed_at — B.1, §6.7/§11.2

One nullable timestamp column, same "opt-in, NULL means never
happened" shape already used for hard_delete_requested_at/
paced_cadence_days (§6.11/§6.2) — not a separate boolean+value pair.
Stamped by metadata.fetch_and_populate() (§4/A.8) at the end of every
run, success or partial failure alike, so a show's very first fetch
(addShow's own inline call) already counts as "refreshed today" and
Ops's own daily pass (Query.dueForMetadataRefresh) correctly skips it
the same day. Same per-entity "last checked" shape season.
last_reconciled_at (§5.5) already established, one level up.

No CHECK constraint needed — any ISO-8601 string or NULL is valid,
unlike paced_cadence_days's own positive-integer requirement.

Revision ID: e3f6b8a1c9d2
Revises: 98cbfe3ad4cb
Create Date: 2026-08-09 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e3f6b8a1c9d2'
down_revision: str | Sequence[str] | None = '98cbfe3ad4cb'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE show ADD COLUMN metadata_last_refreshed_at TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN metadata_last_refreshed_at")
