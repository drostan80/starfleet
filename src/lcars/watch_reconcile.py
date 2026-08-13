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

from lcars import anilist_client, config, ids, pending_review, util

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
    by_anilist_id = {entry["anilist_id"]: entry for entry in my_list}

    seasons = conn.execute(
        "SELECT id, show_id, season_number, anilist_id FROM season WHERE anilist_id IS NOT NULL"
    ).fetchall()

    now = util.now_utc_iso()

    # Fix 1 — detect any anilist_id claimed by more than one season
    # before touching anything. Excluded from this entire run (not
    # just skipped for status — also skipped for episode-progress
    # backfill below, since that data can't be trusted to belong to
    # either show either) and flagged for a human via pending_review,
    # one entry per conflicted season so it surfaces per-show in the
    # existing review screen.
    seasons_by_anilist_id: dict[int, list] = {}
    for season in seasons:
        seasons_by_anilist_id.setdefault(season["anilist_id"], []).append(season)
    conflicted_season_ids: set[str] = set()
    for anilist_id, dupes in seasons_by_anilist_id.items():
        if len(dupes) <= 1:
            continue
        show_ids = [d["show_id"] for d in dupes]
        for season in dupes:
            conflicted_season_ids.add(season["id"])
            other_shows = [s for s in show_ids if s != season["show_id"]]
            pending_review.open_or_extend(
                conn,
                "season",
                season["id"],
                "anilist_id_conflict",
                "anilist_reconcile",
                None,
                f"anilist_id {anilist_id} also claimed by show(s): {other_shows}",
            )
    result["ambiguous_anilist_id_conflicts"] = len(conflicted_season_ids)

    # Fix 2 — a show's true highest linked season number, computed up
    # front from every season this show has an anilist_id for
    # (conflicted ones excluded — already unusable), regardless of
    # whether that highest season actually resolves against the real
    # list. Used below to refuse a status update sourced from anything
    # other than that true highest season.
    highest_season_number_by_show: dict[str, int] = {}
    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        show_id = season["show_id"]
        current = highest_season_number_by_show.get(show_id)
        if current is None or season["season_number"] > current:
            highest_season_number_by_show[show_id] = season["season_number"]

    # show_id -> (season_number, that season's own AniList status) —
    # only ever set from a show's true highest linked season (Fix 2
    # above), applied to show.status once every season's been walked.
    status_candidate_by_show: dict[str, tuple[int, str]] = {}

    for season in seasons:
        if season["id"] in conflicted_season_ids:
            continue
        entry = by_anilist_id.get(season["anilist_id"])
        if entry is None:
            result["not_matched_on_anilist"] += 1
            continue
        result["seasons_checked"] += 1

        show_id = season["show_id"]
        season_number = season["season_number"]
        if season_number == highest_season_number_by_show.get(show_id):
            status_candidate_by_show[show_id] = (season_number, entry["status"])

        progress = entry["progress"] or 0
        if progress <= 0:
            continue
        unwatched = conn.execute(
            "SELECT episode FROM episode"
            " WHERE show_id = ? AND season = ? AND episode <= ? AND state = 'unwatched'",
            (show_id, season_number, progress),
        ).fetchall()
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
            result["episodes_backfilled"] += 1

    for show_id, (_, anilist_status) in status_candidate_by_show.items():
        new_status = _ANILIST_TO_STATUS.get(anilist_status)
        if new_status is None:
            continue  # an AniList status with no LCARS equivalent (see REPEATING note above)
        row = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
        if row is None or row["status"] == new_status:
            continue
        conn.execute(
            "UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, show_id)
        )
        conn.execute(
            "INSERT INTO status_change"
            " (id, show_id, previous_status, new_status, changed_at, changed_by)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                ids.generate_id(conn, "c"),
                show_id,
                row["status"],
                new_status,
                now,
                "anilist_reconcile",
            ),
        )
        result["shows_status_updated"] += 1

    conn.commit()
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
