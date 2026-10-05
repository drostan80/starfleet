#!/usr/bin/env python3
"""Turn one TVDB season that holds several AniList cours into PART levels (RULEBOOK R1.10, R1.23).

Case (2026-10-05, Kusuriya no Hitorigoto S3): TVDB season 3 holds 24 episodes (abs 49–72) = two
cours, AniList 195516 (12 episodes) and 200927 (12). LCARS held 195516 on the season itself and
200927 on a leftover "S4" row with no episodes (a positional row from the old "season number =
position in Fribb's list" idea). In the rebuilt model the TVDB season holds the episodes and NO
list id, and each cour is a `part` level under it with its own AniList/MAL id and its own spans
(Dr. STONE S4 is the reference). This does that conversion:

  - an AniList id the season itself holds  -> a NEW part level takes it (and its MAL id, status,
    list-id rows); the season keeps none;
  - an AniList id held by a leftover row of the same show (no episodes, no spans, no children, a
    higher season number) -> that row BECOMES the part (same id, so its status history, list
    baselines and list-id rows stay with it);
  - each part's spans are its slice of the season's episodes (in absolute order, following the
    season's own gaps); the last part takes what is left;
  - the open "season N has 24 episodes but AniList only covers 12" review on the season is closed.

Usage (labelled snapshot first):
    python3 make_parts_20261005.py --show s-hhbjpn --season 3 --ids 195516,200927            #
dry run
    python3 make_parts_20261005.py --show s-hhbjpn --season 3 --ids 195516,200927 --apply
    --counts 12,12   episodes per part when an AniList entry has no known total
"""

import argparse
import sqlite3
from datetime import UTC, datetime

DEFAULT_DB = "/db/lcars.db"


class Refused(Exception):
    pass


def _new_id(conn, prefix: str) -> str:
    from lcars import ids  # run inside the lcars container (or the repo venv): same id scheme

    return ids.generate_id(conn, prefix)


def _episodes_of(conn, season) -> list:
    spans = conn.execute("SELECT abs_from, abs_to FROM season_span WHERE season_id = ?",
                         (season["id"],)).fetchall()
    if not spans:
        raise Refused("the season has no spans yet (Memory Alpha has not placed it)")
    where = " OR ".join("absolute_number BETWEEN ? AND ?" for _ in spans)
    return conn.execute(
        f"SELECT id, absolute_number FROM episode WHERE show_id = ? AND ({where})"
        " ORDER BY absolute_number",
        (season["show_id"], *[v for s in spans for v in (s[0], s[1])])).fetchall()


def _spans_for(episodes, season_spans) -> list[tuple[float, float]]:
    """The slice's spans: its episodes, cut along the season's own gaps."""
    numbers = [e["absolute_number"] for e in episodes]
    spans = []
    for lo, hi in sorted((s[0], s[1]) for s in season_spans):
        inside = [n for n in numbers if lo <= n <= hi]
        if inside:
            spans.append((min(inside), max(inside)))
    return spans


def plan(conn, show_id: str, season_number: int, anilist_ids: list[int],
         counts: list[int] | None = None) -> dict:
    parent = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number = ? AND kind = 'tvdb_season'",
        (show_id, season_number)).fetchone()
    if parent is None:
        raise Refused(f"show {show_id} has no TVDB season {season_number}")
    if conn.execute("SELECT 1 FROM season WHERE parent_id = ? AND kind = 'part' LIMIT 1",
                    (parent["id"],)).fetchone():
        raise Refused("this season already has part levels — nothing to do")
    if len(anilist_ids) < 2:
        raise Refused("a split needs at least two AniList ids")
    sources = []
    for aid in anilist_ids:
        if parent["anilist_id"] == aid:
            sources.append({"anilist_id": aid, "from": "parent", "row": parent})
            continue
        row = conn.execute(
            "SELECT * FROM season WHERE show_id = ? AND anilist_id = ? AND kind = 'tvdb_season'"
            " AND id != ?", (show_id, aid, parent["id"])).fetchone()
        if row is None:
            raise Refused(f"AniList {aid} is on neither the season nor a leftover row of the show")
        if (row["season_number"] or 0) <= season_number:
            raise Refused(f"the row holding AniList {aid} is not a later season")
        busy = any(
            conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (row["id"],)).fetchone()
            for table, col in (("episode", "season_id"), ("season_span", "season_id"),
                               ("season", "parent_id")))
        if busy:
            raise Refused(f"the row holding AniList {aid} has episodes, spans or children")
        sources.append({"anilist_id": aid, "from": "leftover", "row": row})
    episodes = _episodes_of(conn, parent)
    totals = counts or [s["row"]["episode_total"] for s in sources[:-1]]
    if len(totals) < len(sources) - 1 or any(t in (None, 0) for t in totals[:len(sources) - 1]):
        raise Refused("episode counts unknown for every part but the last — pass --counts")
    totals = list(totals[:len(sources) - 1])
    if sum(totals) >= len(episodes):
        raise Refused(f"the counts {totals} leave nothing for the last part"
                      f" ({len(episodes)} episodes)")
    parent_spans = conn.execute("SELECT abs_from, abs_to FROM season_span WHERE season_id = ?",
                                (parent["id"],)).fetchall()
    cursor = 0
    for i, source in enumerate(sources):
        take = totals[i] if i < len(totals) else len(episodes) - cursor
        chunk = episodes[cursor:cursor + take]
        cursor += take
        source["episodes"] = len(chunk)
        source["spans"] = _spans_for(chunk, parent_spans)
    return {"parent": parent, "sources": sources, "episodes": len(episodes)}


def apply(conn, p: dict) -> None:
    parent = p["parent"]
    now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with conn:
        for number, source in enumerate(p["sources"], start=1):
            row = source["row"]
            if source["from"] == "parent":
                part_id = _new_id(conn, "z")
                conn.execute(
                    "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
                    " anilist_id, mal_id, source, matched, manual_override, status,"
                    " status_set_manually, list_sync, episode_total, decimal_season_number,"
                    " created_at, updated_at)"
                    " VALUES (?, ?, ?, ?, 'part', ?, ?, ?, 'manual', 1, 0, ?, ?, ?, ?, ?, ?, ?)",
                    (part_id, parent["show_id"], parent["season_number"], number, parent["id"],
                     row["anilist_id"], row["mal_id"], row["status"], row["status_set_manually"],
                     row["list_sync"], row["episode_total"], float(parent["season_number"]),
                     now, now))
                conn.execute("UPDATE season_external_id SET season_id = ? WHERE season_id = ?"
                             " AND service IN ('anilist', 'mal')", (part_id, parent["id"]))
                conn.execute("UPDATE list_row_lock SET season_id = ? WHERE season_id = ?",
                             (part_id, parent["id"]))
                conn.execute("UPDATE season SET anilist_id = NULL, mal_id = NULL,"
                             " episode_total = NULL, updated_at = ? WHERE id = ?",
                             (now, parent["id"]))
            else:
                part_id = row["id"]
                conn.execute(
                    "UPDATE season SET kind = 'part', parent_id = ?, season_number = ?,"
                    " part_number = ?, decimal_season_number = ?, updated_at = ? WHERE id = ?",
                    (parent["id"], parent["season_number"], number,
                     float(parent["season_number"]), now, part_id))
            conn.execute("DELETE FROM season_span WHERE season_id = ?", (part_id,))
            conn.executemany("INSERT INTO season_span (season_id, abs_from, abs_to)"
                             " VALUES (?, ?, ?)", [(part_id, a, b) for a, b in source["spans"]])
            conn.execute("UPDATE season SET abs_start = ?, abs_end = ? WHERE id = ?",
                         (min(a for a, _ in source["spans"]), max(b for _, b in source["spans"]),
                          part_id))
        conn.execute(
            "UPDATE pending_review SET resolved_at = ?, resolution_note = ?"
            " WHERE entity_type = 'season' AND entity_id = ? AND field = 'anilist_id'"
            " AND source = 'anilist' AND resolved_at IS NULL",
            (now, "split into part levels (make_parts 2026-10-05): the season no longer holds "
                  "an AniList id", parent["id"]))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--show", required=True)
    parser.add_argument("--season", type=int, required=True)
    parser.add_argument("--ids", required=True, help="AniList ids in cour order, comma separated")
    parser.add_argument("--counts", help="episodes per part (all but the last), comma separated")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        p = plan(conn, args.show, args.season, [int(i) for i in args.ids.split(",")],
                 [int(c) for c in args.counts.split(",")] if args.counts else None)
    except Refused as e:
        print(f"REFUSED: {e}")
        return 1
    print(f"{'APPLY' if args.apply else 'DRY RUN'}: show {args.show} season {args.season}"
          f" ({p['episodes']} episodes)")
    for number, s in enumerate(p["sources"], start=1):
        print(f"  part {number}: AniList {s['anilist_id']} ({s['from']}) — "
              f"{s['episodes']} episodes, spans {s['spans']}")
    if args.apply:
        apply(conn, p)
        print("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
