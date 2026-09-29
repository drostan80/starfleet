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
    "base", "sources", "structure", "sonarr", "numbering", "statuses", "cleanup", "replay",
    "checks", "writes",
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


def _upgrade_schema(run: Run) -> None:
    done = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[2],
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{run.work()}"},
        capture_output=True, text=True,
    )
    if done.returncode != 0:
        raise RebuildError("schema upgrade failed:\n" + done.stderr[-2000:])


def stage_base(run: Run) -> None:
    """The 09-06 snapshot, upgraded to the current schema."""
    _backup(run.snapshot, run.work())
    _upgrade_schema(run)
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
    from lcars import rebuild_cleanup

    cleanup_inputs = rebuild_cleanup.load_inputs(run)
    for s in d["season_statuses"]:
        if conn.execute("SELECT 1 FROM season WHERE id = ?", (s["season"],)).fetchone() is None:
            raise RebuildError(f"season status for a season not in 09-06: {s['key']}")
        status = rebuild_cleanup.status_for(cleanup_inputs, s["season"], s["status"])
        _mine(conn, s["season"], status)
        note = status if status == s["status"] else f"{status} (was {s['status']}: 09-29)"
        run.record("season_status", s["key"], "applied", note)
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
        # The franchise's series id stays with the franchise: a film row holding
        # it too stops Sonarr's episodes being filed for the show (R1.14).
        conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
                     (f["show"],))
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
    from lcars import rebuild_cleanup

    redirects = rebuild_cleanup.redirects_path(run)
    if redirects.exists():  # a re-run of stage 3 rebuilds the map
        redirects.unlink()
    rebuild_cleanup.structure_actions(run, conn, rebuild_cleanup.load_inputs(run))
    _source_ids_from_live(run, conn)
    run.record("stage", "structure", "applied", "")


SOURCE_ID_SERVICES = ("tvmaze", "anidb", "syoboi")


def _source_ids_from_live(run: Run, conn) -> None:
    """TVmaze/AniDB/Syoboi show links are source data (Memory Alpha's cross-id
    propagation since 09-06), not the user's decisions: taken from live for
    each tracked show — by TVDB id (the show's identity after the merges),
    else by show id. Never overwrites a link the show already has."""
    live = _live(run)
    by_tvdb: dict = {}
    by_show: dict = {}
    for sid, service, ext in live.execute(
            "SELECT show_id, service, external_id FROM show_external_id WHERE service IN"
            f" ({', '.join('?' * len(SOURCE_ID_SERVICES))})", SOURCE_ID_SERVICES):
        by_show.setdefault(sid, {})[service] = ext
    for sid, tvdb in live.execute(
            "SELECT show_id, external_id FROM show_external_id WHERE service = 'tvdb'"):
        for service, ext in by_show.get(sid, {}).items():
            by_tvdb.setdefault(tvdb, {}).setdefault(service, ext)
    live.close()
    added = 0
    for show in conn.execute("SELECT id FROM show WHERE tracked = 1").fetchall():
        tvdb = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND"
                            " service = 'tvdb'", (show[0],)).fetchone()
        links = (by_tvdb.get(tvdb[0]) if tvdb else None) or by_show.get(show[0]) or {}
        for service, ext in links.items():
            if conn.execute("SELECT 1 FROM show_external_id WHERE show_id = ? AND service = ?",
                            (show[0], service)).fetchone() is None and ext not in ("-1", ""):
                _set_link(conn, show[0], service, ext)
                added += 1
    conn.commit()
    run.record("source_ids", "tvmaze/anidb/syoboi", "applied", f"{added} links from live")


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


# ── stage 4: the Sonarr read (episodes, files, TVDB numbering) ────────────


def _tvdb_episodes(run: Run, conn, client=None) -> dict:
    """Every tracked episodic show with a TVDB id and no episode rows (never in
    Sonarr, R1.2e): its episode list read straight from TVDB — reads only.
    The raw answers are cached in the run dir (a re-run of stages 4-6 doesn't
    read them again). Returns {show_id: {(season, episode): aired}} for the
    date fill once TVmaze has had its turn."""
    from lcars import config, tvdb_client, tvdb_episodes

    cache_file = run.dir / "tvdb_episodes.json"
    cache = json.loads(cache_file.read_text()) if cache_file.exists() else {}
    rows = conn.execute(
        "SELECT sh.id, x.external_id FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
        " AND x.service = 'tvdb' WHERE sh.tracked = 1 AND sh.media_shape = 'episodic'"
        " AND NOT EXISTS (SELECT 1 FROM episode e WHERE e.show_id = sh.id) ORDER BY sh.id"
    ).fetchall()
    if not rows:
        return {}
    own_client = client is None
    if own_client:
        key = config.get_current().tvdb_api_key
        if not key:
            raise RebuildError("no TVDB api key (set LCARS_TVDB_API_KEY)")
        client = tvdb_client.TvdbClient(key)
    dates: dict = {}
    inserted = missing = failed = 0
    try:
        for sid, tvdb_id in rows:
            if tvdb_id not in cache:
                try:
                    cache[tvdb_id] = client.series_episodes(int(tvdb_id))
                except tvdb_client.TvdbError as e:
                    failed += 1
                    run.record("tvdb_episodes", sid, "skipped", f"TVDB error: {e}")
                    continue
                cache_file.write_text(json.dumps(cache))
            episodes = cache[tvdb_id]
            if not episodes:
                missing += 1
                run.record("tvdb_episodes", sid, "review",
                           f"TVDB {tvdb_id} has no episode list" if episodes is None
                           else f"TVDB {tvdb_id} lists no episodes")
                continue
            n, show_dates = tvdb_episodes.insert_missing(conn, sid, episodes)
            inserted += n
            dates[sid] = show_dates
            conn.commit()
    finally:
        if own_client:
            client.close()
    if failed:
        raise RebuildError(f"{failed} TVDB reads failed — see the ledger")
    run.record("tvdb_episodes", "all shows", "applied",
               f"{inserted} episode rows from TVDB for {len(rows) - missing} of {len(rows)} shows"
               f" with none ({missing} not on TVDB / empty)")
    return dates


def stage_sonarr(run: Run) -> None:
    """Every tracked episodic show whose TVDB series is in the Sonarr library:
    episodes at TVDB season/episode, `tvdb_absolute`, files — `_fetch_sonarr`
    alone (no AniList, art or synopsis fetch). Shows not in Sonarr keep the
    episodes the 09-06 base has."""
    from lcars import metadata, rebuild_reanchor, sonarr_client

    conn = _connect(run.work())
    rows = conn.execute(
        "SELECT sh.* FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
        " AND x.service = 'tvdb' WHERE sh.tracked = 1 AND sh.media_shape = 'episodic'"
        " ORDER BY sh.id").fetchall()
    read = absent = failed = 0
    for show in rows:
        before = conn.execute("SELECT COUNT(*) FROM episode WHERE show_id = ?",
                              (show["id"],)).fetchone()[0]
        try:
            # rows the numbering change of TVDB left on another episode's coordinates go to
            # their own episode (title + air date) before the read stamps the coordinates
            rebuild_reanchor.reanchor_show(run, conn, show["id"])
            metadata._fetch_sonarr(conn, dict(show), derive=False)
        except sonarr_client.SonarrError as e:
            failed += 1
            run.record("sonarr", show["id"], "skipped", f"Sonarr error: {e}")
            continue
        after = conn.execute("SELECT COUNT(*) FROM episode WHERE show_id = ?",
                             (show["id"],)).fetchone()[0]
        if conn.execute("SELECT 1 FROM episode WHERE show_id = ? AND sonarr_season IS NOT NULL",
                        (show["id"],)).fetchone() or after != before:
            read += 1
        else:
            absent += 1
        conn.commit()
    if failed:
        raise RebuildError(f"{failed} Sonarr reads failed — see the ledger")
    tvdb_dates = _tvdb_episodes(run, conn)
    created = _seasons_from_episodes(conn)
    run.record("seasons_from_episodes", "all shows", "applied",
               f"{created} TVDB season rows created for episodes that had none")
    _fill_air_dates(run, conn, tvdb_dates, read, absent)


def _seasons_from_episodes(conn) -> int:
    """Every episode's TVDB season has its season row (R1.13b): shows Sonarr
    doesn't hold (the Trakt import, finished shows) never got theirs."""
    from lcars import metadata

    created = 0
    for show in conn.execute("SELECT id FROM show WHERE tracked = 1 AND"
                             " media_shape = 'episodic'").fetchall():
        numbers = {r[0] for r in conn.execute(
            "SELECT DISTINCT season FROM episode WHERE show_id = ?", (show[0],))}
        have = {r[0] for r in conn.execute(
            "SELECT season_number FROM season WHERE show_id = ? AND kind = 'tvdb_season'",
            (show[0],))}
        missing = {n for n in numbers if n and n > 0} - have
        if not missing:
            continue
        rows = metadata._ensure_seasons(conn, show[0], missing)
        for n, season_id in rows.items():
            conn.execute("UPDATE episode SET season_id = ? WHERE show_id = ? AND season = ?",
                         (season_id, show[0], n))
            if n not in missing:
                continue
            # A season of your own history: what you watched decides its status
            # (the rules apply on top in stage 6). With nothing watched the
            # status `_ensure_seasons` gave it stands — R2.16/R2.19 (skipped
            # after a dropped/paused/skipped season or before a tracked later
            # one), never an override to planned.
            total, watched = conn.execute(
                "SELECT COUNT(*), SUM(state = 'watched') FROM episode WHERE show_id = ?"
                " AND season = ?", (show[0], n)).fetchone()
            if watched:
                conn.execute("UPDATE season SET status = ? WHERE id = ?",
                             ("completed" if watched == total else "watching", season_id))
        created += len(missing)
        conn.commit()
    return created


def _fill_air_dates(run: Run, conn, tvdb_dates: dict, read: int, absent: int) -> None:
    from lcars import anidb, tvdb_episodes

    # Air dates for episodes that have none (the Trakt import, shows never in
    # Sonarr): TVmaze by TVDB season/episode, then AniDB — numbering orders by
    # them (R1.2) and "last aired season" needs them. NULL-only.
    tvmaze = conn.execute(
        """UPDATE episode SET
             air_date_utc = COALESCE(te.airstamp, te.airdate || 'T00:00:00Z'),
             air_date_source = 'tvmaze'
           FROM show_external_id tm, tvmaze_episode te
           WHERE tm.show_id = episode.show_id AND tm.service = 'tvmaze'
             AND te.tvmaze_show_id = CAST(tm.external_id AS INTEGER)
             AND te.season = COALESCE(episode.sonarr_season, episode.season)
             AND te.episode = COALESCE(episode.sonarr_episode, episode.episode)
             AND episode.air_date_utc IS NULL AND episode.kind = 'regular'
             AND (te.airstamp IS NOT NULL OR (te.airdate IS NOT NULL AND te.airdate != ''))"""
    ).rowcount
    # TVDB's own date-only value takes what TVmaze's air time left empty (R1.2e).
    tvdb_filled = sum(tvdb_episodes.fill_air_dates(conn, sid, dates)
                      for sid, dates in tvdb_dates.items())
    anidb_filled = anidb.fill_airdate_gaps_anidb(conn)
    conn.commit()
    run.record("air_dates", "all shows", "applied",
               f"{tvmaze} from TVmaze, {tvdb_filled} from TVDB, {anidb_filled} from AniDB")
    run.record("stage", "sonarr", "applied",
               f"{read} shows read from Sonarr, {absent} not in the Sonarr library")


# ── stage 5: numbering (Memory Alpha: numbers, levels, spans) ─────────────


def stage_numbering(run: Run) -> None:
    import collections

    from lcars import numbering

    conn = _connect(run.work())
    by_source: collections.Counter = collections.Counter()
    flags: collections.Counter = collections.Counter()
    shows = [r[0] for r in conn.execute("SELECT id FROM show WHERE tracked = 1 ORDER BY id")]
    for show_id in shows:
        items, source, main_ids = numbering.load_show(conn, show_id)
        plan = numbering.plan_show(show_id, items, source, main_anidb_ids=main_ids)
        numbering.apply_plan(conn, plan)
        by_source[plan.source] += 1
        for f in plan.flags:
            flags[f["kind"]] += 1
            run.record("numbering_flag", f"{show_id}:{f['kind']}", "applied",
                       json.dumps(f, ensure_ascii=False, default=str)[:400])
        conn.commit()
    run.record("stage", "numbering", "applied",
               f"{len(shows)} shows by source {dict(by_source)}; flags {dict(flags)}")


# ── stage 6: statuses (the rules, the user's decisions, the pieces) ─────────


def _facts(run: Run, anilist_id: int) -> dict:
    """AniList facts, cached in the run dir (re-runs don't re-read)."""
    from lcars import anilist_client

    path = run.dir / "anilist_facts.json"
    cache = json.loads(path.read_text()) if path.exists() else {}
    key = str(anilist_id)
    if key not in cache:
        try:
            cache[key] = anilist_client.fetch_media_facts(anilist_id) or {}
        except anilist_client.AniListError:
            return {}
        path.write_text(json.dumps(cache))
    return cache[key]


def _link_list_levels(run: Run, conn) -> None:
    """A level holding an AniList/MAL id but no span (a folded film/OVA, a
    phase-5 special, a merged piece) and Memory Alpha's level for the same
    episodes become one: the list ids, status and history move onto the
    level with the span (R1.13b, R1.22). Matched by the AniList start date
    against the level's first air date (±3 days); anything unmatched is
    left for the review."""
    from datetime import date

    from lcars import tvdb_vetting

    linked = unmatched = 0
    for lvl in conn.execute(
            "SELECT z.* FROM season z JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1"
            " AND z.kind IN ('special', 'part') AND (z.anilist_id IS NOT NULL OR"
            " z.mal_id IS NOT NULL) AND NOT EXISTS (SELECT 1 FROM season_span p"
            " WHERE p.season_id = z.id)").fetchall():
        start = _iso_date(_facts(run, lvl["anilist_id"]).get("startDate")) \
            if lvl["anilist_id"] else None
        candidates = []
        if start:
            for c in conn.execute(
                    "SELECT z.id, MIN(e.air_date_utc) first FROM season z"
                    " JOIN season_span p ON p.season_id = z.id"
                    " JOIN episode e ON e.show_id = z.show_id"
                    "  AND e.absolute_number BETWEEN p.abs_from AND p.abs_to"
                    " WHERE z.show_id = ? AND z.id != ? AND z.anilist_id IS NULL"
                    "  AND z.mal_id IS NULL AND z.kind = ? GROUP BY z.id",
                    (lvl["show_id"], lvl["id"], lvl["kind"])).fetchall():
                if c["first"] and abs((date.fromisoformat(c["first"][:10])
                                       - date.fromisoformat(start)).days) <= 3:
                    candidates.append(c["id"])
        if len(candidates) == 1:
            target = candidates[0]
            src = conn.execute("SELECT * FROM season WHERE id = ?", (lvl["id"],)).fetchone()
            conn.execute("UPDATE season SET anilist_id = ?, mal_id = ?, label = COALESCE(label, ?)"
                         " WHERE id = ?", (src["anilist_id"], src["mal_id"], src["label"], target))
            conn.execute("UPDATE season_external_id SET season_id = ? WHERE season_id = ?",
                         (target, lvl["id"]))
            tvdb_vetting._merge_into(conn, lvl["id"], target)
            linked += 1
            run.record("level_link", lvl["id"], "applied", f"→ {target} (starts {start})")
        else:
            unmatched += 1
            run.record("level_link", lvl["id"], "review",
                       f"{len(candidates)} Memory Alpha levels start near {start}"
                       f" (AniList {lvl['anilist_id']}) — placed by the user in 9.2")
    conn.commit()
    run.record("level_links", "all", "applied", f"{linked} linked, {unmatched} to review")


def _runs(nums: list, every: list) -> list[tuple]:
    """A level's spans (R1.12): runs of its own numbers, broken wherever an episode
    of another level falls between two of them (S2 = 13–16 and 18–24, the film 17).
    `every`: all the show's absolute numbers, sorted."""
    import bisect

    nums = sorted(nums)
    out: list[tuple] = []
    for n in nums:
        if out:
            # any episode strictly between the run's end and n belongs to another level
            between = bisect.bisect_left(every, n) - bisect.bisect_right(every, out[-1][1])
            if between == 0:
                out[-1] = (out[-1][0], n)
                continue
        out.append((n, n))
    return out


def _part_spans(run: Run, conn) -> None:
    """Parts (cours) divide their TVDB season (R1.10, R1.11): each part's
    span runs from its first episode — Fribb's episode offset within the TVDB
    season, else the earlier cours' AniList episode counts added up — to the
    next part's start. Spans in absolute numbers, from the season's episodes."""
    from lcars import consolidation, fribb

    index = fribb.build_anilist_index(fribb.load_dataset())
    done = review = 0
    for parent in conn.execute(
            "SELECT DISTINCT p.* FROM season p JOIN season c ON c.parent_id = p.id"
            " AND c.kind = 'part' JOIN show sh ON sh.id = p.show_id AND sh.tracked = 1"
            " WHERE p.kind = 'tvdb_season'").fetchall():
        eps = [r[0] for r in conn.execute(
            "SELECT absolute_number FROM episode WHERE show_id = ? AND season = ?"
            " AND absolute_number IS NOT NULL ORDER BY episode",
            (parent["show_id"], parent["season_number"]))]
        every = sorted(r[0] for r in conn.execute(
            "SELECT absolute_number FROM episode WHERE show_id = ?"
            " AND absolute_number IS NOT NULL", (parent["show_id"],)))
        parts = conn.execute("SELECT * FROM season WHERE parent_id = ? AND kind = 'part'"
                             " ORDER BY part_number", (parent["id"],)).fetchall()
        starts, running, ok = [], 0, bool(eps)
        for part in parts:
            n, offset = consolidation._fribb_place(index, part["anilist_id"])
            if n == parent["season_number"] and offset is not None:
                start = offset
            else:
                start = running
            count = (_facts(run, part["anilist_id"]).get("episodes")
                     if part["anilist_id"] else None)
            starts.append(start)
            running = start + (count or 0)
            ok = ok and (count or n == parent["season_number"])
        if not ok or starts != sorted(starts) or len(set(starts)) != len(starts):
            review += 1
            run.record("part_spans", parent["id"], "review",
                       f"cour starts {starts} for {len(eps)} episodes — placed by the user")
            continue
        for i, part in enumerate(parts):
            end = starts[i + 1] if i + 1 < len(starts) else len(eps)
            conn.execute("DELETE FROM season_span WHERE season_id = ?", (part["id"],))
            conn.executemany(
                "INSERT INTO season_span (season_id, abs_from, abs_to) VALUES (?, ?, ?)",
                [(part["id"], a, b) for a, b in _runs(eps[starts[i]:end], every)])
        done += 1
    conn.commit()
    run.record("part_spans", "all", "applied", f"{done} seasons divided, {review} to review")


def _last_aired_season(conn, show_id: str):
    now = util.now_utc_iso()
    return conn.execute(
        "SELECT z.* FROM season z WHERE z.show_id = ? AND z.kind = 'tvdb_season'"
        " AND z.season_number > 0 AND EXISTS (SELECT 1 FROM episode e WHERE e.show_id = z.show_id"
        " AND e.season = z.season_number AND e.air_date_utc <= ?)"
        " ORDER BY z.season_number DESC LIMIT 1", (show_id, now)).fetchone()


def _complete_aired(conn, show_id: str) -> str:
    """"Completed (unless a later season exists → that season planned)" — the
    user's recurring gap rule: every fully aired season completed (episodes
    watched, R2.7), a season still to air planned."""
    from lcars import status_rules

    now = util.now_utc_iso()
    done = []
    for z in conn.execute("SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
                          " AND season_number > 0 ORDER BY season_number",
                          (show_id,)).fetchall():
        eps = conn.execute("SELECT air_date_utc FROM episode WHERE show_id = ? AND season = ?",
                           (show_id, z["season_number"])).fetchall()
        aired = eps and all(e[0] and e[0] <= now for e in eps)
        status = "completed" if aired else "planned"
        status_rules.set_level_status(conn, z["id"], status, "rebuild", confirmed=True,
                                      manual=True)
        done.append(f"S{z['season_number']} {status}")
    return ", ".join(done)


def _show_by_title(conn, title: str):
    return conn.execute(
        "SELECT * FROM show WHERE tracked = 1 AND (title_english = ? OR title_romaji = ?"
        " OR display_title_override = ?)", (title, title, title)).fetchone()


def _episode_totals(run: Run, conn) -> None:
    """R2.15a: each level's AniList episode total, from your list export (null = unknown),
    so the first live watch after go-live has the data to confirm a count."""
    entries = json.load(open(run.inputs / "anilist_entries.json"))
    n = 0
    for z in conn.execute("SELECT id, anilist_id FROM season WHERE anilist_id IS NOT NULL"
                          " AND episode_total IS NULL").fetchall():
        total = ((entries.get(str(z["anilist_id"])) or {}).get("media") or {}).get("episodes")
        if total:
            conn.execute("UPDATE season SET episode_total = ? WHERE id = ?", (total, z["id"]))
            n += 1
    conn.commit()
    run.record("episode_totals", "all", "applied", f"{n} levels got their AniList episode total")


def stage_statuses(run: Run) -> None:
    from lcars import status_rules

    conn = _connect(run.work())
    d = decisions(run)
    _episode_totals(run, conn)
    _part_spans(run, conn)
    _link_list_levels(run, conn)

    # R2.7 on the statuses you decided: a completed season has its episodes watched.
    marked = 0
    # (Yours, or an AniList/MAL entry that says completed: R4.8a marks its episodes too.)
    for z in conn.execute("SELECT * FROM season WHERE status = 'completed' AND"
                          " (status_set_manually = 1 OR anilist_id IS NOT NULL"
                          "  OR mal_id IS NOT NULL)").fetchall():
        eps = status_rules.level_episodes(conn, z)
        if any(e["state"] != "watched" for e in eps):
            status_rules._mark_watched(conn, z["show_id"], eps, status_rules.Effects())
            marked += 1
    run.record("r2_7", "your completed seasons", "applied", f"{marked} seasons' episodes marked")
    conn.commit()

    # The 216 Trakt drops: on the last season that has aired (decision 1).
    live = _live(run)
    trakt = [r[0] for r in live.execute(
        "SELECT sh.id FROM show sh WHERE sh.tracked = 1 AND sh.status = 'dropped'"
        " AND NOT EXISTS (SELECT 1 FROM season z WHERE z.show_id = sh.id AND z.status = 'dropped')"
        " AND (SELECT changed_by FROM status_change c WHERE c.show_id = sh.id"
        "      ORDER BY changed_at DESC LIMIT 1) = 'trakt_import'")]
    live.close()
    for sid in trakt:
        last = _last_aired_season(conn, sid)
        if last is None:
            run.record("trakt_drop", sid, "review", "no season with an aired episode")
            continue
        status_rules.set_level_status(conn, last["id"], "dropped", "rebuild", confirmed=True,
                                      manual=True)
        run.record("trakt_drop", sid, "applied", f"S{last['season_number']} dropped")
    conn.commit()

    # Snapshot and activity follow-ups (PLAN-DATA "Your review decisions").
    for x in d["snapshot"]:
        if x["action"] in ("keep", "evidence"):
            run.record("snapshot", x["key"], "applied", x["action"])
            continue
        show = conn.execute("SELECT * FROM show WHERE id = ?", (x["key"].split(":", 1)[1],)
                            ).fetchone() if x["key"].startswith(("sa:", "su:")) else None
        show = show or _show_by_title(conn, x["title"])
        if show is None:
            run.record("snapshot", x["key"], "review", f"{x['title']}: show not found")
            continue
        if x["action"] == "complete_aired":
            detail = _complete_aired(conn, show["id"])
        elif x["action"] == "sakamoto_dropped":
            last = _last_aired_season(conn, show["id"])
            status_rules.set_level_status(conn, last["id"], "dropped", "rebuild",
                                          confirmed=True, manual=True)
            detail = f"S{last['season_number']} dropped"
        elif x["action"] == "drop_duplicate_show":
            detail = "duplicate show merged away in stage 3 (R1.14)"
        else:  # slime / kaiju: season level by the rules, after the replay
            detail = "season statuses by the rules and the replayed watches"
        run.record("snapshot", x["key"], "applied", detail)
    conn.commit()

    # Urusei Yatsura films: the level holding each film is skipped (answer a).
    pending = [json.loads(line) for line in (run.dir / "pending.jsonl").read_text().splitlines()
               if json.loads(line)["kind"] == "urusei_film"]
    uy = _show_by_title(conn, "Urusei Yatsura")
    for f in pending:
        name = f["title"].split(": ", 1)[-1]
        lvl = conn.execute(
            "SELECT z.id FROM season z JOIN season_span p ON p.season_id = z.id"
            " JOIN episode e ON e.show_id = z.show_id AND e.absolute_number"
            "  BETWEEN p.abs_from AND p.abs_to WHERE z.show_id = ? AND z.kind = 'special'"
            "  AND e.title LIKE ? LIMIT 1", (uy["id"] if uy else "", f"%{name}%")).fetchone()
        if lvl is None:
            run.record("new_show", f["key"], "review", f"no level for {f['title']} in the show")
            continue
        _mine(conn, lvl[0], "skipped")
        run.record("new_show", f["key"], "applied", f"level {lvl[0]} skipped")
    conn.commit()

    # R2.14/R2.15/R2.18 per level, R2.13 per show, for every tracked show.
    for sid in [r[0] for r in conn.execute("SELECT id FROM show WHERE tracked = 1")]:
        # Historical data: what the levels' watch state says (R2.15a governs new watches;
        # past seasons and episodes aren't reviewed for it, user 09-29).
        status_rules.after_episodes_changed(conn, sid, "rebuild", require_confirmed=False)
    conn.commit()
    run.record("stage", "statuses", "applied", "")


def stage_cleanup(run: Run) -> None:
    from lcars import rebuild_cleanup

    rebuild_cleanup.stage_cleanup(run)


def stage_replay(run: Run) -> None:
    from lcars import rebuild_replay

    rebuild_replay.stage_replay(run)


def stage_checks(run: Run) -> None:
    from lcars import rebuild_checks

    rebuild_checks.stage_checks(run)


STAGE_FUNCS = {"base": stage_base, "sources": stage_sources, "structure": stage_structure,
               "sonarr": stage_sonarr, "numbering": stage_numbering,
               "statuses": stage_statuses, "cleanup": stage_cleanup, "replay": stage_replay,
               "checks": stage_checks}


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
        _upgrade_schema(run)  # a checkpoint older than a migration made since
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
