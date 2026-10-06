#!/usr/bin/env python3
"""v0.4.6 (8.8.5): lists the TVDB links Fribb disagrees with — the same rule the new recheck uses
(the stored id is among the TVDB ids Fribb gives for ANY of the show's season AniList ids; Fribb
silent = no opinion; a link with source 'you' is never rechecked) — and, with --seed, stamps the
given shows' links as yours (`source = 'you'`) and closes any `tvdb_recheck` review already open
for them. The user (10-06) settled the three that disagreed on 10-06: s-1h8hn1, s-efnh0n, s-r7cx57.

Run it straight after the v0.4.6 deploy, before the first Memory Alpha pass / hourly backfill:

    docker exec -i lcars python - < scripts/tvdb_recheck_seed_20261006.py                  # list
    docker exec -i lcars python - --seed s-1h8hn1 s-efnh0n s-r7cx57 < scripts/...          # seed

Usage: [--db /db/lcars.db] [--fribb PATH] [--seed SHOW_ID ...]  (labelled snapshot first)
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_DB = "/db/lcars.db"
DEFAULT_FRIBB = Path.home() / ".local/share/starfleet/lcars/anime-lists.json"
NOTE = "kept by you 2026-10-06: the TVDB link stands (8.8.5 seed)"


def fribb_index(path) -> dict[int, set[int]]:
    index: dict[int, set[int]] = {}
    for e in json.load(open(path)):
        a, t = e.get("anilist_id"), e.get("tvdb_id")
        if str(a).isdigit() and str(t).isdigit():
            index.setdefault(int(a), set()).add(int(t))
    return index


def has_source_column(conn) -> bool:
    return any(r[1] == "source" for r in conn.execute("PRAGMA table_info(show_external_id)"))


def disagreements(conn, index) -> list[dict]:
    out = []
    source = "x.source" if has_source_column(conn) else "NULL"  # before the v0.4.6 migration
    for row in conn.execute(
        f"SELECT x.show_id, x.external_id, {source} AS source,"
        " COALESCE(s.title_english, s.title_romaji) t"
        " FROM show_external_id x JOIN show s ON s.id = x.show_id WHERE x.service = 'tvdb'"
        f" AND s.tracked = 1 AND s.media_shape = 'episodic' AND COALESCE({source}, '') != 'you'"
    ).fetchall():
        ids = {int(r[0]) for r in conn.execute(
            "SELECT anilist_id FROM season WHERE show_id = ? AND anilist_id IS NOT NULL",
            (row["show_id"],))}
        ids |= {int(r[0]) for r in conn.execute(
            "SELECT x.external_id FROM season_external_id x JOIN season z ON z.id = x.season_id"
            " WHERE z.show_id = ? AND x.service = 'anilist'", (row["show_id"],))
            if str(r[0]).isdigit()}
        theirs = set().union(*(index.get(a, set()) for a in ids)) if ids else set()
        if theirs and int(row["external_id"]) not in theirs:
            sonarr = conn.execute("SELECT url FROM show_external_id WHERE show_id = ? AND service"
                                  " = 'sonarr'", (row["show_id"],)).fetchone()
            out.append({"show": row["show_id"], "title": row["t"], "stored": row["external_id"],
                        "source": row["source"], "fribb": sorted(theirs),
                        "sonarr": sonarr[0] if sonarr else None})
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--fribb", default=str(DEFAULT_FRIBB))
    parser.add_argument("--seed", nargs="*", default=[], metavar="SHOW_ID")
    args = parser.parse_args(argv)
    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    found = disagreements(conn, fribb_index(args.fribb))
    for d in found:
        print(f"  {d['show']}  {d['title']!r}  stored {d['stored']} ({d['source'] or 'unknown'})"
              f"  Fribb {d['fribb']}  sonarr {d['sonarr']}")
    print(f"{len(found)} link(s) Fribb disagrees with")
    if args.seed:
        if not has_source_column(conn):
            print("the database has no show_external_id.source yet: deploy v0.4.6 first")
            return 1
        now = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        with conn:
            for sid in args.seed:
                conn.execute("UPDATE show_external_id SET source = 'you' WHERE show_id = ? AND"
                             " service = 'tvdb'", (sid,))
                closed = conn.execute(
                    "UPDATE pending_review SET resolved_at = ?, resolution_note = ? WHERE"
                    " entity_type = 'show' AND entity_id = ? AND field = 'tvdb_recheck' AND"
                    " resolved_at IS NULL", (now, NOTE, sid)).rowcount
                print(f"seeded {sid} as yours; closed {closed} review(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
