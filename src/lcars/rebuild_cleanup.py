"""The cleanup decisions of 2026-09-29 (PLAN-DATA 9.1 decision 9; RULEBOOK R3.5, R1.23, R1.14a).

Two halves, split by what they do to the numbering that comes after:

- `structure_actions` (the tail of stage 3): things that change which show a level
  belongs to — folds into a parent show (with the status you gave), a film's season,
  Kaiju Girl's history, the skip list. They run before the episode read (4),
  numbering (5) and statuses (6), so those shows get their episodes, numbers and
  derived statuses.
- `stage_cleanup` (stage 7): the statuses you gave to seasons that other shows' ids point at
  (through the status engine), then removals — the duplicate shows (checked once the season
  that holds their ids exists), the untracked stubs, the show-level list ids a season
  already holds, orphaned watch events. Every removed row is exported
  per table under `<run>/removed/`, and every removed or folded show is written to
  `<run>/redirects.json` (removed show → survivor) for the replay.

Everything is keyed by ids that survive a re-run (the 09-06 base's show ids, TVDB and
AniList ids); an id that no longer resolves raises `RebuildError`.
"""

from __future__ import annotations

import json
from pathlib import Path

from lcars import ids as ids_module
from lcars import util


def _rebuild():
    from lcars import rebuild  # deferred: rebuild imports this module

    return rebuild


def rb_error(message: str):
    return _rebuild().RebuildError(message)


# ── FK-safe delete with export ───────────────────────────────────────────


def _children(conn, table: str) -> list[tuple[str, list[tuple[str, str]]]]:
    """[(child table, [(child column, this table's column)])] for every FK into `table`."""
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall():
        groups: dict[int, list[tuple[str, str]]] = {}
        parent_of: dict[int, str] = {}
        for fk in conn.execute(f'PRAGMA foreign_key_list("{name}")').fetchall():
            groups.setdefault(fk[0], []).append((fk[3], fk[4]))
            parent_of[fk[0]] = fk[2]
        for fid, cols in groups.items():
            if parent_of[fid] == table:
                if any(to is None for _c, to in cols):  # REFERENCES parent: its primary key
                    cols = [(c, pk) for (c, _to), pk in zip(cols, _pk(conn, table), strict=False)]
                out.append((name, cols))
    return out


def _pk(conn, table: str) -> list[str]:
    info = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in sorted(info, key=lambda r: r[5]) if r[5] > 0]


class Deleter:
    """Deletes rows and everything that references them (the FK graph, read from the
    database), writing each removed row to `<export>/<table>.jsonl` first."""

    def __init__(self, conn, export: Path | None):
        self.conn = conn
        self.export = export
        self.counts: dict[str, int] = {}
        self._seen: set[tuple[str, int]] = set()
        if export:
            export.mkdir(parents=True, exist_ok=True)

    def delete(self, table: str, where: str, params: tuple = ()) -> None:
        conn = self.conn
        # rows of one table that reference each other (a part and its season) go in one
        # batch: the check waits for the commit, where any real violation still fails
        conn.execute("PRAGMA defer_foreign_keys = ON")
        rows = conn.execute(f'SELECT rowid AS _rid, * FROM "{table}" WHERE {where}',
                            params).fetchall()
        rows = [r for r in rows if (table, r["_rid"]) not in self._seen]
        if not rows:
            return
        for r in rows:
            self._seen.add((table, r["_rid"]))
        for child, cols in _children(conn, table):
            for r in rows:
                clause = " AND ".join(f'"{c}" = ?' for c, _p in cols)
                values = tuple(r[p] for _c, p in cols)
                if any(v is None for v in values):
                    continue
                self.delete(child, clause, values)
        if self.export:
            with open(self.export / f"{table}.jsonl", "a") as f:
                for r in rows:
                    f.write(json.dumps({k: r[k] for k in r.keys() if k != "_rid"},
                                       ensure_ascii=False, default=str) + "\n")
        for r in rows:
            conn.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (r["_rid"],))
        self.counts[table] = self.counts.get(table, 0) + len(rows)

    def show(self, show_id: str) -> None:
        self.delete("show", "id = ?", (show_id,))


def redirects_path(run) -> Path:
    return run.dir / "redirects.json"


def record_redirect(run, removed: str, survivor: str | None) -> None:
    path = redirects_path(run)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[removed] = survivor
    path.write_text(json.dumps(data, indent=1, sort_keys=True))


def load_inputs(run) -> dict:
    path = run.inputs / "rebuild-inputs" / "cleanup.json"
    return json.loads(path.read_text())


# ── stage 3 tail: structure ──────────────────────────────────────────────


def _parent(conn, spec: dict, not_show: str | None = None):
    """The tracked show a fold goes into, by an id that survives a re-run."""
    rb = _rebuild()
    if "tvdb" in spec:
        return rb._tracked_tvdb_show(conn, spec["tvdb"], not_show)
    if "tvdb_movie" in spec:
        return conn.execute(
            "SELECT sh.* FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
            " AND x.service = 'tvdb_movie' WHERE x.external_id = ? AND sh.tracked = 1"
            " AND sh.id != ? ORDER BY sh.created_at", (str(spec["tvdb_movie"]), not_show or "")
        ).fetchone()
    if "anilist_season" in spec:
        return conn.execute(
            "SELECT sh.* FROM season z JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1"
            " AND sh.id != ? AND (z.anilist_id = ? OR EXISTS (SELECT 1 FROM season_external_id s"
            " WHERE s.season_id = z.id AND s.service = 'anilist' AND s.external_id = ?))",
            (not_show or "", spec["anilist_season"], str(spec["anilist_season"]))).fetchone()
    raise rb_error(f"unknown parent spec {spec}")


def _season_holding(conn, service: str, ext_id: int, tracked_only: bool = True):
    column = "anilist_id" if service == "anilist" else "mal_id"
    return conn.execute(
        "SELECT z.* FROM season z JOIN show sh ON sh.id = z.show_id"
        + (" AND sh.tracked = 1" if tracked_only else "") +
        f" WHERE z.{column} = ? OR EXISTS (SELECT 1 FROM season_external_id s WHERE"
        " s.season_id = z.id AND s.service = ? AND s.external_id = ?)"
        " ORDER BY sh.tracked DESC LIMIT 1", (ext_id, service, str(ext_id))).fetchone()


def structure_actions(run, conn, c: dict) -> None:
    """Stage 3's tail. Order matters: links, duplicate shows, folds, statuses, the rest."""

    rb = _rebuild()
    export = run.dir / "removed"

    # 1. wrong links purged (a Sonarr slug that names another series).
    for u in c.get("unlink", []):
        n = conn.execute("DELETE FROM show_external_id WHERE show_id = ? AND service = ?",
                         (u["show"], u["service"])).rowcount
        run.record("cleanup_unlink", u["show"], "applied", f"{u['service']} link removed ({n})")

    # 2. folds: a stub or a special becomes levels of its parent show.
    for f in c.get("folds", []):
        loser = conn.execute("SELECT * FROM show WHERE id = ?", (f["loser"],)).fetchone()
        if loser is None:
            raise rb_error(f"fold {f['loser']}: the show isn't in the database")
        parent = _parent(conn, f["parent"], f["loser"])
        if parent is None:
            raise rb_error(f"fold {f['loser']}: parent {f['parent']} isn't a tracked show")
        seasons = [r[0] for r in conn.execute("SELECT id FROM season WHERE show_id = ?",
                                              (f["loser"],)).fetchall()]
        rb._fold_into(conn, parent["id"], f["loser"], f"cleanup 09-29: {f['why']}")
        record_redirect(run, f["loser"], parent["id"])
        if f.get("status"):
            for sid in seasons:
                if conn.execute("SELECT 1 FROM season WHERE id = ?", (sid,)).fetchone():
                    rb._mine(conn, sid, f["status"])
        run.record("cleanup_fold", f["loser"], "applied",
                   f"→ {parent['id']} {f.get('status') or ''} ({f['why']})".strip())

    # 3. entries you skip stay as skip-list entries (R2.10: untracked, status skipped).
    for sid in c.get("skip_list", []):
        row = conn.execute("SELECT * FROM show WHERE id = ?", (sid,)).fetchone()
        if row is None:
            raise rb_error(f"skip list: {sid} isn't in the database")
        conn.execute("UPDATE show SET tracked = 0, status = 'skipped' WHERE id = ?", (sid,))
        for (zid,) in conn.execute("SELECT id FROM season WHERE show_id = ?", (sid,)).fetchall():
            rb._mine(conn, zid, "skipped")
        run.record("cleanup_skip", sid, "applied", "kept on the skip list")

    # 4. explicit deletes (an entry you will look at separately).
    for d in c.get("delete_shows", []):
        if conn.execute("SELECT 1 FROM show WHERE id = ?", (d["show"],)).fetchone() is None:
            raise rb_error(f"delete {d['show']}: not in the database")
        Deleter(conn, export).show(d["show"])
        record_redirect(run, d["show"], None)
        run.record("cleanup_delete", d["show"], "applied", d["why"])

    # 5. a film show with no season: its season holds the list ids (R1.23).
    for m in c.get("movie_seasons", []):
        show = conn.execute("SELECT * FROM show WHERE id = ?", (m["show"],)).fetchone()
        if show is None:
            raise rb_error(f"movie season: {m['show']} isn't in the database")
        if conn.execute("SELECT 1 FROM season WHERE show_id = ?", (m["show"],)).fetchone():
            run.record("cleanup_movie_season", m["show"], "skipped", "already has a season")
            continue
        now = util.now_utc_iso()
        zid = ids_module.generate_id(conn, "z")
        conn.execute(
            "INSERT INTO season (id, show_id, season_number, part_number, anilist_id, mal_id,"
            " source, matched, manual_override, status, list_sync, kind, created_at, updated_at)"
            " VALUES (?, ?, 1, 1, ?, ?, 'manual', 1, 0, ?, 1, 'tvdb_season', ?, ?)",
            (zid, m["show"], m.get("anilist"), m.get("mal"), show["status"], now, now))
        for service, ext in (("anilist", m.get("anilist")), ("mal", m.get("mal"))):
            if ext:
                conn.execute("INSERT INTO season_external_id (season_id, service, external_id,"
                             " created_at) VALUES (?, ?, ?, ?)", (zid, service, str(ext), now))
        run.record("cleanup_movie_season", m["show"], "applied", f"season {zid} holds the list ids")

    # 6. a stub that holds watch history: mapped onto the right show, episode by episode.
    for k in c.get("history_moves", []):
        _move_history(run, conn, k, export)
    conn.commit()


def apply_statuses(run, conn, c: dict) -> None:
    """The statuses you gave to seasons and shows that other shows' ids point at. They
    go through the status engine (its cascades, R2.7's watched marks, R2.16), after
    stage 6, when the seasons that hold those ids exist."""
    from lcars import status_rules

    rb = _rebuild()
    for s in c.get("season_status", []):
        season = _season_holding(conn, s["service"], s["id"])
        if season is None:
            raise rb_error(f"status {s['status']}: no tracked season holds "
                           f"{s['service']} {s['id']}")
        status_rules.set_level_status(conn, season["id"], s["status"], "rebuild",
                                      confirmed=True, manual=True)
        run.record("cleanup_status", season["id"], "applied",
                   f"{s['status']} ({s['service']} {s['id']}: {s['why']})")
    for s in c.get("show_status", []):
        show = rb._tracked_tvdb_show(conn, s["tvdb"])
        if show is None:
            raise rb_error(f"show status: no tracked show with tvdb {s['tvdb']}")
        status_rules.set_show_status(conn, show["id"], s["status"], "rebuild", confirmed=True)
        for r in conn.execute("SELECT id FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
                              " AND status = ?", (show["id"], s["status"])).fetchall():
            conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = ?", (r[0],))
        run.record("cleanup_show_status", show["id"], "applied", f"{s['status']} ({s['why']})")


def remove_duplicate_shows(run, conn, c: dict) -> None:
    """Shows whose list ids a season of another show already holds: the season inside the
    main show wins (checked after stage 6, when that season exists), so the duplicate goes."""
    export = run.dir / "removed"
    for d in c.get("duplicate_shows", []):
        row = conn.execute("SELECT * FROM show WHERE id = ?", (d["show"],)).fetchone()
        if row is None:
            raise rb_error(f"duplicate show {d['show']} isn't in the database")
        held = _season_holding(conn, "anilist", d["anilist_held_by"])
        if held is None or held["show_id"] == d["show"]:
            raise rb_error(f"{d['show']}: no other show's season holds anilist "
                           f"{d['anilist_held_by']}")
        events = conn.execute("SELECT COUNT(*) FROM watch_event WHERE show_id = ?",
                              (d["show"],)).fetchone()[0]
        if events:
            raise rb_error(f"{d['show']}: has {events} watch events; not a plain duplicate")
        Deleter(conn, export).show(d["show"])
        record_redirect(run, d["show"], held["show_id"])
        run.record("cleanup_duplicate_show", d["show"], "applied",
                   f"deleted; {held['show_id']} holds it")


def _move_history(run, conn, k: dict, export: Path) -> None:
    stub, into = k["stub"], k["into_tvdb"]
    rb = _rebuild()
    target = rb._tracked_tvdb_show(conn, into, stub)
    if target is None:
        raise rb_error(f"history: no tracked show with tvdb {into}")
    events = conn.execute("SELECT * FROM watch_event WHERE show_id = ? ORDER BY watched_at",
                          (stub,)).fetchall()
    moved = 0
    for e in events:
        if e["season"] is None or e["episode"] is None:
            raise rb_error(f"history {stub}: a watch event with no episode ({e['id']})")
        ep = conn.execute("SELECT id, state FROM episode WHERE show_id = ? AND season = ?"
                          " AND episode = ?", (target["id"], e["season"], e["episode"])).fetchone()
        if ep is None:
            raise rb_error(f"history {stub}: S{e['season']}E{e['episode']} doesn't exist in "
                           f"{target['id']}")
        have = conn.execute("SELECT 1 FROM watch_event WHERE show_id = ? AND season = ? AND"
                            " episode = ?", (target["id"], e["season"], e["episode"])).fetchone()
        if have is None:
            conn.execute(
                "INSERT INTO watch_event (id, show_id, season, episode, watched_at, platform,"
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ids_module.generate_id(conn, "w"), target["id"], e["season"], e["episode"],
                 e["watched_at"], e["platform"], e["created_at"]))
            moved += 1
        if ep["state"] != "watched":
            conn.execute("UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                         (util.now_utc_iso(), ep["id"]))
        run.record("cleanup_history", f"{stub}:S{e['season']}E{e['episode']}", "applied",
                   f"→ {target['id']} watched {e['watched_at']}")
    Deleter(conn, export).show(stub)
    record_redirect(run, stub, target["id"])
    run.record("cleanup_history", stub, "applied",
               f"{len(events)} watch events mapped to {target['id']} ({moved} new), stub removed")


# ── stage 7: removals ────────────────────────────────────────────────────


def _list_ids(run) -> set[int]:
    raw = json.load(open(run.inputs / "anilist_list.json"))
    coll = (raw.get("data") or raw).get("MediaListCollection") or raw
    return {e["mediaId"] for lst in coll["lists"] for e in lst["entries"]}


def _held_by_kept(conn, show_id: str) -> bool:
    """Any AniList/MAL id of the show is held by a season of a tracked show."""
    rows = conn.execute(
        "SELECT service, external_id FROM show_external_id WHERE show_id = ?"
        " AND service IN ('anilist','mal') UNION SELECT s.service, s.external_id FROM"
        " season_external_id s JOIN season z ON z.id = s.season_id WHERE z.show_id = ?"
        " AND s.service IN ('anilist','mal')", (show_id, show_id)).fetchall()
    for service, ext in rows:
        if conn.execute(
                "SELECT 1 FROM season_external_id s JOIN season z ON z.id = s.season_id"
                " JOIN show sh ON sh.id = z.show_id WHERE sh.tracked = 1 AND sh.id != ?"
                " AND s.service = ? AND s.external_id = ?", (show_id, service, ext)).fetchone():
            return True
    return False


def stage_cleanup(run) -> None:
    rb = _rebuild()
    conn = rb._connect(run.work())
    export = run.dir / "removed"
    if export.exists():  # a re-run of this stage replaces its own export
        for f in export.glob("*.jsonl"):
            f.unlink()
    inputs = load_inputs(run)
    apply_statuses(run, conn, inputs)
    remove_duplicate_shows(run, conn, inputs)
    mine = _list_ids(run)
    deleter = Deleter(conn, export)
    kept: dict[str, int] = {}
    removed = 0

    # 1. untracked stubs (R3.5) — only the skip list may stay untracked.
    stubs = conn.execute("SELECT id FROM show WHERE tracked = 0 AND status <> 'skipped'"
                         " ORDER BY id").fetchall()
    for (sid,) in stubs:
        held = _held_by_kept(conn, sid)
        anilist = {int(r[0]) for r in conn.execute(
            "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'anilist'"
            " UNION SELECT s.external_id FROM season_external_id s JOIN season z"
            " ON z.id = s.season_id WHERE z.show_id = ? AND s.service = 'anilist'",
            (sid, sid)).fetchall()
            if str(r[0]).isdigit()}
        history = conn.execute("SELECT COUNT(*) FROM watch_event WHERE show_id = ?",
                               (sid,)).fetchone()[0] + conn.execute(
            "SELECT COUNT(*) FROM episode WHERE show_id = ? AND state = 'watched'",
            (sid,)).fetchone()[0]
        if history:
            run.record("cleanup_stub", sid, "review", "holds watch history: not removed")
            kept["history"] = kept.get("history", 0) + 1
            continue
        if not held and anilist & mine:
            run.record("cleanup_stub", sid, "review",
                       "on your AniList list and held by no season: not removed")
            kept["on_list_unhomed"] = kept.get("on_list_unhomed", 0) + 1
            continue
        winner = conn.execute("SELECT winner_show_id FROM show_merge WHERE loser_show_id = ?"
                              " AND reversed_at IS NULL", (sid,)).fetchone()
        deleter.show(sid)
        record_redirect(run, sid, winner[0] if winner else None)
        removed += 1
    run.record("cleanup_stubs", "all", "applied",
               f"{removed} untracked stubs removed (exported); kept for review: {kept or 'none'}")

    # 2. show-level list ids a season of the same show already holds (R1.23).
    gone = review = 0
    for r in conn.execute(
            "SELECT x.show_id, x.service, x.external_id FROM show_external_id x JOIN show sh ON"
            " sh.id = x.show_id WHERE sh.tracked = 1 AND x.service IN ('anilist','mal')"
    ).fetchall():
        held = conn.execute(
            "SELECT 1 FROM season_external_id s JOIN season z ON z.id = s.season_id WHERE"
            " z.show_id = ? AND s.service = ? AND s.external_id = ?",
            (r[0], r[1], r[2])).fetchone()
        if held:
            deleter.delete("show_external_id", "show_id = ? AND service = ?", (r[0], r[1]))
            gone += 1
        else:
            run.record("cleanup_show_level_id", r[0], "review",
                       f"{r[1]} {r[2]} held by no season of it")
            review += 1
    run.record("cleanup_show_level_ids", "all", "applied",
               f"{gone} removed, {review} left for review")

    # 3. watch events for episodes that no longer exist at those numbers.
    orphans = conn.execute(
        "SELECT id FROM watch_event w WHERE w.season IS NOT NULL AND NOT EXISTS ("
        " SELECT 1 FROM episode e WHERE e.show_id = w.show_id AND e.season = w.season"
        " AND e.episode = w.episode)").fetchall()
    for (wid,) in orphans:
        deleter.delete("watch_event", "id = ?", (wid,))
    run.record("cleanup_orphans", "all", "applied", f"{len(orphans)} orphaned watch events removed")

    conn.commit()
    run.record("cleanup_rows", "all", "applied",
               "removed rows per table: " + json.dumps(deleter.counts, sort_keys=True))
    run.record("stage", "cleanup", "applied", "")
