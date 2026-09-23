#!/usr/bin/env python3
"""One-off: clear the 2026-09-23 review queue by fixing its causes (run in the
lcars container, v0.2.59+). Dry run by default; `--apply` to write.

Rules come from the project plan, not per-case guesses:
- AniList is authoritative; MAL mirrors it (a season's MAL id is Fribb's
  pairing for its AniList id; MAL list entries mirror LCARS/AniList).
- Junk from wrong links is purged locally — never via confirmHardDelete,
  whose AniList mirror would delete the *real* entries these junk rows share.

A. Purge: Tantei junk show s-qp3t5j (Milky Holmes mislink, hard delete
   requested 09-20; its seasons carry the real Tantei ids) and Aristocrat
   s-4k9g5s S4/S5 (manual, empty duplicates of S3's 185756).
B. MAL identity: every tracked season whose MAL id Fribb assigns to a
   different AniList entry gets Fribb's MAL id for its AniList id. On MAL,
   every touched entry (the corrected one, and the old one it used to point
   at) is re-mirrored from its own AniList entry — MAL mirrors AniList, not
   LCARS — and nothing is pushed where the user has no AniList entry. An old
   entry no LCARS season claims and with no AniList counterpart on the list
   is removed (it only exists because LCARS pushed to it).
C. Scores: each open `anilist_score_drift` season takes AniList's score
   (LCARS-side only; AniList already holds it).
D. Milky Holmes (TVDB 197261): removed from AniList and MAL (pushed by the
   junk show; the user never watched it).
E. Reviews: each one fixed above is resolved with a note; the Haruhi S2
   width check is resolved as informational (AniList counts the 2009
   broadcast's 14 reruns, LCARS holds the 14 new episodes).
"""

import sqlite3
import sys

import httpx

from lcars import anilist_client, config, fribb, mal_client, util

APPLY = "--apply" in sys.argv
DB = "/db/lcars.db"
NOTE = "Fixed 2026-09-23 (scripts/fix_review_queue_20260923.py): "
AL_TO_MAL_STATUS = {"CURRENT": "watching", "REPEATING": "watching", "COMPLETED": "completed",
                    "PLANNING": "plan_to_watch", "PAUSED": "on_hold", "DROPPED": "dropped"}

cfg = config.load_config()
c = sqlite3.connect(DB, timeout=30)
c.row_factory = sqlite3.Row
c.execute("PRAGMA foreign_keys = ON")
base_fk = set(map(tuple, c.execute("PRAGMA foreign_key_check").fetchall()))
remote = []  # (description, callable) — run only on --apply, after the DB commit


def say(msg):
    print(("" if APPLY else "[dry] ") + msg)


def resolve(where, params, note):
    n = c.execute(
        "UPDATE pending_review SET resolved_at = ?, resolved_by_client = 'captains_log',"
        f" resolution_note = ? WHERE resolved_at IS NULL AND ({where})",
        (util.now_utc_iso(), NOTE + note, *params),
    ).rowcount
    say(f"  reviews resolved: {n}")


def mirror_from_anilist(mal_id, anilist_ids, why):
    """Queue a MAL push copying the user's AniList entry (status + score)
    onto `mal_id` — MAL mirrors AniList. No AniList entry: nothing pushed."""
    src = next((al_list[a] for a in anilist_ids if a in al_list), None)
    if src is None:
        say(f"  MAL {mal_id}: no AniList entry to mirror ({why}) — not pushed")
        return
    st = AL_TO_MAL_STATUS.get(src["status"])
    sc = round(src["score"] / 10) if src.get("score") else None
    remote.append((f"MAL {mal_id} <- AniList {src['anilist_id']} status={st} score={sc} ({why})",
                   lambda m=mal_id, st=st, sc=sc: mal_client.update_my_list_status(
                       cfg.mal_access_token, m, status=st, score=sc)))


def purge_show_local(show_id):
    eps = [r[0] for r in c.execute("SELECT id FROM episode WHERE show_id = ?", (show_id,))]
    seasons = [r[0] for r in c.execute("SELECT id FROM season WHERE show_id = ?", (show_id,))]
    ep_ph = ",".join("?" * len(eps)) or "''"
    s_ph = ",".join("?" * len(seasons)) or "''"
    c.execute("DELETE FROM watch_event WHERE show_id = ?", (show_id,))
    for t in (
        "episode_movie_link",
        "air_date_change",
        "episode_anidb_mapping",
        "episode_external_id",
    ):
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
    for t in (
        "show_external_id",
        "show_synonym",
        "show_service_presence",
        "episode_numbering_mapping",
        "status_change",
        "score_change",
        "tracked_change",
        "show_person",
        "show_studio",
        "franchise_member",
        "show_tag",
        "next_up_override",
    ):
        c.execute(f"DELETE FROM {t} WHERE show_id = ?", (show_id,))
    c.execute(
        "DELETE FROM show_relation WHERE show_id = ? OR related_show_id = ?", (show_id, show_id)
    )
    c.execute("DELETE FROM pending_review WHERE entity_type = 'show' AND entity_id = ?", (show_id,))
    c.execute("DELETE FROM show WHERE id = ?", (show_id,))
    say(f"  purged show {show_id}: {len(eps)} episodes, {len(seasons)} seasons")


def purge_empty_season(season_id):
    assert (
        c.execute("SELECT count(*) FROM episode WHERE season_id = ?", (season_id,)).fetchone()[0]
        == 0
    )
    for t in ("art_asset", "season_external_id"):
        c.execute(f"DELETE FROM {t} WHERE season_id = ?", (season_id,))
    c.execute(
        "DELETE FROM pending_review WHERE entity_type = 'season' AND entity_id = ?", (season_id,)
    )
    c.execute("DELETE FROM season WHERE id = ?", (season_id,))
    say(f"  purged empty season {season_id}")


dataset = fribb.load_dataset()
al_list = {e["anilist_id"]: e for e in anilist_client.fetch_my_anime_list(cfg.anilist_access_token)}
mal_list = {e["mal_id"]: e for e in mal_client.fetch_my_list(cfg.mal_access_token)}

# --- A. Purges -------------------------------------------------------------
say("A. Tantei junk show s-qp3t5j")
row = c.execute(
    "SELECT tracked, hard_delete_requested_at FROM show WHERE id = 's-qp3t5j'"
).fetchone()
if row is not None:
    assert row["tracked"] == 0 and row["hard_delete_requested_at"]
    purge_show_local("s-qp3t5j")
    resolve(
        "field IN ('anilist_id_conflict','mal_id_conflict') AND entity_id IN"
        " (SELECT id FROM season WHERE show_id = 's-mhz5md')",
        (),
        "other claimant s-qp3t5j (Milky Holmes mislink) purged locally.",
    )
say("A. Aristocrat s-4k9g5s duplicate S4/S5")
for sid in [
    r[0]
    for r in c.execute(
        "SELECT id FROM season WHERE show_id = 's-4k9g5s' AND season_number IN (4, 5)"
        " AND anilist_id = 185756 AND manual_override = 1"
    )
]:
    purge_empty_season(sid)

# --- B. MAL identity --------------------------------------------------------
say("B. MAL ids that belong to a different AniList entry")
split = []
for r in c.execute("""SELECT s.id, s.show_id, s.season_number, s.anilist_id, s.mal_id,
        s.status, s.score,
        substr(coalesce(sh.title_english, sh.title_romaji), 1, 30) t FROM season s
        JOIN show sh ON sh.id = s.show_id
        WHERE sh.tracked = 1 AND s.anilist_id IS NOT NULL AND s.mal_id IS NOT NULL""").fetchall():
    new = fribb.mal_for_anilist(dataset, r["anilist_id"])
    owners = fribb.anilist_ids_for_mal(dataset, r["mal_id"])
    if new is not None and new != r["mal_id"] and owners and r["anilist_id"] not in owners:
        split.append((dict(r), new))
for r, new in split:
    say(f"  {r['t']} S{r['season_number']}: MAL {r['mal_id']} -> {new}")
    c.execute(
        "UPDATE season SET mal_id = ?, updated_at = ? WHERE id = ?",
        (new, util.now_utc_iso(), r["id"]),
    )
    c.execute(
        "UPDATE season_external_id SET external_id = ? WHERE season_id = ? AND service = 'mal'",
        (str(new), r["id"]),
    )
    c.execute(
        "DELETE FROM episode_external_id WHERE service = 'mal' AND episode_id IN"
        " (SELECT id FROM episode WHERE season_id = ?)",
        (r["id"],),
    )
    mirror_from_anilist(new, [r["anilist_id"]], f"{r['t']} S{r['season_number']}")
for r, _new in split:
    old = r["mal_id"]
    still_claimed = c.execute("SELECT count(*) FROM season WHERE mal_id = ?", (old,)).fetchone()[0]
    on_anilist = any(a in al_list for a in fribb.anilist_ids_for_mal(dataset, old))
    if old in mal_list and not still_claimed and not on_anilist:
        remote.append(
            (
                f"MAL {old} removed (no LCARS season claims it; not on AniList)",
                lambda m=old: httpx.delete(
                    f"https://api.myanimelist.net/v2/anime/{m}/my_list_status",
                    headers={"Authorization": "Bearer " + cfg.mal_access_token},
                    timeout=20,
                ).raise_for_status(),
            )
        )
    else:
        say(
            f"  MAL {old} kept (claimed by {still_claimed} season(s), on AniList: {on_anilist},"
            f" on MAL list: {old in mal_list})"
        )
        # Re-mirror it from its own AniList counterpart: the wrong season's
        # pushes (e.g. Bungo S2 'watching' onto S3's entry) may be what MAL
        # holds now.
        if old in mal_list:
            mirror_from_anilist(old, sorted(fribb.anilist_ids_for_mal(dataset, old)),
                                "re-mirror of the old entry")

# --- C. Scores ---------------------------------------------------------------
say("C. Score drifts: AniList is authoritative")
for r in c.execute("""SELECT p.id pid, s.id, s.anilist_id, s.score,
        substr(coalesce(sh.title_english, sh.title_romaji), 1, 30) t, s.season_number sn
        FROM pending_review p JOIN season s ON s.id = p.entity_id JOIN show sh ON sh.id = s.show_id
        WHERE p.resolved_at IS NULL AND p.source = 'anilist_score_drift'""").fetchall():
    e = al_list.get(r["anilist_id"])
    if not e or not e.get("score"):
        say(f"  {r['t']} S{r['sn']}: no AniList score — left for review")
        continue
    new = round(e["score"] / 5, 2)
    say(f"  {r['t']} S{r['sn']}: {r['score']} -> {new} (AniList {e['score']})")
    c.execute(
        "UPDATE season SET score = ?, updated_at = ? WHERE id = ?",
        (new, util.now_utc_iso(), r["id"]),
    )
    resolve(
        "id = ?",
        (r["pid"],),
        f"took AniList's score {e['score']} -> {new} (AniList authoritative).",
    )

# --- D. Milky Holmes ---------------------------------------------------------
say("D. Milky Holmes (TVDB 197261) on AniList/MAL")
mh = [e for e in dataset if e.get("tvdb_id") == 197261]
for a in sorted({e["anilist_id"] for e in mh if e.get("anilist_id")} & set(al_list)):
    remote.append(
        (
            f"AniList {a} removed ({al_list[a]['title']})",
            lambda a=a: anilist_client.delete_media_list_entry(
                cfg.anilist_access_token,
                anilist_client.fetch_my_list_entry_id(cfg.anilist_access_token, a),
            ),
        )
    )
for m in sorted({e["mal_id"] for e in mh if e.get("mal_id")} & set(mal_list)):
    remote.append(
        (
            f"MAL {m} removed (Milky Holmes)",
            lambda m=m: httpx.delete(
                f"https://api.myanimelist.net/v2/anime/{m}/my_list_status",
                headers={"Authorization": "Bearer " + cfg.mal_access_token},
                timeout=20,
            ).raise_for_status(),
        )
    )

# --- E. Remaining informational review ---------------------------------------
say("E. Haruhi S2 width check")
resolve(
    "source = 'anilist_width_check' AND entity_id = 'z-cs62cf'",
    (),
    "informational — AniList's 28 counts the 2009 broadcast's 14 reruns; LCARS holds the 14 new.",
)

new_fk = sorted(set(map(tuple, c.execute("PRAGMA foreign_key_check").fetchall())) - base_fk)
say(f"new FK violations: {len(new_fk)}")
say("remote actions" + (":" if remote else ": none"))
for desc, _ in remote:
    say(f"  {desc}")
if not APPLY or new_fk:
    c.rollback()
    print("rolled back — nothing written" + (" (FK violations!)" if new_fk else ""))
    sys.exit(0)
c.commit()
print("DB changes committed")
for desc, fn in remote:
    try:
        fn()
        print(f"  ok: {desc}")
    except Exception as e:  # keep going; report every failure
        print(f"  FAILED: {desc}: {e}")
left = c.execute(
    "SELECT source, field, count(*) FROM pending_review WHERE resolved_at IS NULL GROUP BY 1, 2"
).fetchall()
print("open reviews now:", [tuple(r) for r in left])
