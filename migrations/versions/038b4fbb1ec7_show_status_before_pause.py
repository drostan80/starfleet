"""show.status_before_pause — monitored<->status reconcile resume state

Sonarr/Radarr -> LCARS reconcile (NEXT_UP.md): when a show's `monitored`
flag flips to false in Sonarr/Radarr, LCARS auto-pauses it. When it flips
back to true, LCARS needs to know which status to resume to (WATCHING vs
PLANNING, etc.) rather than guessing — `status_before_pause` holds that.

Written/cleared in resolvers.py's `_apply_status_change`: set when a
status change enters paused/dropped from an active status, cleared when
leaving paused/dropped (whether by explicit user action or the reconcile
pass restoring it) — a manual status change while paused always wins over
whatever was remembered.

Nullable, TEXT, same convention as every other `show.status` value.
Existing rows get NULL — no historical pause reason to backfill.

Revision ID: 038b4fbb1ec7
Revises: 1b2164585c12
Create Date: 2026-09-19 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "038b4fbb1ec7"
down_revision: str | Sequence[str] | None = "1b2164585c12"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE show ADD COLUMN status_before_pause TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN status_before_pause")
