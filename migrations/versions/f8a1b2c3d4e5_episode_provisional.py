"""episode.provisional: an episode row made from Syoboi's broadcast list until TVDB has it

2026-10-05 (user). When TVDB/Sonarr lists fewer episodes than a running anime has aired, the next
few are added from Syoboi's numbered broadcasts so they can be opened and marked watched. They are
flagged `provisional` and disappear (or are adopted by the real episode) once TVDB takes over.
Additive: one column, default 0.

Revision ID: f8a1b2c3d4e5
Revises: e5f6a7b8c9d1
Create Date: 2026-10-05 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f8a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e5f6a7b8c9d1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE episode ADD COLUMN provisional INTEGER NOT NULL DEFAULT 0")


def downgrade() -> None:
    op.execute("ALTER TABLE episode DROP COLUMN provisional")
