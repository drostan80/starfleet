"""untracked_show_finding table — B.11e, ongoing untracked-show sweep

SCOPE.md's own "Resolved 2026-08-10 (B.11 reconnaissance)" note (§5.2):
`untracked_shows` discovery moves onto Ops's recurring schedule and
persists findings in a small new reviewable list, rather than only
returning them from a one-shot `auditLocalFiles`/`previewShowBackfill`
call — otherwise a show added directly in Sonarr/AniList after B.11d's
one-time backfill would silently never surface once B.11f switches
Data's calendar over to LCARS-tracked shows only. Still never
auto-`addShow`s — that decision is untouched, only how findings surface
changes.

Reuses `show_backfill.preview_backfill()`'s own combined computation
(Sonarr/Radarr untracked catalog items + the AniList-list sweep,
already classified) rather than a second, narrower Sonarr/Radarr-only
pass — SCOPE.md's own resolution text names "Sonarr/AniList" together,
not Sonarr/Radarr alone, and `previewShowBackfill` already *is* exactly
this computation, one-shot; this table is that same computation, run on
a schedule and persisted for a human to review at their own convenience
instead of only at the moment a manual call happens to run.

Deliberately minimal — no resolve/dismiss mutation, unlike
`pending_review`: a finding is only ever removed automatically, by no
longer appearing in a fresh sweep (the show got tracked, or genuinely
disappeared from its source) — matches "small new reviewable list",
not a second review-and-resolve workflow. Easy to add a dismiss action
later if it turns out to be needed; not built speculatively now.

Prefixed id (`u-`, next free letter, §5.0) rather than a bare natural
key, unlike `service_health`/`availability_poll_checkpoint` — this is a
real, individually-addressable finding a client will list/read by id
(same shape `show_service_presence` already established: a prefixed id
*and* a natural-key UNIQUE constraint side by side), not a
one-row-per-service singleton. `UNIQUE (service, external_id)` is the
natural key the sweep upserts against.

Revision ID: d5858f23ed73
Revises: 2818c7efae13
Create Date: 2026-08-10 19:25:59.027994

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5858f23ed73"
down_revision: str | None = "2818c7efae13"
branch_labels: str | None = None
depends_on: str | None = None


def _id_check(column: str, prefix: str) -> str:
    """`{prefix}-{6 chars}` shape check — same as every other migration's
    own copy of this helper (7196ca889757's own docstring has the
    reasoning; each migration keeps its own copy rather than importing
    app code, same precedent as every prior migration here)."""
    return f"length({column}) = 8 AND substr({column}, 1, 2) = '{prefix}-'"


def upgrade() -> None:
    op.execute(f"""
        CREATE TABLE untracked_show_finding (
            id              TEXT PRIMARY KEY CHECK ({_id_check('id', 'u')}),
            service         TEXT NOT NULL CHECK (service IN ('sonarr', 'radarr', 'anilist')),
            external_id     TEXT NOT NULL,
            title           TEXT NOT NULL,
            path            TEXT,
            tracking_space  TEXT NOT NULL CHECK (tracking_space IN ('tv', 'anime')),
            media_shape     TEXT NOT NULL CHECK (media_shape IN ('episodic', 'movie')),
            first_seen_at   TEXT NOT NULL,
            last_seen_at    TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            UNIQUE (service, external_id)
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE untracked_show_finding")
