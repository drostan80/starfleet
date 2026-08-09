"""The polling loop itself — B.1, SCOPE.md §4 Phase B/§6.7.

`run_once()` is the real, testable unit: one full sweep of
`dueForMetadataRefresh`, refreshing whatever comes back — this is what
both a real scheduled tick *and* a test exercise directly, with no
infinite loop or real sleep involved. `run_forever()` is a thin
while-loop wrapper around it, kept separate for exactly that reason.
"""

import asyncio
import logging

from ops.lcars_client import LcarsClient, LcarsError

logger = logging.getLogger("ops.scheduler")


async def run_once(client: LcarsClient) -> int:
    """One sweep: fetch every due show, refresh each. A single show's
    failure (network blip, a real LCARS-side error) is logged and
    skipped, never aborts the rest of the batch — the same best-effort,
    one-branch-failing-doesn't-block-the-others philosophy
    `metadata.fetch_and_populate()` (A.8) already applies show-
    internally, applied here one level up, across shows. Returns the
    count actually refreshed, for the caller to log."""
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


async def run_forever(client: LcarsClient, interval_seconds: int) -> None:
    """Never returns under normal operation — `ops run` (cli.py) awaits
    this directly. A sweep-level failure (e.g. dueForMetadataRefresh
    itself unreachable) is logged and retried on the next tick, not
    fatal to the process — Ops staying up and trying again next
    interval matters more than crash-and-restart here.

    Catches bare Exception, not just LcarsError — deliberately broad,
    same reasoning metadata._guarded (A.8) already uses for the exact
    same "never let one bad response crash the long-running process"
    concern: LcarsClient._query already turns every failure mode it
    knows about into LcarsError, but Ops is an unattended daemon, not
    Data's TUI, so a genuinely unanticipated exception here should still
    be logged and retried next interval rather than kill the process."""
    while True:
        try:
            count = await run_once(client)
            logger.info("refreshed %d show(s)", count)
        except Exception:
            logger.exception("dueForMetadataRefresh sweep failed, will retry next interval")
        await asyncio.sleep(interval_seconds)
