"""Local file availability polling — SCOPE.md §5.2/§5.1/§6.7,
BUILD_PLAN.md B.3.

Polls Sonarr's/Radarr's own grab/import event log (`/history`), not a
re-check of every tracked episode's current state — verified directly
against the user's own real, live Sonarr (4.0.19.2979) and Radarr
(6.3.0.10514) instances before being built (SCOPE.md §5.2's "Resolved
2026-08-09 (B.3)" note has the full rationale). `includeEpisode`/
`includeSeries` (Sonarr) and `includeMovie` (Radarr) embed everything
needed to match a history event straight to LCARS's own
`show_external_id` crosswalk (§5.4) — no separate correlation-id
column needed, confirmed live rather than assumed.

Availability is 3-state (`unavailable | downloading | available`), not
boolean — a file grabbed but not yet imported is a real, distinct,
useful state. Events are processed oldest-first within each poll
(Sonarr/Radarr return newest-first; reversed before applying) so a
quality-upgrade's delete-then-reimport sequence resolves to the
correct final state regardless of pagination order.

Scope, deliberately narrow: Sonarr events only ever touch `episode`
rows (`available_via_sonarr`/`file_path_sonarr`); Radarr events only
ever touch standalone movie `show` rows (`available_via_radarr`/
`file_path_radarr`). A Radarr event also updating a linked
`bonus_movie`-kind episode (via `episode_movie_link`) is deliberately
NOT this module's job — that cross-link's *automatic* derivation is
B.8b's own explicitly later, B.3-dependent step (BUILD_PLAN.md); this
module only populates what B.8b will need to match against.

Best-effort throughout, same philosophy as `metadata.py` (A.8): a
service being unreachable or not configured never fails the whole
poll, just skips that service. A genuine per-service failure here has
no natural `pending_review` home (no single show/season it's about —
that's exactly what B.6's own service-health tracking, not yet built,
is for) — logged, not silently swallowed, but not routed through
`pending_review` either.

Two entry points, not one — resolved 2026-08-09 after B.3's own build,
before commit, once the actual blocking cost was worked through with
the user: `poll_file_availability()` is what Ops's automatic loop
calls, and a service's very first call under it never walks that
service's full history — it seeds `availability_poll_checkpoint` to
"now" and returns, so LCARS's single request-handling thread is never
blocked by an unbounded, unscheduled backfill. `backfill_file_
availability()` is the manual counterpart (`ops backfill-availability`
CLI) that actually walks a service's full history, ignoring any
existing checkpoint — run once, deliberately, at a moment of the
user's own choosing, not on Ops's timer.
"""

import logging

from lcars import radarr_client, sonarr_client, util
from lcars.config import get_current

logger = logging.getLogger("lcars.availability")

_PAGE_SIZE = 250


def poll_file_availability(conn) -> dict:
    """Runs both services' polls, returns
    {"episodes_updated": int, "shows_updated": int} — pollFileAvailability's
    own confirmation summary (schema.graphql's AvailabilityPollResult).

    A service's very first call (no checkpoint row yet) never walks its
    full history here — see _poll_sonarr/_poll_radarr's own docstrings
    and schema.graphql's pollFileAvailability note. Ops's own automatic
    loop calls this one, never backfill_file_availability below."""
    episodes_updated = _poll_sonarr(conn, backfill=False)
    shows_updated = _poll_radarr(conn, backfill=False)
    return {"episodes_updated": episodes_updated, "shows_updated": shows_updated}


def backfill_file_availability(conn) -> dict:
    """The manual, one-time counterpart: walks each configured
    service's *entire* history, ignoring any existing checkpoint,
    rather than only what's new since it. Not called by Ops's own
    automatic loop (scheduler.py) — only by the explicit `ops
    backfill-availability` CLI command, run deliberately by a human at
    a moment of their own choosing, since it blocks LCARS's single
    request-handling thread for real seconds-to-minutes on a
    long-running Sonarr instance. See schema.graphql's
    backfillFileAvailability docstring for the full rationale."""
    episodes_updated = _poll_sonarr(conn, backfill=True)
    shows_updated = _poll_radarr(conn, backfill=True)
    return {"episodes_updated": episodes_updated, "shows_updated": shows_updated}


def _get_checkpoint(conn, service: str) -> str | None:
    row = conn.execute(
        "SELECT last_event_at FROM availability_poll_checkpoint WHERE service = ?", (service,)
    ).fetchone()
    return row["last_event_at"] if row else None


def _set_checkpoint(conn, service: str, last_event_at: str) -> None:
    now = util.now_utc_iso()
    conn.execute(
        "INSERT INTO availability_poll_checkpoint (service, last_event_at, updated_at)"
        " VALUES (?, ?, ?)"
        " ON CONFLICT (service) DO UPDATE SET last_event_at = excluded.last_event_at,"
        "   updated_at = excluded.updated_at",
        (service, last_event_at, now),
    )


def _fetch_new_records(history_page_fn, checkpoint: str | None) -> list[dict]:
    """Pages through a /history endpoint (newest-first, per the real API's
    own sort order) until reaching `checkpoint` or running out of pages,
    then returns the new records in chronological (oldest-first) order —
    correct application order for a rapid delete-then-reimport sequence.
    `checkpoint = None` processes the *entire* history — a real cost
    (tens of thousands of records on a long-running instance). Callers
    only ever pass `None` here from `_poll_sonarr`/`_poll_radarr`'s own
    `backfill=True` path; the automatic (non-backfill) path short-
    circuits before reaching this function at all on a never-polled
    service (seed-and-skip, see the module docstring)."""
    new_records: list[dict] = []
    page = 1
    while True:
        data = history_page_fn(page, _PAGE_SIZE)
        records = data.get("records", [])
        if not records:
            break
        reached_checkpoint = False
        for record in records:
            if checkpoint is not None and record["date"] <= checkpoint:
                reached_checkpoint = True
                break
            new_records.append(record)
        if reached_checkpoint:
            break
        if page * _PAGE_SIZE >= data.get("totalRecords", 0):
            break
        page += 1
    new_records.reverse()
    return new_records


def _show_id_for_tvdb(conn, tvdb_id: int) -> str | None:
    row = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND external_id = ?",
        (str(tvdb_id),),
    ).fetchone()
    return row["show_id"] if row else None


def _show_id_for_tmdb_movie(conn, tmdb_id: int) -> str | None:
    row = conn.execute(
        "SELECT s.id FROM show s"
        " JOIN show_external_id sei ON sei.show_id = s.id"
        " WHERE sei.service = 'tmdb' AND sei.external_id = ? AND s.media_shape = 'movie'",
        (str(tmdb_id),),
    ).fetchone()
    return row["id"] if row else None


# Sonarr's own event -> (available_via_sonarr, clears/sets file_path_sonarr)
# mapping. `downloadIgnored` (a rejected grab) is deliberately absent —
# no availability effect, skipped entirely rather than mapped to a no-op
# state, matching real data (2 occurrences in a 250-record live sample).
_SONARR_EVENT_STATUS = {
    "grabbed": "downloading",
    "downloadFolderImported": "available",
    "episodeFileDeleted": "unavailable",
}
_RADARR_EVENT_STATUS = {
    "grabbed": "downloading",
    "downloadFolderImported": "available",
    "movieFileDeleted": "unavailable",
}


def _poll_sonarr(conn, backfill: bool = False) -> int:
    cfg = get_current()
    if not cfg.sonarr_url or not cfg.sonarr_api_key:
        return 0  # not configured — same as "not linked", not a failure to report
    checkpoint = _get_checkpoint(conn, "sonarr")
    if checkpoint is None and not backfill:
        # First-ever call for this service, via the automatic (non-backfill)
        # path: never silently walk the whole history inline in a resolver —
        # seed the checkpoint to "now" so every later automatic poll stays
        # cheap, and leave real history before this moment to an explicit
        # backfill_file_availability() call instead (schema.graphql's own
        # pollFileAvailability/backfillFileAvailability docstrings).
        _set_checkpoint(conn, "sonarr", util.now_utc_iso())
        conn.commit()
        return 0
    effective_checkpoint = None if backfill else checkpoint
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            records = _fetch_new_records(client.history_page, effective_checkpoint)
    except sonarr_client.SonarrError:
        logger.exception("Sonarr availability poll failed — will retry on the next poll")
        return 0

    if not records:
        return 0

    touched_episode_ids: set[str] = set()
    now = util.now_utc_iso()
    for record in records:
        status = _SONARR_EVENT_STATUS.get(record["eventType"])
        if status is None:
            continue
        series = record.get("series")
        episode = record.get("episode")
        if series is None or episode is None:
            continue
        show_id = _show_id_for_tvdb(conn, series["tvdbId"])
        if show_id is None:
            continue  # not (yet) tracked in LCARS — not an error, §5.1
        row = conn.execute(
            "SELECT id FROM episode WHERE show_id = ? AND season = ? AND episode = ?",
            (show_id, episode["seasonNumber"], episode["episodeNumber"]),
        ).fetchone()
        if row is None:
            continue  # episode not yet fetched into LCARS — A.8's job, not this poll's
        path = record["data"].get("importedPath") if status == "available" else None
        conn.execute(
            "UPDATE episode SET available_via_sonarr = ?, file_path_sonarr = ?,"
            " available_checked_at = ? WHERE id = ?",
            (status, path, now, row["id"]),
        )
        touched_episode_ids.add(row["id"])

    _set_checkpoint(conn, "sonarr", records[-1]["date"])
    conn.commit()
    return len(touched_episode_ids)


def _poll_radarr(conn, backfill: bool = False) -> int:
    cfg = get_current()
    if not cfg.radarr_url or not cfg.radarr_api_key:
        return 0
    checkpoint = _get_checkpoint(conn, "radarr")
    if checkpoint is None and not backfill:
        # Same first-ever-call seed-and-skip as _poll_sonarr above.
        _set_checkpoint(conn, "radarr", util.now_utc_iso())
        conn.commit()
        return 0
    effective_checkpoint = None if backfill else checkpoint
    try:
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            records = _fetch_new_records(client.history_page, effective_checkpoint)
    except radarr_client.RadarrError:
        logger.exception("Radarr availability poll failed — will retry on the next poll")
        return 0

    if not records:
        return 0

    touched_show_ids: set[str] = set()
    now = util.now_utc_iso()
    for record in records:
        status = _RADARR_EVENT_STATUS.get(record["eventType"])
        if status is None:
            continue
        movie = record.get("movie")
        if movie is None:
            continue
        show_id = _show_id_for_tmdb_movie(conn, movie["tmdbId"])
        if show_id is None:
            continue  # not tracked as a standalone movie show in LCARS
        path = record["data"].get("importedPath") if status == "available" else None
        conn.execute(
            "UPDATE show SET available_via_radarr = ?, file_path_radarr = ?,"
            " available_checked_at = ? WHERE id = ?",
            (status, path, now, show_id),
        )
        touched_show_ids.add(show_id)

    _set_checkpoint(conn, "radarr", records[-1]["date"])
    conn.commit()
    return len(touched_show_ids)


def recommended_poll_interval_seconds(conn) -> int:
    """§6.7/B.3 — Ops's own adaptive cadence, computed server-side: 300s
    while a WATCHING show has an episode that aired in the last 2 hours
    and isn't yet locally available; 900s while one aired further back
    and still isn't available (no upper bound — the point, per the
    user's own framing, is to keep catching a stuck import until it
    resolves, not give up after some arbitrary time); 3600s baseline
    otherwise. Movies have no "just aired" moment, so Radarr plays no
    part in this — episode-only."""
    now = util.now_utc_iso()
    two_hours_ago = util.utc_iso_offset_hours(-2)
    urgent = conn.execute(
        "SELECT 1 FROM episode e JOIN show s ON s.id = e.show_id"
        " WHERE s.status = 'watching' AND e.available_locally = 0"
        "   AND e.air_date_utc IS NOT NULL AND e.air_date_utc <= ? AND e.air_date_utc > ?"
        " LIMIT 1",
        (now, two_hours_ago),
    ).fetchone()
    if urgent is not None:
        return 300
    cooldown = conn.execute(
        "SELECT 1 FROM episode e JOIN show s ON s.id = e.show_id"
        " WHERE s.status = 'watching' AND e.available_locally = 0"
        "   AND e.air_date_utc IS NOT NULL AND e.air_date_utc <= ?"
        " LIMIT 1",
        (two_hours_ago,),
    ).fetchone()
    if cooldown is not None:
        return 900
    return 3600
