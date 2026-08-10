"""service_health: add 'mal' to TRACKED_SERVICES — B.10, §6.7/§6.9

MAL's own token-refresh call (mal_client.refresh_access_token(), hooked
from resolvers.py's new refreshMalTokenIfDue) is a real outbound HTTP
call to a real external service, exactly the class of thing B.6's own
service_health table exists to track — extending it here rather than
leaving MAL invisible to it (and, by extension, to B.11's own planned
Data status-bar consumer) is the same "close the small consistency gap
once it's found" precedent B.6/B.8b/B.9 already established, not
something BUILD_PLAN.md's own B.10 text asked for explicitly. Deliberately
scoped to the refresh call only, not every individual score/status push
(resolvers.py's `_push_mal_season_score`/`_push_mal_show_status`) — same
scope B.6 already has for AniList, where `_push_season_score`/`_push_
show_status`'s own push failures were never hooked into service_health
either (only `_fetch_anilist`'s read path is), so this isn't a new
precedent, just consistency with the existing one.

SQLite has no ALTER-a-CHECK-constraint — same table-rebuild shape
this project's other CHECK-constraint changes already use (rename old,
create new with the wider CHECK, copy rows across, drop old).

Revision ID: 2818c7efae13
Revises: 78747cbf0be5
Create Date: 2026-08-10 00:00:00.000000

"""

from alembic import op

revision = "2818c7efae13"
down_revision = "78747cbf0be5"
branch_labels = None
depends_on = None

_OLD_CHECK = "IN ('sonarr', 'radarr', 'anilist', 'animeschedule')"
_NEW_CHECK = "IN ('sonarr', 'radarr', 'anilist', 'animeschedule', 'mal')"


def upgrade() -> None:
    op.execute("ALTER TABLE service_health RENAME TO service_health_old")
    op.execute(f"""
        CREATE TABLE service_health (
            service              TEXT PRIMARY KEY CHECK (service {_NEW_CHECK}),
            status               TEXT NOT NULL CHECK (status IN ('ok', 'unreachable')),
            last_checked_at      TEXT NOT NULL,
            last_success_at      TEXT,
            last_error_message   TEXT,
            updated_at           TEXT NOT NULL
        )
    """)
    op.execute("INSERT INTO service_health SELECT * FROM service_health_old")
    op.execute("DROP TABLE service_health_old")


def downgrade() -> None:
    op.execute("DELETE FROM service_health WHERE service = 'mal'")
    op.execute("ALTER TABLE service_health RENAME TO service_health_old")
    op.execute(f"""
        CREATE TABLE service_health (
            service              TEXT PRIMARY KEY CHECK (service {_OLD_CHECK}),
            status               TEXT NOT NULL CHECK (status IN ('ok', 'unreachable')),
            last_checked_at      TEXT NOT NULL,
            last_success_at      TEXT,
            last_error_message   TEXT,
            updated_at           TEXT NOT NULL
        )
    """)
    op.execute("INSERT INTO service_health SELECT * FROM service_health_old")
    op.execute("DROP TABLE service_health_old")
