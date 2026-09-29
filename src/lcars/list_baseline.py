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
record only what they wrote, and never record on failure — so a failed push is retried
on the next reconcile instead of being overwritten by the list's old value.
"""

from lcars import anilist_client, external_writes, mal_client, util

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



def get(conn, service: str, external_id) -> dict | None:
    row = conn.execute(
        "SELECT status, progress, lcars_progress, lcars_status, written_at, remote_updated_at,"
        " deferred_status, deferred_progress, deferred_updated_at FROM list_baseline"
        " WHERE service = ? AND external_id = ?",
        (service, int(external_id)),
    ).fetchone()
    return dict(row) if row is not None else None


_FIELDS = ("status", "progress", "lcars_progress", "lcars_status", "written_at",
           "remote_updated_at", "deferred_status", "deferred_progress", "deferred_updated_at")


def record(conn, service: str, external_id, **fields) -> None:
    """Upsert the fields given; a field not passed keeps its stored value."""
    unknown = set(fields) - set(_FIELDS)
    if unknown:
        raise TypeError(f"unknown list_baseline field(s): {sorted(unknown)}")
    current = get(conn, service, external_id) or {name: None for name in _FIELDS}
    current.update(fields)
    columns = ", ".join(_FIELDS)
    marks = ", ".join("?" for _ in _FIELDS)
    updates = ", ".join(f"{name} = excluded.{name}" for name in _FIELDS)
    conn.execute(
        f"INSERT INTO list_baseline (service, external_id, {columns}, updated_at)"
        f" VALUES (?, ?, {marks}, ?)"
        f" ON CONFLICT (service, external_id) DO UPDATE SET {updates},"
        " updated_at = excluded.updated_at",
        (service, int(external_id), *(current[name] for name in _FIELDS), util.now_utc_iso()),
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
    """`anilist_client.save_media_list_entry` + remember what LCARS wrote and what
    the service actually holds afterwards (R4.10): the read-back status and
    progress, the service's own update time (same converter as the list fetch,
    so an unchanged entry compares equal), and when LCARS wrote. Raises exactly
    like the client; nothing is recorded on failure.

    A status the service moved by itself (PLANNING -> CURRENT -> COMPLETED when
    progress reaches the end) is part of the read-back, not an outside change."""
    saved = anilist_client.save_media_list_entry(token, anilist_id, **fields) or {}
    if external_writes.capturing():
        return saved  # recorded for review, not sent: nothing is agreed yet (9.0)
    kw = {"written_at": util.now_utc_iso(),
          "remote_updated_at": anilist_client._unix_to_iso(saved.get("updatedAt"))}
    if saved.get("status") in ANILIST_TO_STATUS:
        kw["status"] = ANILIST_TO_STATUS[saved["status"]]
    if fields.get("status") in ANILIST_TO_STATUS:
        kw["lcars_status"] = ANILIST_TO_STATUS[fields["status"]]
    if "progress" in fields:
        kw["progress"] = saved.get("progress", fields["progress"])
        kw["lcars_progress"] = fields["progress"]
    record(conn, "anilist", anilist_id, **kw)
    return saved


def mal_save(conn, token: str, mal_id: int, **fields) -> dict:
    """`mal_client.update_my_list_status` + the same write memory (MAL returns its
    `my_list_status`: read-back status, progress and its own `updated_at`)."""
    saved = mal_client.update_my_list_status(token, mal_id, **fields) or {}
    if external_writes.capturing():
        return saved  # recorded for review, not sent: nothing is agreed yet (9.0)
    kw = {"written_at": util.now_utc_iso(),
          "remote_updated_at": mal_client._iso_utc(saved.get("updated_at"))}
    if saved.get("status") in MAL_TO_STATUS:
        kw["status"] = MAL_TO_STATUS[saved["status"]]
    if fields.get("status") in MAL_TO_STATUS:
        kw["lcars_status"] = MAL_TO_STATUS[fields["status"]]
    if "num_watched_episodes" in fields:
        kw["progress"] = saved.get("num_episodes_watched", fields["num_watched_episodes"])
        kw["lcars_progress"] = fields["num_watched_episodes"]
    record(conn, "mal", mal_id, **kw)
    return saved
