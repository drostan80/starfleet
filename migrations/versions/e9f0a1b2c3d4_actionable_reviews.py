"""actionable reviews — choices, payload and the show a review is about

2026-09-28, phase 5 (RULEBOOK R4.8b): every review offers the choices that
resolve it and links to the show page. `choices` is a JSON list of
{"id", "label"}; `payload` is the JSON the chosen action needs (ids,
proposed show and season); `show_id` is the show page to link to.

Revision ID: e9f0a1b2c3d4
Revises: d8e9f0a1b2c3
Create Date: 2026-09-28 00:00:05.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e9f0a1b2c3d4"
down_revision: str | Sequence[str] | None = "d8e9f0a1b2c3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE pending_review ADD COLUMN choices TEXT")
    op.execute("ALTER TABLE pending_review ADD COLUMN payload TEXT")
    op.execute("ALTER TABLE pending_review ADD COLUMN show_id TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE pending_review DROP COLUMN show_id")
    op.execute("ALTER TABLE pending_review DROP COLUMN payload")
    op.execute("ALTER TABLE pending_review DROP COLUMN choices")
