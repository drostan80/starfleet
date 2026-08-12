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
"AniList/MAL drift detection" bullet (still deliberately unscheduled;
this doesn't replace or preempt that design work, which still needs
its own schema-review-first pass before anything ongoing gets built).
This is a real, reusable mutation — not a throwaway script — but
deliberately NOT wired into Ops's automatic loop (matches
`backfillFileAvailability`/`auditLocalFiles`/`backfillUntrackedShows`'s
own "on-demand, deliberately triggered" precedent) until/unless the
later design work decides it should recur.

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

from lcars import anilist_client, config, ids, util

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
    best-effort integration in this codebase gets, not an error."""
    cfg = config.get_current()
    result = {
        "seasons_checked": 0,
        "not_matched_on_anilist": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
    }
    if not cfg.anilist_access_token:
        return result

    my_list = anilist_client.fetch_my_anime_list(cfg.anilist_access_token)
    by_anilist_id = {entry["anilist_id"]: entry for entry in my_list}

    seasons = conn.execute(
        "SELECT id, show_id, season_number, anilist_id FROM season WHERE anilist_id IS NOT NULL"
    ).fetchall()

    now = util.now_utc_iso()
    # show_id -> (highest season_number seen so far, that season's own
    # AniList status) — applied to show.status once every season's been
    # walked, so a show with multiple linked seasons doesn't get its
    # status flip-flopped mid-loop by whichever season happens first.
    status_candidate_by_show: dict[str, tuple[int, str]] = {}

    for season in seasons:
        entry = by_anilist_id.get(season["anilist_id"])
        if entry is None:
            result["not_matched_on_anilist"] += 1
            continue
        result["seasons_checked"] += 1

        show_id = season["show_id"]
        season_number = season["season_number"]
        current_best = status_candidate_by_show.get(show_id)
        if current_best is None or season_number > current_best[0]:
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
