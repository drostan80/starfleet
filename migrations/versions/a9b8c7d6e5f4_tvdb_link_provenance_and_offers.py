"""show_external_id.source (where a TVDB link came from) and tvdb_offer (who suggested which id)

2026-10-06 (user; RULEBOOK R3.7c, 8.8.5). `source` is the provenance of a link: 'you', 'fribb',
'anime-lists', 'wikidata', 'tvmaze', 'agreed:<a>+<b>' — NULL for the links that existed before
(unknown). `tvdb_offer` remembers each source's suggestion per show and id, so two independent
sources that agree can attach the id (they arrive in different passes). Additive.

Revision ID: a9b8c7d6e5f4
Revises: f8a1b2c3d4e5
Create Date: 2026-10-06 00:00:01.000000
"""

from collections.abc import Sequence

from alembic import op

revision: str = "a9b8c7d6e5f4"
down_revision: str | Sequence[str] | None = "f8a1b2c3d4e5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TABLE show_external_id ADD COLUMN source TEXT")
    op.execute(
        """
        CREATE TABLE tvdb_offer (
            show_id    TEXT NOT NULL REFERENCES show (id) ON DELETE CASCADE,
            tvdb_id    TEXT NOT NULL,
            source     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            PRIMARY KEY (show_id, tvdb_id, source)
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE tvdb_offer")
    op.execute("ALTER TABLE show_external_id DROP COLUMN source")
