#!/usr/bin/env python3
"""One-off data repair, 2026-09-25 (run in the lcars container, v0.2.65+).
Dry run by default; `--apply` to write. Snapshot the DB first.

A. Haruhi duplicate (user: "delete duplicate"): s-31k85m "Haruhi (2009)" is
   an untracked relation stub whose only season claims AniList/MAL 4382 —
   the real Haruhi (2006) S2. Purged locally only: its Sonarr link is the
   real 2006 series and 4382 on the lists belongs to 2006 S2, so neither
   Sonarr nor the lists are touched.
B. Haruhi (2006) S1: MAL 849 (Fribb / AniList 849's own idMal), not 4382.
C. Eight episodes marked watched that never aired (Sonarr "TBA"
   placeholders): the six S2E1s from the 08-12 one-off import, Kaiju No. 8
   S3E1 (09-23) and Ramparts of Ice S2E1. Unwatched locally; their seasons'
   progress pushed as 0 (Kaiju S3 shows 1 on AniList).
D. Future seasons set paused/watching by the 09-19 -> 09-25 loop or the
   fake watch: Blue Box S2, The Dangers in My Heart S3, Ramparts of Ice S2
   -> planned (user rule: a not-yet-released season is planned), pushed to
   AniList + MAL (they are on both lists).
E. Orphaned rows (FK violations left by earlier deletes; user: "delete").
F. The 28 seasons where LCARS and AniList disagreed, per the user's call
   (09-25): LCARS set to the decided status and the decision pushed to both
   lists, so the hub release (list_baseline) starts from agreement.
G. Blue Box / The Dangers in My Heart: re-monitored in Sonarr and resumed
   (user: "re-monitor").
"""

import sqlite3
import sys

from lcars import anilist_client, config, mal_client, resolvers, util

APPLY = "--apply" in sys.argv
DB = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else "/db/lcars.db"

cfg = config.load_config() if APPLY else None
if cfg is not None:
    config.set_current(cfg)  # resolvers' Sonarr re-monitor (G) reads get_current()
c = sqlite3.connect(DB, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA foreign_keys = ON")
remote = []  # (description, callable) — run only on --apply, after the commit


def say(msg):
    print(("" if APPLY else "[dry] ") + msg)


def purge_show_local(show_id):
    eps = [r[0] for r in c.execute("SELECT id FROM episode WHERE show_id = ?", (show_id,))]
    seasons = [r[0] for r in c.execute("SELECT id FROM season WHERE show_id = ?", (show_id,))]
    ep_ph = ",".join("?" * len(eps)) or "''"
    s_ph = ",".join("?" * len(seasons)) or "''"
    c.execute("DELETE FROM watch_event WHERE show_id = ?", (show_id,))
    for t in ("episode_movie_link", "air_date_change", "episode_anidb_mapping",
              "episode_external_id"):
        c.execute(f"DELETE FROM {t} WHERE episode_id IN ({ep_ph})", eps)
    c.execute(
        f"DELETE FROM pending_review WHERE entity_type = 'episode' AND entity_id IN ({ep_ph})", eps
    )
    c.execute("DELETE FROM episode WHERE show_id = ?", (show_id,))
    for t in ("art_asset", "season_external_id"):
        c.execute(f"DELETE FROM {t} WHERE season_id IN ({s_ph})", seasons)
    c.execute(
        f"DELETE FROM pending_review WHERE entity_type = 'season' AND entity_id IN ({s_ph})",
        seasons,
    )
    c.execute("DELETE FROM season WHERE show_id = ?", (show_id,))
    c.execute(
        "UPDATE episode_movie_link SET movie_show_id = NULL WHERE movie_show_id = ?", (show_id,)
    )
    c.execute("DELETE FROM art_asset WHERE show_id = ?", (show_id,))
    for t in ("show_external_id", "show_synonym", "show_service_presence",
              "episode_numbering_mapping", "status_change", "score_change", "tracked_change",
              "show_person", "show_studio", "franchise_member", "show_tag", "next_up_override"):
        c.execute(f"DELETE FROM {t} WHERE show_id = ?", (show_id,))
    c.execute(
        "DELETE FROM show_relation WHERE show_id = ? OR related_show_id = ?", (show_id, show_id)
    )
    # The 09-17 "merge" of this stub into 2006 moved nothing (manifest:
    # every item skipped); its record is the last row pointing at it.
    c.execute("DELETE FROM show_merge WHERE loser_show_id = ?", (show_id,))
    c.execute("DELETE FROM pending_review WHERE entity_type = 'show' AND entity_id = ?", (show_id,))
    c.execute("DELETE FROM show WHERE id = ?", (show_id,))
    say(f"  purged show {show_id}: {len(eps)} episodes, {len(seasons)} seasons")


def season(show_id, n):
    return c.execute(
        "SELECT * FROM season WHERE show_id = ? AND season_number = ?", (show_id, n)
    ).fetchone()


now = util.now_utc_iso()

# A ---------------------------------------------------------------------------
say("A. Haruhi (2009) stub")
stub = c.execute("SELECT id, tracked FROM show WHERE id = 's-31k85m'").fetchone()
if stub is None:
    say("  already gone")
else:
    assert stub["tracked"] == 0
    assert c.execute("SELECT count(*) FROM episode WHERE show_id='s-31k85m'").fetchone()[0] == 0
    assert c.execute("SELECT count(*) FROM watch_event WHERE show_id='s-31k85m'").fetchone()[0] == 0
    purge_show_local("s-31k85m")

# B ---------------------------------------------------------------------------
say("B. Haruhi (2006) S1 -> MAL 849")
s1 = season("s-cgt4ek", 1)
if s1["mal_id"] != 849:
    c.execute("UPDATE season SET mal_id = 849, updated_at = ? WHERE id = ?", (now, s1["id"]))
    c.execute(
        "INSERT INTO season_external_id (season_id, service, external_id, created_at)"
        " VALUES (?, 'mal', '849', ?)"
        " ON CONFLICT (season_id, service) DO UPDATE SET external_id = '849'",
        (s1["id"], now),
    )
n = c.execute(
    "UPDATE show_external_id SET external_id = '849'"
    " WHERE show_id = 's-cgt4ek' AND service = 'mal' AND external_id = '4382'"
).rowcount
say(f"  season S1 mal=849; show-level mal 4382 -> 849: {n}")

# C ---------------------------------------------------------------------------
say("C. Unaired placeholders marked watched")
bad = [("s-kznxz9", 2, 1), ("s-mxryc2", 2, 1), ("s-fehxx7", 2, 1), ("s-a16y56", 2, 1),
       ("s-tk8b7b", 2, 1), ("s-qxm181", 2, 1), ("s-2t40tv", 3, 1), ("s-vkqrm9", 2, 1)]
for show_id, sn, en in bad:
    w = c.execute(
        "DELETE FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
        (show_id, sn, en),
    ).rowcount
    e = c.execute(
        "UPDATE episode SET state = 'unwatched', updated_at = ?"
        " WHERE show_id = ? AND season = ? AND episode = ? AND state = 'watched'",
        (now, show_id, sn, en),
    ).rowcount
    se = season(show_id, sn)
    say(f"  {show_id} S{sn}E{en}: {w} watch events deleted, episode reset={e}")
    if show_id not in ("s-2t40tv", "s-vkqrm9"):
        continue  # the six S2s already read progress 0 on AniList and have no MAL id
    if se["anilist_id"]:
        remote.append((f"AniList {se['anilist_id']} progress=0",
                       lambda a=se["anilist_id"]: anilist_client.save_media_list_entry(
                           cfg.anilist_access_token, a, progress=0)))
    if se["mal_id"]:
        remote.append((f"MAL {se['mal_id']} progress=0",
                       lambda m=se["mal_id"]: mal_client.update_my_list_status(
                           cfg.mal_access_token, m, num_watched_episodes=0)))

# D ---------------------------------------------------------------------------
say("D. Future seasons -> planned")
for show_id, sn in (("s-aq4na5", 2), ("s-5tq9ae", 3), ("s-vkqrm9", 2)):
    se = season(show_id, sn)
    c.execute("UPDATE season SET status = 'planned', updated_at = ? WHERE id = ?", (now, se["id"]))
    if show_id == "s-vkqrm9":
        # Blue Box / Dangers stay paused at show level: Sonarr has them
        # unmonitored, and re-deriving them to watching would get them
        # re-paused (and PAUSED pushed) by the Sonarr reconcile. Whether
        # they're active again is the user's call.
        resolvers._recompute_show_status(c, show_id, "captains_log", _skip_push=True)
    show_status = c.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()[0]
    say(f"  {show_id} S{sn} ({se['status']} -> planned); show status now {show_status}")
    if se["anilist_id"]:
        remote.append((f"AniList {se['anilist_id']} PLANNING",
                       lambda a=se["anilist_id"]: anilist_client.save_media_list_entry(
                           cfg.anilist_access_token, a, status="PLANNING")))
    if se["mal_id"]:
        remote.append((f"MAL {se['mal_id']} plan_to_watch",
                       lambda m=se["mal_id"]: mal_client.update_my_list_status(
                           cfg.mal_access_token, m, status="plan_to_watch")))

# E ---------------------------------------------------------------------------
say("E. Orphaned rows")
orphans = c.execute("PRAGMA foreign_key_check").fetchall()
by_table = {}
for table, rowid, _parent, _fk in orphans:
    by_table.setdefault(table, []).append(rowid)
for table, rowids in by_table.items():
    for rid in rowids:
        c.execute(f'DELETE FROM "{table}" WHERE rowid = ?', (rid,))
    say(f"  {table}: {len(rowids)} deleted")

# F ---------------------------------------------------------------------------
say("F. The 28 LCARS/AniList disagreements, as decided by the user")
decided = {
    # AniList was right (dropped): 17 seasons LCARS had as completed
    "dropped": ["z-805bk9", "z-gdha5q", "z-tszb6m", "z-wb7bkw", "z-z308ec", "z-8vvd65",
                "z-qew9pn", "z-hvc0zh", "z-cxd7tq", "z-kwhnaq", "z-hxqztq", "z-1rhz4m",
                "z-pf4dtg", "z-yycbxm", "z-0l95da", "z-e4dtzk", "z-il6f9r",
                # LCARS was right (dropped): Natsume S1, Returner S1
                "z-pcevp8", "z-0gsptq"],
    # LCARS right (completed): Aristocrat S2, Ranma S2, Saint's Magic S2;
    # AniList right (completed): Bungo S2, Re:ZERO S4
    "completed": ["z-za0jay", "z-ovp0d0", "z-5dyw3e", "z-nb167o", "z-qx1hnx"],
    # LCARS right (still airing): HELL MODE S2, Polar Opposites S2, Slime S5
    "watching": ["z-ghhst4", "z-t16q0y", "z-1578my"],
}
AL = {"dropped": "DROPPED", "completed": "COMPLETED", "watching": "CURRENT"}
MAL = {"dropped": "dropped", "completed": "completed", "watching": "watching"}
f_shows = set()
for target, season_ids in decided.items():
    for sid in season_ids:
        se = c.execute("SELECT * FROM season WHERE id = ?", (sid,)).fetchone()
        if se is None:
            say(f"  {sid}: MISSING")
            continue
        if se["status"] != target:
            c.execute("UPDATE season SET status = ?, updated_at = ? WHERE id = ?",
                      (target, now, sid))
        f_shows.add(se["show_id"])
        say(f"  {se['show_id']} S{se['season_number']}: {se['status']} -> {target}")
        if se["anilist_id"]:
            remote.append((f"AniList {se['anilist_id']} {AL[target]}",
                           lambda a=se["anilist_id"], t=AL[target]:
                           anilist_client.save_media_list_entry(
                               cfg.anilist_access_token, a, status=t)))
        if se["mal_id"]:
            remote.append((f"MAL {se['mal_id']} {MAL[target]}",
                           lambda m=se["mal_id"], t=MAL[target]:
                           mal_client.update_my_list_status(cfg.mal_access_token, m, status=t)))

# G ---------------------------------------------------------------------------
say("G. Blue Box / The Dangers in My Heart: re-monitor in Sonarr and resume")
g_shows = ["s-aq4na5", "s-5tq9ae"]
for show_id in g_shows:
    say(f"  {show_id}: re-monitor in Sonarr, recompute status, clear status_before_pause")

left = c.execute("PRAGMA foreign_key_check").fetchall()
say(f"FK violations remaining: {len(left)}")

if APPLY:
    # Show-status derivation runs only here: its completed path marks
    # episodes and commits, which a dry run must never do.
    for show_id in sorted(f_shows):
        resolvers._recompute_show_status(c, show_id, "captains_log", _skip_push=True)
    for show_id in g_shows:
        resolvers._remonitor_in_arr_on_resume(c, show_id)
        resolvers._recompute_show_status(c, show_id, "captains_log", _skip_push=True)
        c.execute("UPDATE show SET status_before_pause = NULL WHERE id = ?", (show_id,))
        st = c.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()[0]
        print(f"{show_id}: re-monitored, show status {st}")
    c.commit()
    for desc, fn in remote:
        try:
            fn()
            print(f"pushed: {desc}")
        except Exception as e:
            print(f"PUSH FAILED: {desc}: {e}")
else:
    c.rollback()
    for desc, _ in remote:
        say(f"would push: {desc}")
