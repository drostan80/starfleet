"""show_synonym table + show.display_title_override — AniList synonyms read coverage + a per-show display-title pick

Two related schema additions for the AniList `synonyms` feature (NEXT_UP.md's
"last gap in LCARS's AniList read coverage"), user's request 2026-08-26.

**`show_synonym`** — AniList's `Media.synonyms` is a variable-length list of
alternative titles (alt spellings, abbreviations, other-language/regional
names, common fan names — e.g. Urusei Yatsura carries "Lamu"/"Lum the Invader
Girl"/"Those Obnoxious Aliens"), distinct from the three fixed title columns
(`title_romaji`/`title_english`/`title_native`). A child value-table rather
than a JSON blob on `show`, so it can be joined/EXISTS-searched (the `search`
query and `service_presence`'s fuzzy catalog matcher both consume it). Unlike
`primary_title` (deliberately write-once), synonyms **re-sync** on every
`refreshShowMetadata` — AniList accretes them over time — so `UNIQUE(show_id,
synonym)` makes `metadata.py`'s delete-then-insert idempotent and stops a
refresh from duplicating rows. No merge handling needed: `show_merge` only
*demotes* a loser (never deletes it, see that module's docstring), so a loser's
synonym rows keep their FK intact, and the winner re-syncs its own from AniList.

**`show.display_title_override`** — nullable free-text. `displayTitle` resolves
to this when set, else to `title_{primary_title}` as before (default unchanged:
`metadata.py` already picks english-first, romaji fallback). Lets a show be
pinned to any title the user recognizes — a synonym ("Lamu" for Urusei
Yatsura), a romaji/native title, or a hand-typed one — without touching the
write-once `primary_title`. Free-text (not a field/synonym pointer) so the
client can offer a picker over titles+synonyms *and* a type-your-own, and a
later change to the synonym list can't invalidate a stored choice.

Every existing row gets NULL / no synonyms — synonyms populate on the next real
AniList fetch (refreshShowMetadata or the next scheduled pass), the same
go-forward-capture precedent every other source-fact column here already has.

Revision ID: b3f9c2a7d1e4
Revises: 36bbe45d39f3
Create Date: 2026-08-26

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3f9c2a7d1e4"
down_revision: str | Sequence[str] | None = "36bbe45d39f3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE show_synonym (
            show_id     TEXT NOT NULL REFERENCES show (id),
            synonym     TEXT NOT NULL,
            created_at  TEXT NOT NULL,
            UNIQUE (show_id, synonym)
        )
        """
    )
    op.execute("CREATE INDEX ix_show_synonym_show_id ON show_synonym (show_id)")
    op.execute("ALTER TABLE show ADD COLUMN display_title_override TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE show DROP COLUMN display_title_override")
    op.execute("DROP INDEX ix_show_synonym_show_id")
    op.execute("DROP TABLE show_synonym")
