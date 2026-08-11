"""show_merge table — B.14, cross-service show-duplicate merge

B.11f's own "dropped shows still appear" investigation (BUILD_PLAN.md)
surfaced a real, distinct-from-everything-else-fixed-that-day gap: a
pair of `show` rows for the *same real-world anime*, one created from
Sonarr's own tvdb id, one from an AniList-list sweep, sharing no
external id at all — so neither B.11d's `find_existing_show` (same-
external-id collision only) nor a `GROUP BY service, external_id`
audit query can ever see them as duplicates. Measured live before
building anything (not guessed): 102 tracked shows with a `tvdb` link
and no `anilist` link, 1172 tracked `tracking_space = 'anime'` shows
with an `anilist` link and no `tvdb` link, cross-referenced by
normalized title — 2 real pairs ("Black Lagoon", "Mebius Dust"). Small
enough that this table's own first sweep only ever touches 2 rows; it
exists to catch the next ones, not to fix a backlog.

Confirmed with the user this is genuinely new work, not a resumption
of B.7 (`show_service_presence`'s own periodic refresh) — B.7's own
BUILD_PLAN.md entry explicitly scoped itself to "local + Sonarr +
Radarr, the three genuinely buildable now" and named AniList's missing
search/catalog-listing endpoint as the reason it couldn't cover this
exact case. User's own explicit direction, given after that correction
and after a live check surfaced a real conflict (below): "Make this a
real new step now (e.g. B.14)".

**Trigger model** (confirmed with the user): automatic sweep, same
shape every other Phase B poller already uses (`fuzzy.best_match()`,
§5.4/A.7, unchanged — same exact-then-threshold-gated shape, same
0.72 threshold, `service_presence.py`'s own precedent for reusing it
verbatim against a fresh domain). Real N×M cost (every tvdb-only show's
titles against every anilist-only anime show's titles) — rides B.2's
existing monthly tier, not the hourly one, same "confirmed with the
user, real N×M cost" precedent B.7's own catalog-matching sweep
already established (`run_monthly_once`, ops/scheduler.py).

**Merge direction** (confirmed with the user): the AniList-linked show
wins — becomes the surviving, canonical row. This is what makes the
operation genuinely need reversibility and a real manifest, not just a
link-addition: the *loser* (Sonarr/tvdb-sourced) row is very often the
one holding the real episode/watch-history data (Mebius Dust: 8 real
episode rows + 1 downloaded, 0 on the AniList-sourced row) — checked
live before assuming "AniList wins" meant "AniList row's episodes
matter more." It doesn't; the winner keeps its own status/score/
tracking-state fields, but episode/watch/availability data migrates
onto whichever row survives regardless of which side originally held
it (`show_merge.merge_shows()`'s own docstring has the full per-table
reasoning — every table `export_import.py`'s own `EXPORT_IMPORT_TABLES`
lists as show-scoped gets walked).

**Reversible, without resurrecting anything**: the loser row is never
deleted — same vocabulary `_promote_stub` (`shows.py`, B.11d) already
established for "exists, `tracked = 0`, not really a real show right
now." A merge only ever reassigns the loser's *child* rows onto the
winner (external ids, episodes, seasons, history, credits, ...) and
flips `tracked = 0` on the loser itself. `manifest` (JSON) records
precisely what moved and what was left behind on genuine per-slot
conflicts (e.g. both rows independently had their own season 1) — a
human-readable `skipped` list plus exact ids/keys for every table that
did move, so `reverseShowMerge` can move each one back verbatim and
flip `tracked` back to 1, rather than attempting a generic "undo the
last N statements" mechanism.

**Reviewable**: `pending_review` (§5.6) is single-field/value-chain
shaped (`entity_type`/`field`/`previous_value`/`proposed_value_chain`)
— a genuine fit for "this field changed," not for "these two rows
became one, here is the full child-row manifest." A dedicated table is
the honest shape, same reasoning `untracked_show_finding` (`u-`, B.11e)
already used rather than overloading `pending_review` for a
structurally different kind of event.

Prefixed id (`y-`, next free letter, §5.0) — an individually
addressable, listable record a client reads by id, same shape
`show_service_presence`/`untracked_show_finding` already established.
No natural-key UNIQUE constraint (unlike `untracked_show_finding`'s
`(service, external_id)`) — a given (winner, loser) pair is only ever
merged once in practice (the loser stops matching any future sweep's
candidate query the moment it's demoted to `tracked = 0`), so there's
no upsert target to enforce.

Revision ID: fe1f556c14f9
Revises: d5858f23ed73
Create Date: 2026-08-11 00:00:00.000000

"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "fe1f556c14f9"
down_revision: str | None = "d5858f23ed73"
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
        CREATE TABLE show_merge (
            id                    TEXT PRIMARY KEY CHECK ({_id_check("id", "y")}),
            winner_show_id        TEXT NOT NULL REFERENCES show (id),
            loser_show_id         TEXT NOT NULL REFERENCES show (id),
            matched_on            TEXT NOT NULL,
            manifest              TEXT NOT NULL,  -- JSON: {{"moved": {{...}}, "skipped": [...]}}
            merged_at             TEXT NOT NULL,
            reversed_at           TEXT,
            reversed_by_client    TEXT
        )
    """)
    op.execute("CREATE INDEX ix_show_merge_winner_show_id ON show_merge (winner_show_id)")
    op.execute("CREATE INDEX ix_show_merge_loser_show_id ON show_merge (loser_show_id)")


def downgrade() -> None:
    op.execute("DROP TABLE show_merge")
