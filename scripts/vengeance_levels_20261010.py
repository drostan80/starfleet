#!/usr/bin/env python3
"""With Vengeance, Sincerely, Your Broken Saintess: S1 and S2 are two TVDB seasons (user OK
2026-10-10). Dry run by default.

The 10-05 add-check made "part 2" of S1 for AniList 212144 although TVDB already listed S2's
12 episodes (R1.10a: an entry belongs where its episodes are). Part 1 held 195209 and, with
part 2 gone, is S1's only entry. So:
  - S1 (z-wcrs75) takes AniList 195209 / MAL 59961 / Syoboi 7531 from part 1 (z-pagsk3), which
    goes;
  - S2 (z-4hg26z) takes AniList 212144 / MAL 64180 / Syoboi 8062 from part 2 (z-a9aacc), which
    goes, keeps its own status (watching) and turns list sync on;
  - part 2's flip-flop status history (nothing but the 10-05..10-10 flips) goes with it; the
    snapshot lcars.db.bak-20261010-pre-vengeance-s2 holds it.
The AniList/MAL write for S2 (watching, real progress) is a separate step: --push.

    docker exec -i lcars python - [--apply] [--push] < vengeance_levels_20261010.py
"""

import os
import sqlite3
import sys

from lcars import level_parts, list_sync, status_rules, util

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv
PUSH = "--push" in sys.argv

SHOW = "s-yk0kzw"
S1, PART1 = "z-wcrs75", "z-pagsk3"
S2, PART2 = "z-4hg26z", "z-a9aacc"


def one(conn, sql, *args):
    row = conn.execute(sql, args).fetchone()
    if row is None:
        raise SystemExit(f"REFUSED: nothing found for {sql} {args}")
    return row


def audited(conn):
    s1, p1 = one(conn, "SELECT * FROM season WHERE id = ?", S1), one(
        conn, "SELECT * FROM season WHERE id = ?", PART1)
    s2, p2 = one(conn, "SELECT * FROM season WHERE id = ?", S2), one(
        conn, "SELECT * FROM season WHERE id = ?", PART2)
    ok = (
        s1["show_id"] == s2["show_id"] == p1["show_id"] == p2["show_id"] == SHOW
        and (s1["anilist_id"], s1["mal_id"], s2["anilist_id"], s2["mal_id"]) == (None,) * 4
        and (p1["anilist_id"], p1["mal_id"], p2["anilist_id"], p2["mal_id"]) ==
        (195209, 59961, 212144, 64180)
        and p1["parent_id"] == p2["parent_id"] == S1 and s1["kind"] == s2["kind"] == "tvdb_season"
        and (s1["season_number"], s2["season_number"]) == (1, 2)
    )
    held = conn.execute("SELECT COUNT(*) FROM episode WHERE season_id IN (?, ?)",
                        (PART1, PART2)).fetchone()[0]
    kids = conn.execute("SELECT COUNT(*) FROM season WHERE parent_id IN (?, ?)",
                        (PART1, PART2)).fetchone()[0]
    if not ok or held or kids:
        raise SystemExit(f"REFUSED: the show has changed since the audit (ok={ok} episodes on "
                         f"the parts={held} children={kids}) — not touching it")
    return s1, p1, s2, p2


def hand_over(conn, part, parent, total):
    """The part's ids go onto the TVDB season itself; its other ids and rows follow it or go."""
    ids = (part["anilist_id"], part["mal_id"])
    # one season per list id (R1.22): free the id before the season takes it
    conn.execute("UPDATE season SET anilist_id = NULL, mal_id = NULL WHERE id = ?", (part["id"],))
    conn.execute("DELETE FROM season_external_id WHERE season_id = ? AND service IN"
                 " ('anilist', 'mal')", (part["id"],))
    level_parts.link_whole(conn, parent, {"anilist_id": ids[0], "mal_id": ids[1], "row": None,
                                          "total": total})
    for table in ("season_external_id", "list_row_lock", "art_asset", "season_air_choice"):
        conn.execute(f"UPDATE OR IGNORE {table} SET season_id = ? WHERE season_id = ?",
                     (parent["id"], part["id"]))
        conn.execute(f"DELETE FROM {table} WHERE season_id = ?", (part["id"],))
    conn.execute("DELETE FROM season_status_change WHERE season_id = ?", (part["id"],))
    conn.execute("DELETE FROM season_span WHERE season_id = ?", (part["id"],))
    conn.execute("DELETE FROM season WHERE id = ?", (part["id"],))


def report(conn, label):
    print(f"-- {label}")
    for sid in (S1, PART1, S2, PART2):
        z = conn.execute("SELECT season_number, part_number, kind, status, anilist_id, mal_id,"
                         " list_sync, episode_total FROM season WHERE id = ?", (sid,)).fetchone()
        ext = [tuple(r) for r in conn.execute("SELECT service, external_id FROM"
                                              " season_external_id WHERE season_id = ?", (sid,))]
        print(" ", sid, tuple(z) if z else "gone", ext)
    print("  show status:", one(conn, "SELECT status FROM show WHERE id = ?", SHOW)[0])


def main():
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    print("APPLY" if APPLY else "DRY RUN", "on", DB)
    s1, p1, s2, p2 = audited(conn)
    watched = conn.execute("SELECT COUNT(*) FROM episode WHERE season_id = ? AND state ="
                           " 'watched'", (S2,)).fetchone()[0]
    print(f"S2 keeps status {s2['status']!r}, {watched} episode(s) watched")
    report(conn, "before")
    with conn:
        hand_over(conn, p1, s1, p1["episode_total"])
        hand_over(conn, p2, s2, p2["episode_total"])
        conn.execute("UPDATE season SET list_sync = 1, updated_at = ? WHERE id = ?",
                     (util.now_utc_iso(), S2))
        status_rules.after_episodes_changed(conn, SHOW, "cleanup-20261010")
        report(conn, "after (not saved)" if not APPLY else "after")
        if not APPLY:
            conn.rollback()
    if APPLY:
        print("done; foreign_key_check:", conn.execute("PRAGMA foreign_key_check").fetchall())
    if PUSH:
        if not APPLY:
            raise SystemExit("--push needs --apply")
        from lcars import config
        config.set_current(config.load_config())
        for sid in (S2,):
            list_sync.push(conn, sid)
            conn.commit()
        for r in conn.execute("SELECT service, external_id, status, progress, written_at FROM"
                              " list_baseline WHERE external_id IN ('212144','64180')"):
            print("baseline", tuple(r))


main()
