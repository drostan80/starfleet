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

from lcars import (
    anilist_client,
    config,
    external_writes,
    ids,
    list_baseline,
    list_hub,
    pending_review,
    reviews,
    season_status_log,
    util,
)

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


MAX_PUSHES_PER_RUN = 25
MAX_LOWER_PROGRESS = 2  # R4.10: a list's lower progress applies up to this many episodes


def _last_lcars_change(conn, season_id: str) -> str | None:
    row = conn.execute(
        "SELECT MAX(changed_at) FROM season_status_change WHERE season_id = ?", (season_id,)
    ).fetchone()
    return row[0] if row else None


def _apply_remote_status(conn, season, status, service, source, fx_all, unaired) -> bool:
    """Your change on a list, taken by LCARS through the status engine (R4.8):
    its cascades apply. A COMPLETED that would mark unaired episodes watched
    becomes a review instead (R4.8a). Returns whether LCARS took it."""
    from lcars import list_sync, reviews, status_rules

    other = "mal" if service == "anilist" else "anilist"
    if status == "completed" and unaired:
        reviews.open_review(
            conn, "season", season["id"], "remote_completed", source,
            f"{service} says completed, but {len(unaired)} episode(s) haven't aired",
            ["accept_completed", "revert_watching"],
            {"season_id": season["id"], "service": service},
            show_id=season["show_id"],
        )
        return False
    try:
        fx = status_rules.set_level_status(
            conn, season["id"], status, source, confirmed=(status == "completed"), manual=True
        )
    except status_rules.NeedsConfirmation as e:
        # Later seasons you set planned would be skipped: take the status
        # itself, ask about the rest (Q-J4's warning, as a review).
        season_status_log.set_status(conn, season["id"], status, source)
        conn.execute("UPDATE season SET status_set_manually = 1 WHERE id = ?", (season["id"],))
        fx = status_rules.Effects(seasons=[(season["id"], season["status"], status)])
        reviews.open_review(
            conn, "season", season["id"], "later_planned", source, str(e),
            ["skip_later", "keep_later"], {"season_id": season["id"], "status": status},
            show_id=season["show_id"],
        )
    for season_id, old, new in fx.seasons:
        if season_id == season["id"]:
            list_sync.push(conn, season_id, progress=False, services=(other,), guard=True)
        elif new == "skipped":
            list_sync.delete_if_auto_skipped(conn, season_id, old)
        else:
            list_sync.push(conn, season_id, progress=False)
    if fx.watched:
        list_sync.push_progress_for_show(conn, season["show_id"])
    fx_all.extend(fx.seasons)
    return True


def _apply_remote_list(conn, *, entries_by_ext_id, service, source, now):
    """The reconcile hub for one list (phase 7; RULEBOOK R4.2–R4.10), LCARS
    the source of truth, per level (AniList/MAL entries are season level).

    Per list entry, against the last state LCARS and that list agreed on
    (`list_baseline`):
      - the list changed, LCARS didn't → LCARS takes it through the status
        engine and pushes it on to the other list (R4.8);
      - both changed → the later change wins (R4.10: the list's `updatedAt`
        against LCARS's last status change for that level);
      - only LCARS changed → pushed to the list again (a failed or lagging
        push is retried; skipped is never pushed, R4.6);
      - a season LCARS mirrors that the list doesn't have → added (R4.4, R4.5).
    Progress moves LCARS forward over the level's own episodes (a cour's
    progress is the cour's), never marking an unaired one; an unwatch in
    LCARS is pushed back. First run for a list writes nothing: the list's
    values become the baseline. Pushes per run are capped.

    Returns `(stats, changed_status, changed_progress_season_ids)`."""
    from lcars import list_sync, sonarr_sync

    stats = {
        "episodes_unwatched": 0,
        "seasons_checked": 0,
        "not_matched_remotely": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_id_conflicts": 0,
        "lcars_pushed": 0,
        "added_remotely": 0,
    }
    seeded = list_baseline.is_seeded(conn, service)
    seasons = conn.execute(
        "SELECT s.*, CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season_external_id sei JOIN season s ON s.id = sei.season_id"
        " LEFT JOIN show sh ON sh.id = s.show_id"
        " WHERE sei.service = ? AND (sh.tracked = 1 OR s.show_id IS NULL)",
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
            others = [str(d["show_id"]) + " S" + str(d["season_number"])
                      for d in dupes if d["id"] != season["id"]]
            pending_review.open_or_extend(
                conn, "season", season["id"], f"{service}_id_conflict", source, None,
                f"{service}_id {ext_id} also claimed by: {others}",
            )
    stats["ambiguous_id_conflicts"] = len(conflicted_season_ids)

    changed_status: dict[str, str] = {}
    changed_progress_season_ids: set[str] = set()
    touched_shows: set[str] = set()
    fx_all: list = []
    show_before: dict[str, str | None] = {}
    pushes_left = MAX_PUSHES_PER_RUN

    intake = list_hub.intake_enabled()  # R4.10: nothing an outside list holds is taken until live
    capturing = external_writes.capturing()
    total_changed: set[str] = set()  # shows whose entry gave a new episode total (R2.15a)
    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        ext_id = season["ext_id"]
        entry = entries_by_ext_id.get(ext_id)
        base_row = list_baseline.get(conn, service, ext_id)
        if entry is None:
            stats["not_matched_remotely"] += 1
            if not seeded:
                continue
            if base_row is not None and (base_row["status"] is not None
                                         or base_row["progress"] is not None):
                # It was on the list and is gone: a review, never a silent re-add (R4.10).
                if intake:
                    list_hub.log(conn, season["id"], service, "entry_removed", str(ext_id))
                    reviews.open_review(
                        conn, "season", season["id"], "list_entry_removed", source,
                        f"the {service} entry {ext_id} is no longer on the list",
                        ["re_add", "stop_mirroring"],
                        {"season_id": season["id"], "service": service},
                        show_id=season["show_id"])
                continue
            if pushes_left > 0 and list_sync.pushable(conn, season):
                list_sync.push(conn, season["id"], services=(service,))  # R4.4/R4.5
                pushes_left -= 1
                stats["added_remotely"] += 1
            continue
        stats["seasons_checked"] += 1
        show_id = season["show_id"]
        # R2.15a: the entry's episode total is a source fact, kept whatever intake says.
        # AniList's wins; MAL's fills in only where AniList has none.
        total = entry.get("episodes")
        if (service == "anilist" or (season["anilist_id"] is None and total is not None)) \
                and total != season["episode_total"]:
            conn.execute("UPDATE season SET episode_total = ? WHERE id = ?", (total, season["id"]))
            if show_id:
                total_changed.add(show_id)
        base = base_row or {
            "status": None, "progress": None, "lcars_progress": None, "lcars_status": None,
            "remote_updated_at": None,
        }
        remote_updated = entry.get("updated_at")
        # R4.10: the service's update time equal to the one LCARS remembered = nothing
        # was edited there since (its own side effects are in the read-back); a locked
        # row (an outside change still settling) takes no further outside change.
        echo = bool(base["remote_updated_at"]) and remote_updated == base["remote_updated_at"]
        row_locked = list_hub.locked(conn, season["id"])
        take = intake and (not seeded or (not echo and not row_locked))
        if intake and seeded and row_locked and not echo:
            list_hub.log(conn, season["id"], service, "deferred",
                         f"edited on the list ({remote_updated}) while the row settles")
        pushed_here = False
        took_here = False
        level_eps = list_sync.level_episodes_ordered(conn, season) if show_id else []
        air = {r[0]: r[1] for r in conn.execute(
            "SELECT id, air_date_utc FROM episode WHERE show_id = ?", (show_id,))}
        started = any(air.get(e["id"]) and air[e["id"]] <= now for e in level_eps)
        # Dated in the future: never marked, and a remote COMPLETED over
        # them is a review (R4.8a). Undated in a season that hasn't started
        # only holds back the progress backfill (an old undated OVA can
        # still be completed).
        unaired = [e for e in level_eps if air.get(e["id"]) and air[e["id"]] > now]
        unaired_ids = {e["id"] for e in unaired} | {
            e["id"] for e in level_eps if not air.get(e["id"]) and not started
        }

        # --- status -----------------------------------------------------
        remote_status = entry["lcars_status"]
        lcars_status = season["status"]
        # What LCARS last pushed: a status the service then moved itself is not an
        # LCARS change, and is not pushed again (R4.10).
        lcars_pushed = base["lcars_status"] if base["lcars_status"] is not None else base["status"]
        if remote_status is not None:
            if not seeded:
                if intake:
                    # the seed: the list's value is what both are taken to agree on
                    list_baseline.record(conn, service, ext_id, status=remote_status,
                                         lcars_status=remote_status,
                                         remote_updated_at=remote_updated)
            elif remote_status != base["status"] and not echo:
                if not take:
                    pass  # deferred: judged once the row is open (or intake is on)
                else:
                    # No baseline yet: the list's value is the edit (steady state).
                    lcars_changed = (lcars_pushed is not None and lcars_status is not None
                                     and lcars_status != lcars_pushed)
                    last_lcars = _last_lcars_change(conn, season["id"])
                    remote_later = (remote_updated or "") > (last_lcars or "")
                    if lcars_status == remote_status:
                        list_baseline.record(conn, service, ext_id, status=remote_status,
                                             lcars_status=lcars_status)
                    elif lcars_changed and not remote_later:
                        # Both changed; LCARS's is later, or a tie (R4.10): LCARS goes out.
                        list_hub.log(conn, season["id"], service, "lcars_wins",
                                     f"status {lcars_status} over {remote_status}")
                        if pushes_left > 0 and list_sync.pushable(conn, season):
                            list_sync.push(conn, season["id"], progress=False,
                                           services=(service,))
                            pushes_left -= 1
                            stats["lcars_pushed"] += 1
                            pushed_here = True
                    else:
                        if show_id and show_id not in show_before:
                            show_before[show_id] = conn.execute(
                                "SELECT status FROM show WHERE id = ?", (show_id,)
                            ).fetchone()["status"]
                        if _apply_remote_status(conn, season, remote_status, service, source,
                                                fx_all, unaired):
                            changed_status[season["id"]] = remote_status
                            took_here = True
                            list_hub.log(conn, season["id"], service, "took_status",
                                         f"{lcars_status} -> {remote_status}")
                            if show_id:
                                touched_shows.add(show_id)
                        season_after = conn.execute(
                            "SELECT status FROM season WHERE id = ?", (season["id"],)).fetchone()
                        list_baseline.record(conn, service, ext_id, status=remote_status,
                                             lcars_status=season_after["status"])
            elif (lcars_status is not None and lcars_pushed is not None
                  and lcars_status != lcars_pushed):
                # Only LCARS changed: push it again (never skipped, R4.6).
                if pushes_left > 0 and list_sync.pushable(conn, season):
                    list_sync.push(conn, season["id"], progress=False, services=(service,))
                    pushes_left -= 1
                    stats["lcars_pushed"] += 1
                    pushed_here = True

        if not show_id:
            continue  # an individual season has no episodes yet (phase 8)

        # --- progress (the level's own episodes) ------------------------
        progress = entry["progress"] or 0
        season_now = conn.execute("SELECT * FROM season WHERE id = ?", (season["id"],)).fetchone()
        lcars_progress = list_sync.level_progress(conn, season_now)
        if (
            seeded and progress == base["progress"]
            and base["lcars_progress"] is not None
            and lcars_progress < base["lcars_progress"]
        ):
            # LCARS went back (an unwatch) and the list hasn't taken it.
            if pushes_left > 0:
                list_sync.push(conn, season["id"], status=False, services=(service,))
                pushes_left -= 1
                stats["lcars_pushed"] += 1
            continue
        # Only a change on the list is taken (as for statuses): progress the
        # list already had when LCARS and the list last agreed is not new —
        # it may be LCARS's own old push (PLAN-CODE 9.2, cutover guard).
        list_changed = base["progress"] is None or progress != base["progress"]
        if not take:
            list_changed = False
        lower_handled = False
        if progress > 0 and list_changed and progress < lcars_progress and seeded:
            # The list went below LCARS: LCARS's own later change stands (tie: LCARS);
            # else the list is right — up to 2 episodes unwatched, more is a review.
            lcars_moved = (base["lcars_progress"] is not None
                           and lcars_progress != base["lcars_progress"])
            remote_later = (remote_updated or "") > (
                list_hub.last_watch_change(conn, season_now) or "")
            lower_handled = True
            if lcars_moved and not remote_later:
                list_hub.log(conn, season["id"], service, "lcars_wins",
                             f"progress {lcars_progress} over {progress}")
                if pushes_left > 0:
                    list_sync.push(conn, season["id"], status=False, services=(service,))
                    pushes_left -= 1
                    stats["lcars_pushed"] += 1
                    pushed_here = True
            elif (lcars_progress - progress <= MAX_LOWER_PROGRESS
                  and season_now["status"] != "completed"):
                gone = list_hub.unwatch_last(conn, season_now, lcars_progress - progress)
                list_hub.log(conn, season["id"], service, "took_lower_progress",
                             f"{lcars_progress} -> {progress}")
                stats["episodes_unwatched"] += gone
                changed_progress_season_ids.add(season["id"])
                touched_shows.add(show_id)
                took_here = True
            else:
                reviews.open_review(
                    conn, "season", season["id"], "remote_progress_lower", source,
                    f"{service} says {progress}, LCARS has {lcars_progress} watched",
                    ["accept_lower", "keep_lcars"],
                    {"season_id": season["id"], "service": service, "progress": progress},
                    show_id=show_id)
                list_hub.log(conn, season["id"], service, "lower_progress_review",
                             f"{lcars_progress} vs {progress}")
                lower_handled = None  # nothing taken: the baseline keeps the old value
        if progress > 0 and list_changed and not lower_handled:
            if show_id not in show_before:
                show_before[show_id] = conn.execute(
                    "SELECT status FROM show WHERE id = ?", (show_id,)
                ).fetchone()["status"]
            for e in level_eps[:progress]:
                if e["state"] in ("watched", "skipped") or e["id"] in unaired_ids:
                    continue
                conn.execute(
                    "INSERT INTO watch_event"
                    " (id, show_id, season, episode, watched_at, platform, created_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (ids.generate_id(conn, "w"), show_id, e["season"], e["episode"], now,
                     source, now),
                )
                conn.execute(
                    "UPDATE episode SET state = 'watched', updated_at = ? WHERE id = ?",
                    (now, e["id"]),
                )
                stats["episodes_backfilled"] += 1
                changed_progress_season_ids.add(season["id"])
                touched_shows.add(show_id)
                took_here = True
            lcars_progress = list_sync.level_progress(conn, season_now)
        if lower_handled is None:
            continue  # a review is open: nothing recorded, the row isn't touched
        if not pushed_here and (base["progress"] != progress
                                or base["lcars_progress"] != lcars_progress):
            list_baseline.record(
                conn, service, ext_id, progress=progress, lcars_progress=lcars_progress
            )
        if took_here:
            # R4.10: an outside change has been taken — the row settles (every list
            # holds LCARS's decision) before another outside change is taken on it.
            list_hub.lock(conn, season["id"], source, remote_updated)
            list_hub.restamp(conn, season["id"], source, now, remote_updated)
        if take and not pushed_here and remote_updated != base["remote_updated_at"] \
                and not capturing:
            list_baseline.record(conn, service, ext_id, remote_updated_at=remote_updated)

    if not seeded:
        list_baseline.mark_seeded(conn, service)

    # Deferred import: resolvers.py imports this module at load time.
    from lcars import resolvers

    if intake:  # a new total may confirm a fully watched level: LCARS derives, and pushes
        for show_id in total_changed - touched_shows:
            resolvers._recompute_show_status(conn, show_id, source)
    for show_id in touched_shows:
        fx = resolvers._recompute_show_status(conn, show_id, source, _skip_push=True)
        fx_all.extend(fx.seasons)
        after = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if after is not None and show_before.get(show_id) != after["status"]:
            stats["shows_status_updated"] += 1
    sonarr_sync.apply(conn, fx_all)  # R5.6-R5.8 follow the seasons LCARS took

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

    list_hub.reset_snapshots()
    my_list = anilist_client.fetch_my_anime_list(cfg.anilist_access_token)
    entries = {
        entry["anilist_id"]: {
            "lcars_status": _ANILIST_TO_STATUS.get(entry["status"]),
            "progress": entry.get("progress") or 0,
            "updated_at": entry.get("updated_at"),
            "episodes": entry.get("episodes"),
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
    from lcars import list_sync

    for season_id in changed_progress_season_ids:
        list_sync.push(conn, season_id, status=False, services=("mal",), guard=True)
    list_hub.settle_locked(conn)
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
