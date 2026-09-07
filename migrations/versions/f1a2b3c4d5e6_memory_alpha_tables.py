"""Memory Alpha: cross-source episode reference tables (2026-09-07).

Stores the AniDB/Anime-Lists community dataset as a local index —
show-level titles in all languages, AniDB→TVDB episode offset mappings,
and (later) per-episode AniDB data. All populated from free downloads
(no API calls for the initial build).

Tables:
  anidb_anime       — one row per AniDB anime entry (aid)
  anidb_title       — multi-language titles from the titles dump
  anime_list_entry  — Anime-Lists XML rows: AniDB→TVDB season/episode mapping
  anime_list_mapping — per-episode overrides from <mapping-list>
"""

import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "a3b7c9e1f042"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # -- AniDB anime entries (from titles dump) --
    op.create_table(
        "anidb_anime",
        sa.Column("anidb_id", sa.Integer, primary_key=True),
        sa.Column("main_title", sa.Text, nullable=False),
        sa.Column("type", sa.Text),  # TV Series, OVA, Movie, etc.
        sa.Column("fetched_at", sa.Text, nullable=False),
    )

    # -- Multi-language titles (from titles dump) --
    op.create_table(
        "anidb_title",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "anidb_id",
            sa.Integer,
            sa.ForeignKey("anidb_anime.anidb_id"),
            nullable=False,
        ),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("lang", sa.Text, nullable=False),  # en, ja, x-jat, fr, ...
        sa.Column(
            "title_type",
            sa.Integer,
            nullable=False,
        ),  # 1=primary, 2=synonym, 3=short, 4=official, 5=kana
        sa.UniqueConstraint("anidb_id", "lang", "title", name="uq_anidb_title"),
    )
    op.create_index("ix_anidb_title_anidb_id", "anidb_title", ["anidb_id"])
    op.create_index("ix_anidb_title_title", "anidb_title", ["title"])

    # -- Anime-Lists XML entries (ScudLee: AniDB → TVDB mapping) --
    op.create_table(
        "anime_list_entry",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("anidb_id", sa.Integer, nullable=False),
        sa.Column("tvdb_id", sa.Text),  # numeric or 'movie'/'unknown'
        sa.Column("default_tvdb_season", sa.Integer),
        sa.Column("episode_offset", sa.Integer, nullable=False, server_default="0"),
        sa.Column("tmdb_tv", sa.Integer),
        sa.Column("tmdb_season", sa.Integer),
        sa.Column("tmdb_movie", sa.Integer),  # tmdbid attr for movies
        sa.Column("imdb_id", sa.Text),
        sa.Column("name", sa.Text),
        sa.Column(
            "source",
            sa.Text,
            nullable=False,
            server_default="community",
        ),  # community | manual | auto_matched
        sa.Column("fetched_at", sa.Text, nullable=False),
        sa.UniqueConstraint("anidb_id", name="uq_anime_list_anidb"),
    )
    op.create_index("ix_anime_list_entry_anidb_id", "anime_list_entry", ["anidb_id"])
    op.create_index("ix_anime_list_entry_tvdb_id", "anime_list_entry", ["tvdb_id"])

    # -- Per-episode mapping overrides from <mapping-list> --
    op.create_table(
        "anime_list_mapping",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column(
            "entry_id",
            sa.Integer,
            sa.ForeignKey("anime_list_entry.id"),
            nullable=False,
        ),
        sa.Column("anidb_season", sa.Integer),
        sa.Column("tvdb_season", sa.Integer),
        sa.Column("start", sa.Integer),  # range start (in anidb ep space)
        sa.Column("end", sa.Integer),  # range end
        sa.Column("offset", sa.Integer),  # offset within this range
        sa.Column(
            "episode_map", sa.Text
        ),  # ";anidb_ep-tvdb_ep;..." for individual overrides
    )
    op.create_index(
        "ix_anime_list_mapping_entry_id", "anime_list_mapping", ["entry_id"]
    )


def downgrade() -> None:
    op.drop_table("anime_list_mapping")
    op.drop_table("anime_list_entry")
    op.drop_table("anidb_title")
    op.drop_table("anidb_anime")
