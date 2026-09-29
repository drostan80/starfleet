"""Stage 10 — the write list (PLAN-CODE 9.1/9.2, PLAN-DATA §2.0).

Reads only. What LCARS holds at the end of the rebuild against what AniList and MAL hold now,
as the writes that would make them agree — captured (`captured_write`), never sent:

- **the lists** (exported first, raw, under `<run>/lists/`: the rollback copy, and what a re-run
  computes from): one write per level whose status or progress differs from its entry, only the
  fields that differ; an entry LCARS holds no copy of is an add; a level LCARS skipped is never
  written (R4.6);
- **deletes** only from your explicit decisions (`pending.jsonl` list_delete);
- **entries on the lists that no tracked level holds**: listed, not touched;
- **Sonarr monitoring** per season (R5.5–R5.8): only the shows whose flags would change;
- **what the lists will do by themselves** (AniList completes an entry at its last episode and
  sets a completed entry's progress to its total): a write whose outcome differs from LCARS's
  decision is put on the review list, its progress not pushed;
- **`list_baseline`**, seeded from what the lists hold once the writes have been sent, so the
  first poll after the cutover sees nothing as edited elsewhere.

Everything lands in `<run>/writes/write_list.json` (for the review page) and the ledger.
"""

from __future__ import annotations

import json
import os

from lcars import util

ANILIST_STATUS = {"CURRENT": "watching", "PLANNING": "planned", "PAUSED": "paused",
                  "COMPLETED": "completed", "DROPPED": "dropped", "REPEATING": "watching"}
MAL_STATUS = {"watching": "watching", "plan_to_watch": "planned", "on_hold": "paused",
              "completed": "completed", "dropped": "dropped"}


# the September mess: list values changed from here on may be LCARS's wrong pushes (09-29)
MESS_START = "2026-08-15"


def _rebuild():
    from lcars import rebuild

    return rebuild


# ── the lists ────────────────────────────────────────────────────────────


def export_lists(run, refresh: bool = False) -> dict[str, dict[int, dict]]:
    """Both lists, keyed by id, normalised to LCARS's vocabulary; the raw answers are saved in
    `<run>/lists/` on the first run and read from there on a re-run (`refresh` reads again)."""
    from lcars import anilist_client, config, mal_client

    cfg = config.get_current()
    folder = run.dir / "lists"
    folder.mkdir(exist_ok=True)
    out: dict[str, dict[int, dict]] = {}
    for service, fetch, token, key in (
            ("anilist", anilist_client.fetch_my_anime_list, cfg.anilist_access_token, "anilist_id"),
            ("mal", mal_client.fetch_my_list, cfg.mal_access_token, "mal_id")):
        path = folder / f"{service}.json"
        if path.exists() and not refresh:
            rows = json.loads(path.read_text())
        else:
            if not token:
                raise _rebuild().RebuildError(f"no {service} token to read the list with")
            rows = fetch(token)
            path.write_text(json.dumps(rows, ensure_ascii=False, indent=0))
        table = ANILIST_STATUS if service == "anilist" else MAL_STATUS
        out[service] = {int(r[key]): {
            "status": table.get(r.get("status")), "raw_status": r.get("status"),
            "progress": (r.get("progress") if service == "anilist"
                         else r.get("num_watched_episodes")) or 0,
            "total": r.get("episodes") or r.get("num_episodes") or None,
            "updated_at": r.get("updated_at"), "title": r.get("title")} for r in rows}
    return out


# ── the plan (pure) ──────────────────────────────────────────────────────


def plan_level_writes(levels: list[dict], lists: dict[str, dict[int, dict]]) -> dict:
    """levels: id, show, title, status, progress, ids {service: id}. Returns
    {"writes": [...], "reviews": [...], "untracked": {service: [ids]}}."""
    writes, reviews = [], []
    held: dict[str, set] = {"anilist": set(), "mal": set()}
    for lv in levels:
        if lv["status"] in (None, "skipped"):
            continue
        for service, ext in lv["ids"].items():
            held[service].add(ext)
            entry = lists[service].get(ext)
            want_status, count = lv["status"], lv["progress"]
            fields: dict = {}
            before = None
            if entry is None:
                fields = {"status": want_status, "progress": count}
                kind = "add"
            else:
                kind = "change"
                before = {"status": entry["status"], "progress": entry["progress"]}
                if entry["status"] != want_status:
                    fields["status"] = want_status
                if entry["progress"] != count:
                    fields["progress"] = count
            if "progress" in fields and not lv.get("episodes", 1):
                fields.pop("progress")  # a level with no episodes has no progress to give
            if ("progress" in fields and entry is not None and count < entry["progress"]
                    and (entry.get("updated_at") or "") < MESS_START):
                fields.pop("progress")  # the list's own history is never lowered (09-29)
            if not fields:
                continue
            note = _outcome_differs(service, want_status, count, entry, fields)
            if note:  # the service will not end up where LCARS decided: progress not pushed
                fields.pop("progress", None)
                reviews.append({"level": lv["id"], "show": lv["show"], "title": lv["title"],
                                "service": service, "id": ext, "why": note,
                                "lcars": {"status": want_status, "progress": count},
                                "list": before})
            if fields:
                writes.append({"level": lv["id"], "show": lv["show"], "title": lv["title"],
                               "service": service, "id": ext, "kind": kind, "fields": fields,
                               "before": before})
    untracked = {s: sorted(set(lists[s]) - held[s]) for s in lists}
    return {"writes": writes, "reviews": reviews, "untracked": untracked}


def _outcome_differs(service, status, count, entry, fields) -> str | None:
    """What AniList/MAL do on their own: a completed entry takes its total as progress, and a
    watching one that reaches its total completes. Returns why the write would end elsewhere."""
    total = (entry or {}).get("total")
    if not total:
        return None
    if status == "completed" and count != total:
        return (f"{service} completes an entry at its {total} episodes; LCARS holds {count} for"
                f" this level, so its progress would end at {total}")
    if status == "watching" and count >= total:
        return (f"{service} completes an entry that reaches its {total} episodes; LCARS says"
                f" watching with {count}")
    return None


# ── from the database ────────────────────────────────────────────────────


def levels_from(conn) -> list[dict]:
    from lcars import list_sync

    out = []
    for z in conn.execute(
            "SELECT z.*, sh.tracked, COALESCE(sh.display_title_override, sh.title_english,"
            " sh.title_romaji, sh.title_native) AS show_title FROM season z LEFT JOIN show sh"
            " ON sh.id = z.show_id").fetchall():
        if not list_sync.pushable(conn, z):
            continue
        ids = list_sync.list_ids(conn, z["id"])
        if not ids:
            continue
        label = z["label"] or (f"S{z['season_number']}" if z["season_number"] else z["kind"])
        out.append({"id": z["id"], "show": z["show_id"], "title": f"{z['show_title']} {label}",
                    "status": z["status"], "progress": list_sync.level_progress(conn, z),
                    "episodes": len(list_sync.level_episodes_ordered(conn, z)), "ids": ids})
    return out


def explicit_deletes(run) -> list[dict]:
    path = run.dir / "pending.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return [r["data"] if "data" in r else r for r in rows if r.get("kind") == "list_delete"]


def take_list_history(run, conn, lists) -> None:
    """The list's progress is history LCARS never recorded (user, 09-29): a level whose entry
    was last changed before the September mess and holds more progress than LCARS gets its
    first N episodes marked watched (state only: no invented watch dates)."""
    from lcars import list_sync

    taken = episodes = 0
    now = util.now_utc_iso()
    for lv in levels_from(conn):
        if lv["status"] in (None, "skipped"):
            continue
        service = "anilist" if "anilist" in lv["ids"] else "mal"
        entry = lists[service].get(lv["ids"].get(service))
        if (entry is None or (entry.get("updated_at") or "") >= MESS_START
                or entry["progress"] <= lv["progress"]):
            continue
        z = conn.execute("SELECT * FROM season WHERE id = ?", (lv["id"],)).fetchone()
        eps = list_sync.level_episodes_ordered(conn, z)[:entry["progress"]]
        todo = [e["id"] for e in eps if e["state"] != "watched"]
        if not todo:
            continue
        conn.executemany("UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                         [(now, i) for i in todo])
        taken += 1
        episodes += len(todo)
        run.record("list_history", lv["id"], "applied",
                   f"{lv['title']}: {service} progress {entry['progress']} (last changed"
                   f" {entry.get('updated_at')}) taken, {len(todo)} episodes marked watched")
    run.record("list_history", "all", "applied",
               f"{taken} levels took their list's progress ({episodes} episodes)")


def apply_list_decisions(run, conn) -> None:
    """Your answers for single entries (input `list_decisions.json`, {anilist id: {status,
    progress, why}}): the level takes that status and exactly that progress."""
    from lcars import list_sync, status_rules

    path = run.inputs / "rebuild-inputs" / "list_decisions.json"
    if not path.exists():
        return
    now = util.now_utc_iso()
    for ext, d in json.loads(path.read_text()).items():
        z = conn.execute("SELECT * FROM season WHERE anilist_id = ? AND kind != 'tvdb_season'"
                         " OR (anilist_id = ? AND kind = 'tvdb_season') ORDER BY kind = "
                         "'tvdb_season' LIMIT 1", (int(ext), int(ext))).fetchone()
        if z is None:
            run.record("list_decision", ext, "review", f"no level holds it ({d['why']})")
            continue
        # the episodes first: the status engine reads them when the status is set
        eps = list_sync.level_episodes_ordered(conn, z)
        for i, e in enumerate(eps):
            state = "watched" if i < d["progress"] else "unwatched"
            conn.execute("UPDATE episode SET state = ?, updated_at = ? WHERE id = ?",
                         (state, now, e["id"]))
        status_rules.set_level_status(conn, z["id"], d["status"], "rebuild", confirmed=True,
                                      manual=True)
        conn.execute("UPDATE season SET status = ?, status_set_manually = 1 WHERE id = ?",
                     (d["status"], z["id"]))  # yours, whatever the engine derived after
        run.record("list_decision", ext, "applied",
                   f"{d['status']} at {d['progress']} ({d['why']})")


# ── Sonarr ───────────────────────────────────────────────────────────────


def sonarr_writes(run, conn) -> dict:
    """R5.5–R5.8 for every show in Sonarr: the seasons that would change monitoring."""
    from lcars import config, sonarr_client, sonarr_sync

    cfg = config.get_current()
    if not (cfg.sonarr_url and cfg.sonarr_api_key):
        return {"shows": 0, "skipped": "Sonarr not configured"}
    by_show: dict[str, list] = {}
    for z in conn.execute("SELECT id, status FROM season WHERE show_id IS NOT NULL").fetchall():
        if z["status"] not in sonarr_sync.MONITOR and z["status"] not in sonarr_sync.STOP:
            continue
        show, number = sonarr_sync._tvdb_season(conn, z["id"])
        if show and number and number > 0:
            by_show.setdefault(show, []).append((number, z["status"]))
    changed, seasons_off, seasons_on, episodes = [], 0, 0, 0
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        for show, actions in sorted(by_show.items()):
            row = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                               " service = 'tvdb'", (show,)).fetchone()
            tv = conn.execute("SELECT tracked FROM show WHERE id = ?", (show,)).fetchone()
            if row is None or not tv or not tv[0]:
                continue
            series = client.series_by_tvdb_id(int(row[0]))
            if series is None:
                continue
            monitor, stop_from = set(), None
            for number, status in actions:
                if status in sonarr_sync.MONITOR:
                    monitor.add(number)
                else:
                    stop_from = number if stop_from is None else min(stop_from, number)
            flips = []
            for entry in series.get("seasons", []):
                n = entry.get("seasonNumber")
                want = (True if n in monitor else
                        False if stop_from is not None and n is not None and n >= stop_from
                        else None)
                if want is not None and bool(entry.get("monitored")) != want:
                    flips.append((n, want))
            if flips:
                seasons_on += sum(1 for _n, w in flips if w)
                seasons_off += sum(1 for _n, w in flips if not w)
                changed.append({"show": show, "tvdb": int(row[0]),
                                "seasons": [{"season": n, "monitored": w} for n, w in flips]})
                sonarr_sync._apply_show(cfg, int(row[0]), actions)
    return {"shows": len(changed), "seasons_monitored": seasons_on,
            "seasons_unmonitored": seasons_off, "detail": changed, "episodes": episodes}


# ── the stage ────────────────────────────────────────────────────────────


def stage_writes(run) -> None:
    from lcars import anilist_client, config, external_writes, list_sync, mal_client

    if not external_writes.capturing():
        raise _rebuild().RebuildError("the write list is captured, never sent: writes are not"
                                      " capturing")
    rb = _rebuild()
    conn = rb._connect(run.work())
    cfg = config.get_current()
    had = conn.execute("SELECT COUNT(*) FROM captured_write").fetchone()[0]
    conn.execute("DELETE FROM captured_write")
    conn.commit()
    lists = export_lists(run, refresh=bool(os.environ.get("LCARS_REBUILD_REFRESH_LISTS")))
    run.record("write_lists", "exported", "applied",
               f"anilist {len(lists['anilist'])} entries, mal {len(lists['mal'])} entries; "
               f"{had} earlier captured writes cleared")

    apply_list_decisions(run, conn)
    take_list_history(run, conn, lists)
    conn.commit()
    plan = plan_level_writes(levels_from(conn), lists)
    for w in plan["writes"]:
        fields = w["fields"]
        service_fields = list_sync._fields(w["service"], {"status": fields.get("status")},
                                           fields.get("progress"), "status" in fields)
        list_sync._save(conn, cfg, w["service"], w["id"], service_fields)
    conn.commit()

    deletes = []
    for d in explicit_deletes(run):
        for service, key in (("anilist", "anilist"), ("mal", "mal")):
            ext = d.get(key)
            if ext is None or int(ext) not in lists[service]:
                continue
            held = conn.execute(
                "SELECT 1 FROM season_external_id s JOIN season z ON z.id = s.season_id JOIN show"
                " sh ON sh.id = z.show_id WHERE s.service = ? AND s.external_id = ? AND"
                " sh.tracked = 1 AND z.status != 'skipped'", (service, str(ext))).fetchone()
            if held:
                run.record("list_delete", f"{service}:{ext}", "review",
                           f"still held by a tracked level: not deleted ({d.get('why')})")
                continue
            if service == "anilist":
                entry_id = anilist_client.fetch_my_list_entry_id(cfg.anilist_access_token, ext)
                if entry_id is not None:
                    anilist_client.delete_media_list_entry(cfg.anilist_access_token, entry_id,
                                                           anilist_id=ext)
            else:
                mal_client.delete_my_list_status(cfg.mal_access_token, ext)
            deletes.append({"service": service, "id": int(ext), "why": d.get("why"),
                            "title": lists[service][int(ext)].get("title")})
    conn.commit()

    sonarr = sonarr_writes(run, conn)
    conn.commit()

    seeded = _seed_baseline(conn, levels_from(conn), lists, plan)
    conn.commit()

    out = run.dir / "writes"
    out.mkdir(exist_ok=True)
    counts = {"anilist_changes": sum(1 for w in plan["writes"] if w["service"] == "anilist"
                                     and w["kind"] == "change"),
              "anilist_adds": sum(1 for w in plan["writes"] if w["service"] == "anilist"
                                  and w["kind"] == "add"),
              "mal_changes": sum(1 for w in plan["writes"] if w["service"] == "mal"
                                 and w["kind"] == "change"),
              "mal_adds": sum(1 for w in plan["writes"] if w["service"] == "mal"
                              and w["kind"] == "add"),
              "deletes": len(deletes), "reviews": len(plan["reviews"]),
              "sonarr_shows": sonarr.get("shows", 0),
              "captured_rows": conn.execute("SELECT COUNT(*) FROM captured_write").fetchone()[0],
              "baseline_seeded": seeded}
    (out / "write_list.json").write_text(json.dumps(
        {"made_at": util.now_utc_iso(), "counts": counts, "writes": plan["writes"],
         "reviews": plan["reviews"], "deletes": deletes,
         "untracked": {s: [{"id": i, **{k: lists[s][i].get(k) for k in
                                        ("status", "progress", "title")}} for i in ids]
                       for s, ids in plan["untracked"].items()},
         "sonarr": sonarr}, ensure_ascii=False, indent=1, default=str))
    run.record("write_list", "all", "applied", json.dumps(counts))
    run.record("stage", "writes", "applied", "")


def _seed_baseline(conn, levels: list[dict], lists: dict, plan: dict) -> int:
    """What the lists will hold once the writes are sent: their own values, overlaid with what
    is written; LCARS's decision beside it; the list's own update time."""
    from lcars import list_baseline

    written = {(w["service"], w["id"]): w for w in plan["writes"]}
    n = 0
    for lv in levels:
        if lv["status"] in (None, "skipped"):
            continue
        for service, ext in lv["ids"].items():
            entry = lists[service].get(ext) or {}
            w = written.get((service, ext))
            status = entry.get("status")
            progress = entry.get("progress")
            if w:
                status = w["fields"].get("status", status)
                progress = w["fields"].get("progress", progress)
            list_baseline.record(conn, service, ext, status=status, progress=progress,
                                 lcars_status=lv["status"], lcars_progress=lv["progress"],
                                 remote_updated_at=entry.get("updated_at"))
            n += 1
    for service in ("anilist", "mal"):
        list_baseline.mark_seeded(conn, service)
    return n

