"""list hub state — write memory, settle lock, sync log (PLAN-CODE 7.5, R4.9/R4.10)

2026-09-29. LCARS is the truth and remembers, per external list entry, when it
last wrote and what the service actually holds after the write (read back, with
the service's own update time): an unchanged update time means no outside
change. `list_row_lock`: one row per level while an outside change is settling
(no outside change on that row until every list agrees). `list_sync_log`: what
the hub took, deferred, overrode or reviewed. Deferred edits (found while a row
was locked or before a write) wait in the `deferred_*` columns with their real
update time.

Revision ID: c3d4e5f6a7b9
Revises: b2c3d4e5f6a8
Create Date: 2026-09-29 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c3d4e5f6a7b9"
down_revision: str | Sequence[str] | None = "b2c3d4e5f6a8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "lcars_status TEXT",  # the status LCARS last pushed (not the read-back)
    "written_at TEXT",  # when LCARS last wrote this entry
    "remote_updated_at TEXT",  # the service's own update time, as last read/read back
    "deferred_status TEXT",
    "deferred_progress INTEGER",
    "deferred_updated_at TEXT",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE list_baseline ADD COLUMN {column}")
    op.execute("""
        CREATE TABLE list_row_lock (
            season_id  TEXT PRIMARY KEY REFERENCES season (id) ON DELETE CASCADE,
            since      TEXT NOT NULL,
            source     TEXT NOT NULL,
            decided_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE list_sync_log (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            season_id  TEXT,
            service    TEXT,
            at         TEXT NOT NULL,
            kind       TEXT NOT NULL,
            detail     TEXT
        )
    """)
    op.execute("CREATE INDEX ix_list_sync_log_season ON list_sync_log (season_id, at)")


def downgrade() -> None:
    op.execute("DROP TABLE list_sync_log")
    op.execute("DROP TABLE list_row_lock")
    for column in _COLUMNS:
        op.execute(f"ALTER TABLE list_baseline DROP COLUMN {column.split()[0]}")
