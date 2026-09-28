"""Sonarr episode <-> LCARS episode identity, keyed on absolute order.

2026-09-23 — Slime (s-hyj69b), found live: LCARS subdivides seasons to the
finest source (season-subdivision D1-D3), so once TVDB's season 2 became
LCARS seasons 2 *and* 3, every later LCARS season number sat one ahead of
TVDB's. Several writers still assumed "LCARS season N == Sonarr/TVDB
season N" — local_audit put Sonarr's S04 files on LCARS S4 (the 2024
season), and nothing ever recorded the real TVDB coordinates
(`sonarr_season`/`sonarr_episode`), so Memory Alpha's own Anime-Lists
resolution was fed LCARS numbers as if they were TVDB's.

The intended design: absolute episode order is the spine, every other
numbering is mapped from it. This module is the single place a Sonarr
episode is matched to an LCARS episode row, in that order:

1. Sonarr's own raw coordinates already captured on the row
   (`sonarr_season`/`sonarr_episode`) — immutable once captured.
2. `absoluteEpisodeNumber` against the row's `tvdb_absolute` (TVDB's own
   absolute number, a mapping — phase 3.1). Never against LCARS's own
   absolute number, which Memory Alpha sets (R1.2c).
3. LCARS season/episode — only for a row never captured, and only when
   absolute order can't decide it (season 0 specials, or a show/episode
   with no absolute numbers) and no captured row shows this show's
   numbering has diverged from Sonarr's. Never guesses across a known
   divergence. Season numbers follow TVDB (RULEBOOK R1.9a).

One show per TVDB id (R1.14): a Sonarr series maps to one LCARS show.

A match through 2 or 3 captures the Sonarr coordinates onto the row, so
every later lookup (and Memory Alpha's TVDB-keyed resolution) uses the
real ones.
"""

from __future__ import annotations


def sibling_show_ids_for_tvdb(conn, tvdb_id) -> list[str]:
    """Shows holding this tvdb id, excluding any show merged away into
    another (an unreversed `show_merge` loser). A merge loser keeps its
    `show_external_id` rows for reversibility, but it's no longer a real
    sibling — counting it forced Slime (merged with its own "Season 2
    Part 2" stub, 2026-09-17) through the multi-show routing path forever,
    which never captures Sonarr coordinates on existing rows."""
    rows = conn.execute(
        "SELECT sei.show_id FROM show_external_id sei"
        " WHERE sei.service = 'tvdb' AND sei.external_id = ?"
        "   AND NOT EXISTS ("
        "     SELECT 1 FROM show_merge m"
        "     WHERE m.loser_show_id = sei.show_id AND m.reversed_at IS NULL"
        "   )",
        (str(tvdb_id),),
    ).fetchall()
    return [row["show_id"] for row in rows]


def show_numbering_diverged(conn, show_id: str) -> bool:
    """True when a captured row shows this show's LCARS numbering is not
    Sonarr's (display season/episode differ from the raw Sonarr ones)."""
    row = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? AND sonarr_season IS NOT NULL"
        "   AND (sonarr_season <> season OR sonarr_episode <> episode) LIMIT 1",
        (show_id,),
    ).fetchone()
    return row is not None


def _show_has_tvdb_absolute(conn, show_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? AND kind = 'regular'"
        "   AND tvdb_absolute IS NOT NULL LIMIT 1",
        (show_id,),
    ).fetchone()
    return row is not None


def find_episode(conn, show_ids: list[str], ep: dict) -> dict | None:
    """The LCARS episode row a Sonarr episode dict is, or None. Returns
    {id, show_id, season_id, season, episode} — see module docstring for
    the lookup order."""
    season_number = ep.get("seasonNumber")
    episode_number = ep.get("episodeNumber")
    if not show_ids or season_number is None or episode_number is None:
        return None
    placeholders = ",".join("?" for _ in show_ids)
    cols = "id, show_id, season_id, season, episode, sonarr_season"

    row = conn.execute(
        f"SELECT {cols} FROM episode WHERE show_id IN ({placeholders})"
        "   AND sonarr_season = ? AND sonarr_episode = ?",
        (*show_ids, season_number, episode_number),
    ).fetchone()
    if row is not None:
        return dict(row)

    abs_number = ep.get("absoluteEpisodeNumber")
    if abs_number is not None:
        rows = conn.execute(
            f"SELECT {cols} FROM episode WHERE show_id IN ({placeholders})"
            "   AND tvdb_absolute = ? AND sonarr_season IS NULL",
            (*show_ids, int(abs_number)),
        ).fetchall()
        if len(rows) == 1:
            return _capture(conn, dict(rows[0]), season_number, episode_number)
        if len(rows) > 1:
            return None  # ambiguous — never guess
    if len(show_ids) != 1:
        return None  # display numbering restarts per sibling — meaningless across shows
    if season_number != 0:
        if show_numbering_diverged(conn, show_ids[0]):
            return None
        if abs_number is not None and _show_has_tvdb_absolute(conn, show_ids[0]):
            return None  # TVDB's absolute numbers are known here: no match means a new episode
    row = conn.execute(
        f"SELECT {cols} FROM episode WHERE show_id = ? AND season = ? AND episode = ?"
        "   AND sonarr_season IS NULL",
        (show_ids[0], season_number, episode_number),
    ).fetchone()
    if row is None:
        return None
    return _capture(conn, dict(row), season_number, episode_number)


def _capture(conn, row: dict, sonarr_season: int, sonarr_episode: int) -> dict:
    conn.execute(
        "UPDATE episode SET sonarr_season = ?, sonarr_episode = ?"
        " WHERE id = ? AND sonarr_season IS NULL",
        (sonarr_season, sonarr_episode, row["id"]),
    )
    row["sonarr_season"] = sonarr_season
    return row


def route_new_episode(conn, show_id: str, ep: dict) -> tuple[int, int]:
    """LCARS (season, episode) for a Sonarr episode with no LCARS row yet:
    TVDB's own, since season numbers follow TVDB (RULEBOOK R1.9a, phase
    3.1). Where the episode belongs among levels is decided by its absolute
    number and the spans (Memory Alpha), never by renumbering seasons."""
    return ep["seasonNumber"], ep["episodeNumber"]
