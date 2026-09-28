"""Status engine — every season/show status rule in one place (PLAN-CODE phase 4).

RULEBOOK §2.2/§2.4. A *level* is a season row: a TVDB season, a part (an
AniList/MAL cour under it), a special run, or an individual season. Its
episodes are those whose absolute number falls in its spans (P1); a level
without spans yet falls back to its TVDB season's episodes.

- R2.13  show status = its last non-skipped TVDB season's status; every
         season skipped → skipped.
- R2.13a a status picked on the show applies to that last season.
- R2.14  planned + an episode watched → watching (the only automatic path).
- R2.15  every episode of a level watched → completed, also when paused or
         dropped; a level set completed → all its episodes watched (R2.7),
         unaired included, after a warning (R3.4).
- R2.16  a new season: after completed/watching/planned → planned; after
         paused/dropped/skipped → skipped. Setting a season paused/dropped/
         skipped re-skips later seasons auto-added as planned, and those you
         set planned after a warning (Q-J4).
- R2.18  a TVDB season's status cascades to its parts (a completed part stays
         completed); the season's status follows its parts.
- R2.19  earlier seasons found late → skipped.

Functions write `season.status` through `season_status_log` and return an
`Effects` record; the caller pushes (AniList/MAL/Sonarr stay in resolvers).
A rule needing your confirmation raises `NeedsConfirmation` (phase 4.3).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from lcars import ids, season_status_log, util

AUTO = "status_rules"
STOP_FOLLOWING = ("paused", "dropped", "skipped")


class NeedsConfirmation(Exception):
    """A change the user must confirm first (R3.4, R2.16/Q-J4)."""


@dataclass
class Effects:
    seasons: list[tuple[str, str | None, str]] = field(default_factory=list)  # id, old, new
    show: tuple[str, str] | None = None  # old, new
    watched: list[tuple[int, int]] = field(default_factory=list)  # (season, episode)

    def merge(self, other: Effects) -> Effects:
        self.seasons += other.seasons
        self.show = other.show or self.show
        self.watched += other.watched
        return self


def _season(conn, season_id: str):
    return conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()


def _set(conn, season, status: str, changed_by: str, manual: bool, fx: Effects) -> None:
    old = season["status"]
    if season_status_log.set_status(conn, season["id"], status, changed_by):
        fx.seasons.append((season["id"], old, status))
    conn.execute(
        "UPDATE season SET status_set_manually = ? WHERE id = ?", (1 if manual else 0, season["id"])
    )


def level_episodes(conn, season) -> list:
    """The episodes of one level (P1: by its spans)."""
    if season["show_id"] is None:
        return []
    spans = conn.execute(
        "SELECT abs_from, abs_to FROM season_span WHERE season_id = ?", (season["id"],)
    ).fetchall()
    cols = "id, season, episode, state, air_date_utc"
    if spans:
        where = " OR ".join("absolute_number BETWEEN ? AND ?" for _ in spans)
        return conn.execute(
            f"SELECT {cols} FROM episode WHERE show_id = ? AND ({where})",
            (season["show_id"], *[v for s in spans for v in (s[0], s[1])]),
        ).fetchall()
    if season["kind"] == "tvdb_season" and season["season_number"]:
        return conn.execute(
            f"SELECT {cols} FROM episode WHERE show_id = ? AND season = ?",
            (season["show_id"], season["season_number"]),
        ).fetchall()
    return []


def _tvdb_seasons(conn, show_id: str) -> list:
    return conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND season_number > 0"
        " ORDER BY season_number",
        (show_id,),
    ).fetchall()


def last_season(conn, show_id: str):
    """The last TVDB season that isn't skipped (R2.13)."""
    for season in reversed(_tvdb_seasons(conn, show_id)):
        if season["status"] != "skipped":
            return season
    return None


# ── R2.10: skipped = not followed ──────────────────────────────────────


def followed_sql(alias: str = "episode") -> str:
    """SQL condition: the episode's TVDB season isn't skipped (R2.10) — for
    lists (calendar, next-up, backlog), availability and air-date updates."""
    return (
        f"NOT EXISTS (SELECT 1 FROM season z WHERE z.show_id = {alias}.show_id"
        f" AND z.kind = 'tvdb_season' AND z.season_number = {alias}.season"
        " AND z.status = 'skipped')"
    )


def episode_followed(conn, episode_id: str) -> bool:
    return conn.execute(
        f"SELECT 1 FROM episode WHERE id = ? AND {followed_sql()}", (episode_id,)
    ).fetchone() is not None


# ── R2.16 / R2.19: a season LCARS creates on its own ────────────────────


def new_season_status(conn, show_id: str, season_number: int) -> str:
    seasons = _tvdb_seasons(conn, show_id)
    if any(s["season_number"] > season_number for s in seasons):
        return "skipped"  # R2.19: found after a later one is tracked
    previous = [s for s in seasons if s["season_number"] < season_number]
    if previous and previous[-1]["status"] in STOP_FOLLOWING:
        return "skipped"
    return "planned"


# ── R2.13: show status ─────────────────────────────────────────────────


def derive_show_status(conn, show_id: str) -> str | None:
    seasons = _tvdb_seasons(conn, show_id)
    if not seasons:
        return None
    last = last_season(conn, show_id)
    if last is None:
        return "skipped"
    return last["status"]  # a season without a status (§2.2 gap) changes nothing


def recompute_show(conn, show_id: str, changed_by: str) -> Effects:
    """Write the derived show status (R2.13, R2.17). Untracked shows (the skip
    list) keep theirs."""
    fx = Effects()
    show = conn.execute("SELECT status, tracked FROM show WHERE id = ?", (show_id,)).fetchone()
    if show is None or not show["tracked"]:
        return fx
    new = derive_show_status(conn, show_id)
    if new is None or new == show["status"]:
        return fx
    now = util.now_utc_iso()
    conn.execute("UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (new, now, show_id))
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (ids.generate_id(conn, "c"), show_id, show["status"], new, now, changed_by),
    )
    fx.show = (show["status"], new)
    return fx


# ── R2.14 / R2.15 / R2.18: after episodes change ───────────────────────


def _parent_from_parts(conn, parent, changed_by: str, fx: Effects) -> None:
    parts = conn.execute(
        "SELECT status FROM season WHERE parent_id = ? AND kind = 'part'", (parent["id"],)
    ).fetchall()
    statuses = {p["status"] or "planned" for p in parts}
    if not statuses:
        return
    if len(statuses) == 1:
        new = statuses.pop()
    elif "watching" in statuses or ("completed" in statuses and statuses & {"planned"}):
        new = "watching"  # R2.18: part 1 completed + part 2 planned → watching
    else:
        return  # a mix the rulebook doesn't decide: left as it is
    _set(conn, parent, new, changed_by, False, fx)


def after_episodes_changed(conn, show_id: str, changed_by: str = AUTO) -> Effects:
    """R2.14, R2.15 on every level of the show, then R2.18 and R2.13."""
    fx = Effects()
    levels = conn.execute(
        "SELECT * FROM season WHERE show_id = ? ORDER BY kind = 'tvdb_season'", (show_id,)
    ).fetchall()  # parts and specials first, TVDB seasons after (R2.18)
    for level in levels:
        if level["status"] == "skipped":
            continue
        eps = level_episodes(conn, level)
        if not eps:
            continue
        watched = [e for e in eps if e["state"] == "watched"]
        if len(watched) == len(eps):
            if level["status"] != "completed":
                _set(conn, level, "completed", changed_by, False, fx)
        elif watched and (level["status"] or "planned") == "planned":
            _set(conn, level, "watching", changed_by, False, fx)
    for parent in conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season'"
        " AND id IN (SELECT parent_id FROM season WHERE kind = 'part')",
        (show_id,),
    ).fetchall():
        _parent_from_parts(conn, _season(conn, parent["id"]), changed_by, fx)
    return fx.merge(recompute_show(conn, show_id, changed_by))


# ── Setting a status ───────────────────────────────────────────────────


def _mark_watched(conn, show_id: str, eps, fx: Effects) -> None:
    now = util.now_utc_iso()
    for e in eps:
        if e["state"] == "watched":
            continue
        conn.execute(
            "INSERT INTO watch_event (id, show_id, season, episode, watched_at, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (ids.generate_id(conn, "w"), show_id, e["season"], e["episode"], now, now),
        )
        conn.execute(
            "UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?", (now, e["id"])
        )
        fx.watched.append((e["season"], e["episode"]))


def set_level_status(
    conn, season_id: str, status: str | None, changed_by: str, *,
    confirmed: bool = False, manual: bool = True,
) -> Effects:
    """A status set on one level (you, or AniList/MAL on your behalf)."""
    season = _season(conn, season_id)
    fx = Effects()
    if season is None:
        return fx
    show_id = season["show_id"]
    now = util.now_utc_iso()

    if status == "completed":
        eps = level_episodes(conn, season)
        unaired = [e for e in eps if e["state"] != "watched"
                   and (e["air_date_utc"] is None or e["air_date_utc"] > now)]
        if unaired and not confirmed:
            raise NeedsConfirmation(
                f"setting this season completed will mark {len(unaired)} unaired episode(s)"
                " watched — pass confirmed: true to proceed"
            )
    later_planned_by_you = []
    if status in STOP_FOLLOWING and season["kind"] == "tvdb_season" and show_id:
        later = [s for s in _tvdb_seasons(conn, show_id)
                 if s["season_number"] > season["season_number"] and s["status"] == "planned"]
        later_planned_by_you = [s for s in later if s["status_set_manually"]]
        if later_planned_by_you and not confirmed:
            raise NeedsConfirmation(
                f"this show has {len(later_planned_by_you)} later season(s) you set planned"
                " — skip all of them? pass confirmed: true to proceed"
            )

    _set(conn, season, status, changed_by, manual, fx)
    if status == "completed":
        _mark_watched(conn, show_id, level_episodes(conn, season), fx)  # R2.7
    # R2.18: cascade to parts, a completed part stays completed.
    for part in conn.execute(
        "SELECT * FROM season WHERE parent_id = ? AND kind = 'part'", (season_id,)
    ).fetchall():
        if part["status"] == "completed" and status != "completed":
            continue
        _set(conn, part, status, changed_by, manual, fx)
        if status == "completed":
            _mark_watched(conn, show_id, level_episodes(conn, part), fx)
    # R2.16: later seasons stop being followed.
    if status in STOP_FOLLOWING and season["kind"] == "tvdb_season" and show_id:
        for later in _tvdb_seasons(conn, show_id):
            if later["season_number"] > season["season_number"] and later["status"] == "planned":
                _set(conn, later, "skipped", AUTO, False, fx)
    if show_id:
        fx.merge(after_episodes_changed(conn, show_id, changed_by))
    return fx


def set_show_status(
    conn, show_id: str, status: str, changed_by: str, *, confirmed: bool = False
) -> Effects:
    """R2.13a: a status picked on the show applies to its last non-skipped
    season; the show is then derived. A show with no season takes it as is."""
    last = last_season(conn, show_id)
    if last is not None:
        return set_level_status(conn, last["id"], status, changed_by, confirmed=confirmed)
    fx = Effects()
    if not _tvdb_seasons(conn, show_id):
        show = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if show is not None and show["status"] != status:
            now = util.now_utc_iso()
            conn.execute(
                "UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (status, now, show_id)
            )
            conn.execute(
                "INSERT INTO status_change"
                " (id, show_id, previous_status, new_status, changed_at, changed_by)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ids.generate_id(conn, "c"), show_id, show["status"], status, now, changed_by),
            )
            fx.show = (show["status"], status)
        return fx
    # Every season skipped: the picked status goes on the last one (R2.13a).
    return set_level_status(
        conn, _tvdb_seasons(conn, show_id)[-1]["id"], status, changed_by, confirmed=confirmed
    )
