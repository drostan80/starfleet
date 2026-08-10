"""One-time/repeatable show backfill — SCOPE.md §5.1/§5.2, BUILD_PLAN.md
B.11d, grown out of B.11's own reconnaissance (2026-08-10): LCARS's
`show` table was found completely empty (nothing had ever called
`addShow` for real), and switching Data's calendar render path (B.11f)
to LCARS-tracked shows as-is would blank the calendar entirely on day
one.

`preview_backfill()` (the dry-run Query) calls
`local_audit.find_untracked_shows_readonly()` — genuinely read-only,
no writes at all, matching every other Query's own side-effect-free
contract (same reasoning `exportData`'s own docstring gives).
`backfill_untracked_shows()` (the real Mutation) calls the fuller
`local_audit.audit_local_files()` instead, getting its own
reconciliation/orphan-walk side effects as a bonus — both share the
exact same untracked-id-comparison logic underneath
(`_untracked_sonarr_entries`/`_untracked_radarr_entries`,
local_audit.py), not two divergent implementations, so the candidate
list itself matches between a preview and the real run that follows
it (modulo whatever changed in Sonarr/Radarr in between the two
calls — an inherent, accepted gap in any "preview now, run later"
workflow).

Per-item, this is a single `shows.create_show()` call (which is
already the *whole* `addShow` mutation body — insert, external-id
links, the inline A.8 metadata fetch: episodes, AniList link
resolution via Fribb, Fribb season reconciliation, all best-effort).
There is no separate metadata-fetch phase to sequence.

Throttled between anime-classified adds (only those trigger AniList
calls) to stay inside AniList's 30 req/min budget — the same budget
Data's own anilist.py docstring cites as the reason its own `P`
(manual AniList sync) is deliberately not automatic. Scaled by season
count (see `_anilist_call_estimate()`) rather than a flat per-show
sleep — caught in review: a flat 2s sleep assumed one AniList call per
show, but `_fetch_anilist` (1 call) plus `_reconcile_air_dates` (1
call *per season*, its own B.4 docstring) plus this module's own
status-seed read means even a single-season show makes 3 calls, not
1 — a flat 2s sleep would have run at up to 90 req/min against a 30
req/min budget, silently rate-limiting shows into a permanently
under-populated `pending_review` state a resumed backfill would never
revisit (already-tracked, so excluded from every later run's own
untracked-item scan).

Idempotent/resumable by construction, no bespoke resume-state needed:
re-running this after a partial run (interrupted, rate-limited,
crashed partway) only ever sees still-untracked items, since
local_audit's own known-id lookup already excludes anything a
previous run already added.

Radarr-sourced items default to `trackingSpace: TV` (documented cut,
confirmed with the user 2026-08-10) — Radarr gives no anime signal at
all, unlike Sonarr's own `seriesType`. Misclassification is fixable
per-show after the fact, not worth a second live lookup just to
classify one field.

Watched-progress is deliberately NOT backfilled (confirmed with the
user 2026-08-10) — AniList's `mediaListEntry.progress` is an absolute
episode count with no clean, general mapping onto LCARS's own
per-season numbering (the whole absolute-vs-season_episode scheme,
A.22/A.25, exists precisely because that mapping isn't uniform show to
show). Consequence, stated plainly rather than silently absorbed: a
backfilled `watching` show starts with every episode unwatched, so
`Query.backlog` (§6.3) will over-report for it until the user marks
progress by hand.
"""

import time

from lcars import anilist_client, ids, local_audit, shows, util
from lcars.config import get_current

# AniList's own 30 req/min budget is exactly 2.0s/call — a small margin
# above that rather than the bare minimum, so a genuinely-timed call
# right at the boundary doesn't tip over it.
ANILIST_SECONDS_PER_CALL = 2.1

# The reverse of resolvers.py's own _STATUS_TO_ANILIST (A.9's push-
# direction map) — REPEATING has no direct target there either (LCARS
# has no rewatch-specific status), so this maps it back onto watching:
# actively watching it again is still watching, the closest real
# meaning available.
_ANILIST_STATUS_TO_SHOW_STATUS = {
    "CURRENT": "watching",
    "PLANNING": "planned",
    "PAUSED": "paused",
    "COMPLETED": "completed",
    "DROPPED": "dropped",
    "REPEATING": "watching",
}


def _classify(entry: dict) -> dict:
    """One `untracked_shows` entry (local_audit's own shape) to a
    shows.create_show()-ready input dict."""
    if entry["service"] == "sonarr":
        is_anime = entry.get("series_type") == "anime"
        return {
            "media_shape": "episodic",
            "tracking_space": "anime" if is_anime else "tv",
            "title_romaji": entry["title"],
            "primary_title": "romaji",
            "tvdb_id": entry["external_id"],
        }
    # Radarr — see module docstring's own "no anime signal" note.
    return {
        "media_shape": "movie",
        "tracking_space": "tv",
        "title_romaji": entry["title"],
        "primary_title": "romaji",
        "tmdb_id": entry["external_id"],
    }


def preview_backfill(conn) -> list[dict]:
    """Dry-run: what backfill_untracked_shows() below would create —
    genuinely read-only (local_audit.find_untracked_shows_readonly(),
    not audit_local_files()), run this first, always (the same
    report-only-before-you-act shape auditLocalFiles's own
    untracked_shows already has)."""
    preview = []
    for entry in local_audit.find_untracked_shows_readonly(conn):
        classification = _classify(entry)
        preview.append(
            {
                "service": entry["service"],
                "title": entry["title"],
                "external_id": entry["external_id"],
                "tracking_space": classification["tracking_space"],
                "media_shape": classification["media_shape"],
            }
        )
    return preview


def _anilist_call_estimate(conn, show_id: str) -> int:
    """Roughly how many AniList calls this show's own create_show()
    (plus this module's own status-seed) likely just made: 1 for
    _fetch_anilist, 1 per season for _reconcile_air_dates
    (metadata.py's own B.4 docstring: "calls this once per season, not
    once per show"), 1 for _seed_status_from_anilist's own read. Not a
    precise instrumentation of metadata.py's internals — a cheap,
    conservative proxy computed from what create_show() already wrote
    (the season table), used only to scale this module's own throttle
    sleep. At least 1 season assumed even if none were written yet
    (a Sonarr-fetch failure, or a not-yet-aired show with no seasons
    resolved) — never throttles less than the single-season case."""
    row = conn.execute("SELECT COUNT(*) AS n FROM season WHERE show_id = ?", (show_id,)).fetchone()
    season_count = max(row["n"] if row else 0, 1)
    return 2 + season_count


def backfill_untracked_shows(conn) -> dict:
    """The real run — one shows.create_show() per untracked Sonarr/
    Radarr item (local_audit.audit_local_files()'s own untracked_shows,
    not the read-only preview variant — this is a Mutation, its own
    reconciliation/orphan-walk side effects are a legitimate bonus, see
    module docstring). Returns {"created": [...], "failed": [...]} — a
    failure on one item (only possible via ShowInputError, which
    _classify() above never actually produces given local_audit's own
    entry shape, but shows.create_show() is a general-purpose
    function) never stops the rest of the run, same best-effort
    philosophy every other multi-item pass in this codebase already
    follows (metadata.py's own _guarded(), local_audit's own per-
    service try/except)."""
    result = local_audit.audit_local_files(conn)
    created = []
    failed = []
    for entry in result["untracked_shows"]:
        classification = _classify(entry)
        try:
            show_id = shows.create_show(conn, classification)
        except shows.ShowInputError as e:
            failed.append({"service": entry["service"], "title": entry["title"], "error": str(e)})
            continue
        created.append({"show_id": show_id, "service": entry["service"], "title": entry["title"]})
        if classification["tracking_space"] == "anime":
            _seed_status_from_anilist(conn, show_id)
            calls = _anilist_call_estimate(conn, show_id)
            time.sleep(calls * ANILIST_SECONDS_PER_CALL)
    return {"created": created, "failed": failed}


def _seed_status_from_anilist(conn, show_id: str) -> None:
    """Best-effort, never raises past this function — a failed/skipped
    seed leaves the show at its create_show()-default 'planned', same
    "worth retrying by hand, never blocks the rest of the run"
    treatment every other best-effort branch in this codebase gets."""
    cfg = get_current()
    if not cfg.anilist_access_token:
        return
    row = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = 'anilist'",
        (show_id,),
    ).fetchone()
    if row is None:
        return  # no AniList link resolved (Fribb miss, or genuinely no match) — nothing to seed
    try:
        anilist_status = anilist_client.fetch_my_list_status(
            cfg.anilist_access_token, int(row["external_id"])
        )
    except anilist_client.AniListError:
        return
    if anilist_status is None:
        return  # not on the viewer's AniList list at all — the 'planned' default stands
    new_status = _ANILIST_STATUS_TO_SHOW_STATUS.get(anilist_status)
    if new_status is None:
        return

    previous = conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
    now = util.now_utc_iso()
    conn.execute(
        "UPDATE show SET status = ?, updated_at = ? WHERE id = ?", (new_status, now, show_id)
    )
    conn.execute(
        "INSERT INTO status_change"
        " (id, show_id, previous_status, new_status, changed_at, changed_by)"
        " VALUES (?, ?, ?, ?, ?, 'show_backfill')",
        (ids.generate_id(conn, "c"), show_id, previous["status"], new_status, now),
    )
    conn.commit()
