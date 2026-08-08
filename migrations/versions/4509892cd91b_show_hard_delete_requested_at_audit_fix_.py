"""show hard_delete_requested_at (audit fix, missing since A.2)

A real gap caught during a full audit pass, not part of any BUILD_PLAN
step's original work: A.2's schema.graphql wrote `requestHardDelete`/
`cancelHardDelete`/`confirmHardDelete` (§6.11) referencing
`hard_delete_requested_at`, and BUILD_PLAN.md's A.2 entry even claimed
a column for it had "already [been] added via the A.1-addendum
migration" — but it never actually was. Confirmed by cross-checking
schema.graphql's field list against a fresh migrated DB's real
`PRAGMA table_xinfo`, table by table, not by re-reading source.

Also resolves the second half of the same gap: §6.11 never specified
an actual delay *duration*, only that a delay period exists
conceptually. Fixed 24 hours (SCOPE.md §6.11, resolved alongside this
migration) — not stored here, since it's a fixed constant the
application layer checks against `hard_delete_requested_at`, not
itself a piece of per-row state.

Revision ID: 4509892cd91b
Revises: 0c47d1677e7d
Create Date: 2026-08-08 13:04:08.772554

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '4509892cd91b'
down_revision: str | Sequence[str] | None = '0c47d1677e7d'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE show ADD COLUMN hard_delete_requested_at TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN hard_delete_requested_at")
