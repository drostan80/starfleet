"""captured_write — external writes recorded instead of sent (PLAN-CODE 9.0)

2026-09-28. With `external_writes = capture` (the default), every AniList/MAL
list write and Sonarr/Radarr write is recorded here instead of being sent, so
the rebuild's dry run produces the exact write list for the user to approve,
and `lcars captured send` sends the approved ones in capped batches.
A pending write with the same (service, op, dedupe_key) is merged into the
earlier one, so the list holds one line per real change.

Revision ID: a1b2c3d4e5f7
Revises: f0a1b2c3d4e5
Create Date: 2026-09-28 00:00:06.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a1b2c3d4e5f7"
down_revision: str | Sequence[str] | None = "f0a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE captured_write (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            service      TEXT NOT NULL,
            op           TEXT NOT NULL,
            dedupe_key   TEXT,
            args         TEXT NOT NULL,
            captured_at  TEXT NOT NULL,
            sent_at      TEXT,
            send_error   TEXT
        )
    """)
    op.execute(
        "CREATE UNIQUE INDEX ux_captured_write_pending ON captured_write"
        " (service, op, dedupe_key) WHERE sent_at IS NULL AND dedupe_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP TABLE captured_write")
