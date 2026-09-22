"""show.poster_art_not_found_at / banner_art_not_found_at — art-fetch
negative cache (NEXT_UP.md "Art-fetch negative cache + throttle").

fetch_show_art (and its staged siblings, fetch_show_art_for_seasons /
fetch_show_art_show_level) stamp these when the show still has no
*selected* show-level (season_id IS NULL) asset of that kind after the
cascade runs, and clear them the moment one is found/selected — self-
healing, no separate "unmark" step. The web client's automatic
per-page-load auto-fetch trigger (show.js's autoFetchArt) checks these
before firing, so a show whose art genuinely can't be found anywhere
(the confirmed real case that prompted this: a Demon Slayer movie entry
with zero art from any source) stops re-triggering the whole AniList/
TVDB/TMDB/TVmaze/MAL cascade on every single page view. The manual
"Fetch Art from Sources" dialog button always calls the full
`fetchShowArt` mutation directly regardless of these columns — the
cache only suppresses the automatic trigger, never a deliberate user
action.

Nullable, TEXT, same ISO-timestamp convention as every other `_at`
column in this schema. Existing rows get NULL — no historical fetch
attempts to backfill.

Revision ID: 45c08e4d9cff
Revises: 038b4fbb1ec7
Create Date: 2026-09-22 00:00:00.000000

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "45c08e4d9cff"
down_revision: str | Sequence[str] | None = "038b4fbb1ec7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE show ADD COLUMN poster_art_not_found_at TEXT")
    op.execute("ALTER TABLE show ADD COLUMN banner_art_not_found_at TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN poster_art_not_found_at")
    op.execute("ALTER TABLE show DROP COLUMN banner_art_not_found_at")
