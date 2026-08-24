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

**One tvdb id, more than one LCARS show — 2026-08-24, real live bug
user-caught (Ascendance of a Bookworm).** `show_external_id` doesn't
enforce one show per tvdb id — a franchise TVDB tracks as one flat
series can back several LCARS shows, one per AniList-side part
(metadata.py's `_fetch_sonarr_multi_show` docstring has the full
Bookworm case). That function already knew to route by
`absoluteEpisodeNumber` rather than Sonarr's raw season/episode, which
only means anything within Sonarr's own numbering, not any one
sibling's own restarted-at-1 numbering — but it only ever fills a row
once (`available_checked_at IS NULL`), by design, deferring every
later update to this module as "more authoritative." This module's own
`_poll_sonarr`/`apply_sonarr_webhook` had no equivalent routing, so
that later update could in fact never land for a shared-tvdb show:
their season/episode match could never succeed against a sibling's own
restarted numbering, silently no-oping forever the moment an episode's
first fetch had already set `available_checked_at` — exactly what left
a real, already-imported episode reading `UNAVAILABLE` days after
Sonarr had the file. `_show_ids_for_tvdb`/`_route_episode_availability`/
`_apply_episode_availability_multi_show` below are this module's own
counterpart to that same routing, for the exact case
`_apply_episode_availability`'s single-show docstring says doesn't
apply to it.

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

**B.5.1, 2026-08-13 — real-time counterpart via Sonarr/Radarr webhooks**
(`apply_sonarr_webhook`/`apply_radarr_webhook` below, wired in
server.py's `/webhooks/sonarr`/`/webhooks/radarr` routes). Payload
shapes confirmed directly against both projects' own source
(`WebhookGrabPayload`/`WebhookImportPayload`/`WebhookSeries`/
`WebhookEpisode`/`WebhookEpisodeFile` and their Radarr `Movie`/
`MovieFile` equivalents, plus `Json.cs`'s `CamelCasePropertyNames
ContractResolver` for field casing and `WebhookEventType`'s own
`DefaultNamingStrategy` override for the *value* casing) — not
recalled from memory, since a wrong key path here silently no-ops
forever, the exact failure class this project has already hit three
times. Two real, confirmed differences from the `/history` shape this
module already reads:
  - Webhook `eventType` values are `Grab`/`Download`/`Test` (PascalCase,
    a deliberate exception to the payload's otherwise-camelCase field
    names) — not `/history`'s `grabbed`/`downloadFolderImported`/
    `episodeFileDeleted`. A second mapping table, not a reuse of
    `_SONARR_EVENT_STATUS`/`_RADARR_EVENT_STATUS`.
  - Sonarr's payload carries `episodes` (a list) even for a single-
    episode grab/import — a season-pack grab is one webhook call
    covering every episode it touches, unlike `/history`'s one-record-
    one-episode shape. Looped, never assumed singular.
`Test` (and any event type this doesn't act on — `Rename`,
`SeriesAdd`/`MovieAdded`, delete events, health, etc.) is a real
requirement to handle cleanly, not just ignore: Sonarr/Radarr both
refuse to save a webhook connection whose test request doesn't
succeed, so the unmapped-event-type path always returns a "did
nothing, that's fine" result rather than an error.

**Deliberate design choice: the webhook handlers never touch
`availability_poll_checkpoint`.** That column means "how far the
poller has read Sonarr/Radarr's own `/history` log" — a different
fact than "what a webhook just told us," and the two must not be
conflated. This also answers the real question of what happens if a
webhook and the (now much less frequent, safety-net-only) poller
apply conflicting state for the same episode close together: the
poller always replays Sonarr/Radarr's own authoritative `/history` log
in full, chronological order from its own checkpoint forward, so
whenever it runs it re-derives the same final state a webhook already
reached (or a later one, if something changed since) — it can only
converge toward the truth, never lastingly regress past it. No new
"last event timestamp" column was added to arbitrate between the two
writers; not needed, matches this project's existing "apply
immediately, reconcile in the background" principle (§3 principle 1)
rather than inventing a new synchronization primitive for it.

Auth for these routes is a per-service shared secret compared against
a custom request header (`config.py`'s `sonarr_webhook_secret`/
`radarr_webhook_secret`, checked in server.py) — confirmed live against
both projects' own `WebhookSettings.cs` that a user-defined custom
header is a real, supported option on their webhook connections, not
the URL-embedded-secret fallback originally assumed before this was
checked.
"""

import logging

from lcars import radarr_client, service_health, sonarr_client, util
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


def _show_ids_for_tvdb(conn, tvdb_id: int) -> list[str]:
    """Plural on purpose, 2026-08-24 — a franchise TVDB tracks as one flat
    series can back more than one LCARS show (metadata.py's own
    `_fetch_sonarr_multi_show` docstring has the full Bookworm case this
    was built for). A caller with exactly one result can still use the
    single-show `_apply_episode_availability` path unchanged below; more
    than one routes through `_apply_episode_availability_multi_show`
    instead."""
    rows = conn.execute(
        "SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND external_id = ?",
        (str(tvdb_id),),
    ).fetchall()
    return [row["show_id"] for row in rows]


def _apply_episode_availability(
    conn, show_id: str, season: int, episode: int, status: str, path: str | None
) -> str | None:
    """Shared by `_poll_sonarr` and `apply_sonarr_webhook` — the one place
    that writes `episode.available_via_sonarr`/`file_path_sonarr`/
    `available_checked_at`. Returns the touched episode's id, or None if
    no matching row exists yet (episode not fetched into LCARS yet — not
    an error, same "not yet, not wrong" treatment `_poll_sonarr` already
    gave this case before this was extracted).

    Single-show only — matches by Sonarr's own raw season/episode
    numbers, which is exactly this show's own numbering only when it
    isn't sharing its tvdb id with any sibling (§5.1's overwhelmingly
    common case). See `_apply_episode_availability_multi_show` for the
    shared-tvdb-id case, where that equivalence doesn't hold."""
    row = conn.execute(
        "SELECT id FROM episode WHERE show_id = ? AND season = ? AND episode = ?",
        (show_id, season, episode),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE episode SET available_via_sonarr = ?, file_path_sonarr = ?,"
        " available_checked_at = ? WHERE id = ?",
        (status, path, util.now_utc_iso(), row["id"]),
    )
    return row["id"]


def _apply_episode_availability_multi_show(
    conn, show_ids: list[str], absolute_episode_number, status: str, path: str | None
) -> str | None:
    """2026-08-24, real live bug user-caught: Ascendance of a Bookworm
    ("Adopted Daughter of an Archduke" episode 19 showed `UNAVAILABLE`
    days after Sonarr had already imported it). Root cause: this
    function's single-show sibling, `_apply_episode_availability`,
    matches by Sonarr's own raw season/episode numbers — correct only
    when a show isn't sharing its tvdb id with any other LCARS show.
    Bookworm's parts do share one (tvdb 366263, one flat Sonarr series,
    metadata.py's own `_fetch_sonarr_multi_show` is the fetch-side fix
    for the exact same sharing) — Sonarr's raw "episode 55" there is
    this one sibling's own "episode 19", a translation only
    `absolute_number` survives, each sibling's season/episode numbering
    restarting at 1 independently. `_fetch_sonarr_multi_show` already
    knew this (and both DB tables agree it's the same key: this
    function's own `WHERE ... AND absolute_number = ?` is copied
    straight from that function's own existing-row lookup) — but the
    *ongoing* sync paths (`_poll_sonarr`/`apply_sonarr_webhook`) never
    got the equivalent fix, so any episode whose `available_checked_at`
    was already set (i.e. every episode past its own first-ever fetch)
    could never be updated again for a shared-tvdb show: the raw
    season/episode match below in `_apply_episode_availability` can
    never succeed against a sibling's own restarted-at-1 numbering, so
    it silently no-ops (`row is None`) forever. This is that fetch-side
    fix's ongoing-sync counterpart — same matching key, same "episode
    without one is left alone" scope boundary metadata.py's own
    docstring already documents for specials/no-absolute-number
    episodes."""
    placeholders = ",".join("?" for _ in show_ids)
    row = conn.execute(
        f"SELECT id FROM episode WHERE show_id IN ({placeholders}) AND absolute_number = ?",
        (*show_ids, absolute_episode_number),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE episode SET available_via_sonarr = ?, file_path_sonarr = ?,"
        " available_checked_at = ? WHERE id = ?",
        (status, path, util.now_utc_iso(), row["id"]),
    )
    return row["id"]


def _route_episode_availability(
    conn, show_ids: list[str], episode: dict, status: str, path: str | None
) -> str | None:
    """Shared by `_poll_sonarr` and `apply_sonarr_webhook` — picks the
    right one of the two `_apply_episode_availability*` functions above
    for however many LCARS shows `show_ids` (from `_show_ids_for_tvdb`)
    turned out to hold. The overwhelmingly common single-show case is
    unchanged; `episode["seasonNumber"]`/`["episodeNumber"]` genuinely
    are this one show's own numbering there. `absoluteEpisodeNumber` is
    only meaningful (and only needed) once there's more than one
    sibling to route across — real Sonarr `/history`/webhook payloads
    both embed it on every regular episode (specials/no-absolute-number
    episodes come back `None`, left alone entirely, same scope boundary
    metadata.py's `_fetch_sonarr_multi_show` already documents)."""
    if len(show_ids) == 1:
        if episode.get("seasonNumber") is None or episode.get("episodeNumber") is None:
            return None
        return _apply_episode_availability(
            conn, show_ids[0], episode["seasonNumber"], episode["episodeNumber"], status, path
        )
    abs_number = episode.get("absoluteEpisodeNumber")
    if abs_number is None:
        return None
    return _apply_episode_availability_multi_show(conn, show_ids, float(abs_number), status, path)


def _apply_show_availability_radarr(conn, show_id: str, status: str, path: str | None) -> None:
    """Radarr's counterpart to `_apply_episode_availability` above — shared
    by `_poll_radarr` and `apply_radarr_webhook`."""
    conn.execute(
        "UPDATE show SET available_via_radarr = ?, file_path_radarr = ?,"
        " available_checked_at = ? WHERE id = ?",
        (status, path, util.now_utc_iso(), show_id),
    )


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
    except sonarr_client.SonarrError as e:
        logger.exception("Sonarr availability poll failed — will retry on the next poll")
        service_health.record_failure(conn, "sonarr", str(e))
        conn.commit()
        return 0
    service_health.record_success(conn, "sonarr")

    if not records:
        conn.commit()  # persist the health write above even with nothing else to do
        return 0

    touched_episode_ids: set[str] = set()
    for record in records:
        status = _SONARR_EVENT_STATUS.get(record["eventType"])
        if status is None:
            continue
        series = record.get("series")
        episode = record.get("episode")
        if series is None or episode is None:
            continue
        show_ids = _show_ids_for_tvdb(conn, series["tvdbId"])
        if not show_ids:
            continue  # not (yet) tracked in LCARS — not an error, §5.1
        path = record["data"].get("importedPath") if status == "available" else None
        episode_id = _route_episode_availability(conn, show_ids, episode, status, path)
        if episode_id is None:
            continue  # episode not yet fetched into LCARS — A.8's job, not this poll's
        touched_episode_ids.add(episode_id)

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
    except radarr_client.RadarrError as e:
        logger.exception("Radarr availability poll failed — will retry on the next poll")
        service_health.record_failure(conn, "radarr", str(e))
        conn.commit()
        return 0
    service_health.record_success(conn, "radarr")

    if not records:
        conn.commit()  # persist the health write above even with nothing else to do
        return 0

    touched_show_ids: set[str] = set()
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
        _apply_show_availability_radarr(conn, show_id, status, path)
        touched_show_ids.add(show_id)

    _set_checkpoint(conn, "radarr", records[-1]["date"])
    conn.commit()
    return len(touched_show_ids)


# B.5.1 — webhook eventType values, confirmed against Sonarr/Radarr's own
# WebhookEventType.cs: PascalCase strings (a deliberate exception to the
# rest of the payload's camelCase field names — see the module docstring),
# and genuinely distinct from `/history`'s own eventType vocabulary above.
_SONARR_WEBHOOK_EVENT_STATUS = {
    "Grab": "downloading",
    "Download": "available",
}
_RADARR_WEBHOOK_EVENT_STATUS = {
    "Grab": "downloading",
    "Download": "available",
}


def apply_sonarr_webhook(conn, payload: dict) -> dict:
    """Real-time counterpart to `_poll_sonarr` — see the module docstring's
    B.5.1 section for the full design rationale (payload shape, why this
    never touches `availability_poll_checkpoint`). Returns
    {"episodes_updated": int}, same shape as pollFileAvailability's own
    result, for server.py's webhook route to echo back.

    Any `eventType` this doesn't act on (`Test` included) is a deliberate,
    silent no-op — Sonarr won't save a webhook connection whose test
    request fails, so this must never error on content it doesn't
    recognize."""
    status = _SONARR_WEBHOOK_EVENT_STATUS.get(payload.get("eventType"))
    if status is None:
        return {"episodes_updated": 0}
    series = payload.get("series")
    episodes = payload.get("episodes") or []
    if series is None or series.get("tvdbId") is None or not episodes:
        return {"episodes_updated": 0}
    show_ids = _show_ids_for_tvdb(conn, series["tvdbId"])
    if not show_ids:
        return {"episodes_updated": 0}  # not (yet) tracked in LCARS — same as the poller
    path = None
    if status == "available":
        path = (payload.get("episodeFile") or {}).get("path")
    touched_episode_ids: set[str] = set()
    for ep in episodes:
        episode_id = _route_episode_availability(conn, show_ids, ep, status, path)
        if episode_id is not None:
            touched_episode_ids.add(episode_id)
    conn.commit()
    return {"episodes_updated": len(touched_episode_ids)}


def apply_radarr_webhook(conn, payload: dict) -> dict:
    """Radarr's counterpart to `apply_sonarr_webhook` above — see that
    function's docstring, same shape. Returns {"shows_updated": int}."""
    status = _RADARR_WEBHOOK_EVENT_STATUS.get(payload.get("eventType"))
    if status is None:
        return {"shows_updated": 0}
    movie = payload.get("movie")
    if movie is None or movie.get("tmdbId") is None:
        return {"shows_updated": 0}
    show_id = _show_id_for_tmdb_movie(conn, movie["tmdbId"])
    if show_id is None:
        return {"shows_updated": 0}  # not tracked as a standalone movie show in LCARS
    path = None
    if status == "available":
        path = (payload.get("movieFile") or {}).get("path")
    _apply_show_availability_radarr(conn, show_id, status, path)
    conn.commit()
    return {"shows_updated": 1}


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
