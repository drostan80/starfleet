"""Episode rows from TVDB's own list (RULEBOOK R1.2e, PLAN-DATA 9.1 decision 5).

Sonarr is only a TVDB proxy: a tracked show that Sonarr doesn't follow (finished
shows, the Trakt import) has no episode rows until they are read straight from
TVDB. Insert-only — a row that already exists is never touched; Memory Alpha
sets the absolute number afterwards (R1.2c), so `absolute_number` stays empty
and TVDB's own goes to `tvdb_absolute` (a mapping). The date-only air date is
returned to the caller rather than written, so the finer sources (TVmaze's
air time) can fill first and TVDB only takes what is still empty.
"""

from __future__ import annotations

from lcars import ids, util


def insert_missing(conn, show_id: str, episodes: list[dict]) -> tuple[int, dict]:
    """Insert the TVDB `episodes` (`TvdbClient.series_episodes`) that the show
    doesn't have yet, attached to their TVDB season where its row exists.
    Returns (rows inserted, {(season, episode): aired 'YYYY-MM-DD'} for them)."""
    have = {(r[0], r[1]) for r in conn.execute(
        "SELECT season, episode FROM episode WHERE show_id = ?", (show_id,))}
    now = util.now_utc_iso()
    inserted = 0
    dates: dict = {}
    for ep in episodes:
        key = (ep["season"], ep["episode"])
        if key in have:
            continue
        have.add(key)
        conn.execute(
            "INSERT INTO episode (id, show_id, season, episode, kind, tvdb_absolute,"
            " runtime_minutes, title, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "e"), show_id, ep["season"], ep["episode"],
             "special" if ep["season"] == 0 else "regular", ep.get("absolute"),
             ep.get("runtime"), ep.get("title"), now, now))
        inserted += 1
        if ep.get("aired"):
            dates[key] = ep["aired"]
    conn.execute(
        "UPDATE episode SET season_id = (SELECT s.id FROM season s WHERE s.show_id ="
        " episode.show_id AND s.kind = 'tvdb_season' AND s.season_number = episode.season)"
        " WHERE show_id = ? AND season_id IS NULL AND season > 0", (show_id,))
    return inserted, dates


def fill_air_dates(conn, show_id: str, dates: dict) -> int:
    """TVDB's date-only air date, only where the episode still has none."""
    filled = 0
    for (season, episode), aired in dates.items():
        filled += conn.execute(
            "UPDATE episode SET air_date_utc = ?, air_date_source = 'tvdb'"
            " WHERE show_id = ? AND season = ? AND episode = ? AND air_date_utc IS NULL",
            (aired + "T00:00:00Z", show_id, season, episode)).rowcount
    return filled
