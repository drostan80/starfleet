"""ops_tier_checkpoint table — persisted last-run time for Ops's slow tiers

2026-09-23. Ops keeps no state of its own, so its monthly tier (every
season of every show re-reconciled, plus catalog presence and show merge)
was timed by an in-process `asyncio.sleep(30 days)` that restarts with the
container. Its first tick used to fail at startup (Ops beat LCARS to the
network) and then sleep a month — the pass effectively never ran, except
on the odd restart where LCARS happened to be up (2026-09-12, 2026-09-20).
Once Ops started waiting for LCARS (v0.2.57) it ran on *every* deploy
instead. Neither is the intended monthly cadence.

Same "server decides, client acts" shape as the other due-gates
(`availability_poll_checkpoint`, `mal_token_refreshed_at`): LCARS stores
when a tier last completed, Ops asks whether it's due and reports when it
finishes. One row per tier name.

Seeded with the monthly tier as completed *now*: it ran today (10:38 UTC,
v0.2.58's startup), so deploying this doesn't trigger yet another pass.

Revision ID: a3c9e1f40b27
Revises: df50e70a1faa
Create Date: 2026-09-23 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a3c9e1f40b27"
down_revision: str | Sequence[str] | None = "df50e70a1faa"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE ops_tier_checkpoint (
            tier               TEXT PRIMARY KEY,
            last_completed_at  TEXT NOT NULL
        )
    """)
    op.execute(
        "INSERT INTO ops_tier_checkpoint (tier, last_completed_at)"
        " VALUES ('monthly', strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))"
    )


def downgrade() -> None:
    op.execute("DROP TABLE ops_tier_checkpoint")
