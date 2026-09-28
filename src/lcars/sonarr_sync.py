"""LCARS → Sonarr, per season (PLAN-CODE phase 6) — RULEBOOK R5.5–R5.10.

A season status change drives Sonarr's monitoring for that TVDB season:
- planned / watching → the season's **future** episodes monitored (R2.12, R5.6),
  aired ones not;
- paused / dropped / skipped → that season **and every later one** unmonitored
  (R5.8); earlier seasons are untouched;
- completed → nothing (R5.7).
A part or mini sub-season acts on its TVDB season; specials and individual
seasons have none. Sonarr is only touched when the show is already there:
nothing is ever added to Sonarr from here (R5.9). Best effort: a failure opens a
review and never blocks the status change. Same for anime and TV (R5.10).
"""

from __future__ import annotations

from lcars import config, pending_review, service_health, sonarr_client, util

MONITOR = ("planned", "watching")
STOP = ("paused", "dropped", "skipped")


def _tvdb_season(conn, season_id: str) -> tuple[str | None, int | None]:
    row = conn.execute(
        "SELECT show_id, season_number, kind, parent_id FROM season WHERE id = ?", (season_id,)
    ).fetchone()
    if row is None or row[0] is None:
        return None, None
    if row[2] == "tvdb_season":
        return row[0], row[1]
    if row[3] is not None:  # a part / mini sub-season: its TVDB season
        parent = conn.execute(
            "SELECT season_number FROM season WHERE id = ? AND kind = 'tvdb_season'", (row[3],)
        ).fetchone()
        return row[0], parent[0] if parent else None
    return row[0], None


def apply(conn, changes: list[tuple[str, str | None, str]]) -> None:
    """`changes`: (season id, old status, new status), as the status engine
    reports them."""
    cfg = config.get_current()
    if not (cfg.sonarr_url and cfg.sonarr_api_key):
        return
    by_show: dict[str, list[tuple[int, str]]] = {}
    for season_id, _old, new in changes:
        if new not in MONITOR and new not in STOP:
            continue
        show_id, number = _tvdb_season(conn, season_id)
        if show_id and number and number > 0:
            by_show.setdefault(show_id, []).append((number, new))
    for show_id, actions in by_show.items():
        tvdb = conn.execute(
            "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'tvdb'",
            (show_id,),
        ).fetchone()
        if tvdb is None:
            continue
        try:
            _apply_show(cfg, int(tvdb[0]), actions)
            service_health.record_success(conn, "sonarr")
        except sonarr_client.SonarrError as e:
            service_health.record_failure(conn, "sonarr", str(e))
            pending_review.open_or_extend(
                conn, "show", show_id, "sonarr_monitor", "sonarr", None, str(e)
            )


def _apply_show(cfg, tvdb_id: int, actions: list[tuple[int, str]]) -> None:
    with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
        series = client.series_by_tvdb_id(tvdb_id)
        if series is None:
            return  # not in Sonarr: never added from here (R5.9)
        monitor, stop_from = set(), None
        for number, status in actions:
            if status in MONITOR:
                monitor.add(number)
            else:
                stop_from = number if stop_from is None else min(stop_from, number)
        # A season set planned/watching in the same change wins over the
        # "and every later one" of a stop (R5.3: earlier seasons skipped,
        # the latest planned, in one go).
        for entry in series.get("seasons", []):
            n = entry.get("seasonNumber")
            if n in monitor:
                entry["monitored"] = True
            elif stop_from is not None and n is not None and n >= stop_from:
                entry["monitored"] = False
        if monitor:
            series["monitored"] = True
        client.update_series(series)
        if monitor:
            now = util.now_utc_iso()
            eps = [e for e in client.episodes(series["id"]) if e.get("seasonNumber") in monitor]
            future = [e["id"] for e in eps if not e.get("airDateUtc") or e["airDateUtc"] > now]
            aired = [e["id"] for e in eps if e.get("airDateUtc") and e["airDateUtc"] <= now]
            if future:
                client.monitor_episodes(future, True)
            if aired:
                client.monitor_episodes(aired, False)  # future episodes only (R2.12)
