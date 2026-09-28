"""show.skip_picked — you picked skipped on the show (RULEBOOK R2.13b)

2026-09-28, phase 5. R2.13b: skipped picked on a show skips its last season and
the show reads dropped (skipped when no season was ever watched) — the one
exception to "a show has the status of its last non-skipped season". The marker
keeps that exception until you next pick another status on the show or one of
its seasons, or watch an episode of a skipped season (R2.13b (c)).

Revision ID: d8e9f0a1b2c3
Revises: c7d8e9f0a1b2
Create Date: 2026-09-28 00:00:04.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d8e9f0a1b2c3"
down_revision: str | Sequence[str] | None = "c7d8e9f0a1b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE show ADD COLUMN skip_picked INTEGER NOT NULL DEFAULT 0"
        " CHECK (skip_picked IN (0, 1))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN skip_picked")
