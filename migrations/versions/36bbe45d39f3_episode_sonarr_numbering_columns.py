"""episode.sonarr_season / episode.sonarr_episode — NEXT_UP.md, 2026-08-19/25

Closes the other half of archive/todo.md:1209 (the air-date half was already done —
`setEpisodeAirDate`, `air_date_source`): a client-facing way to correct which Sonarr season/
episode slot an episode is actually filed under, e.g. AniList counts episode 6 what Sonarr
filed as episode 5 for the same real episode. No mutation existed for this at all before now.

Real durability problem found while designing the mutation, not assumed: `episode.season`/
`episode.episode` (SDL's own docstring — "the raw Sonarr-numbered season... unchanged since
A.1") are also the exact columns metadata.py's `_fetch_sonarr` uses to decide "is this an
episode LCARS already knows, or a new one" (`WHERE show_id = ? AND season = ? AND episode = ?`).
A mutation that only UPDATEs `season`/`episode` would work once, then get silently undone on
the very next Sonarr metadata refresh: Sonarr still reports the episode under its old number,
that lookup no longer finds the renumbered row, and a phantom duplicate episode gets INSERTed
at the original slot — the correction vanishes and a ghost row appears in its place.

These two new columns capture Sonarr's own raw numbering immutably, once, at the moment an
episode is first fetched — `_fetch_sonarr`'s existing-row lookup switches to match on these
instead, so a `season`/`episode` correction survives every future sync (Sonarr's own raw
report never changes what these two columns say, only what `season`/`episode` display). Both
nullable — mirrors `title`'s own precedent (this migration, `9ca6bf36583f`) for a column only
ever written by the Sonarr fetch paths; an episode that predates the numbering scheme or was
synthesized some other way (e.g. `import_trakt_history.py`) legitimately has neither.

Every EXISTING row gets its current `season`/`episode` copied straight into the new columns —
not a "no backfill, go-forward only" case like `title` — *for the single-show fetch path*.
That's not true for every row, though: `_fetch_sonarr_multi_show` (the Bookworm-style "one
flat tvdb series, several LCARS shows" case) writes locally-derived per-part `season`/`episode`
values that were never Sonarr's own raw numbering to begin with, so backfilling those from
`season`/`episode` would stamp the new "raw Sonarr identity" columns with a value that isn't
one. This migration can't tell *at this row* which path wrote it, but it can tell which shows
are *currently* multi-show-routed (`show_external_id` service='tvdb' shared by >1 show — the
same `sibling_ids` condition `_fetch_sonarr`/`_fetch_sonarr_multi_show` branch on) and excludes
their episodes from the backfill, leaving `sonarr_season`/`sonarr_episode` NULL there instead —
an honest "we don't know" rather than a wrong answer with no marker that it's wrong. (A show
that stops being multi-show-routed after this migration — unlinked, merged — is a known,
unresolved boundary: nothing here or in `_fetch_sonarr`'s fallback re-derives true Sonarr
identity for it retroactively. Logged, not fixed, same as the Bookworm hierarchical-subdivision
gap this whole numbering scheme sits next to.) For every other row, copying `season`/`episode`
forward is the correct, honest starting state, not a guess. A show's next real Sonarr fetch
overwrites `sonarr_season`/`sonarr_episode` for any row it re-derives (harmless — Sonarr's own
raw report hasn't changed, so it just reconfirms the same value) but never touches
`season`/`episode` for a row it already recognizes as known.

Plain (non-unique) index on (show_id, sonarr_season, sonarr_episode) — the sync lookup's own
query shape, same reasoning `ix_watch_event_show_id` already has (a lookup-support index, not
an integrity constraint; app-level logic, not the DB, is what already guards against a
duplicate insert on this key, same as the pre-existing (show_id, season, episode) path did).

Revision ID: 36bbe45d39f3
Revises: 9ca6bf36583f
Create Date: 2026-08-25

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "36bbe45d39f3"
down_revision: str | Sequence[str] | None = "9ca6bf36583f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE episode ADD COLUMN sonarr_season INTEGER")
    op.execute("ALTER TABLE episode ADD COLUMN sonarr_episode INTEGER")
    # Skip episodes of a currently multi-show-tvdb-routed show — see module docstring.
    op.execute("""
        UPDATE episode SET sonarr_season = season, sonarr_episode = episode
        WHERE show_id NOT IN (
            SELECT show_id FROM show_external_id
            WHERE service = 'tvdb' AND external_id IN (
                SELECT external_id FROM show_external_id
                WHERE service = 'tvdb'
                GROUP BY external_id
                HAVING COUNT(*) > 1
            )
        )
    """)
    op.execute(
        "CREATE INDEX ix_episode_sonarr_numbering ON episode (show_id, sonarr_season, sonarr_episode)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX ix_episode_sonarr_numbering")
    op.execute("ALTER TABLE episode DROP COLUMN sonarr_episode")
    op.execute("ALTER TABLE episode DROP COLUMN sonarr_season")
