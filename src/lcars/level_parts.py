"""Part levels under a TVDB season — the one place that plans and makes them (RULEBOOK R1.10,
R1.13b, R1.17, R1.23).

A TVDB season holds the episodes and no list id; each cour (an AniList/MAL entry) is a `part`
level under it with its own ids and its own spans, cut from the season's episodes (Dr. STONE S4
is the reference). Callers — the Memory Alpha reconciler (`level_reconcile`) and the show page's
"make part of season N" action — hand over the ordered entries; this module cuts the episodes,
converts a leftover row in place (same id: its status, history, list baselines and locks stay) or
creates the part, and moves the id the season itself held onto its own part 1.

An entry: {"anilist_id", "mal_id", "count" (episodes, None = the rest), "start_abs" (the episode
its start date fell on, None = unknown), "row" (a leftover level converted in place, the parent
itself when it holds the entry, or None for a part created from nothing)}.
"""

from __future__ import annotations

from lcars import ids, season_ranges, util


class Refused(Exception):
    """The episodes and the entries disagree: nothing is changed, the caller says why."""


def parent_episodes(conn, parent) -> list:
    """The TVDB season's own episodes in absolute order (by its spans)."""
    spans = conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (parent["id"],)
    ).fetchall()
    if not spans:
        raise Refused("the season has no spans yet (Memory Alpha has not placed it)")
    where = " OR ".join("absolute_number BETWEEN ? AND ?" for _ in spans)
    return conn.execute(
        f"SELECT id, absolute_number FROM episode WHERE show_id = ? AND ({where})"
        " ORDER BY absolute_number",
        (parent["show_id"], *[v for s in spans for v in (s[0], s[1])]),
    ).fetchall()


def _spans_for(episodes, season_spans) -> list[tuple[float, float]]:
    """The slice's spans: its episodes, cut along the season's own gaps."""
    numbers = [e["absolute_number"] for e in episodes]
    spans = []
    for lo, hi in sorted((s[0], s[1]) for s in season_spans):
        inside = [n for n in numbers if lo <= n <= hi]
        if inside:
            spans.append((min(inside), max(inside)))
    return spans


def _busy(conn, row) -> bool:
    """A level with episodes of its own, or children. A leftover TVDB-season level also has no
    span; a special level may (Memory Alpha numbers it), and that span is replaced."""
    tables = [("episode", "season_id"), ("season", "parent_id")]
    if row["kind"] != "special":
        tables.append(("season_span", "season_id"))
    return any(
        conn.execute(f"SELECT 1 FROM {table} WHERE {col} = ? LIMIT 1", (row["id"],)).fetchone()
        for table, col in tables
    )


def _claimed(conn, parent, numbers: list) -> set:
    """Absolute numbers already inside one of the season's parts."""
    out: set = set()
    for a, b in conn.execute(
        "SELECT sp.abs_from, sp.abs_to FROM season_span sp JOIN season p ON p.id = sp.season_id"
        " WHERE p.parent_id = ? AND p.kind = 'part'", (parent["id"],)
    ):
        out.update(n for n in numbers if a <= n <= b)
    return out


def plan(conn, parent, entries: list[dict]) -> dict:
    """Cut the season's free episodes between `entries` (in cour order). Raises Refused when the
    entries and the episodes don't agree."""
    if parent["kind"] != "tvdb_season":
        raise Refused("only a TVDB season is divided into parts")
    if not entries:
        raise Refused("nothing to place")
    for e in entries:
        row = e.get("row")
        if row is not None and row["id"] != parent["id"] and _busy(conn, row):
            raise Refused(
                f"the level holding AniList {e['anilist_id']} has episodes, spans or children"
            )
    episodes = parent_episodes(conn, parent)
    claimed = _claimed(conn, parent, [e["absolute_number"] for e in episodes])
    free = [e for e in episodes if e["absolute_number"] not in claimed]
    unknown = [e for e in entries if not e.get("count")]
    if len(unknown) > 1:
        raise Refused("episode counts unknown for more than one entry — can't cut the season")
    parent_spans = conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (parent["id"],)
    ).fetchall()
    cursor = 0
    out = []
    for i, e in enumerate(entries):
        if e.get("start_abs") is not None:
            numbers = [x["absolute_number"] for x in free]
            if e["start_abs"] not in numbers[cursor:cursor + 1]:
                nxt = numbers[cursor] if cursor < len(numbers) else "none"
                raise Refused(
                    f"AniList {e['anilist_id']} starts at abs {e['start_abs']} but the next free"
                    f" episode of the season is {nxt}"
                )
        known_after = sum(x["count"] for x in entries[i + 1:] if x.get("count"))
        take = e["count"] if e.get("count") else len(free) - cursor - known_after
        take = min(take, len(free) - cursor)
        chunk = free[cursor:cursor + take]
        if not chunk:
            raise Refused(f"no free episode left for AniList {e['anilist_id']}")
        cursor += take
        out.append({**e, "episodes": len(chunk), "spans": _spans_for(chunk, parent_spans)})
    return {"parent": parent, "entries": out, "free": len(free)}


def apply(conn, p: dict) -> list[str]:
    """Makes the planned parts. Returns the ids of the part levels, in order. The caller commits."""
    parent = p["parent"]
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT COALESCE(MAX(part_number), 0) FROM season WHERE parent_id = ? AND kind = 'part'",
        (parent["id"],),
    ).fetchone()[0]
    made = []
    for offset, e in enumerate(p["entries"], start=1):
        number = existing + offset
        row = e.get("row")
        if row is not None and row["id"] == parent["id"]:  # the season's own entry → its part
            part_id = ids.generate_id(conn, "z")
            conn.execute(
                "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
                " anilist_id, mal_id, source, matched, manual_override, status,"
                " status_set_manually, list_sync, episode_total, decimal_season_number, score,"
                " started_at, completed_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'part', ?, ?, ?, 'manual', 1, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?,"
                " ?)",
                (part_id, parent["show_id"], parent["season_number"], number, parent["id"],
                 parent["anilist_id"], parent["mal_id"], parent["status"],
                 parent["status_set_manually"], parent["list_sync"], parent["episode_total"],
                 float(parent["season_number"]), parent["score"], parent["started_at"],
                 parent["completed_at"], now, now),
            )
            conn.execute(
                "UPDATE season_external_id SET season_id = ? WHERE season_id = ?"
                " AND service IN ('anilist', 'mal')", (part_id, parent["id"]),
            )
            conn.execute(
                "UPDATE list_row_lock SET season_id = ? WHERE season_id = ?",
                (part_id, parent["id"]),
            )
            conn.execute(
                "UPDATE season_status_change SET season_id = ? WHERE season_id = ?",
                (part_id, parent["id"]),
            )
            conn.execute(
                "UPDATE season SET anilist_id = NULL, mal_id = NULL, episode_total = NULL,"
                " updated_at = ? WHERE id = ?", (now, parent["id"]),
            )
        elif row is not None:  # a leftover level becomes the part, in place
            part_id = row["id"]
            conn.execute(
                "UPDATE season SET kind = 'part', parent_id = ?, season_number = ?,"
                " part_number = ?, decimal_season_number = ?, updated_at = ? WHERE id = ?",
                (parent["id"], parent["season_number"], number, float(parent["season_number"]),
                 now, part_id),
            )
        else:  # nothing held it: a new planned part
            part_id = ids.generate_id(conn, "z")
            status, list_sync = season_ranges.auto_season_fields(
                conn, parent["show_id"], parent["season_number"], e["anilist_id"]
            )
            conn.execute(
                "INSERT INTO season (id, show_id, season_number, part_number, kind, parent_id,"
                " anilist_id, mal_id, source, matched, manual_override, status, list_sync,"
                " episode_total, decimal_season_number, last_reconciled_at, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, 'part', ?, ?, ?, 'fribb', 1, 0, ?, ?, ?, ?, ?, ?, ?)",
                (part_id, parent["show_id"], parent["season_number"], number, parent["id"],
                 e["anilist_id"], e.get("mal_id"), status, list_sync, e.get("total"),
                 float(parent["season_number"]), now, now, now),
            )
            season_ranges.upsert_season_external_id(
                conn, part_id, e["anilist_id"], e.get("mal_id"), now
            )
        conn.execute("DELETE FROM season_span WHERE season_id = ?", (part_id,))
        conn.executemany(
            "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
            [(part_id, a, b) for a, b in e["spans"]],
        )
        conn.execute(
            "UPDATE season SET abs_start = ?, abs_end = ? WHERE id = ?",
            (min(a for a, _ in e["spans"]), max(b for _, b in e["spans"]), part_id),
        )
        made.append(part_id)
    conn.execute(
        "UPDATE pending_review SET resolved_at = ?, resolution_note = ?"
        " WHERE entity_type = 'season' AND entity_id = ? AND resolved_at IS NULL"
        " AND field = 'anilist_id' AND source = 'anilist'",
        (now, "divided into part levels: the season no longer holds an AniList id", parent["id"]),
    )
    return made


def link_whole(conn, parent, e: dict) -> str:
    """A TVDB season with one entry holds it itself (no part): the entry's id goes onto the
    season. A leftover level holding it hands over its id, status, history, art and locks and
    goes. Returns the season's id. The caller commits."""
    now = util.now_utc_iso()
    row = e.get("row")
    if row is None:
        conn.execute(
            "UPDATE season SET anilist_id = ?, mal_id = ?, episode_total = ?, updated_at = ?"
            " WHERE id = ?", (e["anilist_id"], e.get("mal_id"), e.get("total"), now, parent["id"]),
        )
        season_ranges.upsert_season_external_id(
            conn, parent["id"], e["anilist_id"], e.get("mal_id"), now
        )
        return parent["id"]
    conn.execute(
        "UPDATE season SET anilist_id = ?, mal_id = ?, status = ?, status_set_manually = ?,"
        " score = COALESCE(?, score), started_at = COALESCE(?, started_at),"
        " completed_at = COALESCE(?, completed_at), list_sync = ?, episode_total = ?,"
        " updated_at = ? WHERE id = ?",
        (row["anilist_id"], row["mal_id"], row["status"], row["status_set_manually"], row["score"],
         row["started_at"], row["completed_at"], row["list_sync"], e.get("total"), now,
         parent["id"]),
    )
    for table in ("season_external_id", "list_row_lock", "season_status_change", "art_asset",
                  "season_air_choice"):
        conn.execute(f"UPDATE OR IGNORE {table} SET season_id = ? WHERE season_id = ?",
                     (parent["id"], row["id"]))
        conn.execute(f"DELETE FROM {table} WHERE season_id = ?", (row["id"],))
    conn.execute("DELETE FROM season_span WHERE season_id = ?", (row["id"],))
    conn.execute("DELETE FROM season WHERE id = ?", (row["id"],))
    return parent["id"]
