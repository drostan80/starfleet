"""B.15 — one-time-triggered AniList → LCARS watch reconciliation.

Live-caught 2026-08-12, not speculative: `episode.state`/`watch_event`
in LCARS had silently diverged from what the user actually watched, for
an unknown number of shows — the pre-existing silent-bridge-no-op bug
(`~/repos/data` commits `744c6ff`/`8b9abab`/`6d1baef`, same day) meant a
real mark-watched in Data could write to AniList directly (§6.8's own
permanent exception, unaffected) while silently never reaching LCARS at
all — no queued entry left behind to retry, so nothing could
self-heal. Confirmed live: three real shows checked, zero `watch_event`
rows for any of them despite the user reporting having watched every
released episode. `show.status` can diverge the same way, independent
of watched-state (a show that resumed airing after being marked
`completed` has nothing that flips it back).

Explicitly scoped narrow, per the user's own direction (2026-08-12):
"LCARS must be correct... fix this" — not the full generalized,
recurring reverse-sync architecture from `BUILD_PLAN.md`'s own parked
"AniList/MAL drift detection" bullet. This function itself is still
deliberately NOT wired into Ops's automatic loop (matches
`backfillFileAvailability`/`auditLocalFiles`/`backfillUntrackedShows`'s
own "on-demand, deliberately triggered" precedent) — but B.5.3
(2026-08-13, below) is the "later design work" this note used to defer
to: `poll_anilist_activity` is a real recurring trigger for exactly
this function, built once AniList's own activity feed was confirmed
live to be a viable, cheap enough signal for "did anything change."

Scope, deliberately narrow:
  - `show.status` and per-episode `episode.state`/`watch_event` only.
    Score is untouched — not what broke, not what was reported.
  - AniList wins outright, no `pending_review` gate — the entire point
    of this pass is "AniList is known-correct, LCARS is known-wrong,
    make LCARS match" for a confirmed, real divergence, not a routine
    two-way sync where either side could legitimately be right.
  - `SKIPPED` episodes are never touched — a deliberate prior choice
    (§5.2: "state=SKIPPED still satisfies show-level completion"),
    not the same thing as "never watched." AniList's own `progress`
    has no skip concept, so only `state = 'unwatched'` is backfilled.
  - A show spanning multiple seasons/AniList entries gets its
    show-level `status` from whichever season has the *highest*
    `season_number` — the most recently-airing cour is the one a
    real-world "what's my status on this show" question means.
  - Every synthesized `watch_event.watched_at` uses this reconciliation
    run's own timestamp — AniList's `progress` carries no per-episode
    date, and BUILD_PLAN.md's own parked note already settled this as
    the honest fallback rather than fabricating a real-looking one.
"""

from lcars import anilist_client, config, ids, list_baseline, mal_client, pending_review, util

# B.5.3, 2026-08-13 — see the module docstring's own note above for how
# this relates to reconcile_watch_progress. Design checked live against
# the user's real AniList account before being written, not assumed:
# the activity feed genuinely is visible for this account (5000+ real
# entries, confirmed non-private), the exact query shape used below
# (`Page.pageInfo` + `activities` in one selection, `sort: ID`) executes
# correctly, and real cadence is bursty in minutes during an active
# watching session, hours apart otherwise — informing why this is a
# cheap poll-and-trigger, not something that needs its own detailed
# per-activity apply logic (reconcile_watch_progress already is that,
# and reused as-is).
#
# Deliberately built against the existing flat AniList throttle
# (anilist_client._graphql_request's own `_ANILIST_SECONDS_PER_CALL`
# gate, shipped 2026-08-13 for the rate-limit incident), not yet B.5.2's
# still-unbuilt priority queue — a quiet poll costs 1 call (the
# viewer id is cached after this process's first real fetch, see
# _cached_viewer_id below — an earlier draft called fetch_viewer_id
# unconditionally on every poll, caught and fixed before this shipped,
# not after), a triggered one costs 2 (+ reconcile_watch_progress's own
# fetch_my_anime_list) ≈ 4.2s of blocking sleep inside this resolver on
# LCARS's single request-handling thread. Acceptable at the low cadence
# this is manually tested at; a known, temporary condition B.5.2
# resolves once real numbers from running this decide its tiers — not
# rediscovered as a bug later.
#
# Deliberately NOT wired into Ops's automatic loop in this same change
# — per the user's own build-order call (2026-08-13): gather real
# numbers (how often activity actually appears, whether a triggered
# reconcile ever fires spuriously, whether pagination ever triggers)
# by calling the `pollAnilistActivity` mutation by hand against
# production first, then decide B.5.2's tiers and Ops's own cadence
# from that, not from a guess.

_ANILIST_TO_STATUS = {
    "CURRENT": "watching",
    "PLANNING": "planned",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
    # REPEATING has no LCARS-side equivalent (same gap _STATUS_TO_ANILIST
    # in resolvers.py already documents in the opposite direction) — a
    # rewatch is still actively being watched, so it maps to watching
    # rather than being dropped/skipped by this reconciliation.
    "REPEATING": "watching",
}

# MAL's own list_status vocabulary -> LCARS status (2026-08-26, MAL
# bidirectional sync). Exhaustive against MAL's five list states; MAL has
# no rewatch state in this field (is_rewatching is separate, never read).
_MAL_TO_STATUS = {
    "watching": "watching",
    "plan_to_watch": "planned",
    "on_hold": "paused",
    "completed": "completed",
    "dropped": "dropped",
}

# LCARS status -> each service's own vocabulary, for the *onward* push a
# reconcile makes to the OTHER service (hub model: an AniList change lands
# in LCARS and is pushed to MAL; a MAL change lands and is pushed to
# AniList). Same maps resolvers.py's forward push uses, duplicated here
# rather than imported to avoid a circular import (resolvers imports this
# module) — small and stable enough that drift isn't a real risk.
_STATUS_TO_ANILIST = {
    "watching": "CURRENT",
    "planned": "PLANNING",
    "paused": "PAUSED",
    "completed": "COMPLETED",
    "dropped": "DROPPED",
}
_STATUS_TO_MAL = {
    "watching": "watching",
    "planned": "plan_to_watch",
    "paused": "on_hold",
    "completed": "completed",
    "dropped": "dropped",
}


def _season_progress(conn, show_id: str, season_number: int) -> int:
    """This season's episode-watched high-water mark — the same value
    `resolvers._compute_season_episode_progress` computes (MAX watched/
    skipped episode number), duplicated here to keep the onward push free
    of a resolvers import (which would be circular)."""
    row = conn.execute(
        "SELECT MAX(episode) AS furthest FROM episode"
        " WHERE show_id = ? AND season = ? AND state IN ('watched', 'skipped')",
        (show_id, season_number),
    ).fetchone()
    return row["furthest"] or 0


def _push_progress_onward(conn, service: str, season_id: str) -> None:
    """Push a just-reconciled episode-progress change (this season's new
    high-water mark) to a service. Best-effort, same shape as
    `_push_season_status_onward`."""
    cfg = config.get_current()
    # S3: read ext_id from season_external_id (same source as _apply_remote_list).
    season = conn.execute(
        "SELECT s.id, s.show_id, s.season_number, s.list_sync,"
        " CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season s LEFT JOIN season_external_id sei"
        " ON sei.season_id = s.id AND sei.service = ?"
        " WHERE s.id = ?",
        (service, season_id),
    ).fetchone()
    if season is None or season["ext_id"] is None or not season["list_sync"]:
        return
    token = cfg.anilist_access_token if service == "anilist" else cfg.mal_access_token
    if not token:
        return
    progress = _season_progress(conn, season["show_id"], season["season_number"])
    try:
        if service == "anilist":
            list_baseline.anilist_save(conn, token, season["ext_id"], progress=progress)
        else:
            list_baseline.mal_save(
                conn, token, season["ext_id"], num_watched_episodes=progress
            )
    except (anilist_client.AniListError, mal_client.MALError) as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], f"{service}_push", service, None, str(e)
        )


def _push_season_status_onward(conn, service: str, season_id: str, lcars_status: str) -> None:
    """Push one season's status to `service` (the hub's onward push, or a
    retry of an LCARS change that list hasn't taken). Best-effort; records
    the baseline via list_baseline on success, opens a review on failure."""
    cfg = config.get_current()
    if service == "anilist":
        token, remote_status = cfg.anilist_access_token, _STATUS_TO_ANILIST.get(lcars_status)
    else:
        token, remote_status = cfg.mal_access_token, _STATUS_TO_MAL.get(lcars_status)
    if not token or remote_status is None:
        return
    season = conn.execute(
        "SELECT s.id, s.list_sync, CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season s JOIN season_external_id sei"
        " ON sei.season_id = s.id AND sei.service = ?"
        " WHERE s.id = ?",
        (service, season_id),
    ).fetchone()
    if season is None or not season["list_sync"]:
        return
    try:
        if service == "anilist":
            list_baseline.anilist_save(conn, token, season["ext_id"], status=remote_status)
        else:
            list_baseline.mal_save(conn, token, season["ext_id"], status=remote_status)
    except (anilist_client.AniListError, mal_client.MALError) as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], f"{service}_push", service, None, str(e)
        )


def _apply_remote_list(conn, *, entries_by_ext_id, service, source, now):
    """The shared reconcile core for AniList and MAL, as a hub with LCARS
    the source of truth (user rule, 2026-09-25; see list_baseline.py).

    Per list entry, against the last state LCARS and that list agreed on
    (`list_baseline`):
      - the list's status changed  -> LCARS takes it; the caller pushes it
                                      on to the other list
      - only LCARS's status changed -> pushed to this list again (a push
                                      that failed or lagged is retried,
                                      never overwritten by the old value)
      - progress only ever moves LCARS forward from a list, except when
        LCARS itself went back (an unwatch) — then LCARS is pushed.

    Only seasons of tracked shows take part: untracked relation stubs
    reused real seasons' ids and turned every one into a conflict or a
    loop (Haruhi, 09-25). An id claimed by more than one tracked season is
    excluded and flagged — never pushed from either.

    First run for a service (no baseline yet): where LCARS and the list
    agree the baseline is recorded; where they don't, LCARS wins and is
    pushed. The unaired guard (Bookworm, 08-19) is kept: a list saying
    completed while an episode hasn't aired gives `watching`, and no
    unaired episode is ever marked watched.

    Returns `(stats, changed_status, changed_progress_season_ids)` —
    `changed_status` maps season_id -> the status LCARS took from the list,
    for the caller's onward push to the other service."""
    stats = {
        "seasons_checked": 0,
        "not_matched_remotely": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_id_conflicts": 0,
        "lcars_pushed": 0,
    }
    seeded = list_baseline.is_seeded(conn, service)
    seasons = conn.execute(
        "SELECT s.id, s.show_id, s.season_number, s.list_sync,"
        " COALESCE(s.status, sh.status) AS status,"
        " CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season_external_id sei JOIN season s ON s.id = sei.season_id"
        " JOIN show sh ON sh.id = s.show_id"
        " WHERE sei.service = ? AND sh.tracked = 1",
        (service,),
    ).fetchall()

    seasons_by_ext_id: dict[int, list] = {}
    for season in seasons:
        seasons_by_ext_id.setdefault(season["ext_id"], []).append(season)
    conflicted_season_ids: set[str] = set()
    for ext_id, dupes in seasons_by_ext_id.items():
        if len(dupes) <= 1:
            continue
        for season in dupes:
            conflicted_season_ids.add(season["id"])
            others = [d["show_id"] + " S" + str(d["season_number"])
                      for d in dupes if d["id"] != season["id"]]
            pending_review.open_or_extend(
                conn, "season", season["id"], f"{service}_id_conflict", source, None,
                f"{service}_id {ext_id} also claimed by: {others}",
            )
    stats["ambiguous_id_conflicts"] = len(conflicted_season_ids)

    changed_status: dict[str, str] = {}
    changed_progress_season_ids: set[str] = set()
    touched_shows: set[str] = set()

    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        ext_id = season["ext_id"]
        entry = entries_by_ext_id.get(ext_id)
        if entry is None:
            if season["list_sync"]:
                stats["not_matched_remotely"] += 1
            continue
        if not season["list_sync"]:
            # The user put this season on the list themselves: mirror it.
            conn.execute("UPDATE season SET list_sync = 1 WHERE id = ?", (season["id"],))
        stats["seasons_checked"] += 1

        show_id = season["show_id"]
        season_number = season["season_number"]
        base = list_baseline.get(conn, service, ext_id) or {"status": None, "progress": None}

        unaired_episodes = {
            row["episode"]
            for row in conn.execute(
                "SELECT episode FROM episode"
                " WHERE show_id = ? AND season = ? AND air_date_utc IS NOT NULL"
                "   AND air_date_utc > ?",
                (show_id, season_number, now),
            ).fetchall()
        }
        # A season that hasn't started (no episode with a past air date)
        # has nothing aired: its undated episodes — Sonarr "TBA"
        # placeholders — are unaired too. Kaiju No. 8 S3E1 was marked
        # watched this way on 09-23 from a stray progress=1 on AniList.
        # Undated episodes of a season that has started stay eligible
        # (old TV seasons often lack dates for real, watched episodes).
        # Kept separate from `unaired_episodes`: it only blocks the
        # progress backfill, never the completed -> watching downgrade
        # (an old, fully undated OVA season is genuinely completed).
        unstarted_undated: set[int] = set()
        season_started = conn.execute(
            "SELECT 1 FROM episode WHERE show_id = ? AND season = ?"
            " AND air_date_utc IS NOT NULL AND air_date_utc <= ? LIMIT 1",
            (show_id, season_number, now),
        ).fetchone()
        if season_started is None:
            unstarted_undated = {
                row["episode"]
                for row in conn.execute(
                    "SELECT episode FROM episode"
                    " WHERE show_id = ? AND season = ? AND air_date_utc IS NULL",
                    (show_id, season_number),
                ).fetchall()
            }

        # --- status -----------------------------------------------------
        remote_status = entry["lcars_status"]
        lcars_status = season["status"]  # the season's own, else its show's
        if remote_status is not None:
            effective = remote_status
            if effective == "completed" and unaired_episodes:
                effective = "watching"
            if seeded and remote_status != base["status"]:
                # Edited on the list: LCARS takes it.
                if lcars_status != effective:
                    conn.execute(
                        "UPDATE season SET status = ?, updated_at = ? WHERE id = ?",
                        (effective, now, season["id"]),
                    )
                    changed_status[season["id"]] = effective
                    touched_shows.add(show_id)
                list_baseline.record(conn, service, ext_id, status=remote_status)
            elif lcars_status is not None and lcars_status != remote_status and (
                not seeded or lcars_status != base["status"]
            ):
                # LCARS changed (or first run and they disagree): LCARS wins.
                _push_season_status_onward(conn, service, season["id"], lcars_status)
                stats["lcars_pushed"] += 1
            elif lcars_status == remote_status and base["status"] != remote_status:
                list_baseline.record(conn, service, ext_id, status=remote_status)

        # --- progress ---------------------------------------------------
        progress = entry["progress"] or 0
        lcars_progress = _season_progress(conn, show_id, season_number)
        if (
            seeded and progress == base["progress"]
            and base["progress"] is not None and lcars_progress < base["progress"]
        ):
            # LCARS went back (an unwatch) and the list hasn't taken it.
            _push_progress_onward(conn, service, season["id"])
            stats["lcars_pushed"] += 1
            continue
        if base["progress"] != progress:
            list_baseline.record(conn, service, ext_id, progress=progress)
        if progress <= 0:
            continue
        unwatched = conn.execute(
            "SELECT episode FROM episode"
            " WHERE show_id = ? AND season = ? AND episode <= ? AND state = 'unwatched'",
            (show_id, season_number, progress),
        ).fetchall()
        unwatched = [
            row for row in unwatched
            if row["episode"] not in unaired_episodes
            and row["episode"] not in unstarted_undated
        ]
        for ep_row in unwatched:
            conn.execute(
                "INSERT INTO watch_event"
                " (id, show_id, season, episode, watched_at, platform, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    ids.generate_id(conn, "w"),
                    show_id,
                    season_number,
                    ep_row["episode"],
                    now,
                    None,
                    now,
                ),
            )
            conn.execute(
                "UPDATE episode SET state = 'watched', updated_at = ?"
                " WHERE show_id = ? AND season = ? AND episode = ?",
                (now, show_id, season_number, ep_row["episode"]),
            )
            stats["episodes_backfilled"] += 1
            changed_progress_season_ids.add(season["id"])

    if not seeded:
        list_baseline.mark_seeded(conn, service)

    # show.status has a single authority (_recompute_show_status); the
    # season statuses above are its input. _skip_push: the onward push is
    # per season, by the caller — never back to the list it came from.
    # Deferred import: resolvers.py imports this module at load time.
    from lcars import resolvers

    for show_id in touched_shows:
        before = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        resolvers._recompute_show_status(conn, show_id, source, _skip_push=True)
        after = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if before is not None and after is not None and before["status"] != after["status"]:
            stats["shows_status_updated"] += 1

    return stats, changed_status, changed_progress_season_ids


def reconcile_watch_progress(conn) -> dict:
    """Fetches the real AniList list once, then reconciles every LCARS
    `season` row with a known `anilist_id` against it. No AniList
    credential configured is the same clean no-op every other
    best-effort integration in this codebase gets, not an error.

    **Two hardening fixes, 2026-08-13, both found and reproduced (not
    theoretical) during the first real triggered reconcile via B.5.3**:

    1. **Two seasons sharing one `anilist_id`** (real, and not caught
       anywhere else: `setSeasonMapping` has no check against reusing
       an id already assigned elsewhere, and B.14's own duplicate-show
       detection watches a different signal entirely — tvdb-linked-
       no-anilist vs. anilist-linked-no-tvdb — so it never even sees a
       pair that already both have an anilist link). Reproduced: both
       shows silently received the same watch state from one real
       AniList entry. Doesn't self-heal — every future run keeps
       reapplying the same cross-contamination. Fixed: detected up
       front, excluded from this run entirely, flagged via
       `pending_review` (a human decision, same as every other
       genuine ambiguity in this codebase — §3 principle 1).
    2. **A stale/unresolved link on a show's actual highest season
       silently falling back to an older, lower season's status.**
       Reproduced: a show genuinely `watching` (its real current
       season not yet matched) got wrongly reverted to `completed` by
       an unrelated, already-finished earlier season. Unlike #1, this
       usually *does* self-heal (the common cause is simply "linked,
       but not yet added to the user's own AniList list yet" — nothing
       wrong, just not there yet), so this is deliberately NOT flagged
       via `pending_review` — that would fire on every ordinary
       freshly-linked season and be pure noise. Fixed instead by
       simply not guessing: a show's status is only ever set from its
       true highest linked season number, never a lower one standing
       in for it.

    **Third hardening fix, 2026-08-19**, same "reproduced live, not
    theoretical" bar as the two above — Ascendance of a Bookworm
    ("Adopted Daughter of an Archduke"): this pass marked six not-yet-
    aired episodes watched and flipped a still-weekly-releasing show to
    `completed`, straight from AniList's raw `progress`/`status`,
    overwriting a correct manual fix from hours earlier. The module
    docstring's "AniList wins outright" was never meant to license
    fabricating watch history for an episode that doesn't exist yet —
    AniList's own progress/status can be ahead of reality. Both the
    per-episode backfill and the show-level completion status now
    exclude any episode this show's own already-known (Sonarr/TVDB)
    `air_date_utc` places in the future — self-heals the moment that
    episode actually airs, same as fix #2's own self-healing case.
    """
    cfg = config.get_current()
    result = {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_anilist_id_conflicts": 0,
    }
    if not cfg.anilist_access_token:
        return result

    my_list = anilist_client.fetch_my_anime_list(cfg.anilist_access_token)
    entries = {
        entry["anilist_id"]: {
            "lcars_status": _ANILIST_TO_STATUS.get(entry["status"]),
            "progress": entry.get("progress") or 0,
        }
        for entry in my_list
    }
    now = util.now_utc_iso()
    stats, changed_status, changed_progress_season_ids = _apply_remote_list(
        conn, entries_by_ext_id=entries, service="anilist", source="anilist_reconcile", now=now
    )
    # Hub model: an AniList change has now landed in LCARS -> mirror it
    # onward to MAL (never back to AniList). Only the seasons/shows that
    # actually changed, never a per-season-walked push.
    for season_id, new_status in changed_status.items():
        _push_season_status_onward(conn, "mal", season_id, new_status)
    for season_id in changed_progress_season_ids:
        _push_progress_onward(conn, "mal", season_id)
    conn.commit()

    result["seasons_checked"] = stats["seasons_checked"]
    result["not_matched_on_anilist"] = stats["not_matched_remotely"]
    result["shows_status_updated"] = stats["shows_status_updated"]
    result["episodes_backfilled"] = stats["episodes_backfilled"]
    result["ambiguous_anilist_id_conflicts"] = stats["ambiguous_id_conflicts"]
    return result


# B.5.3 — cached after the first real fetch within this process's
# lifetime: a viewer id is a stable property of a fixed account (there's
# only ever one AniList account here), not something that needs
# re-fetching on every poll. Found before it shipped, not after: an
# earlier draft called fetch_viewer_id unconditionally at the top of
# poll_anilist_activity, which would have doubled every poll's real call
# count (2 calls quiet, 4 triggered, not 1/2) and directly distorted the
# real-cadence numbers this whole build-order reversal exists to gather.
_viewer_id_cache: int | None = None


def _cached_viewer_id(token: str) -> int:
    global _viewer_id_cache
    if _viewer_id_cache is None:
        _viewer_id_cache = anilist_client.fetch_viewer_id(token)
    return _viewer_id_cache


def _get_activity_checkpoint(conn) -> tuple[int, int] | None:
    row = conn.execute(
        "SELECT last_activity_id, last_activity_created_at"
        " FROM anilist_activity_checkpoint WHERE id = 1"
    ).fetchone()
    if row is None or row["last_activity_id"] is None:
        return None
    return (row["last_activity_id"], row["last_activity_created_at"])


def _set_activity_checkpoint(conn, last_activity_id: int, last_activity_created_at: int) -> None:
    conn.execute(
        "INSERT INTO anilist_activity_checkpoint"
        " (id, last_activity_id, last_activity_created_at, updated_at)"
        " VALUES (1, ?, ?, ?)"
        " ON CONFLICT (id) DO UPDATE SET last_activity_id = excluded.last_activity_id,"
        "   last_activity_created_at = excluded.last_activity_created_at,"
        "   updated_at = excluded.updated_at",
        (last_activity_id, last_activity_created_at, util.now_utc_iso()),
    )


def poll_anilist_activity(conn) -> dict:
    """B.5.3 — see the module-level comment above this section for the
    full design rationale. Polls AniList's own activity feed (one cheap
    call, almost always) since the last checkpoint; only when it shows
    something genuinely new does this go on to actually run
    `reconcile_watch_progress` (a second, heavier call) — the activity
    feed is purely a "did anything change" signal, never itself the
    thing applying a correction.

    Returns `{"activities_seen": int, "reconcile_result": dict | None}`
    — `reconcile_result` is `reconcile_watch_progress`'s own result
    dict, present only when it actually ran (i.e. `activities_seen >
    0`), `None` on every quiet poll rather than a dict of zeros, so a
    caller can tell "nothing new" apart from "checked and genuinely
    found nothing to fix" at a glance.

    No AniList credential configured is the same clean no-op every
    other best-effort integration in this codebase gets."""
    result: dict = {"activities_seen": 0, "reconcile_result": None}
    cfg = config.get_current()
    if not cfg.anilist_access_token:
        return result

    viewer_id = _cached_viewer_id(cfg.anilist_access_token)
    checkpoint = _get_activity_checkpoint(conn)
    if checkpoint is None:
        # First-ever call — seed straight to "everything up to right
        # now" rather than walking this account's entire activity
        # history (5000+ deep on a real account, confirmed live) and
        # triggering a reconcile for all of it. Same seed-and-skip
        # shape availability.py's own _poll_sonarr established.
        marker = anilist_client.fetch_latest_activity_marker(cfg.anilist_access_token, viewer_id)
        seed_id, seed_created_at = marker if marker is not None else (0, 0)
        _set_activity_checkpoint(conn, seed_id, seed_created_at)
        conn.commit()
        return result

    since_id, since_created_at = checkpoint
    new_activities = anilist_client.fetch_activity_feed(
        cfg.anilist_access_token, viewer_id, since_id, since_created_at
    )
    result["activities_seen"] = len(new_activities)
    if not new_activities:
        return result

    result["reconcile_result"] = reconcile_watch_progress(conn)
    newest_id = max(a["id"] for a in new_activities)
    newest_created_at = max(a["created_at"] for a in new_activities)
    _set_activity_checkpoint(conn, newest_id, newest_created_at)
    conn.commit()
    return result
