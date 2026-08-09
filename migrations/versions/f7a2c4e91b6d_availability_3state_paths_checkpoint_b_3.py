"""episode/show availability: bool -> 3-state, file paths, poll checkpoint — B.3, §5.2

Full rationale in SCOPE.md §5.2's "Resolved 2026-08-09 (B.3)" note —
verified against the user's own real, live Sonarr/Radarr instances
before being built, not assumed. Summary:

  - available_via_sonarr/available_via_radarr (episode) and
    available_via_radarr (show, movie-only) refine from a plain
    boolean to a 3-state `unavailable | downloading | available` —
    "grabbed not imported" is a real, useful, distinct state, and a
    file can go available -> unavailable again (episodeFileDeleted,
    confirmed common in real history data), not just the other way.
  - New file_path_sonarr/file_path_radarr (episode) and
    file_path_radarr (show) — the actual imported path, one per
    source, same reasoning the two availability booleans were already
    kept independent for (a bonus_movie episode can be available via
    both sources at once, with two genuinely different files on disk).
  - available_locally's generated formula updates to check for the
    string 'available' specifically on either source column —
    'downloading' does not count as locally available.
  - New availability_poll_checkpoint table: one row per service
    (sonarr/radarr), tracking the last processed /history event's
    timestamp so repeat polls only look at new events. Global, not
    per-show (one shared history feed covers every tracked show at
    once) — no id prefix (§5.0), same natural/composite-key reasoning
    show_service_presence already uses, generalized to a global
    singleton-per-service key. Not exposed via GraphQL — pure internal
    polling bookkeeping, same treatment alembic_version already gets.

No real data exists yet for any of the columns being converted
(available_via_sonarr/available_via_radarr have never been written by
any code path before this step, confirmed by grep before drafting this
migration) — a plain DROP+ADD is safe here, not a data-preserving
rebuild.

Revision ID: f7a2c4e91b6d
Revises: e3f6b8a1c9d2
Create Date: 2026-08-09 00:00:00.000000

"""
from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f7a2c4e91b6d'
down_revision: str | Sequence[str] | None = 'e3f6b8a1c9d2'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATE_CHECK = "IN ('unavailable', 'downloading', 'available')"


def upgrade() -> None:
    # -- episode -----------------------------------------------------------
    # Generated column depends on the two source columns — must go first.
    op.execute("ALTER TABLE episode DROP COLUMN available_locally")
    op.execute("ALTER TABLE episode DROP COLUMN available_via_sonarr")
    op.execute("ALTER TABLE episode DROP COLUMN available_via_radarr")
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_via_sonarr TEXT NOT NULL DEFAULT 'unavailable'"
        f" CHECK (available_via_sonarr {_STATE_CHECK})"
    )
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_via_radarr TEXT NOT NULL DEFAULT 'unavailable'"
        f" CHECK (available_via_radarr {_STATE_CHECK})"
    )
    op.execute("ALTER TABLE episode ADD COLUMN file_path_sonarr TEXT")
    op.execute("ALTER TABLE episode ADD COLUMN file_path_radarr TEXT")
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_locally INTEGER GENERATED ALWAYS AS ("
        "  CASE WHEN available_via_sonarr = 'available' OR available_via_radarr = 'available'"
        "       THEN 1 ELSE 0 END"
        ") STORED"
    )

    # -- show (movie-only availability fields) ------------------------------
    op.execute("ALTER TABLE show DROP COLUMN available_locally")
    op.execute("ALTER TABLE show DROP COLUMN available_via_radarr")
    op.execute(
        "ALTER TABLE show ADD COLUMN available_via_radarr TEXT NOT NULL DEFAULT 'unavailable'"
        f" CHECK (available_via_radarr {_STATE_CHECK})"
    )
    op.execute("ALTER TABLE show ADD COLUMN file_path_radarr TEXT")
    op.execute(
        "ALTER TABLE show ADD COLUMN available_locally INTEGER GENERATED ALWAYS AS ("
        "  CASE WHEN available_via_radarr = 'available' THEN 1 ELSE 0 END"
        ") STORED"
    )

    # -- new: availability poll checkpoint (global, not per-show) ----------
    op.execute("""
        CREATE TABLE availability_poll_checkpoint (
            service        TEXT PRIMARY KEY CHECK (service IN ('sonarr', 'radarr')),
            last_event_at  TEXT,
            updated_at     TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE availability_poll_checkpoint")

    op.execute("ALTER TABLE show DROP COLUMN available_locally")
    op.execute("ALTER TABLE show DROP COLUMN file_path_radarr")
    op.execute("ALTER TABLE show DROP COLUMN available_via_radarr")
    op.execute(
        "ALTER TABLE show ADD COLUMN available_via_radarr INTEGER NOT NULL DEFAULT 0"
        " CHECK (available_via_radarr IN (0, 1))"
    )
    op.execute(
        "ALTER TABLE show ADD COLUMN available_locally"
        " INTEGER GENERATED ALWAYS AS (available_via_radarr) STORED"
    )

    op.execute("ALTER TABLE episode DROP COLUMN available_locally")
    op.execute("ALTER TABLE episode DROP COLUMN file_path_sonarr")
    op.execute("ALTER TABLE episode DROP COLUMN file_path_radarr")
    op.execute("ALTER TABLE episode DROP COLUMN available_via_sonarr")
    op.execute("ALTER TABLE episode DROP COLUMN available_via_radarr")
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_via_sonarr INTEGER NOT NULL DEFAULT 0"
        " CHECK (available_via_sonarr IN (0, 1))"
    )
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_via_radarr INTEGER NOT NULL DEFAULT 0"
        " CHECK (available_via_radarr IN (0, 1))"
    )
    op.execute(
        "ALTER TABLE episode ADD COLUMN available_locally"
        " INTEGER GENERATED ALWAYS AS (available_via_sonarr OR available_via_radarr) STORED"
    )
