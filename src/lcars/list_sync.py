"""LCARS → AniList/MAL per level (PLAN-CODE phase 7) — RULEBOOK R4.2–R4.6, R3.6b, R2.10.

- Every season that isn't skipped is mirrored (R4.5, R3.6b: individual seasons
  too); skipped is never written anywhere (R4.6).
- Progress is per level (AniList/MAL are always season level, R1.23): the
  furthest watched episode among the level's own episodes, in absolute order —
  a cour's progress counts only the cour's episodes.
- One list entry can cover several TVDB seasons (Urusei Yatsura: AniList 1293 is four seasons,
  54 + 52 + 43 + 46 = 195 episodes). Those levels are one entry (`group_levels`): its progress is
  the furthest watched episode across all of them in absolute order, so every level says the same
  thing; its status is never written from LCARS (the list's status is mirrored onto the last
  level instead, watch_reconcile) — user decision 2026-10-05.
- A season LCARS added itself as planned and then moved to skipped is deleted
  from AniList/MAL (R2.10); one you set yourself never is.
Best effort: a failed write opens a review and is retried by the reconcile.
"""

from __future__ import annotations

from lcars import (
    anilist_client,
    config,
    external_writes,
    list_baseline,
    list_hub,
    mal_client,
    pending_review,
    reviews,
)

STATUS_TO_ANILIST = {
    "watching": "CURRENT", "planned": "PLANNING", "paused": "PAUSED",
    "completed": "COMPLETED", "dropped": "DROPPED",
}
STATUS_TO_MAL = {
    "watching": "watching", "planned": "plan_to_watch", "paused": "on_hold",
    "completed": "completed", "dropped": "dropped",
}


def _season(conn, season_id: str):
    return conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()


def list_ids(conn, season_id: str) -> dict[str, int]:
    rows = conn.execute(
        "SELECT service, external_id FROM season_external_id WHERE season_id = ?"
        " AND service IN ('anilist', 'mal')",
        (season_id,),
    ).fetchall()
    ids = {r[0]: int(r[1]) for r in rows}
    season = _season(conn, season_id)
    if season is not None:
        if "anilist" not in ids and season["anilist_id"] is not None:
            ids["anilist"] = int(season["anilist_id"])
        if "mal" not in ids and season["mal_id"] is not None:
            ids["mal"] = int(season["mal_id"])
    return ids


def pushable(conn, season) -> bool:
    """Mirrored unless skipped (R4.6); a season of an untracked show (the
    skip list) never is."""
    if season is None or season["status"] in (None, "skipped"):
        return False  # skipped never (R4.6); no status yet (a §2.2 gap) — nothing to say
    if season["show_id"] is None:
        return True  # an individual season (R3.6b)
    show = conn.execute("SELECT tracked FROM show WHERE id = ?", (season["show_id"],)).fetchone()
    return show is not None and bool(show[0])


def group_levels(conn, season) -> list:
    """The levels that are ONE list entry: those of the same show holding this level's AniList
    (or MAL) id, in season order — just the level itself when its entry is its own."""
    if season is None or season["show_id"] is None:
        return [season]
    if season["anilist_id"] is None and season["mal_id"] is None:
        return [season]
    rows = conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND status IS NOT NULL AND status != 'skipped'"
        " AND ((? IS NOT NULL AND anilist_id = ?) OR (? IS NOT NULL AND mal_id = ?))"
        " ORDER BY COALESCE(season_number, 9999), part_number, id",
        (season["show_id"], season["anilist_id"], season["anilist_id"],
         season["mal_id"], season["mal_id"]),
    ).fetchall()
    return rows if len(rows) > 1 and any(r["id"] == season["id"] for r in rows) else [season]


def is_shared_entry(conn, season) -> bool:
    return len(group_levels(conn, season)) > 1


def is_group_representative(conn, season) -> bool:
    """One level speaks for a shared entry — the last, so the list's status is mirrored onto the
    level that is current. A level with its own entry is its own representative."""
    group = group_levels(conn, season)
    return group[-1]["id"] == season["id"]


def level_episodes_ordered(conn, season) -> list:
    from lcars import status_rules

    group = group_levels(conn, season)
    if len(group) > 1:  # one entry, several seasons: its episodes are all of theirs
        seen: dict = {}
        for level in group:
            for e in status_rules.level_episodes(conn, level):
                seen.setdefault(e["id"], e)
        eps = list(seen.values())
    else:
        eps = status_rules.level_episodes(conn, season)
    number = {r[0]: r[1] for r in conn.execute(
        "SELECT id, absolute_number FROM episode WHERE show_id = ?", (season["show_id"],))}
    return sorted(eps, key=lambda e: (number.get(e["id"]) is None, number.get(e["id"]) or 0,
                                      e["season"], e["episode"]))


def level_progress(conn, season) -> int:
    """The furthest watched episode among the level's own, as a count (the
    AniList/MAL `progress` of that entry)."""
    furthest = 0
    for i, e in enumerate(level_episodes_ordered(conn, season), start=1):
        if e["state"] == "watched":
            furthest = i
    return furthest


def _fields(service: str, season, count, status: bool) -> dict:
    """What LCARS says for one service: its status (unless `status` is False) and
    progress, in that service's own vocabulary."""
    fields: dict = {}
    if service == "anilist":
        if status and season["status"] in STATUS_TO_ANILIST:
            fields["status"] = STATUS_TO_ANILIST[season["status"]]
        if count is not None:
            fields["progress"] = count
    else:
        if status and season["status"] in STATUS_TO_MAL:
            fields["status"] = STATUS_TO_MAL[season["status"]]
        if count is not None:
            fields["num_watched_episodes"] = count
    return fields


def _save(conn, cfg, service: str, ext: int, fields: dict) -> None:
    if service == "anilist" and cfg.anilist_access_token:
        list_baseline.anilist_save(conn, cfg.anilist_access_token, ext, **fields)
    elif service == "mal" and cfg.mal_access_token:
        list_baseline.mal_save(conn, cfg.mal_access_token, ext, **fields)


def _read_back(conn, cfg, season, service: str, ext: int, count, check_status: bool = True) -> None:
    """R4.10: what the service holds after the write is compared with LCARS's
    decision. A status it moved by itself gets one corrective, status-only write;
    what still differs (or a progress it clamped) is not pushed again — the
    baseline holds the read-back, so the reconcile leaves it — but one review says so."""
    if external_writes.capturing():
        return
    base = list_baseline.get(conn, service, ext) or {}
    wanted = _fields(service, season, None, True).get("status") if check_status else None
    if wanted and base.get("status") not in (None, season["status"]):
        _save(conn, cfg, service, ext, {"status": wanted})
        base = list_baseline.get(conn, service, ext) or {}
    differs = []
    if wanted and base.get("status") not in (None, season["status"]):
        differs.append(f"status {base.get('status')} (LCARS {season['status']})")
    if count is not None and base.get("progress") not in (None, count):
        differs.append(f"progress {base.get('progress')} (LCARS {count})")
    if differs:
        list_hub.log(conn, season["id"], service, "readback_differs", "; ".join(differs))
        reviews.open_review(
            conn, "season", season["id"], "list_readback_differs", service,
            f"{service} holds {'; '.join(differs)} after LCARS wrote its value",
            ["acknowledge"], {"season_id": season["id"], "service": service},
            show_id=season["show_id"])


def push(conn, season_id: str, *, status: bool = True, progress: bool = True,
         services=("anilist", "mal"), guard: bool = False) -> None:
    """Writes one level's status and/or progress to its AniList/MAL entries.
    `guard`: this write propagates an outside change (R4.10) — an entry someone
    edited on that list since LCARS last looked is not overwritten but held."""
    season = _season(conn, season_id)
    if not pushable(conn, season):
        return
    cfg = config.get_current()
    ids = list_ids(conn, season_id)
    shared = is_shared_entry(conn, season)
    if shared:
        status = False  # several seasons, one entry: its status is the list's to say
    count = level_progress(conn, season) if progress else None
    for service in services:
        ext = ids.get(service)
        if ext is None:
            continue
        try:
            if guard and not external_writes.capturing() and list_hub.outside_edit_pending(
                    conn, service, ext):
                list_hub.defer(conn, season_id, service, ext)
                continue
            fields = _fields(service, season, count, status)
            if fields:
                _save(conn, cfg, service, ext, fields)
                _read_back(conn, cfg, season, service, ext, count, check_status=not shared)
        except (anilist_client.AniListError, mal_client.MALError) as e:
            pending_review.open_or_extend(
                conn, "season", season_id, f"{service}_push", service, None, str(e)
            )


def push_progress_for_show(conn, show_id: str) -> None:
    """After watches: every level of the show whose progress moved since the
    list last agreed is pushed (both lists)."""
    for season in conn.execute(
        "SELECT * FROM season WHERE show_id = ?", (show_id,)
    ).fetchall():
        if not pushable(conn, season):
            continue
        if not is_group_representative(conn, season):
            continue  # one write per shared entry, by the level that speaks for it
        count = level_progress(conn, season)
        for service, ext in list_ids(conn, season["id"]).items():
            base = list_baseline.get(conn, service, ext) or {}
            if base.get("lcars_progress") != count:
                push(conn, season["id"], status=False, services=(service,))


def delete_if_auto_skipped(conn, season_id: str, old_status: str | None) -> None:
    """R2.10: a season LCARS added itself as planned, now skipped → deleted
    from AniList/MAL. One you set yourself (`status_set_manually`) never is."""
    season = _season(conn, season_id)
    if (season is None or season["status"] != "skipped" or old_status != "planned"
            or season["status_set_manually"]):
        return
    cfg = config.get_current()
    ids = list_ids(conn, season_id)
    try:
        if ids.get("anilist") and cfg.anilist_access_token:
            entry_id = anilist_client.fetch_my_list_entry_id(cfg.anilist_access_token,
                                                             ids["anilist"])
            if entry_id is not None:
                anilist_client.delete_media_list_entry(cfg.anilist_access_token, entry_id,
                                                       anilist_id=ids["anilist"])
            if not external_writes.capturing():  # a captured delete hasn't happened (9.0)
                conn.execute("DELETE FROM list_baseline WHERE service = 'anilist'"
                             " AND external_id = ?", (ids["anilist"],))
        if ids.get("mal") and cfg.mal_access_token:
            mal_client.delete_my_list_status(cfg.mal_access_token, ids["mal"])
            if not external_writes.capturing():
                conn.execute("DELETE FROM list_baseline WHERE service = 'mal'"
                             " AND external_id = ?", (ids["mal"],))
    except (anilist_client.AniListError, mal_client.MALError) as e:
        pending_review.open_or_extend(
            conn, "season", season_id, "list_delete", "lcars", None, str(e)
        )
