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

from lcars import anilist_client, config, ids, mal_client, pending_review, util

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


def _push_status_onward(conn, service: str, show_id: str, lcars_status: str) -> None:
    """Push a just-reconciled status change to the OTHER service — every
    one of the show's seasons linked there. Best-effort, exactly like
    resolvers.py's forward push: a failure opens a pending_review rather
    than aborting the reconcile (LCARS is already correct; the push is
    the mirror). No-op when that service isn't authenticated."""
    cfg = config.get_current()
    if service == "anilist":
        token, remote_status = cfg.anilist_access_token, _STATUS_TO_ANILIST.get(lcars_status)
    else:
        token, remote_status = cfg.mal_access_token, _STATUS_TO_MAL.get(lcars_status)
    if not token or remote_status is None:
        return
    # S3: read from season_external_id (the table _apply_remote_list now reads too)
    # rather than the legacy season.{anilist_id,mal_id} columns — both are kept
    # in sync by the S2 dual-write, but reading from one source avoids drift.
    seasons = conn.execute(
        "SELECT s.id, CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season_external_id sei JOIN season s ON s.id = sei.season_id"
        " WHERE s.show_id = ? AND sei.service = ?",
        (show_id, service),
    ).fetchall()
    for season in seasons:
        try:
            if service == "anilist":
                anilist_client.save_media_list_entry(token, season["ext_id"], status=remote_status)
            else:
                mal_client.update_my_list_status(token, season["ext_id"], status=remote_status)
        except (anilist_client.AniListError, mal_client.MALError) as e:
            pending_review.open_or_extend(
                conn, "season", season["id"], f"{service}_push", service, None, str(e)
            )


def _push_progress_onward(conn, service: str, season_id: str) -> None:
    """Push a just-reconciled episode-progress change (this season's new
    high-water mark) to the OTHER service. Best-effort, same shape as
    `_push_status_onward`."""
    cfg = config.get_current()
    # S3: read ext_id from season_external_id (same source as _apply_remote_list).
    season = conn.execute(
        "SELECT s.id, s.show_id, s.season_number,"
        " CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season s LEFT JOIN season_external_id sei"
        " ON sei.season_id = s.id AND sei.service = ?"
        " WHERE s.id = ?",
        (service, season_id),
    ).fetchone()
    if season is None or season["ext_id"] is None:
        return
    token = cfg.anilist_access_token if service == "anilist" else cfg.mal_access_token
    if not token:
        return
    progress = _season_progress(conn, season["show_id"], season["season_number"])
    try:
        if service == "anilist":
            anilist_client.save_media_list_entry(token, season["ext_id"], progress=progress)
        else:
            mal_client.update_my_list_status(
                token, season["ext_id"], num_watched_episodes=progress
            )
    except (anilist_client.AniListError, mal_client.MALError) as e:
        pending_review.open_or_extend(
            conn, "season", season["id"], f"{service}_push", service, None, str(e)
        )


def _apply_remote_list(conn, *, entries_by_ext_id, service, source, now):
    """The shared reconcile core (2026-08-26 — extracted from
    `reconcile_watch_progress` so the MAL reverse-sync reuses the exact
    same hardening rather than a drift-prone second copy). `service` is
    `"anilist"` or `"mal"` — used to join `season_external_id`
    (S3: replaces the old `id_key` f-string column lookup on `season`).
    `entries_by_ext_id` maps an external id to
    `{"lcars_status": <LCARS status or None>, "progress": int}` (the
    caller normalizes each service's own status/progress vocabulary to
    these before calling). `source` is the `changed_by`/pending_review
    label. Applies, in order: duplicate-id exclusion (a remote id claimed
    by >1 season — flagged, skipped), true-highest-season status
    derivation, the unaired-episode guard (never mark/complete an episode
    with a real future air date), and the per-episode progress backfill.

    Returns `(changed_status, changed_progress_season_ids)` — `changed_
    status` maps each show whose status actually changed to its new LCARS
    status, `changed_progress_season_ids` is the set of season ids that
    got episodes backfilled — so the caller can push exactly those
    changes onward to the other service (never a per-season-walked push,
    same "only write real changes to a live account" rule the Trakt
    import's docstring spells out)."""
    stats = {
        "seasons_checked": 0,
        "not_matched_remotely": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_id_conflicts": 0,
    }
    # S3: read from season_external_id rather than season.{anilist_id,mal_id}.
    # The legacy columns stay live (dual-write from S2 keeps them in sync),
    # but the mapping table is the authoritative read source from here on —
    # it will also express the coarse-source / multi-fine-season case once S4
    # introduces real subdivision, which the column can't represent.
    seasons = conn.execute(
        "SELECT s.id, s.show_id, s.season_number,"
        " CAST(sei.external_id AS INTEGER) AS ext_id"
        " FROM season_external_id sei JOIN season s ON s.id = sei.season_id"
        " WHERE sei.service = ?",
        (service,),
    ).fetchall()

    # Duplicate external id claimed by more than one season — excluded
    # from this whole run and flagged, since its watch state can't be
    # trusted to belong to either show (watch_reconcile.py Fix 1).
    #
    # Subdivision suppression (2026-09-03): shows that share the same
    # TVDB ID are season subdivisions of the same Sonarr series (e.g.
    # Dr. STONE S3 split into New World Part 1 / Part 2).  An AniList/
    # MAL ID claimed by both the parent and a subdivision is expected,
    # not a conflict — suppress the review and keep processing.
    tvdb_by_show: dict[str, str] = {}
    for row in conn.execute(
        "SELECT show_id, external_id FROM show_external_id WHERE service = 'tvdb'"
    ).fetchall():
        tvdb_by_show[row["show_id"]] = row["external_id"]

    seasons_by_ext_id: dict[int, list] = {}
    for season in seasons:
        seasons_by_ext_id.setdefault(season["ext_id"], []).append(season)
    conflicted_season_ids: set[str] = set()
    for ext_id, dupes in seasons_by_ext_id.items():
        if len(dupes) <= 1:
            continue
        show_ids = {d["show_id"] for d in dupes}
        # Suppress when every show in the conflict shares a TVDB ID —
        # they're subdivisions of the same Sonarr series.
        tvdb_ids = {tvdb_by_show.get(sid) for sid in show_ids} - {None}
        if len(tvdb_ids) == 1:
            continue
        for season in dupes:
            conflicted_season_ids.add(season["id"])
            other_shows = [s for s in show_ids if s != season["show_id"]]
            pending_review.open_or_extend(
                conn,
                "season",
                season["id"],
                f"{service}_id_conflict",
                source,
                None,
                f"{service}_id {ext_id} also claimed by show(s): {other_shows}",
            )
    stats["ambiguous_id_conflicts"] = len(conflicted_season_ids)

    # A show's true highest linked season, so a status update is only ever
    # sourced from it, never a stale lower season standing in (Fix 2).
    highest_season_number_by_show: dict[str, int] = {}
    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        show_id = season["show_id"]
        current = highest_season_number_by_show.get(show_id)
        if current is None or season["season_number"] > current:
            highest_season_number_by_show[show_id] = season["season_number"]

    status_candidate_by_show: dict[str, tuple[str, str]] = {}  # show_id -> (season_id, status)
    changed_progress_season_ids: set[str] = set()

    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        entry = entries_by_ext_id.get(season["ext_id"])
        if entry is None:
            stats["not_matched_remotely"] += 1
            continue
        stats["seasons_checked"] += 1

        show_id = season["show_id"]
        season_number = season["season_number"]

        # Never mark/complete an episode with a real future air date —
        # the remote's own progress/status can be ahead of reality (Fix 3,
        # the Bookworm incident). Same guard, both services.
        unaired_episodes = {
            row["episode"]
            for row in conn.execute(
                "SELECT episode FROM episode"
                " WHERE show_id = ? AND season = ? AND air_date_utc IS NOT NULL"
                "   AND air_date_utc > ?",
                (show_id, season_number, now),
            ).fetchall()
        }

        if season_number == highest_season_number_by_show.get(show_id):
            lcars_status = entry["lcars_status"]
            if lcars_status == "completed" and unaired_episodes:
                lcars_status = "watching"
            if lcars_status is not None:
                status_candidate_by_show[show_id] = (season["id"], lcars_status)

        progress = entry["progress"] or 0
        if progress <= 0:
            continue
        unwatched = conn.execute(
            "SELECT episode FROM episode"
            " WHERE show_id = ? AND season = ? AND episode <= ? AND state = 'unwatched'",
            (show_id, season_number, progress),
        ).fetchall()
        unwatched = [row for row in unwatched if row["episode"] not in unaired_episodes]
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

    # 2026-09-20 fix: write the SEASON's own status (legitimate — mirrors
    # what setSeasonStatus already does manually), never show.status
    # directly. show.status derivation has a single authority
    # (_recompute_show_status/_compute_show_status, resolvers.py) used by
    # every other status-writing path in this codebase; a second, parallel
    # show-level writer here is exactly what caused a real, live
    # multi-week oscillation between anilist_reconcile and mal_reconcile —
    # each blindly copying the remote's current season status onto
    # show.status with no "real unwatched aired episodes exist" guard.
    # Deferred import: resolvers.py imports this module at load time.
    from lcars import resolvers

    changed_status: dict[str, str] = {}
    for show_id, (season_id, new_status) in status_candidate_by_show.items():
        season_row = conn.execute(
            "SELECT status FROM season WHERE id = ?", (season_id,)
        ).fetchone()
        if season_row is not None and season_row["status"] != new_status:
            conn.execute(
                "UPDATE season SET status = ?, updated_at = ? WHERE id = ?",
                (new_status, now, season_id),
            )
        before = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        resolvers._recompute_show_status(conn, show_id, source)
        after = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if before is not None and after is not None and before["status"] != after["status"]:
            stats["shows_status_updated"] += 1
            changed_status[show_id] = after["status"]

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
    for show_id, new_status in changed_status.items():
        _push_status_onward(conn, "mal", show_id, new_status)
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
