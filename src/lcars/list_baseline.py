"""LCARS as the hub between AniList and MAL (user rule, 2026-09-25).

"Keep LCARS the source of truth: if a change is made in an external list it
writes to LCARS and propagates to the other list." Doing that needs to know
*which side changed*. This module keeps, per list entry `(service,
external_id)`, the last status/progress LCARS and that list agreed on:

  - list value != baseline          -> the list was edited: LCARS takes it,
                                       then pushes it to the other list
  - list == baseline, LCARS differs -> LCARS changed and the list hasn't
                                       taken it yet: push again
  - all equal                       -> nothing to do

Every write to a list goes through `anilist_save` / `mal_save`, which
record what the list *saved* (AniList changes status on its own when
progress moves), and never record on failure — so a failed push is retried
on the next reconcile instead of being overwritten by the list's old value.
"""

from lcars import anilist_client, mal_client, util

ANILIST_TO_STATUS = {
    "CURRENT": "watching",
    "PLANNING": "planned",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
    "REPEATING": "watching",
}
MAL_TO_STATUS = {
    "watching": "watching",
    "plan_to_watch": "planned",
    "on_hold": "paused",
    "completed": "completed",
    "dropped": "dropped",
}

_UNSET = object()


def get(conn, service: str, external_id) -> dict | None:
    row = conn.execute(
        "SELECT status, progress FROM list_baseline WHERE service = ? AND external_id = ?",
        (service, int(external_id)),
    ).fetchone()
    return dict(row) if row is not None else None


def record(conn, service: str, external_id, *, status=_UNSET, progress=_UNSET) -> None:
    """Upsert the fields given; a field not passed keeps its stored value."""
    now = util.now_utc_iso()
    current = get(conn, service, external_id) or {"status": None, "progress": None}
    if status is not _UNSET:
        current["status"] = status
    if progress is not _UNSET:
        current["progress"] = progress
    conn.execute(
        "INSERT INTO list_baseline (service, external_id, status, progress, updated_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (service, external_id) DO UPDATE SET"
        " status = excluded.status, progress = excluded.progress,"
        " updated_at = excluded.updated_at",
        (service, int(external_id), current["status"], current["progress"], now),
    )


def is_seeded(conn, service: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM list_baseline_seed WHERE service = ?", (service,)
    ).fetchone() is not None


def mark_seeded(conn, service: str) -> None:
    conn.execute(
        "INSERT OR IGNORE INTO list_baseline_seed (service, seeded_at) VALUES (?, ?)",
        (service, util.now_utc_iso()),
    )


def anilist_save(conn, token: str, anilist_id: int, **fields) -> dict:
    """`anilist_client.save_media_list_entry` + record what AniList saved.
    Raises exactly like the client; nothing is recorded on failure.

    Only the fields this push wrote are recorded — a score push must not
    stamp the list's current status as agreed, or an edit made on the list
    moments earlier would be swallowed. A progress push also records the
    saved status: AniList moves PLANNING -> CURRENT -> COMPLETED itself."""
    saved = anilist_client.save_media_list_entry(token, anilist_id, **fields) or {}
    kw = {}
    if "status" in fields or "progress" in fields:
        status = saved.get("status") or fields.get("status")
        if status in ANILIST_TO_STATUS:
            kw["status"] = ANILIST_TO_STATUS[status]
    if "progress" in fields:
        kw["progress"] = saved.get("progress", fields["progress"])
    if kw:
        record(conn, "anilist", anilist_id, **kw)
    return saved


def mal_save(conn, token: str, mal_id: int, **fields) -> dict:
    """`mal_client.update_my_list_status` + record what MAL saved (same
    only-what-was-written rule as `anilist_save`)."""
    saved = mal_client.update_my_list_status(token, mal_id, **fields) or {}
    kw = {}
    if "status" in fields or "num_watched_episodes" in fields:
        status = saved.get("status") or fields.get("status")
        if status in MAL_TO_STATUS:
            kw["status"] = MAL_TO_STATUS[status]
    if "num_watched_episodes" in fields:
        kw["progress"] = saved.get("num_episodes_watched", fields["num_watched_episodes"])
    if kw:
        record(conn, "mal", mal_id, **kw)
    return saved
