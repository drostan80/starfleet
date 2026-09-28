"""season.status_set_manually — the user's own status vs automation's

2026-09-28, PLAN-CODE phase 4 (RULEBOOK R2.6, R2.8, R2.16). R2.16 re-skips
later seasons auto-added as planned, but a season the user set planned only
after a warning; R2.10 deletes from AniList/MAL only an auto-added planned
season moved to skipped. This records which one a season's status is.

Existing rows: 0 (unknown). The rebuild sets it from the user's review
decisions (PLAN-DATA).

Also: `status_change` (show history) accepts `skipped` — a show whose every
season is skipped is skipped (R2.13); its CHECK predates the skipped status.

Revision ID: c7d8e9f0a1b2
Revises: b6c7d8e9f0a1
Create Date: 2026-09-28 00:00:03.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7d8e9f0a1b2"
down_revision: str | Sequence[str] | None = "b6c7d8e9f0a1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_STATUS_CHANGE = """
    CREATE TABLE status_change_new (
        id                TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'c-'),
        show_id           TEXT NOT NULL REFERENCES show (id),
        previous_status   TEXT CHECK (previous_status IN ({statuses})),
        new_status        TEXT NOT NULL CHECK (new_status IN ({statuses})),
        changed_at        TEXT NOT NULL,
        changed_by        TEXT NOT NULL
    )
"""
_OLD = "'watching', 'planned', 'paused', 'completed', 'dropped'"


def _rebuild_status_change(statuses: str, where: str = "") -> None:
    op.execute("PRAGMA foreign_keys = OFF")
    op.execute(_STATUS_CHANGE.format(statuses=statuses))
    op.execute(f"INSERT INTO status_change_new SELECT * FROM status_change {where}")
    op.execute("DROP TABLE status_change")
    op.execute("ALTER TABLE status_change_new RENAME TO status_change")
    op.execute("CREATE INDEX ix_status_change_show_id ON status_change (show_id)")
    op.execute("PRAGMA foreign_keys = ON")


def upgrade() -> None:
    op.execute(
        "ALTER TABLE season ADD COLUMN status_set_manually INTEGER NOT NULL DEFAULT 0"
        " CHECK (status_set_manually IN (0, 1))"
    )
    _rebuild_status_change(_OLD + ", 'skipped'")


def downgrade() -> None:
    _rebuild_status_change(
        _OLD, "WHERE new_status <> 'skipped' AND COALESCE(previous_status, '') <> 'skipped'"
    )
    op.execute("ALTER TABLE season DROP COLUMN status_set_manually")
