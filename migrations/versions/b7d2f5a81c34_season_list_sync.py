"""season.list_sync + list_baseline — LCARS as the hub between AniList/MAL

2026-09-25. Seasons LCARS creates on its own (Fribb season fill, sequel
auto-attach, episode-gap rows) must not reach the user's lists: the user's
rule is "in either case do not push to external db". Before this, every
show-level status change pushed the status of *every* linked season, so
the 86 auto-created "dropped" seasons would have been added to AniList/MAL
the next time their show's status changed.

1 = this season is on the user's lists (or the user acted on it: set its
status, watched one of its episodes) and LCARS mirrors it there.
0 = LCARS-only; no AniList/MAL write of any kind.

Existing rows default to 1 (unchanged behaviour); a one-off data step sets
0 for seasons not on the user's AniList. New auto-created rows are
inserted with 0.

list_baseline (same date): the last status/progress LCARS and each list
agreed on, per list entry `(service, external_id)`. The reconcilers compare
the list against it to tell *which side changed* — a list edit goes into
LCARS and on to the other list; an LCARS change the list hasn't taken yet
is pushed again. Before this the list always won, so a failed or lagging
push, or two LCARS seasons sharing an entry, ping-ponged forever.
`lcars_progress` is LCARS's own watched count at that moment — many
seasons have list progress but no LCARS episode rows, so "LCARS went back"
(an unwatch to push) means LCARS dropped below *that*, not below the list.
`list_baseline_seed` records when each service's baseline was first
seeded (only where LCARS and the list already agreed).

Revision ID: b7d2f5a81c34
Revises: a3c9e1f40b27
Create Date: 2026-09-25 00:00:00.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b7d2f5a81c34"
down_revision: str | Sequence[str] | None = "a3c9e1f40b27"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE season ADD COLUMN list_sync INTEGER NOT NULL DEFAULT 1"
        " CHECK (list_sync IN (0, 1))"
    )
    op.execute("""
        CREATE TABLE list_baseline (
            service      TEXT NOT NULL CHECK (service IN ('anilist', 'mal')),
            external_id  INTEGER NOT NULL,
            status       TEXT CHECK (status IS NULL OR status IN
                             ('watching','completed','planned','paused','dropped')),
            progress     INTEGER,
            lcars_progress INTEGER,
            updated_at   TEXT NOT NULL,
            PRIMARY KEY (service, external_id)
        )
    """)
    op.execute("""
        CREATE TABLE list_baseline_seed (
            service    TEXT PRIMARY KEY CHECK (service IN ('anilist', 'mal')),
            seeded_at  TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE list_baseline_seed")
    op.execute("DROP TABLE list_baseline")
    op.execute("ALTER TABLE season DROP COLUMN list_sync")
