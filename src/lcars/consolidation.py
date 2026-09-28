"""Same-TVDB show consolidation and films — the rebuild's merge list (PLAN-CODE phase 5).

RULEBOOK R1.14: one show per TVDB id. The 09-06 snapshot has shows that share
one (a cour or a season added as its own show). This plans, for your review,
folding each group into one show — it writes nothing:

- the **winner** is the show holding TVDB season 1 (Fribb), else the oldest;
- every other show becomes a level of the winner (at 09-06 they hold no
  episodes — only season rows with AniList/MAL ids); any episode they do hold
  moves onto the winner at **TVDB season/episode**:
  the Sonarr coordinates already captured on the episode, else Sonarr's own
  episode with the same absolute number (at 09-06 LCARS's absolute number was
  still Sonarr's), else Fribb's placement of that show's AniList entry (TVDB
  season + episode offset); an episode none of them places is listed;
- an episode landing where the winner already has one is a **collision**,
  listed, never resolved here;
- each moved show's AniList/MAL ids go through the add check afterwards (part /
  link season / new season) — listed as the season they'd become.

Films (R1.4, R1.13a): a tracked movie show whose AniList entry Fribb maps to a
tracked show's TVDB id (a film of that show) is listed for folding in as its
own level. Nothing is folded automatically.
"""

from __future__ import annotations

import json
import sqlite3
import sys

from lcars import fribb, season_ranges

_TITLE = "COALESCE(display_title_override, title_english, title_romaji, title_native)"


def _groups(conn) -> dict[str, list[str]]:
    rows = conn.execute(
        "SELECT x.external_id, x.show_id FROM show_external_id x JOIN show sh ON sh.id = x.show_id"
        " WHERE x.service = 'tvdb' AND sh.tracked = 1 ORDER BY sh.created_at"
    ).fetchall()
    groups: dict[str, list[str]] = {}
    for tvdb_id, show_id in rows:
        groups.setdefault(tvdb_id, []).append(show_id)
    return {k: v for k, v in groups.items() if len(v) > 1}


def _fribb_place(anilist_index, anilist_id) -> tuple[int | None, int]:
    """(TVDB season, episode offset) Fribb gives an AniList entry."""
    if anilist_id is None:
        return None, 0
    entries = anilist_index.get(int(anilist_id), [])
    if len(entries) != 1:
        return None, 0
    e = entries[0]
    return (e.get("season") or {}).get("tvdb"), (e.get("episode_offset") or {}).get("tvdb") or 0


def _becomes(conn, winner: str, season: int | None, offset: int) -> str:
    """The level the moved show's season becomes on the winner (the add
    check's rules: R1.10 part, link season, new TVDB season, R1.13a piece)."""
    if season is None:
        return "for you — Fribb gives no TVDB season"
    if season == 0:
        return "season-0 piece (placed by Memory Alpha)"
    row = conn.execute(
        "SELECT anilist_id, mal_id FROM season WHERE show_id = ? AND season_number = ?"
        " AND kind = 'tvdb_season'",
        (winner, season),
    ).fetchone()
    if row is None:
        return f"new TVDB season {season}"
    if row[0] is None and row[1] is None:
        return f"TVDB season {season} (its AniList/MAL id)"
    return f"part of TVDB season {season} (cour from episode {offset + 1})"


def plan(conn, dataset: list[dict], sonarr_episodes: dict) -> dict:
    """`sonarr_episodes`: TVDB id (str) → Sonarr's episode list, or None."""
    anilist_index = fribb.build_anilist_index(dataset)
    out = {"groups": [], "films": []}
    winners: dict[str, str] = {}
    for tvdb_id, show_ids in _groups(conn).items():
        places = {sid: _fribb_place(anilist_index,
                                    season_ranges.show_list_id(conn, sid, "anilist"))
                  for sid in show_ids}
        winner = next((sid for sid in show_ids if places[sid][0] == 1), show_ids[0])
        sonarr_by_abs = {}
        for ep in sonarr_episodes.get(tvdb_id) or []:
            if ep.get("absoluteEpisodeNumber") is not None and ep["seasonNumber"] > 0:
                sonarr_by_abs[float(ep["absoluteEpisodeNumber"])] = (
                    ep["seasonNumber"], ep["episodeNumber"])
        taken = {(r[0], r[1]) for r in conn.execute(
            "SELECT COALESCE(sonarr_season, season), COALESCE(sonarr_episode, episode)"
            " FROM episode WHERE show_id = ?", (winner,))}
        group = {"tvdb_id": tvdb_id, "winner": winner, "shows": []}
        for sid in show_ids:
            if sid == winner:
                continue
            title = conn.execute(f"SELECT {_TITLE} FROM show WHERE id = ?", (sid,)).fetchone()[0]
            season, offset = places[sid]
            moved, unplaced, collisions = [], [], []
            for eid, s, e, ss, se, absn, state in conn.execute(
                "SELECT id, season, episode, sonarr_season, sonarr_episode, absolute_number, state"
                " FROM episode WHERE show_id = ? ORDER BY season, episode", (sid,)
            ).fetchall():
                if ss is not None:
                    target, how = (ss, se), "sonarr coordinates"
                elif absn is not None and float(absn) in sonarr_by_abs:
                    target, how = sonarr_by_abs[float(absn)], "sonarr absolute"
                elif season and s == 1:
                    target, how = (season, e + offset), "fribb"
                else:
                    unplaced.append({"episode_id": eid, "at": f"S{s}E{e}", "state": state})
                    continue
                entry = {"episode_id": eid, "from": f"S{s}E{e}",
                         "to": f"S{target[0]}E{target[1]}", "how": how, "state": state}
                (collisions if target in taken else moved).append(entry)
                taken.add(target)
            group["shows"].append({
                "show_id": sid, "title": title, "fribb_season": season, "offset": offset,
                "moved": moved, "unplaced": unplaced, "collisions": collisions,
                "becomes": _becomes(conn, winner, season, offset),
            })
        out["groups"].append(group)
        winners[tvdb_id] = winner

    tracked_tvdb = {r[0]: r[1] for r in conn.execute(
        "SELECT x.external_id, x.show_id FROM show_external_id x JOIN show sh ON sh.id = x.show_id"
        " WHERE x.service = 'tvdb' AND sh.tracked = 1 AND sh.media_shape = 'episodic'")}
    for (sid,) in conn.execute(
        "SELECT id FROM show WHERE tracked = 1 AND media_shape = 'movie'"
    ).fetchall():
        anilist_id = season_ranges.show_list_id(conn, sid, "anilist")
        entries = anilist_index.get(int(anilist_id), []) if anilist_id else []
        for e in entries:
            key = str(e.get("tvdb_id"))
            parent = winners.get(key) or tracked_tvdb.get(key)
            if parent is not None:
                title = conn.execute(f"SELECT {_TITLE} FROM show WHERE id = ?", (sid,)).fetchone()
                out["films"].append({"show_id": sid, "title": title[0], "into": parent,
                                     "tvdb_id": e.get("tvdb_id")})
                break
    return out


def main(argv=None) -> int:
    """lcars consolidation <db> <sonarr-episodes.json> — prints the plan as JSON."""
    argv = sys.argv[1:] if argv is None else argv
    conn = sqlite3.connect(argv[0])
    sonarr_episodes = json.load(open(argv[1])) if len(argv) > 1 else {}
    print(json.dumps(plan(conn, fribb.load_dataset(), sonarr_episodes), ensure_ascii=False,
                     indent=1))
    return 0
