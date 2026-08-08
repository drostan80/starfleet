"""paced/catch-up mode schema — A.10, §6.2

One nullable column, not a separate boolean+value pair: NULL means
"not in paced mode" (the vast majority of shows), a positive integer
is both the flag and the cadence in one — same "nullability IS the
flag" shape already used elsewhere in this schema (e.g.
show.hard_delete_requested_at). No DEFAULT — existing rows land on
NULL (not in paced mode), matching "opt-in per show" (§6.2); a client
enabling paced mode without specifying a cadence gets the "default
weekly" behavior at the API layer (enablePacedMode's own default
argument), not via a DB-level DEFAULT that would apply to every
future row regardless of whether paced mode was actually requested.

The CHECK allows NULL through (SQLite's own three-valued logic: any
comparison against NULL in a CHECK is neither true nor false, so it
never fails the constraint) while still rejecting a non-positive
cadence once a value IS set.

Revision ID: 98cbfe3ad4cb
Revises: 1168ad1ebaa0
Create Date: 2026-08-08 17:18:24.205276

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = '98cbfe3ad4cb'
down_revision: str | Sequence[str] | None = '1168ad1ebaa0'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE show ADD COLUMN paced_cadence_days INTEGER "
        "CHECK (paced_cadence_days IS NULL OR paced_cadence_days > 0)"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN paced_cadence_days")
