"""service_health table — per-integration reachability tracking, B.6, §6.7

Full rationale in SCOPE.md §6.7's "Resolved 2026-08-09 (B.6)" note.
Summary: one row per tracked integration (sonarr/radarr/anilist/
animeschedule — the exact four §6.7 names; deliberately excludes the
`local` pseudo-service, §5.4/B.7's own concept, a SQL aggregate over
episode rows, not a reachability check). Written by
`lcars/service_health.py`'s `record_success`/`record_failure`, called
from every existing call site that already makes a real outbound call
to one of these four services (metadata.py's `_guarded`,
availability.py's `_poll_sonarr`/`_poll_radarr`, local_audit.py's
`_audit_sonarr`/`_audit_radarr`, animeschedule.py's
`poll_anime_schedule`) — no new client code, no new polling loop, this
just observes outcomes that already happen. No id prefix (§5.0), same
natural-key-per-service reasoning `availability_poll_checkpoint` (B.3)
already uses. A service never yet contacted (no config set, or simply
not polled yet) has no row at all — `get_all()` synthesizes an
`unknown` placeholder for it rather than writing one, so "unknown" is
a presentation-layer concept, never a stored value (the CHECK
constraint below only allows the two states code actually observes).

Revision ID: 78747cbf0be5
Revises: f7a2c4e91b6d
Create Date: 2026-08-10 00:00:00.000000

"""

from alembic import op

revision = "78747cbf0be5"
down_revision = "f7a2c4e91b6d"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE service_health (
            service              TEXT PRIMARY KEY CHECK (service IN ('sonarr', 'radarr', 'anilist', 'animeschedule')),
            status               TEXT NOT NULL CHECK (status IN ('ok', 'unreachable')),
            last_checked_at      TEXT NOT NULL,
            last_success_at      TEXT,
            last_error_message   TEXT,
            updated_at           TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE service_health")
