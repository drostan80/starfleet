"""Stage 8 — replay the gap (09-06 08:53Z → the live copy) through the engine's own
functions (PLAN-DATA §2; your review decisions of 09-27/28 in `decisions.json`).

What replays, and only that:
- **watches**: the live watch events your review marked valid (`gap_watch`, 51 shows and the
  Ludwig burst), each on the rebuilt episode it maps to, with its original time and
  platform, through the same effects a live watch has (episode watched, the level's start,
  R2.13b/R2.14/R2.15a via the status engine). Invalid bursts and the shows you marked
  not-to-replay are not replayed; watches nobody reviewed are not replayed either;
- **status changes** (`gap_status`): the final status you gave each (completed / completed if
  aired / watching / dropped / replay of the change), discarded ones ignored;
- your **manual watches** (the ones told in the conversation) and the one **score change**.
  The Kaiju No. 8 minis are not guessed: they go to the review list with candidates.

Live show and episode ids are mapped to the rebuilt ones (`redirects.json` from stage 3/7, then
TVDB/AniList ids for shows created after 09-06); nothing that can't be placed is guessed: it is
recorded as a `review` line. Nothing is written outside the work copy.
"""

from __future__ import annotations

import json
import re
import sqlite3

from lcars import ids as ids_module
from lcars import util

GAP_START = "2026-09-06T08:53:00Z"
EPISODE_TAG = re.compile(r"S(\d+)E(\d+)")


def _rebuild():
    from lcars import rebuild

    return rebuild


class Mapper:
    """Live (or 09-06 base) show and episode ids → the rebuilt database's."""

    def __init__(self, run, conn, live):
        self.conn, self.live = conn, live
        path = run.dir / "redirects.json"
        self.redirects = json.loads(path.read_text()) if path.exists() else {}

    def show(self, live_show_id: str) -> str | None:
        conn = self.conn
        if conn.execute("SELECT 1 FROM show WHERE id = ? AND tracked = 1",
                        (live_show_id,)).fetchone():
            return live_show_id
        if live_show_id in self.redirects:
            survivor = self.redirects[live_show_id]
            if survivor and conn.execute("SELECT 1 FROM show WHERE id = ?", (survivor,)).fetchone():
                return survivor
            return None
        # created after 09-06: found by the ids it carries (TVDB first, then a season's AniList id)
        for service, ext in self.live.execute(
                "SELECT service, external_id FROM show_external_id WHERE show_id = ?",
                (live_show_id,)).fetchall():
            if service in ("tvdb", "tvdb_movie"):
                row = conn.execute(
                    "SELECT sh.id FROM show sh JOIN show_external_id x ON x.show_id = sh.id"
                    " WHERE x.service = ? AND x.external_id = ? AND sh.tracked = 1",
                    (service, ext)).fetchone()
                if row:
                    return row[0]
        for (anilist,) in self.live.execute(
                "SELECT external_id FROM season_external_id s JOIN season z ON z.id = s.season_id"
                " WHERE z.show_id = ? AND s.service = 'anilist'", (live_show_id,)).fetchall():
            row = conn.execute(
                "SELECT sh.id FROM season z JOIN show sh ON sh.id = z.show_id AND sh.tracked = 1"
                " WHERE z.anilist_id = ? OR EXISTS (SELECT 1 FROM season_external_id s WHERE"
                " s.season_id = z.id AND s.service = 'anilist' AND s.external_id = ?)",
                (anilist, str(anilist))).fetchone()
            if row:
                return row[0]
        return None

    def episode(self, live_show_id: str, season, episode, show_id: str):
        """The rebuilt episode a live (season, episode) is: by TVDB coordinates (Sonarr's,
        else the live ones), then, when the coordinates aren't there, by absolute number
        and air date when that is unique."""
        le = self.live.execute("SELECT * FROM episode WHERE show_id = ? AND season = ? AND"
                               " episode = ?", (live_show_id, season, episode)).fetchone()
        cs = season if le is None or le["sonarr_season"] is None else le["sonarr_season"]
        ce = episode if le is None or le["sonarr_episode"] is None else le["sonarr_episode"]
        row = self.conn.execute("SELECT * FROM episode WHERE show_id = ? AND season = ? AND"
                                " episode = ?", (show_id, cs, ce)).fetchone()
        if (row is None and le is not None and le["absolute_number"] is not None
                and le["air_date_utc"]):
            rows = self.conn.execute(
                "SELECT * FROM episode WHERE show_id = ? AND absolute_number = ?"
                " AND substr(air_date_utc, 1, 10) = ?",
                (show_id, le["absolute_number"], le["air_date_utc"][:10])).fetchall()
            row = rows[0] if len(rows) == 1 else None
        return row


def replay_watch(conn, show_id: str, ep, watched_at: str, platform, created_at: str) -> None:
    """A live watch, with the effects addWatchEvent has (no push: the write list is derived
    from the end state, stage 10)."""
    from lcars import resolvers

    conn.execute(
        "INSERT INTO watch_event (id, show_id, season, episode, watched_at, platform, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (ids_module.generate_id(conn, "w"), show_id, ep["season"], ep["episode"], watched_at,
         platform, created_at))
    conn.execute("UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                 (util.now_utc_iso(), ep["id"]))
    resolvers._stamp_season_started_at(conn, show_id, ep["season"], watched_at)
    resolvers._recompute_show_status(conn, show_id, "rebuild", _skip_push=True, at=watched_at,
                                     watched_season=ep["season"])


def _live(run) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{run.live}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _events_for(live, g: dict):
    """The live watch events one `gap_watch` entry stands for."""
    if "show" in g:
        want = {(int(a), int(b)) for a, b in EPISODE_TAG.findall(g.get("episodes") or "")}
        rows = live.execute("SELECT * FROM watch_event WHERE show_id = ? AND watched_at > ?"
                            " ORDER BY watched_at", (g["show"], GAP_START)).fetchall()
        return [(g["show"], e) for e in rows if (e["season"], e["episode"]) in want]
    out = []  # a valid burst: the events at that instant, on the show(s) it names
    for name in [t.strip() for t in g["title"].split(",")]:
        base = re.sub(r"\s*\(\d{4}\)$", "", name)
        for (sid,) in live.execute("SELECT id FROM show WHERE COALESCE(title_english,"
                                   " title_romaji) LIKE ?", (base + "%",)).fetchall():
            for e in live.execute("SELECT * FROM watch_event WHERE show_id = ? AND watched_at = ?",
                                  (sid, g["burst_at"])).fetchall():
                out.append((sid, e))
    return out


def replay_watches(run, conn, live, d: dict, mapper: Mapper, touched: set) -> None:
    done = review = 0
    seen: set = set()
    for g in d["gap_watch"]:
        if g["replay"] is False:
            run.record("replay_watch", g["key"], "skipped", "not replayed (your review)")
            continue
        if g["replay"] == "realign":
            _kaiju_minis(run, conn, live, g, mapper, touched)
            continue
        for live_show, e in _events_for(live, g):
            key = (e["id"],)
            if key in seen:
                continue
            seen.add(key)
            show = mapper.show(live_show)
            if show is None:
                run.record("replay_watch", f"{live_show}:S{e['season']}E{e['episode']}", "review",
                           f"{g['title']}: no show in the rebuilt copy")
                review += 1
                continue
            ep = mapper.episode(live_show, e["season"], e["episode"], show)
            if ep is None:
                run.record("replay_watch", f"{live_show}:S{e['season']}E{e['episode']}", "review",
                           f"{g['title']}: no episode at S{e['season']}E{e['episode']} in {show}")
                review += 1
                continue
            replay_watch(conn, show, ep, e["watched_at"], e["platform"], e["created_at"])
            touched.add(show)
            done += 1
    run.record("replay_watches", "all", "applied",
               f"{done} watch events replayed, {review} for review")


def _kaiju_minis(run, conn, live, g: dict, mapper: Mapper, touched: set) -> None:
    """The Kaiju No. 8 minis: the watch may have been put on the wrong episodes, so each is
    checked: exactly one rebuilt episode with the same title on the same air date → replayed
    there (recorded, for your confirmation); anything else is listed for review."""
    want = {(int(a), int(b)) for a, b in EPISODE_TAG.findall(g["episodes"])}
    show = mapper.show(g["show"])
    for e in live.execute("SELECT * FROM watch_event WHERE show_id = ? AND watched_at > ?"
                          " ORDER BY watched_at", (g["show"], GAP_START)).fetchall():
        if (e["season"], e["episode"]) not in want:
            continue
        key = f"{g['show']}:S{e['season']}E{e['episode']}"
        le = live.execute("SELECT * FROM episode WHERE show_id = ? AND season = ? AND"
                          " episode = ?", (g["show"], e["season"], e["episode"])).fetchone()
        title = le["title"] if le is not None else None
        same_day = []
        if show and le is not None and le["air_date_utc"]:
            same_day = conn.execute(
                "SELECT * FROM episode WHERE show_id = ? AND substr(air_date_utc, 1, 10) = ?"
                " ORDER BY season, episode", (show, le["air_date_utc"][:10])).fetchall()
        exact = [r for r in same_day if title and r["title"] == title]
        if len(exact) == 1:
            replay_watch(conn, show, exact[0], e["watched_at"], e["platform"], e["created_at"])
            touched.add(show)
            run.record("replay_kaiju", key, "applied",
                       f"'{title}' → S{exact[0]['season']}E{exact[0]['episode']} abs "
                       f"{exact[0]['absolute_number']} (same title and air date; confirm)")
        else:
            near = [f"S{r['season']}E{r['episode']} {r['title']}" for r in same_day]
            run.record("replay_kaiju", key, "review",
                       f"live '{title}' ({e['watched_at']}): candidates {near or 'none'}")


# ── statuses, manual watches, score ────────────────────────────────────────


def replay_statuses(run, conn, d: dict, mapper: Mapper, touched: set) -> None:
    from lcars import status_rules

    rb = _rebuild()
    for g in d["gap_status"]:
        final = g["final"]
        if final == "discard":
            run.record("replay_status", g["key"], "skipped", "discarded (your review)")
            continue
        show = mapper.show(g["show"])
        if show is None:
            run.record("replay_status", g["key"], "review", f"{g['title']}: no show in the copy")
            continue
        seasons = conn.execute("SELECT COUNT(*) FROM season WHERE show_id = ? AND kind ="
                               " 'tvdb_season' AND season_number > 0", (show,)).fetchone()[0]
        if final in ("completed", "complete_aired") and seasons:
            detail = rb._complete_aired(conn, show)
        elif final in ("completed", "complete_aired"):
            status_rules.set_show_status(conn, show, "completed", "rebuild", confirmed=True)
            detail = "no TVDB season: the show itself set completed"
        else:
            target = ("watching" if final == "watching" else "dropped" if final == "dropped"
                      else g["change"].split("→")[-1].strip())  # "replay": the change as made
            status_rules.set_show_status(conn, show, target, "rebuild", confirmed=True)
            detail = f"show set {target}"
        touched.add(show)
        run.record("replay_status", g["key"], "applied", f"{g['title']}: {final} — {detail}")


def replay_manual(run, conn, d: dict, touched: set) -> None:
    from lcars import status_rules

    rb = _rebuild()
    for m in d["manual_watches"]:
        show = rb._show_by_title(conn, m["title"])
        if show is None:
            like = conn.execute(
                "SELECT * FROM show WHERE tracked = 1 AND (title_english LIKE ? OR title_romaji"
                " LIKE ?)", (m["title"] + "%", m["title"] + "%")).fetchall()
            show = like[0] if len(like) == 1 else None
        if show is None:
            run.record("replay_manual", m["key"], "review", f"{m['title']}: show not found")
            continue
        if m["episode"] == "last aired":
            row = conn.execute(
                "SELECT * FROM episode WHERE show_id = ? AND season = ? AND air_date_utc <= ?"
                " ORDER BY episode DESC LIMIT 1", (show["id"], m["season"], m["at"])).fetchone()
        else:
            row = conn.execute("SELECT * FROM episode WHERE show_id = ? AND season = ? AND"
                               " episode = ?", (show["id"], m["season"], m["episode"])).fetchone()
        if row is None:
            run.record("replay_manual", m["key"], "review",
                       f"{m['title']}: no S{m['season']}E{m['episode']} in the copy")
            continue
        replay_watch(conn, show["id"], row, m["at"], None, m["at"])
        touched.add(show["id"])
        if m["key"] == "mw:mushoku-s3-final":  # "S3 finished → completed" (your note)
            level = conn.execute("SELECT id FROM season WHERE show_id = ? AND season_number = ?"
                                 " AND kind = 'tvdb_season'", (show["id"], m["season"])).fetchone()
            if level:
                status_rules.set_level_status(conn, level[0], "completed", "rebuild",
                                              confirmed=True, manual=True)
        run.record("replay_manual", m["key"], "applied",
                   f"{m['title']} S{row['season']}E{row['episode']} watched {m['at']}")


def replay_scores(run, conn, live, mapper: Mapper) -> None:
    rows = live.execute("SELECT * FROM score_change WHERE changed_at > ? AND changed_by IN"
                        " ('holodeck', 'data', 'captains_log') ORDER BY changed_at",
                        (GAP_START,)).fetchall()
    for r in rows:
        show = mapper.show(r["show_id"])
        if show is None:
            run.record("replay_score", r["id"], "review", f"{r['show_id']}: no show in the copy")
            continue
        before = conn.execute("SELECT score FROM show WHERE id = ?", (show,)).fetchone()[0]
        conn.execute("UPDATE show SET score = ?, updated_at = ? WHERE id = ?",
                     (r["new_score"], r["changed_at"], show))
        conn.execute(
            "INSERT INTO score_change (id, show_id, previous_score, new_score, changed_at,"
            " changed_by) VALUES (?, ?, ?, ?, ?, ?)",
            (ids_module.generate_id(conn, "o"), show, before, r["new_score"], r["changed_at"],
             r["changed_by"]))
        run.record("replay_score", r["id"], "applied", f"{show}: {r['new_score']}")


def stage_replay(run) -> None:
    from lcars import status_rules

    rb = _rebuild()
    conn = rb._connect(run.work())
    live = _live(run)
    d = rb.decisions(run)
    mapper = Mapper(run, conn, live)
    touched: set[str] = set()
    replay_watches(run, conn, live, d, mapper, touched)
    replay_statuses(run, conn, d, mapper, touched)
    replay_manual(run, conn, d, touched)
    replay_scores(run, conn, live, mapper)
    for show in sorted(touched):  # R2.15a applies to what was just watched
        status_rules.after_episodes_changed(conn, show, "rebuild")
    conn.commit()
    live.close()
    run.record("stage", "replay", "applied", f"{len(touched)} shows touched")

