"""Per-integration service-health tracking — SCOPE.md §6.7, BUILD_PLAN.md
B.6.

Reachability only — **confirmed with the user, 2026-08-09**: none of
the four client modules (sonarr_client/radarr_client/anilist_client/
animeschedule_client) currently distinguish a 429/rate-limit response
from any other non-200, so "rate-limited?" would need new detection
logic in all four plus a decision on whether LCARS acts on it (defers
a poll) or just reports it — real scope, deliberately deferred, not
built here.

**"Tracked by Ops" (§6.7's own phrasing) means Ops's regular poll
cycle is what triggers the calls this module observes, not that Ops
holds any state itself** — confirmed with the user rather than assumed:
Ops has no client code for any of these four services and no database
of its own (§11.2's B.1 resolution), so the actual HTTP calls, and
every reachability signal they carry, already happen entirely inside
`lcars/`. This module is a thin, no-new-polling observer bolted onto
call sites that already exist.

**The rule that decides where a hook goes: `record_success` means "an
HTTP request actually completed," never "a function returned without
raising."** Tried hooking `metadata._guarded` first (its own single
choke point for all of `_fetch_sonarr`/`_fetch_radarr`/`_fetch_anilist`/
`_ensure_anilist_link`/`_reconcile_air_dates`) — reverted before commit,
caught by a real, reproduced test failure
(`test_add_show_fetch_failure_logs_pending_review_and_refresh_retries`):
every one of those functions has its own legitimate no-HTTP
early-return path (missing external id, service not configured, no
season carries an `anilist_id` yet...), so `_guarded` recorded a false
`ok` for a service never actually contacted that call — a genuine
`_fetch_anilist` failure got silently overwritten back to `ok` by the
very next `_guarded` call, `_reconcile_air_dates`'s own trivially-
succeeding no-mapped-season no-op. Fixed by hooking each function's
own actual client call directly instead — `_fetch_anilist`/
`_reconcile_air_dates`/`_fetch_sonarr`/`_fetch_radarr` (metadata.py,
see each one's own inline comment), plus `availability.py`'s
`_poll_sonarr`/`_poll_radarr`, `local_audit.py`'s `_audit_sonarr`/
`_audit_radarr` (whole-pass outcome — a failure anywhere in the
per-series walk marks the pass unreachable, even though partial
results are kept, same as it was before this hook existed), and
`animeschedule.py`'s `poll_anime_schedule`. `_ensure_anilist_link` is
deliberately never hooked — its only network path is a Fribb dataset
download, not one of `TRACKED_SERVICES`. Whether triggered by Ops's
automatic cadence or a human's on-demand `refreshShowMetadata`, both
are equally informative about reachability, so neither path is
special-cased.

`record_success`/`record_failure` silently no-op for a service outside
`TRACKED_SERVICES` rather than needing every call site to filter
first.

**`mal` added 2026-08-10 (B.10)** — hooked at `resolvers.py`'s
`refreshMalTokenIfDue` (the actual `mal_client.refresh_access_token()`
call), same "close the small consistency gap" precedent B.6/B.8b/B.9
already established, not something BUILD_PLAN.md's own B.10 text
asked for explicitly. Deliberately scoped to the refresh call only,
not every individual score/status push — same scope this table
already has for AniList, where the *push* functions
(`_push_season_score`/`_push_show_status`) were never hooked either,
only `_fetch_anilist`'s read path.
"""

from lcars import util

TRACKED_SERVICES = ("sonarr", "radarr", "anilist", "animeschedule", "mal")


def record_success(conn, service: str) -> None:
    if service not in TRACKED_SERVICES:
        return
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO service_health"
        " (service, status, last_checked_at, last_success_at, last_error_message, updated_at)"
        " VALUES (?, 'ok', ?, ?, NULL, ?)"
        " ON CONFLICT (service) DO UPDATE SET"
        "   status = 'ok', last_checked_at = excluded.last_checked_at,"
        "   last_success_at = excluded.last_success_at, last_error_message = NULL,"
        "   updated_at = excluded.updated_at",
        (service, now, now, now),
    )


def record_failure(conn, service: str, message: str) -> None:
    """`last_success_at` is deliberately left untouched on a repeat
    failure (the `ON CONFLICT` clause below never assigns it) — it
    should keep showing "last known good contact" through an ongoing
    outage, not go blank the moment a second failure lands."""
    if service not in TRACKED_SERVICES:
        return
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO service_health"
        " (service, status, last_checked_at, last_success_at, last_error_message, updated_at)"
        " VALUES (?, 'unreachable', ?, NULL, ?, ?)"
        " ON CONFLICT (service) DO UPDATE SET"
        "   status = 'unreachable', last_checked_at = excluded.last_checked_at,"
        "   last_error_message = excluded.last_error_message, updated_at = excluded.updated_at",
        (service, now, message, now),
    )


def get_all(conn) -> list[dict]:
    """One entry per `TRACKED_SERVICES` member, always — a service
    with no row yet (never configured, or just not polled yet) gets a
    synthesized `status = "unknown"` placeholder rather than being
    omitted, so a caller (Data's status bar, §6.7) never has to treat
    "missing from the list" as a fourth, implicit state."""
    rows = {r["service"]: dict(r) for r in conn.execute("SELECT * FROM service_health")}
    return [
        rows.get(
            service,
            {
                "service": service,
                "status": "unknown",
                "last_checked_at": None,
                "last_success_at": None,
                "last_error_message": None,
            },
        )
        for service in TRACKED_SERVICES
    ]
