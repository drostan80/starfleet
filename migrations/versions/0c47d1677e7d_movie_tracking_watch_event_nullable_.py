"""movie tracking, watch_event nullable season/episode, episode_movie_link, next_up_override

Follow-up to 7196ca889757, discovered while drafting A.2 (SDL needs a
complete schema to bind against) — not a pre-planned step, a real gap
found by re-reading §6.10/§6.4 against the finished A.1 schema. Full
rationale for each piece lives in SCOPE.md §5.1/§5.3/§5.9 (search
"2026-08-08" there), not repeated here. Summary:

  - A standalone movie show (media_shape='movie') gets no `episode`
    row at all (clarified directly by the user, not inferred) — so the
    availability-tracking mechanism `episode` normally carries moves
    onto `show` itself, Radarr-only (no Sonarr side for a movie show's
    own record).
  - `watch_event.season`/`episode` become nullable so a movie's watch
    events can carry show_id alone — standard FK-with-null semantics
    already do the right thing, just needed the NOT NULL relaxed.
    SQLite can't ALTER a column's nullability directly, hence the
    batch (rebuild-under-the-hood) mode below — this is Alembic's
    migration-authoring tool for exactly this SQLite limitation, not a
    reintroduction of the ORM/autogenerate approach §11.2 ruled out;
    no persistent model classes, no autogenerate diffing against them.
  - `episode_movie_link`: new id-mapper-shaped reconciliation table
    (same pattern as `show_id_mapping`, §5.5/§3.1) linking a
    `bonus_movie`-kind episode to the standalone movie show
    representing the same film, when both exist.
  - `next_up_override`: new table storing the manual reorder §6.4
    describes for the cross-show next-up query — nothing existed for
    this in §5 before, unlike franchise ordering's own sort_order.

Revision ID: 0c47d1677e7d
Revises: 7196ca889757
Create Date: 2026-08-08 10:11:19.890759

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = '0c47d1677e7d'
down_revision: str | Sequence[str] | None = '7196ca889757'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- 5.1 show: movie-only tracking fields --------------------------------
    op.execute("ALTER TABLE show ADD COLUMN available_via_radarr INTEGER NOT NULL DEFAULT 0 CHECK (available_via_radarr IN (0, 1))")
    op.execute("ALTER TABLE show ADD COLUMN available_checked_at TEXT")
    op.execute(
        "ALTER TABLE show ADD COLUMN available_locally "
        "INTEGER GENERATED ALWAYS AS (available_via_radarr) STORED"
    )

    # -- 5.3 watch_event: season/episode nullable (movie watch events) -------
    # SQLite can't ALTER a column's NOT NULL directly; batch mode rebuilds the
    # table under the hood (migrations/README's documented pattern for this).
    with op.batch_alter_table("watch_event", recreate="always") as batch_op:
        batch_op.alter_column("season", existing_type=sa.Integer(), nullable=True)
        batch_op.alter_column("episode", existing_type=sa.Integer(), nullable=True)

    # -- 5.9 addendum: episode_movie_link (movie <-> bonus_movie-episode) ----
    op.execute("""
        CREATE TABLE episode_movie_link (
            id                TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'm-'),
            episode_id        TEXT NOT NULL UNIQUE REFERENCES episode (id),
            movie_show_id     TEXT REFERENCES show (id),
            source            TEXT NOT NULL CHECK (source IN ('tmdb_match', 'manual', 'unmatched')),
            matched           INTEGER NOT NULL DEFAULT 0 CHECK (matched IN (0, 1)),
            manual_override   INTEGER NOT NULL DEFAULT 0 CHECK (manual_override IN (0, 1)),
            created_at        TEXT NOT NULL,
            updated_at        TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_episode_movie_link_movie_show_id ON episode_movie_link (movie_show_id)")

    # -- 5.9 addendum: next_up_override (manual reorder, §6.4) ---------------
    op.execute("""
        CREATE TABLE next_up_override (
            id          TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'v-'),
            show_id     TEXT NOT NULL UNIQUE REFERENCES show (id),
            sort_order  INTEGER NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE next_up_override")
    op.execute("DROP TABLE episode_movie_link")

    with op.batch_alter_table("watch_event", recreate="always") as batch_op:
        batch_op.alter_column("season", existing_type=sa.Integer(), nullable=False)
        batch_op.alter_column("episode", existing_type=sa.Integer(), nullable=False)

    # SQLite can DROP COLUMN directly (3.35+) for plain columns, but not for
    # one referenced by a generated column — drop available_locally first.
    op.execute("ALTER TABLE show DROP COLUMN available_locally")
    op.execute("ALTER TABLE show DROP COLUMN available_checked_at")
    op.execute("ALTER TABLE show DROP COLUMN available_via_radarr")
