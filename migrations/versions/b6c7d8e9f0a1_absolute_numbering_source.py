"""absolute numbering source + change log; the engine writes spans

2026-09-28, PLAN-CODE phase 3.2 (RULEBOOK R1.2c, R1.2d). Memory Alpha's
numbering engine (`lcars/numbering.py`) sets every absolute number and every
TVDB season's spans.

- `show.absolute_numbering_source` (`anidb` | `tvmaze` | `tvdb`): which source
  numbered the show. `tvdb` = TVDB order + air date, the R1.2d fallback,
  reconciled when AniDB/TVmaze has the data.
- `absolute_number_change`: every number a reconciliation changed (not the
  first numbering of a show), for the user's review.
- The phase 2 triggers copying `abs_start/abs_end` into spans go: the engine
  writes spans (several per season when something not mapped to it sits
  inside, R1.12) and keeps `abs_start/abs_end` as their min/max for old
  readers.

Revision ID: b6c7d8e9f0a1
Revises: a5b6c7d8e9f0
Create Date: 2026-09-28 00:00:02.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: str | Sequence[str] | None = "a5b6c7d8e9f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE show ADD COLUMN absolute_numbering_source TEXT"
        " CHECK (absolute_numbering_source IN ('anidb', 'tvmaze', 'tvdb'))"
    )
    op.execute(
        """
        CREATE TABLE absolute_number_change (
            episode_id  TEXT NOT NULL REFERENCES episode (id) ON DELETE CASCADE,
            show_id     TEXT NOT NULL REFERENCES show (id),
            old_number  REAL,
            new_number  REAL,
            source      TEXT NOT NULL,
            changed_at  TEXT NOT NULL
        )
        """
    )
    op.execute("CREATE INDEX ix_absolute_number_change_show ON absolute_number_change (show_id)")
    op.execute("DROP TRIGGER season_span_from_abs_insert")
    op.execute("DROP TRIGGER season_span_from_abs_update")


def downgrade() -> None:
    op.execute(
        """
        CREATE TRIGGER season_span_from_abs_insert AFTER INSERT ON season
        WHEN NEW.abs_start IS NOT NULL AND NEW.abs_end IS NOT NULL
        BEGIN
            INSERT INTO season_span (season_id, abs_from, abs_to)
            VALUES (NEW.id, NEW.abs_start, NEW.abs_end);
        END
        """
    )
    op.execute(
        """
        CREATE TRIGGER season_span_from_abs_update AFTER UPDATE OF abs_start, abs_end ON season
        BEGIN
            DELETE FROM season_span WHERE season_id = NEW.id;
            INSERT INTO season_span (season_id, abs_from, abs_to)
            SELECT NEW.id, NEW.abs_start, NEW.abs_end
            WHERE NEW.abs_start IS NOT NULL AND NEW.abs_end IS NOT NULL;
        END
        """
    )
    op.execute("DROP TABLE absolute_number_change")
    op.execute("ALTER TABLE show DROP COLUMN absolute_numbering_source")
