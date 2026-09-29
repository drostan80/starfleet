"""The list hub's state (PLAN-CODE 7.5, RULEBOOK R4.9/R4.10).

LCARS is the truth and is written first. This module holds what the reconcile
(`watch_reconcile._apply_remote_list`, shared by AniList and MAL) needs on top of
`list_baseline`'s write memory:

- the **settle lock**, per row (a level with its AniList and MAL entries): once an
  outside change on a row has been taken, no outside change on it is taken until
  LCARS has written its decision to every list and stored the read-backs
  ("settled"); a row still locked after `LOCK_LIMIT_MINUTES` opens a review;
- `list_sync_log`: what the hub took, deferred, overrode or sent to review;
- LCARS's own last change of a level's progress (watch events that didn't come
  from a list), for the both-changed comparison;
- `unwatch_last`: a list's lower progress applied to the level.

Nothing here is locked or recorded while writes are only captured (9.0): the
rebuild's dry run has no read-backs to settle on, so it logs what it would do.
"""

from __future__ import annotations

import datetime
import logging

from lcars import anilist_client, config, external_writes, list_baseline, mal_client, reviews, util

logger = logging.getLogger(__name__)

LOCK_LIMIT_MINUTES = 15
LIST_SOURCES = ("anilist_reconcile", "mal_reconcile")  # watch_event.platform of list intake


def intake_enabled() -> bool:
    """R4.10: until the whole new setup is live, nothing an outside list holds is
    taken into LCARS (LCARS → lists still follows `external_writes`)."""
    return bool(config.get_current().list_intake_enabled)


def log(conn, season_id, service, kind: str, detail: str = "") -> None:
    logger.info("list hub %s %s %s %s", season_id, service, kind, detail)
    if external_writes.capturing():
        return  # a dry run only reports (the log line above)
    conn.execute(
        "INSERT INTO list_sync_log (season_id, service, at, kind, detail) VALUES (?, ?, ?, ?, ?)",
        (season_id, service, util.now_utc_iso(), kind, detail),
    )


# ── outside edits found before a propagation write ─────────────────────

_SNAPSHOTS: dict = {}


def reset_snapshots() -> None:
    _SNAPSHOTS.clear()


def snapshot(service: str) -> dict:
    """{external id: {status, progress, updated_at}} of one list, read once per run
    (the base of the "did someone edit it since LCARS last looked" check)."""
    if service not in _SNAPSHOTS:
        cfg = config.get_current()
        entries: dict = {}
        try:
            if service == "anilist" and cfg.anilist_access_token:
                for e in anilist_client.fetch_my_anime_list(cfg.anilist_access_token):
                    entries[e["anilist_id"]] = {
                        "status": e["status"], "progress": e.get("progress"),
                        "updated_at": e.get("updated_at")}
            elif service == "mal" and cfg.mal_access_token:
                for e in mal_client.fetch_my_list(cfg.mal_access_token):
                    entries[e["mal_id"]] = {"status": e["status"],
                                            "progress": e.get("num_watched_episodes"),
                                            "updated_at": e.get("updated_at")}
        except (anilist_client.AniListError, mal_client.MALError):
            return {}  # unknown: not cached, the write goes ahead (its failure is handled)
        _SNAPSHOTS[service] = entries
    return _SNAPSHOTS[service]


def outside_edit_pending(conn, service: str, ext_id: int) -> bool:
    """R4.10: the entry's update time moved since LCARS last wrote or judged it — an
    edit made on the list that LCARS hasn't looked at. A propagation write must not
    overwrite it: it is held (deferred) and judged on its own real time next poll."""
    base = list_baseline.get(conn, service, ext_id)
    if base is None or not base["remote_updated_at"]:
        return False  # nothing remembered to compare with
    seen = snapshot(service).get(ext_id)
    return seen is not None and seen["updated_at"] not in (None, base["remote_updated_at"])


def defer(conn, season_id: str, service: str, ext_id: int) -> None:
    seen = snapshot(service).get(ext_id) or {}
    if not external_writes.capturing():
        # kept until the entry is judged: what was found there, not what to do
        list_baseline.record(conn, service, ext_id, deferred_status=seen.get("status"),
                             deferred_progress=seen.get("progress"),
                             deferred_updated_at=seen.get("updated_at"))
    log(conn, season_id, service, "deferred",
        f"edited on the list ({seen.get('updated_at')}) before LCARS's write reached it")


# ── the settle lock ──────────────────────────────────────────────────────


def locked(conn, season_id: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM list_row_lock WHERE season_id = ?", (season_id,)
    ).fetchone() is not None


def lock(conn, season_id: str, source: str, decided_at: str | None = None) -> None:
    if external_writes.capturing():
        return  # no read-backs exist to settle on
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO list_row_lock (season_id, since, source, decided_at) VALUES (?, ?, ?, ?)"
        " ON CONFLICT (season_id) DO UPDATE SET decided_at = excluded.decided_at,"
        " source = excluded.source",
        (season_id, now, source, decided_at or now),
    )


def restamp(conn, season_id: str, source: str, since: str, real_time: str | None) -> None:
    """A status change taken from a list is timestamped with the list's own update
    time, not the moment LCARS noticed it (R4.10) — the order of two outside edits
    is the order they were made in."""
    if real_time and real_time < since:
        conn.execute(
            "UPDATE season_status_change SET changed_at = ? WHERE season_id = ?"
            " AND changed_by = ? AND changed_at >= ?", (real_time, season_id, source, since))


def unlock(conn, season_id: str) -> None:
    conn.execute("DELETE FROM list_row_lock WHERE season_id = ?", (season_id,))


def settled(conn, season_id: str) -> bool:
    """Every list entry of the row holds LCARS's latest decision: its status and
    progress were written and read back (what a service then holds is its own
    business, R4.10: it is compared against the read-back, not the intention)."""
    from lcars import list_sync

    season = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if season is None or not list_sync.pushable(conn, season):
        return True
    want_progress = list_sync.level_progress(conn, season) if season["show_id"] else None
    for service, ext in list_sync.list_ids(conn, season_id).items():
        base = list_baseline.get(conn, service, ext)
        if base is None:
            return False
        if base["deferred_updated_at"]:
            continue  # an outside edit waits there; it is judged once the row is open
        if base["lcars_status"] != season["status"]:
            return False
        if want_progress is not None and base["lcars_progress"] != want_progress:
            return False
    return True


def settle_locked(conn) -> int:
    """After a run's propagation: unlock every row that settled; a row locked past
    the limit opens a review. Returns how many rows were unlocked."""
    unlocked = 0
    limit = (datetime.datetime.now(datetime.UTC)
             - datetime.timedelta(minutes=LOCK_LIMIT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for row in conn.execute("SELECT * FROM list_row_lock").fetchall():
        if settled(conn, row["season_id"]):
            unlock(conn, row["season_id"])
            unlocked += 1
        elif row["since"] < limit:
            season = conn.execute("SELECT show_id FROM season WHERE id = ?",
                                  (row["season_id"],)).fetchone()
            reviews.open_review(
                conn, "season", row["season_id"], "list_not_settled", row["source"],
                f"a list change since {row['since']} hasn't reached every list",
                ["retry_now", "unlock"], {"season_id": row["season_id"]},
                show_id=season["show_id"] if season else None)
            log(conn, row["season_id"], None, "not_settled", f"locked since {row['since']}")
    return unlocked


# ── LCARS's own changes ──────────────────────────────────────────────────


def last_watch_change(conn, season) -> str | None:
    """When LCARS itself last changed the level's progress: the latest watch event
    over the level's episodes that did not come from a list (R4.10)."""
    from lcars import list_sync

    marks = ",".join("?" for _ in LIST_SOURCES)
    latest = None
    for e in list_sync.level_episodes_ordered(conn, season):
        row = conn.execute(
            f"SELECT MAX(created_at) FROM watch_event WHERE show_id = ? AND season = ?"
            f" AND episode = ? AND COALESCE(platform, '') NOT IN ({marks})",
            (season["show_id"], e["season"], e["episode"], *LIST_SOURCES)).fetchone()
        if row[0] and (latest is None or row[0] > latest):
            latest = row[0]
    return latest


def unwatch_last(conn, season, count: int) -> int:
    """A list's lower progress: the last `count` watched episodes of the level are
    unwatched (their watch events removed, like deleteWatchEvent). Returns how
    many were unwatched."""
    from lcars import list_sync

    watched = [e for e in list_sync.level_episodes_ordered(conn, season)
               if e["state"] == "watched"]
    now = util.now_utc_iso()
    done = 0
    for e in watched[-count:] if count > 0 else []:
        conn.execute("DELETE FROM watch_event WHERE show_id = ? AND season = ? AND episode = ?",
                     (season["show_id"], e["season"], e["episode"]))
        conn.execute("UPDATE episode SET state = 'unwatched', updated_at = ? WHERE id = ?",
                     (now, e["id"]))
        done += 1
    return done
