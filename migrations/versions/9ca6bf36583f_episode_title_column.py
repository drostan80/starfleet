"""episode.title — the last irreducible direct-Sonarr-read blocker for the Data thin-client swap

Closes the one real gap `~/repos/data/DATA_THIN_CLIENT_PLAN.md`'s §0/§7.1 found while planning the
thin-client swap: LCARS's `episode` table had no title column at all, so Data's calendar/detail
pane had no way to show episode titles without reading Sonarr directly — the one thing standing
between "LCARS covers everything Data reads" and "Data has one narrow permanent Sonarr read left."
User's own instruction, verbatim, settled it: "EVERYTHING IS MOVED TO LCARS... DATA IS A THIN
CLIENT SO IT JUST READS LCARS" — no exception, so LCARS grows the field instead.

Nullable TEXT, sourced from Sonarr's own `episode.title` field (metadata.py's `_fetch_sonarr`/
`_fetch_sonarr_multi_show`, both INSERT and existing-row-backfill paths, same "only fill a row
that's never been checked" gating already established for `available_via_sonarr`/
`file_path_sonarr`/`absolute_number`). No Radarr/movie equivalent needed — `media_shape = MOVIE`
has no episode row at all (§5.1); a movie's own title already lives on `show.title_*`.

Every existing row gets NULL — deliberate, not a missed backfill: title only populates on the next
real Sonarr fetch for a given show (refreshShowMetadata, or the next scheduled B.1 pass), same
go-forward-capture precedent every other newly-added source-fact column in this schema already has
(`started_at`/`completed_at`, `absolute_number`, `available_via_sonarr`, all landed the same way).

Revision ID: 9ca6bf36583f
Revises: 8217a5e43344
Create Date: 2026-08-16

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "9ca6bf36583f"
down_revision: str | Sequence[str] | None = "8217a5e43344"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE episode ADD COLUMN title TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE episode DROP COLUMN title")
