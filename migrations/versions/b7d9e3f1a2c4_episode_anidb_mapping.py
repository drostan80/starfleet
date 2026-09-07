"""Memory Alpha: episode ↔ AniDB mapping table (2026-09-07).

Links individual LCARS episodes to their AniDB anime entry and derived
AniDB absolute episode number, computed from the Anime-Lists offset data.

Table:
  episode_anidb_mapping — one row per mapped episode
"""

import sqlalchemy as sa
from alembic import op

revision = "b7d9e3f1a2c4"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "episode_anidb_mapping",
        sa.Column("episode_id", sa.Text, sa.ForeignKey("episode.id"),
                  primary_key=True),
        sa.Column("anidb_anime_id", sa.Integer, nullable=False),
        sa.Column("anidb_season", sa.Integer, nullable=False,
                  server_default="1"),  # 0=special, 1=regular
        sa.Column("anidb_epno", sa.Integer),  # derived AniDB absolute ep
        sa.Column("confidence", sa.Text, nullable=False,
                  server_default="auto"),  # auto | manual | ambiguous
        sa.Column("created_at", sa.Text, nullable=False),
    )
    op.create_index("ix_episode_anidb_mapping_anidb_anime_id",
                     "episode_anidb_mapping", ["anidb_anime_id"])


def downgrade() -> None:
    op.drop_table("episode_anidb_mapping")
