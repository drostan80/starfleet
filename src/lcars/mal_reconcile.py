"""MAL → LCARS reverse sync (2026-08-26) — the MAL half of the
bidirectional mirror the user asked for: a change made on MyAnimeList
lands in LCARS, which then pushes it onward to AniList (LCARS is the
hub). The mirror image of `watch_reconcile.reconcile_watch_progress`
(AniList → LCARS → MAL), and it deliberately reuses that module's
`_apply_remote_list` — the same, separately-hardened core (duplicate-id
exclusion, true-highest-season status, the unaired-episode guard that
keeps a still-airing show from being force-completed) — rather than a
second copy that could drift from those fixes.

Scope, matching `watch_reconcile` exactly: **status + episode progress
only**. Score reverse-sync is not built in either direction (neither
MAL→LCARS nor AniList→LCARS reconciles score — see NEXT_UP); the forward
push of score to both services on a local `setScore` is unaffected.

**Why a stale mirror is safe for progress**: the shared apply only ever
marks `unwatched` episodes up to the remote's high-water mark — it never
un-watches. So a MAL list that lags LCARS can only push LCARS *forward*
on the shows where MAL is genuinely ahead, never roll one back; a MAL
that's behind is a plain no-op there. (Status is "remote wins" like the
AniList side — a genuinely stale MAL status could revert a fresher LCARS
one, then converge; in practice the user's external tool keeps MAL≈
AniList, and status changes are rare, so this stays a theoretical edge,
not an oscillation.)

**No activity-feed equivalent**: MAL has no "did anything change" feed
like AniList's, so this fetches the whole list and diffs on a cadence
(Ops's own loop) rather than being cheaply poll-triggered. **Token
expiry**: MAL access tokens are long-lived (~1 month) and the Ops
`refreshMalTokenIfDue` loop renews weekly-ish, so this poll finds a
valid token in practice; a `MALAuthError` here is handled as a
best-effort no-op (service_health failure recorded, next cycle retries
after the refresh loop has run) rather than raising.
"""

from lcars import config, mal_client, service_health, util, watch_reconcile


def reconcile_mal_progress(conn) -> dict:
    """Fetch the viewer's whole MAL list once, reconcile every LCARS
    `season` with a known `mal_id` against it (via the shared
    `watch_reconcile._apply_remote_list`), then mirror every change that
    actually landed onward to AniList. Best-effort throughout; a missing
    MAL credential is the same clean no-op every other integration gets."""
    result = {
        "seasons_checked": 0,
        "not_matched_on_mal": 0,
        "shows_status_updated": 0,
        "episodes_backfilled": 0,
        "ambiguous_mal_id_conflicts": 0,
    }
    cfg = config.get_current()
    if not cfg.mal_access_token:
        return result

    try:
        my_list = mal_client.fetch_my_list(cfg.mal_access_token)
    except mal_client.MALError as e:
        service_health.record_failure(conn, "mal", str(e))
        conn.commit()
        return result
    service_health.record_success(conn, "mal")

    entries = {
        entry["mal_id"]: {
            "lcars_status": watch_reconcile._MAL_TO_STATUS.get(entry["status"]),
            "progress": entry.get("num_watched_episodes") or 0,
        }
        for entry in my_list
    }
    now = util.now_utc_iso()
    stats, changed_status, changed_progress_season_ids = watch_reconcile._apply_remote_list(
        conn, entries_by_ext_id=entries, service="mal", source="mal_reconcile", now=now
    )
    # Hub model: a MAL change has now landed in LCARS -> mirror it onward
    # to AniList (never back to MAL). Only the seasons/shows that actually
    # changed, never a per-season-walked push.
    for season_id, new_status in changed_status.items():
        watch_reconcile._push_season_status_onward(conn, "anilist", season_id, new_status)
    for season_id in changed_progress_season_ids:
        watch_reconcile._push_progress_onward(conn, "anilist", season_id)
    conn.commit()

    result["seasons_checked"] = stats["seasons_checked"]
    result["not_matched_on_mal"] = stats["not_matched_remotely"]
    result["shows_status_updated"] = stats["shows_status_updated"]
    result["episodes_backfilled"] = stats["episodes_backfilled"]
    result["ambiguous_mal_id_conflicts"] = stats["ambiguous_id_conflicts"]
    return result
