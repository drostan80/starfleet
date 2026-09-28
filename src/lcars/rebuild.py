"""The rebuild (PLAN-CODE 9.1; PLAN-DATA §1–§3, "Phase 9.1 decisions").

    lcars rebuild <run-dir> --snapshot <09-06 file> --live <live backup> --inputs <dir>
                  [--from-stage N] [--until-stage N]

Builds the new database from the 09-06 snapshot (the 08:53Z state) with the new
engines and the user's decisions, on copies only. Every stage ends in a labelled
checkpoint (`<run-dir>/NN-<stage>.db`), so a later stage can be re-run from the
previous one and any point can be rolled back to. External writes are always
captured here (PLAN-CODE 9.0), whatever the config says; reads (Sonarr, AniList,
MAL, TVDB) are real.

Every input decision is consumed exactly once and written to the ledger
(`<run-dir>/ledger.jsonl`: applied / skipped-with-reason); a decision that can't
be placed fails the run. The ledger is the proof that everything the user
decided landed.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from lcars import util

STAGES = [
    "base", "sources", "structure", "sonarr", "numbering", "statuses", "replay", "checks",
    "writes",
]
SOURCE_TABLES = [
    "anidb_anime", "anidb_title", "anidb_episode", "anime_list_entry", "anime_list_mapping",
    "tvmaze_episode", "syoboi_title", "syoboi_program",
]


class RebuildError(RuntimeError):
    """A decision or input that can't be placed: the run stops."""


@dataclass
class Run:
    dir: Path
    snapshot: Path
    live: Path
    inputs: Path
    ledger: list = field(default_factory=list)
    stage: str = ""

    def checkpoint(self, n: int) -> Path:
        return self.dir / f"{n:02d}-{STAGES[n - 1]}.db"

    def work(self) -> Path:
        return self.dir / "work.db"

    def record(self, kind: str, key: str, outcome: str, detail: str = "") -> None:
        entry = {"at": util.now_utc_iso(), "stage": self.stage, "kind": kind, "key": key,
                 "outcome": outcome, "detail": detail}
        self.ledger.append(entry)
        with open(self.dir / "ledger.jsonl", "a") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _backup(src: Path, dst: Path) -> None:
    """A consistent copy (`.backup`, never `cp` — PLAN-DATA §1)."""
    if dst.exists():
        dst.unlink()
    with sqlite3.connect(f"file:{src}?mode=ro", uri=True) as s, sqlite3.connect(dst) as d:
        s.backup(d)


def _connect(path: Path) -> sqlite3.Connection:
    from lcars import db

    db.close()
    return db.connect(path)


# ── stage 1: base ────────────────────────────────────────────────────────


def stage_base(run: Run) -> None:
    """The 09-06 snapshot, upgraded to the current schema."""
    _backup(run.snapshot, run.work())
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{run.work()}"},
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise RebuildError("schema upgrade failed:\n" + done.stderr[-2000:])
    run.record("stage", "base", "applied", str(run.snapshot))


# ── stage 2: sources ─────────────────────────────────────────────────────


def stage_sources(run: Run) -> None:
    """Source tables from live (PLAN-DATA "Source data first"), plus the
    AniDB answers fetched on 09-28 (`<inputs>/anidb_xml/*.xml`)."""
    import xml.etree.ElementTree as ET

    from lcars import anidb

    conn = _connect(run.work())
    conn.execute("ATTACH DATABASE ? AS live", (str(run.live),))  # the file is read-only
    for table in SOURCE_TABLES:
        cols = [r[1] for r in conn.execute(f"PRAGMA main.table_info('{table}')")]
        live_cols = {r[1] for r in conn.execute(f"PRAGMA live.table_info('{table}')")}
        shared = [c for c in cols if c in live_cols]
        if not shared:
            raise RebuildError(f"source table {table} missing from live")
        conn.execute(f"DELETE FROM main.{table}")
        conn.execute(f"INSERT INTO main.{table} ({', '.join(shared)})"
                     f" SELECT {', '.join(shared)} FROM live.{table}")
        n = conn.execute(f"SELECT COUNT(*) FROM main.{table}").fetchone()[0]
        run.record("source", table, "applied", f"{n} rows")
    conn.commit()
    conn.execute("DETACH DATABASE live")
    xml_dir = run.inputs / "anidb_xml"
    fetched = 0
    for path in sorted(xml_dir.glob("*.xml")):
        anidb_id = int(path.stem.split("_")[-1]) if path.stem.split("_")[-1].isdigit() else None
        if anidb_id is None:
            run.record("anidb_xml", path.name, "skipped", "no AniDB id in the file name")
            continue
        root = ET.parse(path).getroot()
        if root.tag == "error":
            run.record("anidb_xml", path.name, "skipped", f"AniDB error: {root.text}")
            continue
        episodes = anidb._parse_episodes_xml(root)
        conn.execute("DELETE FROM anidb_episode WHERE anidb_anime_id = ?", (anidb_id,))
        anidb.ingest_anime_episodes(conn, anidb_id, episodes, "2026-09-28T00:00:00Z")
        fetched += 1
        run.record("anidb_xml", path.name, "applied", f"{len(episodes)} episodes")
    conn.commit()
    run.record("stage", "sources", "applied", f"{fetched} AniDB answers ingested")


# ── stage 3: structure (the user's decisions) ────────────────────────────


def decisions(run: Run) -> dict:
    return json.load(open(run.inputs / "rebuild-inputs" / "decisions.json"))


def _mine(conn, season_id: str, status: str) -> None:
    """A status the user decided in this rebuild's reviews (PLAN-DATA 9.1
    decision 2: everything reviewed since step 0 is theirs)."""
    from lcars import season_status_log

    season_status_log.set_status(conn, season_id, status, "rebuild")
    conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = ?", (season_id,))


def _show_for(conn, t: dict):
    if t.get("show"):
        row = conn.execute("SELECT * FROM show WHERE id = ?", (t["show"],)).fetchone()
        if row is not None:
            return row
    if t.get("anilist"):
        return conn.execute(
            "SELECT sh.* FROM season z JOIN show sh ON sh.id = z.show_id WHERE z.anilist_id = ?"
            " ORDER BY sh.tracked DESC", (t["anilist"],)).fetchone()
    return None


def _set_link(conn, show_id: str, service: str, value) -> None:
    from lcars import shows

    conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = ?",
                 (show_id, service))
    if value is not None:
        url = shows._EXTERNAL_ID_URL_TEMPLATES.get(service, "").format(id=value)
        conn.execute(
            "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
            " VALUES (?, ?, ?, ?, ?)", (show_id, service, str(value), url, util.now_utc_iso()))


def _as_pieces(conn, show_id: str) -> None:
    """The show's seasons become special levels (placed later by Memory
    Alpha, R1.8/R1.13a), keeping their status and list ids."""
    conn.execute("UPDATE season SET kind = 'special', season_number = NULL,"
                 " decimal_season_number = NULL, parent_id = NULL WHERE show_id = ?"
                 " AND kind = 'tvdb_season'", (show_id,))


def _tracked_tvdb_show(conn, tvdb_id, not_show=None):
    return conn.execute(
        "SELECT sh.* FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
        " WHERE x.service = 'tvdb' AND x.external_id = ? AND sh.tracked = 1 AND sh.id != ?"
        " ORDER BY sh.created_at", (str(tvdb_id), not_show or "")).fetchone()


def _fold_into(conn, winner: str, loser: str, why: str) -> None:
    from lcars import show_merge

    _as_pieces(conn, loser)
    conn.execute("UPDATE season_status_change SET show_id = ? WHERE show_id = ?",
                 (winner, loser))
    tracked = conn.execute("SELECT tracked FROM show WHERE id = ?", (loser,)).fetchone()[0]
    if tracked:
        show_merge.merge_shows(conn, winner, loser, why)
    else:  # an untracked piece: only its seasons (status, ids) come across
        conn.execute("UPDATE season SET show_id = ? WHERE show_id = ?", (winner, loser))


PIECE_FORMATS = {"MOVIE", "SPECIAL", "OVA", "ONA", "MUSIC", "TV_SHORT_SPECIAL"}


def _place_without_fribb(run: Run, conn, tvdb_id: str, anilist_index) -> dict:
    """Seasons of a same-TVDB group Fribb can't place yet (new sequels, pieces;
    the group exists because the user confirmed the TVDB link). The user's
    standing rules: a sequel is a new season of the same show; a piece is its
    own level; a season with no list id keeps the TVDB number Sonarr gave it.
    Each placement is recorded for the 9.2 review."""
    from lcars import anilist_client, consolidation

    show_ids = consolidation._groups(conn).get(str(tvdb_id)) or []
    from lcars import season_ranges

    places = {sid: consolidation._fribb_place(
        anilist_index, season_ranges.show_list_id(conn, sid, "anilist")) for sid in show_ids}
    winner = next((sid for sid in show_ids if places[sid][0] == 1),
                  show_ids[0] if show_ids else None)
    top = conn.execute("SELECT MAX(season_number) FROM season WHERE show_id = ? AND"
                       " kind = 'tvdb_season'", (winner,)).fetchone()[0] or 0
    placed = {}
    for sid in show_ids:
        if sid == winner:
            continue
        for z in conn.execute(
                "SELECT id, anilist_id, season_number FROM season WHERE show_id = ?"
                " AND kind = 'tvdb_season' ORDER BY season_number", (sid,)).fetchall():
            if consolidation._fribb_place(anilist_index, z["anilist_id"])[0] is not None:
                continue
            if z["anilist_id"] is None:
                placed[z["id"]], why = (z["season_number"], 0), "no list id: its own TVDB number"
            else:
                facts = None
                try:
                    facts = anilist_client.fetch_media_facts(z["anilist_id"])
                except anilist_client.AniListError:
                    pass
                fmt = ((facts or {}).get("format") or "").upper()
                start = _iso_date((facts or {}).get("startDate"))
                inside = _season_airing_on(conn, winner, start) if start else None
                if fmt in PIECE_FORMATS:
                    placed[z["id"]], why = (0, 0), f"{fmt}: its own level"
                elif inside is not None:
                    placed[z["id"]], why = (inside, 0), (
                        f"{fmt or 'TV'} starting {start}: inside TVDB season {inside}'s airing")
                else:
                    top += 1
                    placed[z["id"]], why = (top, 0), (
                        f"{fmt or 'TV'} starting {start or '?'}: new TVDB season {top}")
            run.record("placement", f"{tvdb_id}:{z['id']}", "applied",
                       f"placed without Fribb (review in 9.2) — {why}")
    return placed


def _iso_date(fuzzy: dict | None) -> str | None:
    if not fuzzy or not fuzzy.get("year"):
        return None
    return f"{fuzzy['year']:04d}-{fuzzy.get('month') or 1:02d}-{fuzzy.get('day') or 1:02d}"


def _season_airing_on(conn, show_id: str, date: str) -> int | None:
    """The TVDB season of `show_id` whose episodes were airing on `date`
    (first episode − 14 days … last episode)."""
    for n, first, last in conn.execute(
            "SELECT season, MIN(air_date_utc), MAX(air_date_utc) FROM episode"
            " WHERE show_id = ? AND season > 0 AND air_date_utc IS NOT NULL GROUP BY season",
            (show_id,)):
        if first and date >= _days_before(first[:10], 14) and date <= last[:10]:
            return n
    return None


def _days_before(day: str, n: int) -> str:
    from datetime import date, timedelta

    return (date.fromisoformat(day) - timedelta(days=n)).isoformat()


def stage_structure(run: Run) -> None:
    from lcars import consolidation, fribb

    conn = _connect(run.work())
    d = decisions(run)

    # a) the 1,700 accepted season statuses, by their 09-06 season ids (before
    #    anything changes ids — merges and numbering carry them forward).
    for s in d["season_statuses"]:
        if conn.execute("SELECT 1 FROM season WHERE id = ?", (s["season"],)).fetchone() is None:
            raise RebuildError(f"season status for a season not in 09-06: {s['key']}")
        _mine(conn, s["season"], s["status"])
        run.record("season_status", s["key"], "applied", s["status"])
    conn.commit()

    # b0) Show-level TVDB ids come from today's data — the latest fixes (PLAN-DATA
    #     "TVDB alignment": 1,147 identical, 35 gained since 09-06, 0 conflicting);
    #     season placement doesn't (Memory Alpha re-derives it).
    conn.execute("ATTACH DATABASE ? AS live", (str(run.live),))
    live_ids = dict(conn.execute(
        "SELECT show_id, external_id FROM live.show_external_id WHERE service = 'tvdb'"))
    by_list = dict(conn.execute(
        "SELECT z.anilist_id, x.external_id FROM live.season z"
        " JOIN live.show_external_id x ON x.show_id = z.show_id AND x.service = 'tvdb'"
        " WHERE z.anilist_id IS NOT NULL"))
    conn.execute("DETACH DATABASE live")
    changed = gained = 0
    for show in conn.execute("SELECT id FROM show WHERE tracked = 1").fetchall():
        sid = show["id"]
        today = live_ids.get(sid)
        if today is None:
            anilist = conn.execute("SELECT anilist_id FROM season WHERE show_id = ? AND"
                                   " anilist_id IS NOT NULL ORDER BY season_number LIMIT 1",
                                   (sid,)).fetchone()
            today = by_list.get(anilist[0]) if anilist else None
        if today is None:
            continue
        had = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                           " service = 'tvdb'", (sid,)).fetchone()
        if had is None or had[0] != today:
            _set_link(conn, sid, "tvdb", today)
            gained += had is None
            changed += had is not None
    run.record("tvdb_from_live", "all shows", "applied",
               f"{gained} gained a TVDB id, {changed} changed to today's")
    conn.commit()

    # b) TVDB decisions (PLAN-DATA "TVDB decisions — authoritative").
    folds = []
    for t in d["tvdb"]:
        show = _show_for(conn, t)
        if show is None:
            run.record("tvdb", t["key"], "deferred", "show created after 09-06 (new shows)")
            continue
        sid, action, value = show["id"], t["action"], t["value"]
        detail = f"{action} {value or ''} on {sid}".strip()
        if t.get("note_differs"):
            detail += f" (user note differed: {t['note_differs']}; PLAN-DATA followed)"
        if action == "series":
            _set_link(conn, sid, "tvdb", value)
        elif action == "movie":
            _set_link(conn, sid, "tvdb", None)
            _set_link(conn, sid, "tvdb_movie", value)
        elif action == "own_show":
            _set_link(conn, sid, "tvdb", None)  # R3.3 exception, user-checked
            detail += " (R3.3: own show, no TVDB id — user-checked)"
        elif action == "remove":
            conn.execute("UPDATE show SET tracked = 0, status = 'skipped' WHERE id = ?", (sid,))
        elif action == "parent" and conn.execute(
                "SELECT 1 FROM show_external_id WHERE show_id = ? AND service = 'tvdb'"
                " AND external_id = ?", (sid, str(value))).fetchone():
            detail += " (already inside its parent show)"
        elif action in ("parent", "parent_show"):
            parent = (_tracked_tvdb_show(conn, value, sid) if action == "parent"
                      else conn.execute(
                          "SELECT * FROM show WHERE tracked = 1 AND id != ? AND"
                          " (title_english = ? OR title_romaji LIKE ?) ORDER BY created_at",
                          (sid, value, "Dungeon Meshi%")).fetchone())
            if parent is None:
                raise RebuildError(f"{t['key']}: parent {value} isn't a tracked show")
            folds.append((parent["id"], sid, t["key"]))
            detail += f" → folded into {parent['id']}"
        run.record("tvdb", t["key"], "applied", detail)
    conn.commit()
    for parent_id, sid, key in folds:
        _fold_into(conn, parent_id, sid, f"{key}: belongs to its parent show (PLAN-DATA)")
    conn.commit()

    # c) films fold into their show as a special level (PLAN-CODE 3.3).
    dataset = fribb.load_dataset()
    plan = consolidation.plan(conn, dataset, {})
    into = {f["show_id"]: f["into"] for f in plan["films"]}
    for f in d["phase5"]["films"]:
        target = into.get(f["show"])
        if target is None:
            raise RebuildError(f"film {f['key']}: no show to fold into")
        conn.execute("UPDATE season SET show_id = ?, kind = 'special', season_number = NULL,"
                     " decimal_season_number = NULL WHERE show_id = ?", (target, f["show"]))
        conn.execute("UPDATE season_status_change SET show_id = ? WHERE show_id = ?",
                     (target, f["show"]))
        conn.execute("UPDATE show SET tracked = 0 WHERE id = ?", (f["show"],))
        run.record("film", f["key"], "applied", f"→ {target} (episode link after numbering)")
    conn.commit()
    # d) same-TVDB merges (all 181 OK, phase 5 page).
    merged = set()
    anilist_index = fribb.build_anilist_index(dataset)
    for tvdb_id in sorted(consolidation._groups(conn)):
        placed = _place_without_fribb(run, conn, tvdb_id, anilist_index)
        result = consolidation.apply_group(conn, tvdb_id, dataset, placed)
        if not result.get("merged"):
            raise RebuildError(f"merge of TVDB {tvdb_id} stopped: {result.get('reason')}")
        merged.update(result["merged"])
        merged.add(result["winner"])
    for m in d["phase5"]["merges"]:
        outcome = "applied" if m["show"] in merged else "skipped"
        run.record("merge", m["key"], outcome,
                   "" if outcome == "applied" else "no longer shares its TVDB id")
    conn.commit()
    _structure_adds(run, conn, d)
    run.record("stage", "structure", "applied", "")


# ── stage 3, part 2: adds (through the one add check, 8.8 included) ───────

LIST_STATUS = {"CURRENT": "watching", "PLANNING": "planned", "COMPLETED": "completed",
               "PAUSED": "paused", "DROPPED": "dropped", "REPEATING": "watching"}


def _anilist_entries(run: Run) -> dict:
    raw = json.load(open(run.inputs / "anilist_list.json"))
    return {e["mediaId"]: e for lst in raw["data"]["MediaListCollection"]["lists"]
            for e in lst["entries"]}


def _live(run: Run):
    live = sqlite3.connect(f"file:{run.live}?mode=ro", uri=True)
    live.row_factory = sqlite3.Row
    return live


def _live_input(live, show_id: str) -> dict | None:
    row = live.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    if row is None:
        return None
    ids = dict(live.execute("SELECT service, external_id FROM show_external_id"
                            " WHERE show_id = ?", (show_id,)).fetchall())
    first = live.execute("SELECT anilist_id, mal_id FROM season WHERE show_id = ? AND"
                         " (anilist_id IS NOT NULL OR mal_id IS NOT NULL)"
                         " ORDER BY season_number LIMIT 1", (show_id,)).fetchone()
    inp = {"media_shape": row["media_shape"], "tracking_space": row["tracking_space"],
           "title_english": row["title_english"], "title_romaji": row["title_romaji"],
           "title_native": row["title_native"], "primary_title": row["primary_title"]}
    for service, key, cast in (("anilist", "anilist_id", int), ("mal", "mal_id", int),
                               ("tvdb", "tvdb_id", int), ("tmdb", "tmdb_id", int),
                               ("imdb", "imdb_id", str)):
        value = ids.get(service)
        if value is None and first is not None and service in ("anilist", "mal"):
            value = first[f"{service}_id"]
        if value not in (None, "", "-1"):
            inp[key] = cast(value)
    if not inp.get(f"title_{inp['primary_title']}"):
        inp["primary_title"] = next(t for t in ("romaji", "english", "native")
                                    if inp.get(f"title_{t}"))
    return inp


def _add(run: Run, conn, kind: str, key: str, inp: dict, status: str | None = None):
    """One add through `shows.add_checked` (vetting, add check, Sonarr add —
    captured). Returns the new level's id; outcomes that need the user go to
    the ledger as `review` (shown in 9.2), not a failure."""
    from lcars import add_check, shows, status_rules

    try:
        target = shows.add_checked(conn, dict(inp))
        level = conn.execute("SELECT id FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
                             " ORDER BY season_number DESC LIMIT 1", (target,)).fetchone()
        level_id = level[0] if level else None
        run.record(kind, key, "applied", f"show {target}")
    except shows.SequelDetectedError as e:
        c = add_check.Candidate(add_check.USER, anilist_id=inp.get("anilist_id"),
                                mal_id=inp.get("mal_id"), tvdb_id=inp.get("tvdb_id"),
                                titles=[inp[k] for k in ("title_english", "title_romaji")
                                        if inp.get(k)])
        existing = conn.execute("SELECT id FROM season WHERE show_id = ? AND season_number = ?"
                                " AND kind = 'tvdb_season'",
                                (e.parent_show_id, e.next_season)).fetchone()
        d = add_check.Decision("link_season" if existing else "new_season", e.parent_show_id,
                               existing[0] if existing else None, season_number=e.next_season)
        level_id = add_check.apply_decision(conn, d, c)
        run.record(kind, key, "applied",
                   f"season {e.next_season} of {e.parent_show_id} ({e.parent_title})")
    except shows.IndividualSeasonAdded as e:
        level_id = e.season_id
        run.record(kind, key, "applied", f"individual season {e.season_id}")
    except shows.ShowInputError as e:
        conn.rollback()
        if inp.get("user_tvdb_decision") and "Fribb says" in str(e):
            # The user settled this TVDB id on TVDB itself (PLAN-DATA "TVDB
            # decisions"): their decision is the second source (R3.7a).
            target = shows.create_show(conn, {**inp, "skip_sequel_check": True})
            shows._add_new_show_to_sonarr(conn, target, inp)
            level = conn.execute("SELECT id FROM season WHERE show_id = ? AND"
                                 " kind = 'tvdb_season' ORDER BY season_number DESC LIMIT 1",
                                 (target,)).fetchone()
            level_id = level[0] if level else None
            run.record(kind, key, "applied",
                       f"show {target}: {inp['user_tvdb_decision']} over Fribb ({e})"[:500])
        else:
            run.record(kind, key, "review", str(e)[:500])
            return None
    if status and level_id:
        try:
            status_rules.set_level_status(conn, level_id, status, "rebuild", confirmed=True,
                                          manual=True)
        except status_rules.NeedsConfirmation:
            _mine(conn, level_id, status)
        conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = ?", (level_id,))
    conn.commit()
    return level_id


def _tracked_parent_for(conn, anilist_id: int):
    """A season-0 piece's show: its Fribb TVDB show, else the show of its
    AniList parent/prequel (AniList relations, read)."""
    from lcars import add_check, anilist_client, fribb

    for e in fribb.load_dataset():
        if e.get("anilist_id") == anilist_id and e.get("tvdb_id"):
            show = add_check._tracked_show_for_tvdb(conn, e["tvdb_id"])
            if show is not None:
                return show["id"]
    try:
        media = anilist_client.fetch_media(anilist_id) or {}
    except anilist_client.AniListError:
        return None
    for edge in (media.get("relations") or {}).get("edges") or []:
        if edge["relationType"] in ("PARENT", "PREQUEL", "SOURCE", "SIDE_STORY"):
            row = conn.execute("SELECT show_id FROM season WHERE anilist_id = ? AND"
                               " show_id IS NOT NULL", (edge["node"]["id"],)).fetchone()
            if row:
                return row[0]
    return None


def _pending(run: Run, kind: str, item: dict) -> None:
    """Work a later stage does (list deletes are written in stage 9)."""
    with open(run.dir / "pending.jsonl", "a") as f:
        f.write(json.dumps({"stage": run.stage, "kind": kind, **item}) + "\n")


def _structure_adds(run: Run, conn, d: dict) -> None:
    from lcars import add_check, shows

    live = _live(run)
    entries = _anilist_entries(run)
    tvdb_by_anilist = {t["anilist"]: t for t in d["tvdb"] if t["anilist"]}

    # New shows since 09-06 (77), as reviewed.
    for n in d["new_shows"]:
        inp = _live_input(live, n["show"])
        if inp is None:
            raise RebuildError(f"{n['key']}: not in the live copy")
        t = tvdb_by_anilist.get(inp.get("anilist_id"))
        if t and t["action"] == "series":
            inp["tvdb_id"] = t["value"]
            inp["user_tvdb_decision"] = t["key"]
            run.record("tvdb", t["key"], "applied", f"series {t['value']} on the new show")
        elif t and t["action"] == "keep":
            inp["user_tvdb_decision"] = t["key"]
            run.record("tvdb", t["key"], "applied", "keep today's id on the new show")
        if n["action"] == "keep_skipped_no_arr":
            if inp["media_shape"] == "movie" and "Urusei Yatsura" in (n["title"] or ""):
                _pending(run, "urusei_film", {"key": n["key"], "title": n["title"],
                                              "tmdb": inp.get("tmdb_id")})
                run.record("new_show", n["key"], "deferred",
                           "film placed in Urusei Yatsura by numbering, then skipped (stage 6)")
                continue
            sid = shows.create_show(conn, {**inp, "skip_sequel_check": True})
            conn.execute("UPDATE show SET status = 'skipped' WHERE id = ?", (sid,))
            conn.execute("UPDATE season SET status = 'skipped', status_set_manually = 1"
                         " WHERE show_id = ?", (sid,))
            conn.commit()
            run.record("new_show", n["key"], "applied", f"show {sid}, skipped, no Sonarr/Radarr")
            continue
        _add(run, conn, "new_show", n["key"], inp,
             "watching" if n["action"] == "add_watching" else None)

    # Phase 5: the 22 entries from your AniList list.
    for a in d["phase5"]["adds"]:
        e = entries.get(a["anilist"]) or {}
        media = e.get("media") or {}
        title = media.get("title") or {}
        inp = {"media_shape": "movie" if media.get("format") == "MOVIE" else "episodic",
               "tracking_space": "anime", "title_romaji": title.get("romaji"),
               "title_english": title.get("english"), "primary_title": "romaji",
               "anilist_id": a["anilist"], "mal_id": media.get("idMal")}
        status = LIST_STATUS.get(e.get("status"))
        c = add_check.Candidate(add_check.USER, anilist_id=a["anilist"], mal_id=media.get("idMal"),
                                titles=[x for x in (title.get("english"), title.get("romaji"))
                                        if x],
                                media_type=media.get("format"), status=e.get("status"))
        action = a["action"]
        if action in ("special", "attach_anilist_parent"):
            if add_check._season_by_list_id(conn, c.anilist_id, c.mal_id) is not None:
                run.record("phase5_add", a["key"], "applied", "already tracked")
                continue
            if action == "special":
                parent = _tracked_parent_for(conn, a["anilist"])
            else:
                row = conn.execute("SELECT show_id FROM season WHERE anilist_id = ? AND"
                                   " show_id IS NOT NULL", (a["value"],)).fetchone()
                parent = row[0] if row else None
            if parent is None:
                run.record("phase5_add", a["key"], "review", "no tracked show to attach it to")
                continue
            lvl = add_check.apply_decision(
                conn, add_check.Decision("special", parent, season_number=0), c)
            if action == "special":
                _mine(conn, lvl, "completed" if a["value"] else "planned")
            elif status:
                _mine(conn, lvl, status)
            run.record("phase5_add", a["key"], "applied", f"level {lvl} of {parent}")
        elif action == "individual":
            lvl = add_check.create_individual_season(conn, c, status)
            conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = ?", (lvl,))
            run.record("phase5_add", a["key"], "applied", f"individual season {lvl}")
        elif action == "part":
            _add(run, conn, "phase5_add", a["key"], inp, status)
        elif action == "tvdb":
            _add(run, conn, "phase5_add", a["key"], {**inp, "tvdb_id": a["value"]}, status)
        elif action == "skip_and_list_delete":
            lvl = _add(run, conn, "phase5_add", a["key"], inp, None)
            if lvl:
                _mine(conn, lvl, "skipped")
            _pending(run, "list_delete", {"anilist": a["anilist"], "mal": media.get("idMal"),
                                          "why": a["key"]})
        elif action == "remove_list_skip_tvdb":
            shows.skip_show(conn, {**inp, "tvdb_id": a["value"]})
            _pending(run, "list_delete", {"anilist": a["anilist"], "mal": media.get("idMal"),
                                          "why": a["key"]})
            run.record("phase5_add", a["key"], "applied",
                       f"not added; TVDB {a['value']} on the skip list; list delete queued")
        conn.commit()

    # Magical Girl Raising Project: restart — not followed, on the skip list.
    shows.skip_show(conn, {"media_shape": "episodic", "tracking_space": "anime",
                           "title_romaji": "Mahou Shoujo Ikusei Keikaku: restart",
                           "primary_title": "romaji", "anilist_id": 160803})
    _pending(run, "list_delete", {"anilist": 160803, "why": "tvmaho"})
    run.record("tvdb", "tvmaho", "applied", "skip list + list delete queued")

    # Books Bought with Tax (live action), planned — the user's own new add.
    for x in d["extra_adds"]:
        _add(run, conn, "extra_add", x["key"],
             {"media_shape": "episodic", "tracking_space": "tv", "title_english": x["title"],
              "primary_title": "english", "tvdb_id": x["tvdb"]}, x["status"])

    # The 178 skipped (the user's own skip list).
    for k in d["skipped"]:
        inp = _live_input(live, k["show"])
        if inp is None:
            raise RebuildError(f"{k['key']}: not in the live copy")
        run.record("skipped", k["key"], "applied", shows.skip_show(conn, inp))
    conn.commit()
    live.close()


STAGE_FUNCS = {"base": stage_base, "sources": stage_sources, "structure": stage_structure}


def main(argv=None) -> int:
    import argparse

    from lcars import config

    parser = argparse.ArgumentParser(prog="lcars rebuild", description=__doc__.split("\n\n")[0])
    parser.add_argument("run_dir")
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--live", required=True)
    parser.add_argument("--inputs", required=True)
    parser.add_argument("--from-stage", type=int, default=1)
    parser.add_argument("--until-stage", type=int, default=len(STAGES))
    args = parser.parse_args(argv)

    os.environ["LCARS_EXTERNAL_WRITES"] = "capture"  # never sends, whatever the config says
    os.environ["LCARS_AUTOMATION_FROZEN"] = "0"  # the engines run — on this copy only
    config.set_current(config.load_config())
    run = Run(*(Path(p).expanduser().resolve()
                for p in (args.run_dir, args.snapshot, args.live, args.inputs)))
    run.dir.mkdir(parents=True, exist_ok=True)
    if args.from_stage > 1:
        _backup(run.checkpoint(args.from_stage - 1), run.work())
    for name in ("pending.jsonl",):  # later-stage work a re-run recomputes
        f = run.dir / name
        if f.exists():
            keep = [line for line in f.read_text().splitlines()
                    if STAGES.index(json.loads(line)["stage"]) < args.from_stage - 1]
            f.write_text("".join(line + "\n" for line in keep))
    ledger = run.dir / "ledger.jsonl"
    if ledger.exists():  # a re-run replaces what those stages recorded before
        keep = [line for line in ledger.read_text().splitlines()
                if STAGES.index(json.loads(line).get("stage") or "base") < args.from_stage - 1]
        ledger.write_text("".join(line + "\n" for line in keep))
    for n in range(args.from_stage, args.until_stage + 1):
        name = STAGES[n - 1]
        if name not in STAGE_FUNCS:
            raise RebuildError(f"stage {n} ({name}) isn't built yet")
        print(f"stage {n}: {name}", flush=True)
        run.stage = name
        STAGE_FUNCS[name](run)
        from lcars import db

        db.close()
        _backup(run.work(), run.checkpoint(n))
    return 0
