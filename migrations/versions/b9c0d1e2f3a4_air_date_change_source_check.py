"""Widen air_date_change source CHECK to include anidb/tvmaze/syoboi.

The air_date_change table's previous_source and new_source columns had
a CHECK constraint allowing only ('sonarr', 'anilist', 'animeschedule',
'manual'). Since syoboi, anidb, and tvmaze are now valid air_date_source
values on the episode table, the change-tracking table needs to accept
them too. Without this, _record_rewire_changes in syoboi.py fails with
CHECK constraint violation when recording a source change to 'syoboi'.

SQLite table rebuild required (can't ALTER CHECK constraints).

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
Create Date: 2026-09-12
"""

from alembic import op

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None

_VALID_SOURCES = "('sonarr', 'anilist', 'animeschedule', 'manual', 'tvmaze', 'anidb', 'syoboi')"


def upgrade() -> None:
    op.execute("PRAGMA foreign_keys = OFF")

    op.execute(f"""
        CREATE TABLE air_date_change_new (
            id                     TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'g-'),
            episode_id             TEXT NOT NULL REFERENCES episode (id),
            previous_air_date_utc  TEXT,
            new_air_date_utc       TEXT NOT NULL,
            previous_source        TEXT CHECK (previous_source IN {_VALID_SOURCES}),
            new_source             TEXT NOT NULL CHECK (new_source IN {_VALID_SOURCES}),
            changed_at             TEXT NOT NULL,
            changed_by             TEXT NOT NULL
        )
    """)
    op.execute("""
        INSERT INTO air_date_change_new
        SELECT id, episode_id, previous_air_date_utc, new_air_date_utc,
               previous_source, new_source, changed_at, changed_by
        FROM air_date_change
    """)
    op.execute("DROP TABLE air_date_change")
    op.execute("ALTER TABLE air_date_change_new RENAME TO air_date_change")
    op.execute(
        "CREATE INDEX ix_air_date_change_episode_id"
        " ON air_date_change (episode_id)"
    )

    op.execute("PRAGMA foreign_keys = ON")


def downgrade() -> None:
    pass
