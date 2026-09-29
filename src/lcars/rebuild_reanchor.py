"""Stage 4 pre-pass — episode rows re-anchored to TVDB by identity (title + air date).

Found 2026-09-29 (Slime, Re:ZERO, SPY x FAMILY, Mushoku, Dr. STONE, Black Lagoon, Fire Force,
Tonbo!): TVDB renumbered these shows after 09-06 (per-cour seasons merged, seasons shifted),
and stage 4 matched the old rows to Sonarr's episodes by their season/episode number, so a
row carried the TVDB coordinates of another episode (Slime S3E1 "The Visitors", 2021, was
stamped TVDB S3E1 "Demons and Strategies", 2024) and the rows that had no counterpart stayed
behind as stale copies.

`plan` decides, from the rows and Sonarr's episode list alone, where each row really is:

- **stable**: the row's own coordinates hold the same episode in Sonarr (or the title differs
  but the air date agrees: a renamed episode; a TBA title is never a difference);
- **moved**: the same title and air date sit at other coordinates: the row goes there;
- **duplicate**: two rows are the same episode (same target): one is kept, the other removed;
- **unmatched**: nothing in Sonarr is this episode: the row stays where it is.

A show with nothing moved and no duplicate is left alone, so the ordinary shows are not touched.
"""

from __future__ import annotations

import datetime
import re

DATE_SLACK_DAYS = 2
# the same title is the same episode even when the two sources date it a few days apart
# (Mushoku S2: 2023-07-02 against 2023-07-10); a title that repeats is told apart by date
SAME_TITLE_SLACK_DAYS = 21


def norm(title) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def informative(title) -> bool:
    """A title that tells one episode from another (not TBA, not 'Episode 12')."""
    n = norm(title)
    return bool(n) and n not in ("tba", "tbd") and not re.fullmatch(r"episode\d+", n)


def _date(value) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def dates_agree(a, b) -> bool | None:
    """True/False when both are dated, None when one is not."""
    da, db = _date(a), _date(b)
    if da is None or db is None:
        return None
    return abs((da - db).days) <= DATE_SLACK_DAYS


def conflict(row_title, row_date, ep_title, ep_date) -> bool:
    """The row and the Sonarr episode at its coordinates are different episodes: the titles
    differ and the dates do not settle it in favour of the row. A TBA title says nothing (an
    airing show whose episodes were named since), so it is never a conflict."""
    if not (informative(row_title) and informative(ep_title)):
        return False
    if norm(row_title) == norm(ep_title):
        return False
    return dates_agree(row_date, ep_date) is not True


def same_episode(row_title, row_date, ep_title, ep_date) -> bool:
    if not informative(row_title) or norm(row_title) != norm(ep_title):
        return False
    da, db = _date(row_date), _date(ep_date)
    return da is None or db is None or abs((da - db).days) <= SAME_TITLE_SLACK_DAYS


def plan(rows: list[dict], eps: list[dict]) -> dict:
    """rows: id, season, episode, title, date, created_at, watched (bool);
    eps: seasonNumber, episodeNumber, title, airDate. Regular seasons only (season > 0)."""
    by_coords = {(e["seasonNumber"], e["episodeNumber"]): e for e in eps if e["seasonNumber"] > 0}
    by_title: dict[str, list] = {}
    for e in by_coords.values():
        if informative(e.get("title")):
            by_title.setdefault(norm(e["title"]), []).append(e)
    out = {"stable": [], "moved": [], "unmatched": [], "blind": [], "duplicates": [], "target": {}}
    taken: dict[tuple, list] = {}
    for r in rows:
        if r["season"] <= 0:
            continue
        here = by_coords.get((r["season"], r["episode"]))
        if here is not None and not conflict(
            r["title"], r["date"], here.get("title"), here.get("airDate")
        ):
            out["stable"].append(r["id"])
            out["target"][r["id"]] = (r["season"], r["episode"])
            taken.setdefault((r["season"], r["episode"]), []).append(r)
            continue
        if not informative(r["title"]):
            out["blind"].append(r["id"])
            continue
        cands = [
            e
            for e in by_title.get(norm(r["title"]), [])
            if same_episode(r["title"], r["date"], e.get("title"), e.get("airDate"))
        ]
        if not cands:
            out["unmatched"].append(r["id"])
            continue
        if len(cands) > 1:
            d = _date(r["date"])
            if d is not None:
                cands.sort(key=lambda e: abs((_date(e.get("airDate")) or d) - d))
        e = cands[0]
        target = (e["seasonNumber"], e["episodeNumber"])
        out["target"][r["id"]] = target
        taken.setdefault(target, []).append(r)
        if target != (r["season"], r["episode"]):
            out["moved"].append(r["id"])
    if out["moved"] or any(len(g) > 1 for g in taken.values()):
        # only in a show where the numbering has shifted: an airing show's TBA rows stay
        _by_date(rows, by_coords, out, taken)
    for target, group in taken.items():
        if len(group) > 1:
            # keep the row that carries history, then the older one
            group = sorted(group, key=lambda r: (not r["watched"], r["created_at"] or "", r["id"]))
            out["duplicates"].append(
                {"target": target, "keep": group[0]["id"], "drop": [r["id"] for r in group[1:]]}
            )
    return out


def _by_date(rows: list[dict], by_coords: dict, out: dict, taken: dict) -> None:
    """Rows nothing identified by title (a TBA title, a title Sonarr words differently): the
    Sonarr episode that aired within a day or two, when only one row and one episode could
    be each other."""
    leftovers = set(out["blind"]) | set(out["unmatched"])
    left = [r for r in rows if r["season"] > 0 and r["id"] in leftovers and _date(r["date"])]
    free = [e for c, e in by_coords.items() if c not in taken and _date(e.get("airDate"))]

    def near(r, e):
        return abs((_date(r["date"]) - _date(e["airDate"])).days) <= DATE_SLACK_DAYS

    for r in left:
        eps = [e for e in free if near(r, e)]
        if len(eps) != 1:
            continue
        e = eps[0]
        if len([x for x in left if near(x, e)]) != 1:
            continue
        target = (e["seasonNumber"], e["episodeNumber"])
        for key in ("blind", "unmatched"):
            if r["id"] in out[key]:
                out[key].remove(r["id"])
        out["target"][r["id"]] = target
        taken.setdefault(target, []).append(r)
        if target != (r["season"], r["episode"]):
            out["moved"].append(r["id"])
        else:
            out["stable"].append(r["id"])


def needs_reanchor(p: dict) -> bool:
    return bool(p["moved"] or p["duplicates"])


# ── applying a plan ──────────────────────────────────────────────────────


def sonarr_episodes(conn, show_id: str) -> list[dict] | None:
    """Sonarr's episode list for a show's TVDB series (a read); None when it has no TVDB id or
    is not in the Sonarr library."""
    from lcars import sonarr_client
    from lcars.config import get_current

    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
        (show_id,),
    ).fetchone()
    cfg = get_current()
    if row is None or not cfg.sonarr_url or not cfg.sonarr_api_key:
        return None
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        series = client.series_by_tvdb_id(int(row[0]))
        return None if series is None else client.episodes(series["id"])


def _rows(conn, show_id: str) -> list[dict]:
    out = []
    for r in conn.execute(
        "SELECT e.id, e.season, e.episode, e.title, e.air_date_utc AS date, e.created_at,"
        " e.state, e.season_id, (SELECT COUNT(*) FROM watch_event w WHERE"
        " w.show_id = e.show_id AND w.season = e.season AND w.episode = e.episode)"
        " AS events FROM episode e WHERE e.show_id = ?",
        (show_id,),
    ):
        out.append({**dict(r), "watched": r["state"] == "watched" or r["events"] > 0})
    return out


def reanchor_show(run, conn, show_id: str, eps: list[dict] | None = None) -> dict | None:
    """Plans the show against Sonarr and, when a row is somewhere else than its episode is,
    applies it (the caller commits). Returns the summary, or None when the show is fine."""
    from lcars import rebuild_cleanup

    if eps is None:
        eps = sonarr_episodes(conn, show_id)
    if not eps:
        return None
    rows = _rows(conn, show_id)
    p = plan(rows, eps)
    if not needs_reanchor(p):
        return None
    return apply(
        run,
        conn,
        show_id,
        rows,
        p,
        rebuild_cleanup.Deleter(conn, run.dir / "removed" if run is not None else None),
    )


def apply(run, conn, show_id: str, rows: list[dict], p: dict, deleter) -> dict:
    from lcars import util

    now = util.now_utc_iso()
    conn.commit()  # foreign keys can only be switched inside no transaction
    fk_was_on = conn.execute("PRAGMA foreign_keys").fetchone()[0]
    broken_before = set(map(tuple, conn.execute("PRAGMA foreign_key_check").fetchall()))
    conn.execute("PRAGMA foreign_keys = OFF")  # watch events are keyed by (season, episode)
    try:
        return _apply(run, conn, show_id, rows, p, deleter, now)
    finally:
        conn.commit()
        conn.execute(f"PRAGMA foreign_keys = {'ON' if fk_was_on else 'OFF'}")
        broken = set(map(tuple, conn.execute("PRAGMA foreign_key_check").fetchall()))
        if broken - broken_before:
            raise RuntimeError(f"reanchor {show_id}: {len(broken - broken_before)} new broken keys")


def _apply(run, conn, show_id, rows, p, deleter, now) -> dict:
    by_id = {r["id"]: r for r in rows}
    old_level = {r["id"]: r["season_id"] for r in rows}

    # 1. duplicates: the kept row takes the watched state; the dropped rows' watch events are
    #    set aside, the rows go (with what references them), the events come back on the kept row
    carried: list[tuple[tuple, dict]] = []
    dropped = 0
    remap_old_to_new: dict[tuple, tuple] = {}
    for g in p["duplicates"]:
        keep = by_id[g["keep"]]
        for d in g["drop"]:
            drow = by_id[d]
            if drow["watched"] or drow["state"] == "watched":
                conn.execute(
                    "UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                    (now, keep["id"]),
                )
            for ev in conn.execute(
                "SELECT * FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
                (show_id, drow["season"], drow["episode"]),
            ).fetchall():
                carried.append((g["target"], dict(ev)))
            conn.execute(
                "DELETE FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
                (show_id, drow["season"], drow["episode"]),
            )
            deleter.delete("episode", "id = ?", (d,))
            dropped += 1
    dropped_ids = {d for g in p["duplicates"] for d in g["drop"]}

    # 2. the moved rows (and the kept rows of duplicates) to their TVDB coordinates
    moves = [
        (rid, (by_id[rid]["season"], by_id[rid]["episode"]), tgt)
        for rid, tgt in p["target"].items()
        if rid not in dropped_ids and tgt != (by_id[rid]["season"], by_id[rid]["episode"])
    ]
    for _rid, old, new in moves:
        remap_old_to_new[old] = new
    if moves:
        conn.execute(
            "CREATE TEMP TABLE IF NOT EXISTS _reanchor_map (os INTEGER, oe INTEGER,"
            " ns INTEGER, ne INTEGER)"
        )
        conn.execute("DELETE FROM _reanchor_map")
        conn.executemany(
            "INSERT INTO _reanchor_map VALUES (?, ?, ?, ?)",
            [(o[0], o[1], n[0], n[1]) for _r, o, n in moves],
        )
        conn.execute(
            "UPDATE watch_event SET season = (SELECT ns FROM _reanchor_map WHERE os = season"
            " AND oe = episode), episode = (SELECT ne FROM _reanchor_map WHERE os = season"
            " AND oe = episode) WHERE show_id = ? AND EXISTS (SELECT 1 FROM _reanchor_map WHERE"
            " os = season AND oe = episode)",
            (show_id,),
        )
        for i, (rid, _o, _n) in enumerate(moves):  # out of the way first: UNIQUE (show, s, e)
            conn.execute(
                "UPDATE episode SET season = ?, episode = ? WHERE id = ?", (-1 - i, i, rid)
            )
        for rid, _o, (s, e) in moves:
            conn.execute(
                "UPDATE episode SET season = ?, episode = ?, sonarr_season = ?,"
                " sonarr_episode = ?, season_id = NULL, updated_at = ? WHERE id = ?",
                (s, e, s, e, now, rid),
            )
    for ev_target, ev in carried:  # the dropped rows' events, on the kept row (earliest only)
        have = conn.execute(
            "SELECT MIN(watched_at) FROM watch_event WHERE show_id = ? AND"
            " season = ? AND episode = ?",
            (show_id, *ev_target),
        ).fetchone()[0]
        if have is None:
            conn.execute(
                "INSERT INTO watch_event (id, show_id, season, episode, watched_at,"
                " platform, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ev["id"],
                    show_id,
                    ev_target[0],
                    ev_target[1],
                    ev["watched_at"],
                    ev["platform"],
                    ev["created_at"],
                ),
            )
        elif ev["watched_at"] < have:
            conn.execute(
                "UPDATE watch_event SET watched_at = ?, platform = ?, created_at = ?"
                " WHERE show_id = ? AND season = ? AND episode = ? AND watched_at = ?",
                (ev["watched_at"], ev["platform"], ev["created_at"], show_id, *ev_target, have),
            )

    # 3. the seasons follow their episodes
    levels = _levels_follow_episodes(conn, show_id, p, old_level, dropped_ids, now, deleter)
    levels.update(_place_by_fribb(conn, show_id, now, deleter))
    conn.execute(
        "UPDATE episode SET season_id = (SELECT z.id FROM season z"
        " WHERE z.show_id = episode.show_id"
        " AND z.kind = 'tvdb_season' AND z.season_number = episode.season)"
        " WHERE show_id = ? AND season > 0",
        (show_id,),
    )
    summary = {"moved": len(moves), "duplicates_removed": dropped, **levels}
    if run is not None:
        run.record(
            "reanchor",
            show_id,
            "applied",
            f"{len(moves)} rows moved to their TVDB episode, {dropped} duplicates removed;"
            f" seasons: {levels}",
        )
    return summary


def _levels_follow_episodes(conn, show_id, p, old_level, dropped_ids, now, deleter) -> dict:
    """A TVDB-season level takes the number its episodes now have. Two that land on the same
    number are one season now: the one already there stays, the other becomes a part of it
    (R1.13b), or goes when its list id is held elsewhere."""
    import collections

    levels = [
        dict(z)
        for z in conn.execute(
            "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND season_number > 0"
            " ORDER BY season_number",
            (show_id,),
        )
    ]
    want: dict[str, int] = {}
    tally: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    for rid, level in old_level.items():
        if level and rid in p["target"] and rid not in dropped_ids:
            tally[level][p["target"][rid][0]] += 1
    for z in levels:
        t = tally.get(z["id"])
        want[z["id"]] = (
            sorted(t.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if t else z["season_number"]
        )
    groups: dict[int, list[dict]] = collections.defaultdict(list)
    for z in levels:
        groups[want[z["id"]]].append(z)
    renumbered = folded = removed = 0
    survivors: dict[str, int] = {}
    for n, group in sorted(groups.items()):
        # the level already numbered n, else the one with the most episodes
        group.sort(
            key=lambda z: (
                z["season_number"] != n,
                -sum(tally.get(z["id"], {}).values()),
                z["season_number"],
            )
        )
        survivors[group[0]["id"]] = n
        for loser in group[1:]:
            if _fold(conn, loser, group[0], n, now, deleter):
                folded += 1
            else:
                removed += 1
    keep = [z for z in levels if z["id"] in survivors]
    for i, z in enumerate(keep):  # temp numbers first: UNIQUE (show, season_number, ..)
        conn.execute("UPDATE season SET season_number = ? WHERE id = ?", (10000 + i, z["id"]))
    for z in keep:
        n = survivors[z["id"]]
        if n != z["season_number"]:
            renumbered += 1
        conn.execute(
            "UPDATE season SET season_number = ?, updated_at = ? WHERE id = ?", (n, now, z["id"])
        )
        conn.execute(
            "UPDATE season SET season_number = ? WHERE parent_id = ? AND kind = 'part'",
            (n, z["id"]),
        )
    return {"renumbered": renumbered, "folded_into_parts": folded, "removed": removed}


_FRIBB: dict = {}


def _fribb_index() -> dict | None:
    """AniList id -> Fribb entries (None when there is no dataset: the placement is skipped)."""
    if "index" not in _FRIBB:
        try:
            from lcars import fribb

            _FRIBB["index"] = fribb.build_anilist_index(fribb.load_dataset())
        except Exception:
            _FRIBB["index"] = None
    return _FRIBB["index"]


def _place_by_fribb(conn, show_id: str, now: str, deleter) -> dict:
    """An entry sits under the TVDB season Fribb gives it (Slime 116742 → S2, Dr. STONE 172019 →
    S4). A part goes under that season; a season level of its own becomes a part of it. Two
    parts of one season holding the same entry are one: the reviewed one stays."""
    index = _fribb_index()
    placed = duplicates = 0
    if index:
        for z in conn.execute(
            "SELECT * FROM season WHERE show_id = ? AND kind IN ('tvdb_season', 'part')"
            " AND anilist_id IS NOT NULL ORDER BY season_number, part_number",
            (show_id,),
        ).fetchall():
            entries = index.get(int(z["anilist_id"]), [])
            n = (entries[0].get("season") or {}).get("tvdb") if len(entries) == 1 else None
            if not n or n <= 0:
                continue
            current = z["season_number"]
            if z["kind"] == "part":
                parent = conn.execute(
                    "SELECT season_number FROM season WHERE id = ?", (z["parent_id"],)
                ).fetchone()
                current = parent[0] if parent else current
            target = conn.execute(
                "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND"
                " season_number = ?",
                (show_id, n),
            ).fetchone()
            if current == n or target is None or target["id"] == z["id"]:
                continue
            if z["kind"] == "part":
                conn.execute(
                    "UPDATE season SET parent_id = ?, season_number = ?, part_number = ?,"
                    " updated_at = ? WHERE id = ?",
                    (target["id"], n, _next_part(conn, target["id"]), now, z["id"]),
                )
            else:
                _fold(conn, dict(z), dict(target), n, now, deleter)
            placed += 1
    for row in conn.execute(
        "SELECT parent_id, anilist_id FROM season WHERE show_id = ? AND kind = 'part' AND"
        " anilist_id IS NOT NULL GROUP BY parent_id, anilist_id HAVING COUNT(*) > 1",
        (show_id,),
    ).fetchall():
        same = conn.execute(
            "SELECT id FROM season WHERE parent_id = ? AND anilist_id = ? ORDER BY"
            " status_set_manually DESC, (source = 'manual') DESC, part_number",
            (row[0], row[1]),
        ).fetchall()
        for extra in same[1:]:
            conn.execute(
                "UPDATE episode SET season_id = ? WHERE season_id = ?", (same[0][0], extra[0])
            )
            deleter.delete("season", "id = ?", (extra[0],))
            duplicates += 1
    return {"placed_by_fribb": placed, "duplicate_parts_removed": duplicates}


def _holds_id(conn, show_id, level_id, anilist, mal) -> bool:
    for service, value in (("anilist", anilist), ("mal", mal)):
        col = "anilist_id" if service == "anilist" else "mal_id"
        if value is None:
            continue
        if conn.execute(
            f"SELECT 1 FROM season WHERE show_id = ? AND id != ? AND {col} = ?",
            (show_id, level_id, value),
        ).fetchone():
            return True
    return False


def _next_part(conn, parent_id: str) -> int:
    return conn.execute(
        "SELECT COALESCE(MAX(part_number), 0) + 1 FROM season WHERE parent_id = ?"
        " AND kind = 'part'",
        (parent_id,),
    ).fetchone()[0]


def _fold(conn, loser: dict, target: dict, n: int, now: str, deleter) -> bool:
    """`loser` is the same TVDB season as `target`. True when it became a part of it."""
    from lcars import ids, season_ranges

    show_id = loser["show_id"]
    for child in conn.execute(
        "SELECT id FROM season WHERE parent_id = ? ORDER BY part_number", (loser["id"],)
    ).fetchall():
        conn.execute(
            "UPDATE season SET parent_id = ?, season_number = ?, part_number = ? WHERE id = ?",
            (target["id"], n, _next_part(conn, target["id"]), child[0]),
        )
    fresh = conn.execute("SELECT * FROM season WHERE id = ?", (loser["id"],)).fetchone()
    has_id = fresh["anilist_id"] is not None or fresh["mal_id"] is not None
    if not has_id or _holds_id(conn, show_id, loser["id"], fresh["anilist_id"], fresh["mal_id"]):
        conn.execute(
            "UPDATE episode SET season_id = ? WHERE season_id = ?", (target["id"], loser["id"])
        )
        deleter.delete("season", "id = ?", (loser["id"],))
        return False
    tgt = conn.execute("SELECT * FROM season WHERE id = ?", (target["id"],)).fetchone()
    if (
        tgt["anilist_id"] is None
        and tgt["mal_id"] is None
        and not conn.execute("SELECT 1 FROM season WHERE parent_id = ?", (target["id"],)).fetchone()
    ):
        # the TVDB season is this one entry: it takes the id, status and history
        conn.execute(
            "UPDATE season SET anilist_id = ?, mal_id = ?, status = ?, status_set_manually = ?,"
            " score = COALESCE(?, score), started_at = COALESCE(?, started_at),"
            " completed_at = COALESCE(?, completed_at), updated_at = ? WHERE id = ?",
            (
                fresh["anilist_id"],
                fresh["mal_id"],
                fresh["status"],
                fresh["status_set_manually"],
                fresh["score"],
                fresh["started_at"],
                fresh["completed_at"],
                now,
                target["id"],
            ),
        )
        season_ranges.upsert_season_external_id(
            conn, target["id"], fresh["anilist_id"], fresh["mal_id"], now
        )
        conn.execute(
            "UPDATE season_status_change SET season_id = ?, show_id = ? WHERE season_id = ?",
            (target["id"], show_id, loser["id"]),
        )
        conn.execute(
            "UPDATE season SET anilist_id = NULL, mal_id = NULL WHERE id = ?", (loser["id"],)
        )
        conn.execute(
            "DELETE FROM season_external_id WHERE season_id = ? AND service IN ('anilist', 'mal')",
            (loser["id"],),
        )
        conn.execute(
            "UPDATE episode SET season_id = ? WHERE season_id = ?", (target["id"], loser["id"])
        )
        deleter.delete("season", "id = ?", (loser["id"],))
        return False
    parts = conn.execute(
        "SELECT COUNT(*) FROM season WHERE parent_id = ? AND kind = 'part'", (target["id"],)
    ).fetchone()[0]
    if parts == 0:  # the TVDB season's own entry becomes its first part
        first = ids.generate_id(conn, "z")
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
            " anilist_id, mal_id, source, status, status_set_manually, score, started_at,"
            " completed_at, created_at, updated_at) VALUES (?, ?, ?, 1, 'part', ?, ?, ?, 'auto',"
            " ?, ?, ?, ?, ?, ?, ?)",
            (
                first,
                show_id,
                n,
                target["id"],
                tgt["anilist_id"],
                tgt["mal_id"],
                tgt["status"],
                tgt["status_set_manually"],
                tgt["score"],
                tgt["started_at"],
                tgt["completed_at"],
                now,
                now,
            ),
        )
        season_ranges.upsert_season_external_id(conn, first, tgt["anilist_id"], tgt["mal_id"], now)
        conn.execute(
            "UPDATE season SET anilist_id = NULL, mal_id = NULL, updated_at = ? WHERE id = ?",
            (now, target["id"]),
        )
        conn.execute(
            "DELETE FROM season_external_id WHERE season_id = ? AND service IN ('anilist', 'mal')",
            (target["id"],),
        )
        parts = 1
    conn.execute(
        "UPDATE season SET kind = 'part', parent_id = ?, season_number = ?,"
        " part_number = ?, updated_at = ? WHERE id = ?",
        (target["id"], n, _next_part(conn, target["id"]), now, loser["id"]),
    )
    conn.execute(
        "UPDATE episode SET season_id = ? WHERE season_id = ?", (target["id"], loser["id"])
    )
    return True
