"""Levels follow the episodes (RULEBOOK R1.10, R1.13b, R1.17, R1.2f, R1.9a, R3.6).

An AniList/MAL entry belongs where its episodes are. For every tracked anime show with a TVDB id
this finds the entries no level places yet — an entry nobody holds, or one held by a leftover
level with no episodes — and decides, at episode level:

  1. Sonarr/TVDB has the episodes: the entry is a **part of the TVDB season those episodes sit
     in**. Its start date (as a calendar date in Japan, where AniList counts it) must be the air
     date of an episode of the season; an entry AniList gives no start date is placed by count
     (the season's free episodes number exactly its episodes). A leftover level is converted in
     place (same id: status, history, list baselines, locks). The season itself keeps the
     episodes and no list id (Dr. STONE S4 is the reference).
  2. TVDB does not have them yet: a **season level only**, planned, numbered as Fribb gives it
     when that is the next TVDB season (never any other number, R1.9a), until Sonarr has the
     episodes — then step 1 places it.
  3. Anything else (the dates and counts disagree, or the season has no spans yet) is left as it
     is and reported — no review: nobody can answer it better than the episodes will.

A level the user placed by hand (`source = 'manual'` or `manual_override`) is never moved.
AniList facts (start date, episode count) are fetched only for shows that have an unplaced entry
and are remembered for six hours, so a settled show costs no call.
"""

from __future__ import annotations

import datetime as dt
import logging
import time

from lcars import air_time, anilist_client, fribb, ids, level_parts, season_ranges, util

log = logging.getLogger(__name__)

_FACTS_TTL = 6 * 3600
_facts_cache: dict[int, tuple[float, dict | None]] = {}

_QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) { id status episodes startDate { year month day } }
  }
}
"""


def _start(media: dict) -> dt.date | None:
    d = media.get("startDate") or {}
    if d.get("year") and d.get("month") and d.get("day"):
        return dt.date(d["year"], d["month"], d["day"])
    return None


def facts_for(anilist_ids) -> dict[int, dict | None]:
    """{id: {"start": date|None, "episodes": int|None, "status": str} | None (AniList has no such
    id)}. Raises anilist_client.AniListError when AniList can't be reached."""
    now = time.time()
    wanted = [int(i) for i in dict.fromkeys(anilist_ids)]
    need = [i for i in wanted if i not in _facts_cache or now - _facts_cache[i][0] > _FACTS_TTL]
    for k in range(0, len(need), 50):
        batch = need[k:k + 50]
        data = anilist_client._graphql_request(_QUERY, {"ids": batch}, token=None, client=None)
        got = {m["id"]: m for m in (data.get("Page") or {}).get("media") or []}
        for i in batch:
            m = got.get(i)
            _facts_cache[i] = (
                now,
                None if m is None else
                {"start": _start(m), "episodes": m.get("episodes"), "status": m.get("status")},
            )
    return {i: _facts_cache[i][1] for i in wanted}


def jst_date(air_date_utc: str) -> dt.date:
    """AniList counts a start date as the calendar date in Japan; Sonarr stores UTC."""
    return air_time.jst_date(air_date_utc)


def _is_leftover(conn, z) -> bool:
    """A level that holds an entry but nothing else: no episode, no span, no child. A TVDB-season
    one the user placed by hand is never moved; a special level that holds a TV entry (Durarara!!×2
    Shou, Hozuki 2) was made for want of a place, and TVDB's episodes now give it one."""
    if z["anilist_id"] is None:
        return False
    if z["kind"] == "special":
        return not level_parts._busy(conn, z)
    if z["kind"] != "tvdb_season" or z["source"] == "manual" or z["manual_override"]:
        return False
    if conn.execute(
        "SELECT 1 FROM episode WHERE show_id = ? AND (season_id = ? OR season = ?) LIMIT 1",
        (z["show_id"], z["id"], z["season_number"]),
    ).fetchone():
        return False
    return not level_parts._busy(conn, z)


def _show_entries(conn, show_id: str, tvdb_id: int, index: dict) -> list[dict]:
    """The show's entries in Fribb's order: ids and Fribb's season hint."""
    out = []
    for c in sorted(
        (c for c in index.get(tvdb_id, [])
         if c.get("type", "TV") in ("TV", "ONA") and (c.get("season") or {}).get("tvdb", 0) > 0),
        key=lambda c: (c["season"]["tvdb"], (c.get("episode_offset") or {}).get("tvdb") or 0),
    ):
        aid, mal = fribb.extract_ids(c)
        if aid is not None:
            out.append({"anilist_id": int(aid), "mal_id": mal, "hint": c["season"]["tvdb"]})
    return out


def plan_show(conn, show_id: str, tvdb_id: int, index: dict, facts=facts_for) -> dict:
    """What to do for one show: {"parts": [(parent_row, [entries])], "future": [entry],
    "left": [(anilist_id, reason)]}. Nothing is written."""
    out: dict = {"parts": [], "links": [], "future": [], "left": []}
    entries = _show_entries(conn, show_id, tvdb_id, index)
    if not entries:
        return out
    levels = [
        z for z in conn.execute(
            "SELECT * FROM season WHERE show_id = ? AND kind IN ('tvdb_season', 'part')"
            " AND season_number > 0", (show_id,)
        )
    ]
    held = {z["anilist_id"]: z for z in levels if z["anilist_id"] is not None}
    for sp in conn.execute(  # a special level holding an entry places it unless it is a leftover
        "SELECT * FROM season WHERE show_id = ? AND kind = 'special' AND anilist_id IS NOT NULL",
        (show_id,),
    ):
        held.setdefault(sp["anilist_id"], sp)
    unplaced = [
        e for e in entries
        if e["anilist_id"] not in held or (held[e["anilist_id"]] is not None
                                           and _is_leftover(conn, held[e["anilist_id"]]))
    ]
    if not unplaced:
        return out
    by_season: dict[int, list] = {}
    for ep in conn.execute(
        "SELECT season, absolute_number, air_date_utc, air_precision, air_local_date FROM episode"
        " WHERE show_id = ? AND season > 0"
        " AND kind = 'regular' AND absolute_number IS NOT NULL ORDER BY absolute_number",
        (show_id,),
    ):
        by_season.setdefault(ep["season"], []).append(ep)
    parents = {
        z["season_number"]: z for z in levels
        if z["kind"] == "tvdb_season" and z["season_number"] in by_season
    }
    holders_in = {  # settled entries per TVDB season (the season's own, or its parts')
        n: [z for z in levels if z["season_number"] == n and z["anilist_id"] is not None
            and not _is_leftover(conn, z)]
        for n in parents
    }
    wanted = [e["anilist_id"] for e in unplaced] + [
        z["anilist_id"] for hs in holders_in.values() for z in hs
    ]
    f = facts(wanted)
    last_tvdb = max(by_season, default=0)
    placed: dict[int, list] = {}
    undated: list[tuple[dict, dict]] = []
    for e in unplaced:
        fa = f.get(e["anilist_id"])
        if fa is None:
            out["left"].append((e["anilist_id"], "AniList has no such entry"))
            continue
        target = start_abs = None
        if fa["start"] is not None:  # the first episode that aired on its start date, in Japan
            for n, eps in by_season.items():
                for ep in eps:
                    if ep["air_date_utc"] and air_time.day_in_japan(
                        ep["air_date_utc"], ep["air_precision"], ep["air_local_date"]
                    ) == fa["start"]:
                        target, start_abs = n, ep["absolute_number"]
                        break
                if target is not None:
                    break
        if target is None:
            undated.append((e, fa))
            continue
        placed.setdefault(target, []).append(
            {**e, "count": fa["episodes"], "start_abs": start_abs, "total": fa["episodes"],
             "row": held.get(e["anilist_id"])}
        )
    unresolved: list[tuple[dict, dict]] = []
    for e, fa in undated:  # no usable start date: the season whose free episodes are its own
        target = _fit_by_count(conn, e, fa, by_season, holders_in, f, placed)
        if target is None:
            if e["hint"] == last_tvdb + 1 or (
                held.get(e["anilist_id"]) is not None
                and held[e["anilist_id"]]["season_number"] == e["hint"] > last_tvdb
            ):
                out["future"].append(e)  # TVDB has no such season yet: a season level only
            else:
                unresolved.append((e, fa))
            continue
        placed.setdefault(target, []).append(
            {**e, "count": fa["episodes"], "start_abs": None, "total": fa["episodes"],
             "row": held.get(e["anilist_id"])}
        )
    # Entries whose dates miss (TVDB and AniList count a broadcast day differently) but that
    # together are exactly the free episodes of the season Fribb puts them in: cours of one
    # season, in Fribb's release order (Dungeon S4 11+11, Yuki Yuna S2 6+6, JoJo S5 12+26).
    groups: dict[int, list] = {}
    for e, fa in unresolved:
        groups.setdefault(e["hint"], []).append((e, fa))
    for k, group in groups.items():
        counts = [fa["episodes"] for _, fa in group]
        if (k in by_season and all(counts) and len(group) > 1 and sum(counts) == _free(
                conn, k, by_season, holders_in, f, placed)):
            for e, fa in group:
                placed.setdefault(k, []).append(
                    {**e, "count": fa["episodes"], "start_abs": None, "total": fa["episodes"],
                     "row": held.get(e["anilist_id"])}
                )
        else:
            out["left"].extend(
                (e["anilist_id"], "no episode of the show fits its start date or episode count")
                for e, _ in group
            )
    for n in [n for n in placed if n not in parents]:
        out["left"].extend(
            (x["anilist_id"], f"TVDB season {n} has no level yet") for x in placed.pop(n)
        )
    order = {e["anilist_id"]: i for i, e in enumerate(entries)}
    for n, new in sorted(placed.items()):
        parent = parents[n]
        existing_parts = conn.execute(
            "SELECT 1 FROM season WHERE parent_id = ? AND kind = 'part' LIMIT 1", (parent["id"],)
        ).fetchone()
        group = list(new)
        if parent["anilist_id"] is not None and not existing_parts:  # the season's own entry
            fa = f.get(parent["anilist_id"])
            if fa is None:
                out["left"].extend((x["anilist_id"], "its season's own entry is unknown to AniList")
                                   for x in new)
                continue
            first = by_season[n][0]["absolute_number"]
            group.append({
                "anilist_id": parent["anilist_id"], "mal_id": parent["mal_id"], "row": parent,
                "count": fa["episodes"], "total": fa["episodes"], "hint": n,
                "start_abs": first,
            })
        group.sort(key=lambda x: order.get(x["anilist_id"], len(order)))  # Fribb: release order
        if len(group) == 1 and parent["anilist_id"] is None and not existing_parts:
            out["links"].append((parent, group[0]))  # the season's only entry: the season holds it
            continue
        try:
            p = level_parts.plan(conn, parent, group)
        except level_parts.Refused as r:
            out["left"].extend((x["anilist_id"], str(r)) for x in new)
            continue
        out["parts"].append((parent, p))
    return out


def _free(conn, n, by_season, holders_in, f, placed) -> int:
    """Episodes of TVDB season `n` no entry takes yet: what the season has, less what its settled
    entries and the entries already placed in it take."""
    taken = sum((x["count"] or 0) for x in placed.get(n, []))
    for z in holders_in.get(n, []):
        if z["kind"] == "part":
            taken += _span_count(conn, z, by_season[n])
        else:
            taken += (f.get(z["anilist_id"]) or {}).get("episodes") or 0
    return len(by_season[n]) - taken


def _fit_by_count(conn, e, fa, by_season, holders_in, f, placed) -> int | None:
    """An entry with no usable start date: the season whose free episodes are exactly its own."""
    n_ep = fa["episodes"]
    if not n_ep:
        return None
    fits = [n for n in by_season if _free(conn, n, by_season, holders_in, f, placed) == n_ep]
    if e["hint"] in fits:
        return e["hint"]
    return fits[0] if len(fits) == 1 else None


def _span_count(conn, part, eps) -> int:
    spans = conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (part["id"],)
    ).fetchall()
    return sum(1 for ep in eps if any(a <= ep["absolute_number"] <= b for a, b in spans))


def reconcile_all(conn, *, apply: bool = True, index: dict | None = None, facts=facts_for) -> dict:
    """The Memory Alpha step. Returns counts and, for a dry run, the whole plan."""
    if index is None:
        index = fribb.build_tvdb_index(fribb.load_dataset())
    summary = {"shows": 0, "parts_made": 0, "linked": 0, "future_levels": 0, "left": 0, "plan": [],
               "changed": []}
    now = util.now_utc_iso()
    for row in conn.execute(
        "SELECT sh.id AS show_id, x.external_id AS tvdb_id FROM show sh"
        " JOIN show_external_id x ON x.show_id = sh.id AND x.service = 'tvdb'"
        " WHERE sh.tracked = 1 AND sh.tracking_space = 'anime'"
    ).fetchall():
        try:
            tvdb_id = int(row["tvdb_id"])
        except (TypeError, ValueError):
            continue
        try:
            p = plan_show(conn, row["show_id"], tvdb_id, index, facts)
        except anilist_client.AniListError:
            log.warning("level_reconcile: AniList unreachable, skipped the pass")
            break
        except Exception:
            log.exception("level_reconcile: planning show=%s failed", row["show_id"])
            continue
        if not (p["parts"] or p["links"] or p["future"] or p["left"]):
            continue
        summary["shows"] += 1
        summary["left"] += len(p["left"])
        summary["plan"].append({
            "show_id": row["show_id"], "future": p["future"], "left": p["left"],
            "links": [(pa["season_number"], x["anilist_id"]) for pa, x in p["links"]],
            "parts": [
                (pa["season_number"], [{**x, "row": None} for x in pl["entries"]])
                for pa, pl in p["parts"]
            ],
        })
        if not apply:
            continue
        try:
            changed = False
            for _parent, pl in p["parts"]:
                made = level_parts.apply(conn, pl)
                summary["parts_made"] += len(made)
                changed = changed or bool(made)
            for parent, x in p["links"]:
                level_parts.link_whole(conn, parent, x)
                summary["linked"] += 1
                changed = True
            for e in p["future"]:
                if _make_future_level(conn, row["show_id"], e, now):
                    summary["future_levels"] += 1
                    changed = True
            if changed:
                summary["changed"].append(row["show_id"])
            conn.commit()
        except Exception:
            log.exception("level_reconcile: applying show=%s failed", row["show_id"])
            conn.rollback()
    return summary


def _make_future_level(conn, show_id: str, e: dict, now: str) -> bool:
    """A season TVDB doesn't have yet: a season level, planned, at the number Fribb gives it."""
    if conn.execute(
        "SELECT 1 FROM season WHERE show_id = ? AND (anilist_id = ? OR (season_number = ?"
        " AND kind = 'tvdb_season'))", (show_id, e["anilist_id"], e["hint"]),
    ).fetchone():
        return False  # a leftover level already holds it at that number: it stays
    season_id = ids.generate_id(conn, "z")
    status, list_sync = season_ranges.auto_season_fields(conn, show_id, e["hint"], e["anilist_id"])
    conn.execute(
        "INSERT INTO season (id, show_id, season_number, status, anilist_id, mal_id, source,"
        " matched, manual_override, last_reconciled_at, created_at, updated_at, list_sync)"
        " VALUES (?, ?, ?, ?, ?, ?, 'fribb', 1, 0, ?, ?, ?, ?)",
        (season_id, show_id, e["hint"], status, e["anilist_id"], e["mal_id"], now, now, now,
         list_sync),
    )
    season_ranges.upsert_season_external_id(conn, season_id, e["anilist_id"], e["mal_id"], now)
    return True


def make_part(conn, season_id: str, parent_number: int) -> str:
    """The show page's "make this a part of season N" (the manual way to the same result).

    The level holding the entry becomes a part of the show's TVDB season `parent_number`, cut from
    the season's free episodes by the entry's episode count (AniList). Marked as placed by hand,
    so the reconciler never moves it back. Raises level_parts.Refused with the reason when the
    episodes can't take it. Returns the part's id; the caller commits."""
    z = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if z is None or z["show_id"] is None:
        raise level_parts.Refused("no such season level")
    if z["kind"] not in ("tvdb_season", "special") or z["anilist_id"] is None:
        raise level_parts.Refused("only a level that holds an AniList entry can become a part")
    parent = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND season_number = ?",
        (z["show_id"], parent_number),
    ).fetchone()
    if parent is None:
        raise level_parts.Refused(f"the show has no TVDB season {parent_number}")
    if parent["id"] == z["id"]:
        raise level_parts.Refused("that is this season itself")
    if level_parts._busy(conn, z):
        raise level_parts.Refused("this level has episodes or parts of its own")
    own = parent["anilist_id"] is not None and not conn.execute(
        "SELECT 1 FROM season WHERE parent_id = ? AND kind = 'part' LIMIT 1", (parent["id"],)
    ).fetchone()
    try:
        f = facts_for([z["anilist_id"]] + ([parent["anilist_id"]] if own else []))
    except anilist_client.AniListError as e:
        raise level_parts.Refused(f"AniList could not be reached: {e}") from e
    entry = {"anilist_id": z["anilist_id"], "mal_id": z["mal_id"], "row": z, "start_abs": None,
             "count": (f.get(z["anilist_id"]) or {}).get("episodes"),
             "total": (f.get(z["anilist_id"]) or {}).get("episodes")}
    group = [entry]
    if own:  # the season's own entry becomes its part 1, ahead of this one unless it started later
        pf = f.get(parent["anilist_id"]) or {}
        mine = f.get(z["anilist_id"]) or {}
        first = {"anilist_id": parent["anilist_id"], "mal_id": parent["mal_id"], "row": parent,
                 "start_abs": None, "count": pf.get("episodes"), "total": pf.get("episodes")}
        later = pf.get("start") and mine.get("start") and mine["start"] < pf["start"]
        group = [entry, first] if later else [first, entry]
    if len(group) == 1 and parent["anilist_id"] is None and not conn.execute(
        "SELECT 1 FROM season WHERE parent_id = ? AND kind = 'part' LIMIT 1", (parent["id"],)
    ).fetchone():  # nothing else shares the season: it holds the entry itself, as a season does
        return level_parts.link_whole(conn, parent, entry)
    p = level_parts.plan(conn, parent, group)
    made = level_parts.apply(conn, p)
    part_id = made[[e["row"]["id"] if e["row"] is not None else None for e in p["entries"]]
                   .index(z["id"])]
    conn.execute(
        "UPDATE season SET source = 'manual', manual_override = 1 WHERE id = ?", (part_id,)
    )
    return part_id
