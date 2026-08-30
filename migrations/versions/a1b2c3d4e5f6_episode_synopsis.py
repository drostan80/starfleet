"""episode synopsis column

Revision ID: a1b2c3d4e5f6
Revises: fe1f556c14f9
Create Date: 2026-08-30
"""

from alembic import op

# revision identifiers
revision = "a1b2c3d4e5f6"
down_revision = "fe1f556c14f9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE episode ADD COLUMN synopsis TEXT")


def downgrade() -> None:
    # SQLite doesn't support DROP COLUMN before 3.35; accept the no-op.
    pass
