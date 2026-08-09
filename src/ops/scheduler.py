"""The polling loops themselves — B.1 (daily metadata refresh), B.2
(Fribb reconciliation, two tiers: weekly + monthly).

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


async def run_monthly_once(client: LcarsClient) -> int:
    """§5.5/B.2 — the monthly tier: every season of every show,
    unconditional — no due-query for this tier, Ops's own monthly timer
    is the correctness boundary (SCOPE.md §5.5's B.2 note). Same
    per-item error isolation as the other two tiers."""
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


async def run_daily_and_weekly_once(client: LcarsClient) -> int:
    """B.1's daily tier and B.2's weekly tier share one loop/interval
    (run_forever's own docstring explains why: the weekly tier is
    self-gating, so it doesn't need its own timer) — this is the single
    unit that loop actually calls each tick. Returns the combined count,
    for the caller to log."""
    daily = await run_once(client)
    weekly = await run_weekly_once(client)
    return daily + weekly


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


async def run_forever(
    client: LcarsClient, interval_seconds: int, monthly_interval_seconds: int
) -> None:
    """Two concurrent loops, not three independent timers — B.2's own
    weekly tier is self-gating (dueForSeasonReconciliation only ever
    returns a season once it's genuinely 7+ days stale, regardless of
    how often it's checked), so it rides the same cadence as B.1's daily
    tier rather than needing its own interval. Only the monthly tier is
    unconditional/not self-limiting, so it alone gets its own,
    much-longer-period loop (SCOPE.md §5.5's B.2 note, BUILD_PLAN.md's
    B.2 entry)."""
    await asyncio.gather(
        _loop(run_daily_and_weekly_once, client, interval_seconds, "daily+weekly"),
        _loop(run_monthly_once, client, monthly_interval_seconds, "monthly"),
    )
