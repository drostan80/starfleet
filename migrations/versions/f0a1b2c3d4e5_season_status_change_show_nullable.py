"""season_status_change.show_id nullable — an individual season has no show

2026-09-28, PLAN-CODE 8.8. An individual season (RULEBOOK R3.6c/d) belongs to
no show until it joins one, so logging its status change failed on the NOT
NULL show_id — setting any status on one errored. The row keeps show_id NULL
until the season joins a show (tvdb_vetting.join fills it in).

Revision ID: f0a1b2c3d4e5
Revises: e9f0a1b2c3d4
Create Date: 2026-09-28 00:00:05.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: str | Sequence[str] | None = "e9f0a1b2c3d4"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rebuild(show_id_null: str) -> None:
    op.execute("ALTER TABLE season_status_change RENAME TO season_status_change_old")
    op.execute(f"""
        CREATE TABLE season_status_change (
            id               TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'j-'),
            season_id        TEXT NOT NULL REFERENCES season (id),
            show_id          TEXT {show_id_null} REFERENCES show (id),
            previous_status  TEXT,
            new_status       TEXT,
            changed_at       TEXT NOT NULL,
            changed_by       TEXT NOT NULL
        )
    """)
    op.execute("INSERT INTO season_status_change SELECT * FROM season_status_change_old")
    op.execute("DROP TABLE season_status_change_old")
    op.execute("CREATE INDEX ix_season_status_change_season ON season_status_change (season_id)")


def upgrade() -> None:
    _rebuild("")


def downgrade() -> None:
    op.execute("DELETE FROM season_status_change WHERE show_id IS NULL")
    _rebuild("NOT NULL")
