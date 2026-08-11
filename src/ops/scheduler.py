"""The polling loops themselves — B.1 (daily metadata refresh), B.2
(Fribb reconciliation, two tiers: weekly + monthly), B.3 (file
availability, one adaptive-cadence loop), B.5 (animeschedule.net RSS
sweep, riding B.1's own hourly tick), B.7 (show_service_presence
refresh, split across two cadences — the `local` rollup rides the
hourly tick, Sonarr/Radarr catalog matching rides B.2's monthly one),
B.8b (episode_movie_link automatic tmdb_match derivation, also riding
the hourly tick — pure internal SQL, no external HTTP call at all),
B.10 (proactive MAL refresh-token renewal, self-gating on its own
7-day checkpoint so it too rides the hourly tick cheaply), B.11e
(ongoing untracked-show sweep, also riding the hourly tick — a handful
of global calls, not per-show), B.14 (cross-service show-duplicate
merge sweep, riding the monthly tier alongside B.7's own catalog
matching — same real N×M cost).

Each `run_*_once()` is the real, testable unit — one full sweep,
exercised directly by a test with no infinite loop or real sleep
involved. `run_forever()` composes them into the actual long-running
process `ops run` (cli.py) awaits.
"""

import asyncio
import logging

from ops.lcars_client import LcarsClient, LcarsError

logger = logging.getLogger("ops.scheduler")


async def run_once(client: LcarsClient) -> int:
    """§6.7/B.1 — one sweep: fetch every show the daily pass owes a
    refresh, refresh each. A single show's failure (network blip, a real
    LCARS-side error) is logged and skipped, never aborts the rest of
    the batch — the same best-effort, one-branch-failing-doesn't-block-
    the-others philosophy `metadata.fetch_and_populate()` (A.8) already
    applies show-internally, applied here one level up, across shows.
    Returns the count actually refreshed, for the caller to log."""
    shows = await client.due_for_metadata_refresh()
    refreshed = 0
    for show in shows:
        try:
            await client.refresh_show_metadata(show["id"])
            refreshed += 1
        except LcarsError:
            logger.exception(
                "refreshShowMetadata failed for %s (%s)", show["id"], show.get("displayTitle")
            )
    return refreshed


async def run_weekly_once(client: LcarsClient) -> int:
    """§5.5/B.2 — the weekly tier: every season of a watching+actively-
    airing show not Fribb-reconciled in the last 7 days
    (dueForSeasonReconciliation, already filtered server-side). Same
    per-item error isolation as run_once() above."""
    seasons = await client.due_for_season_reconciliation()
    reconciled = 0
    for season in seasons:
        try:
            await client.reconcile_season_mapping(season["show"]["id"], season["seasonNumber"])
            reconciled += 1
        except LcarsError:
            logger.exception(
                "reconcileSeasonMapping failed for season %s (show %s, season %s)",
                season["id"],
                season["show"]["id"],
                season["seasonNumber"],
            )
    return reconciled


async def run_season_reconciliation_once(client: LcarsClient) -> int:
    """§5.5/B.2 — the monthly tier's own season-reconciliation half:
    every season of every show, unconditional — no due-query for this
    tier, Ops's own monthly timer is the correctness boundary
    (SCOPE.md §5.5's B.2 note). Same per-item error isolation as the
    other tiers. Renamed from run_monthly_once (B.7) now that a second,
    unrelated sweep (catalog service-presence) shares this same
    cadence — see run_monthly_once below, the actual unit the loop
    calls each tick."""
    seasons = await client.all_seasons()
    reconciled = 0
    for season in seasons:
        try:
            await client.reconcile_season_mapping(season["show"]["id"], season["seasonNumber"])
            reconciled += 1
        except LcarsError:
            logger.exception(
                "reconcileSeasonMapping failed for season %s (show %s, season %s)",
                season["id"],
                season["show"]["id"],
                season["seasonNumber"],
            )
    return reconciled


async def run_catalog_presence_once(client: LcarsClient) -> int:
    """§5.4/§6.7, B.7 — the Sonarr/Radarr catalog-matching sweep
    (pollCatalogServicePresence): no per-item loop, the mutation itself
    covers every tracked show of the matching mediaShape in one call —
    same shape run_availability_once/run_animeschedule_once already
    use. Rides B.2's own monthly cadence (run_monthly_once below), not
    the hourly tick — real N×M cost, confirmed with the user
    (service_presence.py's own module docstring has the full
    reasoning)."""
    result = await client.poll_catalog_service_presence()
    return result["showsUpdated"]


async def run_show_merge_once(client: LcarsClient) -> int:
    """§5.0, B.14 — the cross-service show-duplicate merge sweep
    (pollShowMerges): no per-item loop, the mutation itself covers the
    whole candidate pool in one call, same shape run_catalog_presence_
    once above already uses. Real N×M cost (fuzzy title match every
    tvdb-only show against every anilist-only anime show), confirmed
    with the user — rides this same monthly tier rather than the hourly
    one, not a fourth interval. Returns the merged count (not
    candidatesFound — a fresh recomputation every pass isn't a
    *change*, same "count real changes" convention every other tier
    already follows)."""
    result = await client.poll_show_merges()
    return result["merged"]


async def run_monthly_once(client: LcarsClient) -> int:
    """B.2's season-reconciliation tier, B.7's catalog
    service-presence sweep, and B.14's cross-service show-merge sweep
    share one loop/interval — same "no new interval unless a real
    technical constraint forces one" precedent B.1/B.4/B.5's own
    cadence decisions already established, applied here to the monthly
    tier instead of the hourly one. Returns the combined count, for the
    caller to log."""
    reconciled = await run_season_reconciliation_once(client)
    presence = await run_catalog_presence_once(client)
    merged = await run_show_merge_once(client)
    return reconciled + presence + merged


async def run_availability_once(client: LcarsClient) -> int:
    """§5.2/§6.7, B.3 — one global availability sweep
    (pollFileAvailability): no per-item loop here at all, unlike the
    other tiers — the mutation itself covers every tracked show in one
    call (LCARS's own checkpoint state, not Ops, is what keeps repeat
    calls cheap). Returns the combined episodes+shows-updated count."""
    result = await client.poll_file_availability()
    return result["episodesUpdated"] + result["showsUpdated"]


async def run_animeschedule_once(client: LcarsClient) -> int:
    """§6.7, B.5 — one global animeschedule.net RSS sweep
    (pollAnimeSchedule): same "no per-item loop, the mutation itself
    covers everything" shape as run_availability_once above. Returns
    the combined episodes-updated+flagged count."""
    result = await client.poll_anime_schedule()
    return result["episodesUpdated"] + result["flagged"]


async def run_local_presence_once(client: LcarsClient) -> int:
    """§5.4/§6.7, B.7 — the `local` pseudo-service rollup
    (pollLocalServicePresence): pure SQL aggregate, no external
    dependency, negligible cost — rides the hourly tick, unlike B.7's
    other half (run_catalog_presence_once, which rides the monthly one
    instead — service_presence.py's own module docstring has the cost
    reasoning)."""
    result = await client.poll_local_service_presence()
    return result["showsUpdated"]


async def run_episode_movie_link_reconciliation_once(client: LcarsClient) -> int:
    """§5.1/§6.7, B.8b — episode_movie_link's own automatic tmdb_match
    derivation (reconcileEpisodeMovieLinks): no per-item loop, the
    mutation itself covers every bonus_movie-kind episode in one call,
    same shape run_local_presence_once above already uses. No external
    HTTP call in the sweep at all, so it shares that same hourly tick
    rather than needing its own interval. Returns the combined
    matched+flagged+unmatched+availabilitySynced count."""
    result = await client.reconcile_episode_movie_links()
    return (
        result["matched"] + result["flagged"] + result["unmatched"] + result["availabilitySynced"]
    )


async def run_untracked_shows_once(client: LcarsClient) -> int:
    """§5.2, B.11e — the ongoing untracked-show sweep
    (pollUntrackedShows): no per-item loop, the mutation itself
    recomputes and reconciles the whole persisted list in one call, same
    shape run_animeschedule_once/run_local_presence_once above already
    use. Returns the combined new+resolved count — "found" (the total
    current list) isn't a *change*, so it's not part of this count,
    matching the "count real changes" convention every other tier
    already follows."""
    result = await client.poll_untracked_shows()
    return result["newFindings"] + result["resolvedFindings"]


async def run_mal_token_refresh_once(client: LcarsClient) -> int:
    """§6.9, B.10 — the proactive weekly-ish MAL refresh-token renewal
    (refreshMalTokenIfDue): self-gating on LCARS's own internal 7-day
    checkpoint, so an outbound HTTP call to MAL only actually happens
    on the rare tick it's due — same "cheap no-op almost every call"
    shape run_local_presence_once/run_episode_movie_link_reconciliation_
    once already ride the hourly tick with. Returns 1 if a refresh
    genuinely happened this call, else 0 — same "count real changes"
    convention as every other tier, just boolean-shaped here since
    there's only ever one credential to refresh, not a list."""
    result = await client.refresh_mal_token_if_due()
    return 1 if result["refreshed"] else 0


async def run_daily_and_weekly_once(client: LcarsClient) -> int:
    """B.1's daily tier, B.2's weekly tier, B.5's animeschedule sweep,
    B.7's local-presence rollup, B.8b's episode_movie_link
    reconciliation, B.10's MAL token refresh check, and B.11e's
    untracked-show sweep share one loop/interval (run_forever's own
    docstring explains why the weekly tier doesn't need its own timer;
    B.5's own module docstring explains why animeschedule can't wait for
    a daily one; B.7's local rollup and B.8b's reconciliation are both
    negligible-cost, no-external-HTTP SQL; B.10's refresh check
    self-gates on its own 7-day checkpoint, so an hourly tick just means
    "checked cheaply, acted rarely"; B.11e's sweep is a handful of
    global calls (Sonarr/Radarr catalog listings, one AniList list
    fetch), not per-show, cheap enough to share too — no reason at all
    not to share this tick) — this is the single unit that loop
    actually calls each tick. Returns the combined count, for the
    caller to log."""
    daily = await run_once(client)
    weekly = await run_weekly_once(client)
    animeschedule = await run_animeschedule_once(client)
    local_presence = await run_local_presence_once(client)
    episode_movie_links = await run_episode_movie_link_reconciliation_once(client)
    mal_token_refresh = await run_mal_token_refresh_once(client)
    untracked_shows = await run_untracked_shows_once(client)
    return (
        daily
        + weekly
        + animeschedule
        + local_presence
        + episode_movie_links
        + mal_token_refresh
        + untracked_shows
    )


async def _loop(coro_fn, client: LcarsClient, interval_seconds: int, label: str) -> None:
    """Shared while-loop shape for both timers below — never returns
    under normal operation. Catches bare Exception, not just LcarsError
    — deliberately broad, same reasoning metadata._guarded (A.8) already
    uses for the identical "never let one bad response crash the
    long-running process" concern: LcarsClient._query already turns
    every failure mode it knows about into LcarsError, but Ops is an
    unattended daemon, not Data's TUI, so a genuinely unanticipated
    exception here should still be logged and retried next interval
    rather than kill the process."""
    while True:
        try:
            count = await coro_fn(client)
            logger.info("%s: processed %d item(s)", label, count)
        except Exception:
            logger.exception("%s: sweep failed, will retry next interval", label)
        await asyncio.sleep(interval_seconds)


async def _availability_loop(client: LcarsClient) -> None:
    """§5.2/§6.7, B.3 — the one loop with a genuinely dynamic interval,
    unlike every other tier's flat one: after each sweep, asks LCARS for
    the next interval (300s/900s/900s baseline 3600s, computed
    server-side from watching shows' own episode air dates — see
    recommendedAvailabilityPollIntervalSeconds's own docstring) rather
    than sleeping a fixed amount. Same broad-except/log-and-continue
    shape as _loop() above, for the same reason."""
    while True:
        try:
            count = await run_availability_once(client)
            logger.info("availability: processed %d item(s)", count)
        except Exception:
            logger.exception("availability: sweep failed, will retry")
        try:
            interval = await client.recommended_availability_poll_interval_seconds()
        except Exception:
            logger.exception("availability: interval check failed, falling back to 3600s")
            interval = 3600
        await asyncio.sleep(interval)


async def run_forever(
    client: LcarsClient, interval_seconds: int, monthly_interval_seconds: int
) -> None:
    """Three concurrent loops, not one shared cadence — B.2's own
    weekly tier is self-gating (dueForSeasonReconciliation only ever
    returns a season once it's genuinely 7+ days stale, regardless of
    how often it's checked), so it rides the same cadence as B.1's daily
    tier rather than needing its own interval. B.5's animeschedule sweep
    rides that identical tick too — no per-item due-gating to be
    self-limiting about, it just needs "more often than daily" (its own
    module docstring has the rolling-window reasoning), and hourly
    already satisfies that with no fourth loop needed. B.7's `local`
    presence rollup rides it too — negligible SQL-only cost, no reason
    not to share. B.8b's episode_movie_link reconciliation shares it for
    the identical reason — no external HTTP call in that sweep either.
    B.10's MAL token refresh check shares it too — a real outbound call
    to MAL, but self-gated on its own 7-day checkpoint, so an hourly
    check is cheap even though the actual refresh itself is rare. B.11e's
    untracked-show sweep shares it too — a handful of global calls
    (Sonarr/Radarr catalog listings, one AniList list fetch), not
    per-show, cheap enough that "checked hourly" costs nothing extra
    even though a new untracked show showing up is a rare event.
    The monthly tier is unconditional/not self-limiting,
    so it gets its own, much-longer-period loop (SCOPE.md §5.5's B.2
    note, BUILD_PLAN.md's B.2 entry); B.7's own Sonarr/Radarr catalog
    matching shares *that* tier instead — real N×M cost, confirmed with
    the user, the same "unconditional sweep, the tier itself is the
    correctness boundary" shape B.2's own reconciliation already uses,
    reused rather than adding a fourth interval. B.14's cross-service
    show-merge sweep shares that same tier for the identical reason —
    also real N×M cost, confirmed with the user, no fifth interval
    added. B.3's availability loop is its own third, dynamic-interval
    loop — it can't share either of the other two: faster than the
    hourly one when urgent, but not on a fixed cadence at all."""
    await asyncio.gather(
        _loop(
            run_daily_and_weekly_once,
            client,
            interval_seconds,
            "daily+weekly+animeschedule+local_presence+episode_movie_links"
            "+mal_token_refresh+untracked_shows",
        ),
        _loop(
            run_monthly_once,
            client,
            monthly_interval_seconds,
            "monthly+catalog_presence+show_merge",
        ),
        _availability_loop(client),
    )
