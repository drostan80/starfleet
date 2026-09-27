"""tvmaze_special + tvmaze_special_fetch — TVmaze specials for TV numbering

2026-09-27, PLAN-CODE 0.1 (RULEBOOK R1.0, R1.8, R1.19). `/shows/{id}/episodes`
leaves specials out, so TV season 0 never had TVmaze data. With `?specials=1`
TVmaze returns them with `number = null` and the season they aired in — the
air-date placement R1.8 needs. They can't go in `tvmaze_episode` (keyed by
season + episode number), so they get their own table keyed by TVmaze's own
episode id.

`tvmaze_special_fetch`: one row per TVmaze show once its specials have been
fetched (a show with none still gets a row), so the drip fetches each show once.

Revision ID: c1d2e3f4a5b6
Revises: b7d2f5a81c34
Create Date: 2026-09-27 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: str | Sequence[str] | None = "b7d2f5a81c34"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE tvmaze_special (
            tvmaze_episode_id INTEGER PRIMARY KEY,
            tvmaze_show_id    INTEGER NOT NULL,
            season            INTEGER,
            type              TEXT,
            title             TEXT,
            airdate           TEXT,
            airstamp          TEXT,
            airtime           TEXT,
            runtime_minutes   INTEGER,
            fetched_at        TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_tvmaze_special_show_id ON tvmaze_special (tvmaze_show_id)")
    op.execute("""
        CREATE TABLE tvmaze_special_fetch (
            tvmaze_show_id INTEGER PRIMARY KEY,
            fetched_at     TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE tvmaze_special_fetch")
    op.execute("DROP TABLE tvmaze_special")
