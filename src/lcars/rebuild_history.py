"""Stage 8 part — your AniList history, from the GDPR export (user, 2026-09-29).

"Get everything you can from AniList and map it; if it gives new information trust it; only
discard what is likely an artefact of the late mess and what conflicts with what I explicitly
confirmed today." Input `rebuild-inputs/anilist_history.json` (the export's anime list entries
and activity, no personal data; `tools/extract_anilist_gdpr.py`). The export's times are Japan
time (UTC+9): converted.

- **Watched / rewatched episodes** (activity 3 / 6, "N" or "A - B"): the entry's Nth episode,
  in absolute order across every level that holds the entry (Urusei Yatsura: one entry, four
  TVDB seasons), is watched, with a watch event at that time — unless a watch event within a
  day is already there (the same watch, e.g. one LCARS pushed).
- **Start and finish** of a level: `started_at` / `completed_at` from the entry's own dates,
  else from its first activity / its completion, where LCARS has none.
- **Score**: where the level has none, the entry's (0–100 → LCARS 0–20).

Discarded: anything from the mess on (activity or entry changed after `MESS_START`); an episode
that has not aired (R2.7); a level whose status you set today (`season_status_overrides`,
`list_decisions`). What doesn't map (no level holds the entry, an episode beyond the level's
episodes) is on the review list. Statuses are not derived from this: history is history.
"""

from __future__ import annotations

import collections
import datetime
import json

from lcars import ids as ids_module
from lcars import util

MESS_START = "2026-08-15"
JST = datetime.timedelta(hours=9)
WATCHED, REWATCHED, COMPLETED = 3, 6, 1


def utc(jst: str) -> str:
    t = datetime.datetime.strptime(jst, "%Y-%m-%d %H:%M:%S") - JST
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def day(yyyymmdd) -> str | None:
    if not yyyymmdd:
        return None
    s = str(yyyymmdd)
    if len(s) != 8 or s.endswith("0000"):
        return None
    month, dd = s[4:6], s[6:8]
    return f"{s[:4]}-{month}-{'01' if dd == '00' else dd}T00:00:00Z"


def episodes_of(value: str) -> list[int]:
    v = value.replace(" ", "")
    if not v:
        return []
    if "-" in v:
        a, b = v.split("-", 1)
        return list(range(int(a), int(b) + 1)) if a.isdigit() and b.isdigit() else []
    return [int(v)] if v.isdigit() else []


def _levels(conn, anilist_id: int) -> list:
    return conn.execute(
        "SELECT DISTINCT z.* FROM season z LEFT JOIN season_external_id s ON s.season_id = z.id"
        " AND s.service = 'anilist' JOIN show sh ON sh.id = z.show_id AND sh.tracked = 1"
        " WHERE z.anilist_id = ? OR s.external_id = ?",
        (anilist_id, str(anilist_id)),
    ).fetchall()


def _entry_episodes(conn, levels) -> list:
    from lcars import list_sync

    seen, out = set(), []
    for z in levels:
        for e in list_sync.level_episodes_ordered(conn, z):
            if e["id"] not in seen:
                seen.add(e["id"])
                out.append(e)
    shows = sorted({z["show_id"] for z in levels})
    marks = ",".join("?" * len(shows))
    number = {
        r[0]: r[1]
        for r in conn.execute(
            f"SELECT id, absolute_number FROM episode WHERE show_id IN ({marks})", tuple(shows)
        )
    }
    return sorted(out, key=lambda e: (number.get(e["id"]) is None, number.get(e["id"]) or 0))


def replay_history(run, conn) -> None:
    from lcars import rebuild, rebuild_cleanup

    path = run.inputs / "rebuild-inputs" / "anilist_history.json"
    if not path.exists():
        run.record("anilist_history", "all", "skipped", "no anilist_history.json")
        return
    data = json.loads(path.read_text())
    inputs = rebuild_cleanup.load_inputs(run)
    confirmed = {o["season"] for o in inputs.get("season_status_overrides", [])}
    decisions_path = run.inputs / "rebuild-inputs" / "list_decisions.json"
    decided = (
        {int(k) for k in json.loads(decisions_path.read_text())}
        if decisions_path.exists()
        else set()
    )
    today = util.now_utc_iso()[:10]

    by_entry: dict[int, list] = collections.defaultdict(list)
    for a in data["activity"]:
        by_entry[a["anilist"]].append(a)
    entries = {x["anilist"]: x for x in data["lists"]}

    stats = collections.Counter()
    unmapped: dict[int, str] = {}
    for anilist_id in sorted(set(by_entry) | set(entries)):
        levels = _levels(conn, anilist_id)
        acts = sorted(by_entry.get(anilist_id, []), key=lambda a: a["at"])
        entry = entries.get(anilist_id)
        if not levels:
            if acts or entry:
                unmapped[anilist_id] = "no tracked level holds it"
            continue
        if anilist_id in decided or any(z["id"] in confirmed for z in levels):
            stats["confirmed_today"] += 1
            continue
        eps = _entry_episodes(conn, levels)
        show_id = levels[0]["show_id"]
        unaired = {e["id"] for e in rebuild._unaired(conn, show_id, eps, today)}
        beyond = 0
        for a in acts:
            when = utc(a["at"])
            if when[:10] >= MESS_START:
                stats["mess_discarded"] += 1
                continue
            if a["type"] not in (WATCHED, REWATCHED):
                continue
            for n in episodes_of(a["value"]):
                if n < 1 or n > len(eps):
                    beyond += 1
                    continue
                e = eps[n - 1]
                if e["id"] in unaired:
                    stats["unaired_discarded"] += 1
                    continue
                near = conn.execute(
                    "SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?"
                    " AND abs(julianday(watched_at) - julianday(?)) <= 1",
                    (show_id, e["season"], e["episode"], when),
                ).fetchone()
                if near and a["type"] == WATCHED:
                    stats["already_there"] += 1
                    continue
                if (
                    a["type"] == WATCHED
                    and conn.execute(
                        "SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND"
                        " episode = ? AND platform = 'anilist'",
                        (show_id, e["season"], e["episode"]),
                    ).fetchone()
                ):
                    stats["already_there"] += 1  # a second "watched" log of the same episode
                    continue
                conn.execute(
                    "INSERT INTO watch_event (id, show_id, season, episode, watched_at, platform,"
                    " created_at) VALUES (?, ?, ?, ?, ?, 'anilist', ?)",
                    (
                        ids_module.generate_id(conn, "w"),
                        show_id,
                        e["season"],
                        e["episode"],
                        when,
                        util.now_utc_iso(),
                    ),
                )
                if e["state"] != "watched":
                    conn.execute(
                        "UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                        (util.now_utc_iso(), e["id"]),
                    )
                    stats["episodes_newly_watched"] += 1
                stats["rewatch_events" if a["type"] == REWATCHED else "watch_events"] += 1
        if beyond:
            unmapped[anilist_id] = (
                f"{beyond} watched episode(s) beyond the {len(eps)} its level(s) hold"
            )
        _dates_and_score(conn, levels, acts, entry, stats)
    conn.commit()
    for anilist_id, why in unmapped.items():
        run.record("anilist_history", str(anilist_id), "review", why)
    run.record(
        "anilist_history", "all", "applied", f"{dict(stats)}; {len(unmapped)} entries for review"
    )


def _dates_and_score(conn, levels, acts, entry, stats) -> None:
    pre = [a for a in acts if utc(a["at"])[:10] < MESS_START]
    if entry and (entry.get("updated_at") or "") >= MESS_START:
        entry = None  # changed in the mess: its dates (AniList sets them on a status change)
        #               and its score may be LCARS's wrong pushes
    started = day(entry and entry.get("started_on")) or (utc(pre[0]["at"]) if pre else None)
    done = [a for a in pre if a["type"] == COMPLETED]
    finished = day(entry and entry.get("finished_on")) or (utc(done[-1]["at"]) if done else None)
    score = entry["score"] / 5 if entry and entry.get("score") else None
    for z in levels:
        sets, args = [], []
        if started and not z["started_at"]:
            sets.append("started_at = ?")
            args.append(started)
            stats["started_at_set"] += 1
        if finished and not z["completed_at"] and z["status"] == "completed":
            sets.append("completed_at = ?")
            args.append(finished)
            stats["completed_at_set"] += 1
        if score and z["score"] is None:
            sets.append("score = ?")
            args.append(score)
            stats["score_set"] += 1
        if sets:
            conn.execute(f"UPDATE season SET {', '.join(sets)} WHERE id = ?", (*args, z["id"]))
