"""Stage 9 — checks on the rebuilt copy (PLAN-DATA §2.0, §3; PLAN-CODE 9.1). Reads only.

Writes `<run>/checks/`:
- `rulecheck.json`: every rule with its count and samples;
- `reconciliation.json`: shows where you made an explicit status change after 09-06 and
  the rebuilt status is not what live has (with your decision for that change, if any);
- `cross_0826.json`: AniList entries tracked on 08-26 whose status then differs from the
  rebuilt season's, split into *explained* (the episodes are all watched now, or you
  decided it) and *to check*;
- `pages.json`: the show detail query the web client runs, sent through the real server
  (a scratch copy, no list/Sonarr credentials) for every tracked show: how many answered,
  which errors.
"""

from __future__ import annotations

import collections
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from lcars import rulecheck

GAP_START = "2026-09-06T08:53:00Z"
# the 08-26 snapshot (before the schema change), under the run's parent directory
OLD_SNAPSHOT = "old/pre-0826.db"

SHOW_DETAIL = """
query ShowDetail($id: ID!) {
  show(id: $id) {
    id displayTitle status score totalEpisodes mediaShape trackingSpace tracked
    watchedEpisodeCount availableEpisodeCount
    seasons(first: 100) { edges { node { id seasonNumber anilistId malId status
      absStart absEnd source externalIds { service externalId } } } }
    levels(first: 500) { edges { node { id seasonNumber decimalSeasonNumber kind parentId
      label partNumber anilistId malId status episodeIds } } }
    externalIds(first: 20) { edges { node { service externalId url } } }
    episodes(first: 200) { edges { node { id season episode absoluteNumber kind title
      airDateUtc state } } pageInfo { hasNextPage } }
  }
}
"""


def _rebuild():
    from lcars import rebuild

    return rebuild


def _ro(path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


_TITLE = "COALESCE(sh.display_title_override, sh.title_english, sh.title_romaji, sh.title_native)"


def rulecheck_report(path) -> list[dict]:
    conn = rulecheck.open_readonly(str(path))
    rulecheck.SAMPLE_SIZE = 25
    return [{"rule": f.rule, "title": f.title, "kind": f.kind, "count": f.count,
             "samples": f.samples, "note": f.note} for f in rulecheck.run(conn)]


def reconciliation(run, conn, live, mapper, d: dict) -> list[dict]:
    """Your explicit status changes since 09-06 (Data / Holodeck / Captain's Log) against
    what the rebuilt copy holds for that show."""
    finals: dict = collections.defaultdict(list)
    for g in d["gap_status"]:
        finals[g["show"]].append(g["final"])
    changes: dict = collections.defaultdict(list)
    for r in live.execute(
            "SELECT * FROM status_change WHERE changed_at > ? AND changed_by IN"
            " ('holodeck', 'data', 'captains_log') ORDER BY changed_at", (GAP_START,)):
        changes[r["show_id"]].append(f"{r['changed_at'][:16]} {r['previous_status']} → "
                                     f"{r['new_status']}")
    out = []
    for live_show, ch in changes.items():
        live_row = live.execute(f"SELECT {_TITLE} AS t, sh.status FROM show sh WHERE sh.id = ?",
                                (live_show,)).fetchone()
        target = mapper.show(live_show)
        rebuilt = conn.execute("SELECT status FROM show WHERE id = ?", (target,)).fetchone() \
            if target else None
        same = bool(rebuilt) and live_row is not None and rebuilt[0] == live_row["status"]
        if same:
            continue
        out.append({
            "title": live_row["t"] if live_row else live_show, "live_show": live_show,
            "rebuilt_show": target, "changes": ch,
            "live_status": live_row["status"] if live_row else None,
            "rebuilt_status": rebuilt[0] if rebuilt else None, "decision": finals.get(live_show),
        })
    return out


def _my_list(run) -> dict:
    raw = json.load(open(run.inputs / "anilist_list.json"))
    coll = (raw.get("data") or raw).get("MediaListCollection") or raw
    return {e["mediaId"]: e["status"] for lst in coll["lists"] for e in lst["entries"]}


def cross_0826(run, conn) -> dict:
    """08-26 statuses (season level, mostly right) against the rebuilt seasons, by AniList id."""
    from lcars import status_rules

    old_path = run.dir.parent / OLD_SNAPSHOT
    if not old_path.exists():
        return {"skipped": f"{old_path} not found", "rows": []}
    old = _ro(old_path)
    mine = _my_list(run)
    rows, same, missing = [], 0, 0
    for r in old.execute(
            f"SELECT sh.id, {_TITLE} AS title, sh.status, x.external_id FROM show sh JOIN"
            " show_external_id x ON x.show_id = sh.id AND x.service = 'anilist'"
            " WHERE sh.tracked = 1"):
        aid = int(r["external_id"])
        levels = conn.execute("SELECT * FROM season WHERE anilist_id = ? OR EXISTS (SELECT 1 FROM"
                              " season_external_id s WHERE s.season_id = season.id AND"
                              " s.service = 'anilist' AND s.external_id = ?)",
                              (aid, str(aid))).fetchall()
        if not levels:
            missing += 1
            continue
        lvl = levels[0]
        if lvl["status"] == r["status"]:
            same += 1
            continue
        eps = status_rules.level_episodes(conn, lvl)
        watched = sum(1 for e in eps if e["state"] == "watched")
        explained = None
        if lvl["status"] == "completed" and eps and watched == len(eps):
            explained = "every episode of the level is watched"
        elif lvl["status_set_manually"]:
            explained = "a status you gave in a review"
        rows.append({
            "title": r["title"], "anilist": aid, "status_0826": r["status"],
            "status_rebuilt": lvl["status"], "watched": watched, "episodes": len(eps),
            "your_list_0927": mine.get(aid), "explained": explained,
        })
    to_check = [x for x in rows if not x["explained"]]
    return {"tracked_0826": same + missing + len(rows), "same": same,
            "no_rebuilt_season": missing, "differ": len(rows),
            "explained": len(rows) - len(to_check), "rows": to_check}


def check_pages(run, db_path: Path, limit: int | None = None) -> dict:
    """The web client's show query for every tracked show, through the real server."""
    import httpx

    out = run.dir / "checks"
    scratch = out / "ui.db"
    shutil.copy(db_path, scratch)
    token = "checks-" + os.urandom(6).hex()
    port = 8891
    env = {**os.environ, "LCARS_DB_PATH": str(scratch), "LCARS_BEARER_TOKEN": token,
           "LCARS_WEB_ROOT": str(Path(__file__).resolve().parents[2] / "ui" / "src"),
           "LCARS_ANILIST_ACCESS_TOKEN": "", "LCARS_ANILIST_CLIENT_SECRET": "",
           "LCARS_MAL_ACCESS_TOKEN": "", "LCARS_MAL_REFRESH_TOKEN": "",
           "LCARS_MAL_CLIENT_SECRET": "",
           "LCARS_SONARR_URL": "http://127.0.0.1:9", "LCARS_SONARR_API_KEY": "",
           "LCARS_RADARR_URL": "http://127.0.0.1:9", "LCARS_RADARR_API_KEY": "",
           "LCARS_EXTERNAL_WRITES": "capture", "LCARS_AUTOMATION_FROZEN": "1"}
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "lcars.server:create_app", "--factory", "--host",
         "127.0.0.1", "--port", str(port), "--log-level", "warning"],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    result = {"asked": 0, "answered": 0, "errors": collections.Counter(), "examples": []}
    try:
        client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30.0,
                              headers={"Authorization": f"Bearer {token}",
                                       "X-LCARS-Client": "holodeck"})
        for _ in range(60):
            try:
                if client.post("/", json={"query": "{ __typename }"}).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        ids = [r[0] for r in _ro(db_path).execute(
            "SELECT id FROM show WHERE tracked = 1 ORDER BY id")]
        for sid in ids[:limit] if limit else ids:
            result["asked"] += 1
            try:
                body = client.post(
                    "/", json={"query": SHOW_DETAIL, "variables": {"id": sid}}).json()
            except (httpx.HTTPError, ValueError) as e:
                body = {"errors": [{"message": f"transport: {e}"}]}
            if body.get("errors") or not (body.get("data") or {}).get("show"):
                msg = (body.get("errors") or [{"message": "no show"}])[0]["message"][:120]
                result["errors"][msg] += 1
                if len(result["examples"]) < 10:
                    result["examples"].append({"show": sid, "error": msg})
            else:
                result["answered"] += 1
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    result["errors"] = dict(result["errors"])
    return result


def stage_checks(run) -> None:
    from lcars import rebuild_replay

    rb = _rebuild()
    out = run.dir / "checks"
    out.mkdir(exist_ok=True)
    d = rb.decisions(run)
    work = run.work()

    report = rulecheck_report(work)
    (out / "rulecheck.json").write_text(json.dumps(report, indent=1, ensure_ascii=False))
    open_rules = {r["rule"]: r["count"] for r in report if r["count"]}
    run.record("check_rulecheck", "all", "applied", f"open: {open_rules or 'none'}")

    conn = rb._connect(work)
    live = _ro(run.live)
    mapper = rebuild_replay.Mapper(run, conn, live)
    recon = reconciliation(run, conn, live, mapper, d)
    (out / "reconciliation.json").write_text(json.dumps(recon, indent=1, ensure_ascii=False))
    run.record("check_reconciliation", "all", "applied",
               f"{len(recon)} shows where your status change differs from the rebuilt copy")
    cross = cross_0826(run, conn)
    (out / "cross_0826.json").write_text(json.dumps(cross, indent=1, ensure_ascii=False))
    run.record("check_cross_0826", "all", "applied",
               f"{cross.get('same')} same, {cross.get('explained')} explained, "
               f"{len(cross.get('rows', []))} to check, "
               f"{cross.get('no_rebuilt_season')} with no season")
    live.close()
    conn.close()

    pages = check_pages(run, work)
    (out / "pages.json").write_text(json.dumps(pages, indent=1, ensure_ascii=False))
    run.record("check_pages", "all", "applied",
               f"{pages['answered']} of {pages['asked']} shows answered the show-page query; "
               f"errors: {pages['errors'] or 'none'}")
    run.record("stage", "checks", "applied", "")
