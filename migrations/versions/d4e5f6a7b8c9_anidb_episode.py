"""Memory Alpha: AniDB per-episode data table (2026-09-07).

Stores episode-level data fetched from the AniDB HTTP API —
titles (EN, JA, romaji), airdates, and lengths. Keyed AniDB-side
so the data is fetched once per anime, independent of mapping
derivation.

Table:
  anidb_episode — one row per AniDB episode (regulars + specials)
"""

import sqlalchemy as sa
from alembic import op

revision = "d4e5f6a7b8c9"
down_revision = "b7d9e3f1a2c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "anidb_episode",
        sa.Column("anidb_anime_id", sa.Integer, nullable=False),
        # 0=special (AniDB type 2), 1=regular (AniDB type 1)
        sa.Column("anidb_season", sa.Integer, nullable=False),
        sa.Column("anidb_epno", sa.Integer, nullable=False),
        sa.Column("title_en", sa.Text),
        sa.Column("title_ja", sa.Text),
        sa.Column("title_romaji", sa.Text),
        sa.Column("airdate", sa.Text),  # YYYY-MM-DD
        sa.Column("length_minutes", sa.Integer),
        sa.Column("fetched_at", sa.Text, nullable=False),
        sa.PrimaryKeyConstraint("anidb_anime_id", "anidb_season", "anidb_epno"),
    )
    op.create_index(
        "ix_anidb_episode_anime_id",
        "anidb_episode",
        ["anidb_anime_id"],
    )


def downgrade() -> None:
    op.drop_table("anidb_episode")
