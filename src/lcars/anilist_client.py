"""AniList metadata fetch + push — SCOPE.md §5.1/§5.4/§5.5/§6.1/§6.8,
BUILD_PLAN.md A.8/A.9.

`fetch_media` is AniList's public, unauthenticated GraphQL endpoint —
fetching a `Media` by id for display metadata isn't tied to a specific
viewer (A.8). `authorize_url`/`exchange_code`/`save_media_list_entry`
are the *authenticated* half (A.9, §6.8: LCARS pushes status/score to
AniList itself, a separate OAuth session from Data's own narrow
episode-watch-status-only path) — close ports of Data's own real,
working `anilist.py` (`~/repos/data/src/data/anilist.py`), same
error-handling shape (AniListError/AniListAuthError, connect/timeout/
GraphQL-error/401 distinctions), same OAuth endpoints/PIN-redirect
flow (LCARS reuses Data/aniq's existing registered AniList app,
confirmed — not a new app registration). Adapted to LCARS's own sync
execution model (§11.2) — a sync `httpx.Client` throughout, not
Data's `httpx.AsyncClient`.
"""

import time

import httpx

GRAPHQL_URL = "https://graphql.anilist.co"
AUTHORIZE_URL = "https://anilist.co/api/v2/oauth/authorize"
TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
# AniList's hosted "pin" page — see Data's own anilist.py for the full
# history of why this redirect (not a local callback server, not the
# Implicit Grant) is the one that actually works; unchanged here.
PIN_REDIRECT_URI = "https://anilist.co/api/v2/oauth/pin"

# 2026-08-13 — quick fix for a real production burst (22 false
# pending_review "Too many requests" entries), while the bigger
# LCARS/Ops-owns-every-AniList-write rewrite is still just a note in
# BUILD_PLAN.md, not built. Root cause: `run_once` (B.1's daily
# metadata refresh, ops/scheduler.py) loops over every show due for
# refresh calling `refresh_show_metadata` back-to-back with zero
# delay, and each show fires 1 (`_fetch_anilist`) + 1-per-season
# (`_reconcile_air_dates`) real GraphQL calls (metadata.py) — nothing
# anywhere throttled that loop. show_backfill.py already established
# the actual budget to design against (AniList's real ~30 req/min,
# 2.1s/call with a small safety margin) but only applied it between
# shows in its own one-time sweep, not here, and not at the one place
# every GraphQL call in the whole process actually funnels through.
# Fixed at that single choke point instead (`_graphql_request` below)
# so it's automatically enforced for every current and future caller
# — this loop, show_backfill's own calls, interactive addShow/refresh
# mutations, watch_reconcile, all of it — process-wide, no per-caller
# bookkeeping needed. Doesn't reduce total call *volume*, just paces
# it under the documented budget, which is what was actually being
# violated. Module-level, not per-client, deliberately: LCARS's own
# sync/one-shared-connection execution model (§11.2, this module's own
# docstring) means there's never real concurrent AniList traffic
# within one process to coordinate across.
#
# Known real limitation, not fixed by this: AniList's rate limit is
# per-account, not per-process — Data still writes to AniList directly
# too (§6.8's permanent exception), and aniq is a third, fully
# independent legacy client. This throttle only paces LCARS/Ops's own
# share of that shared budget; it can't see or pace the other two.
# That's exactly the gap the parked "AniList write should live only in
# LCARS/Ops" rewrite (BUILD_PLAN.md, "Deliberately not on this plan")
# is for — not addressed here.
_ANILIST_SECONDS_PER_CALL = 2.1
_last_anilist_call_at: float | None = None


def _throttle_anilist_call() -> None:
    global _last_anilist_call_at
    now = time.monotonic()
    if _last_anilist_call_at is not None:
        wait = _ANILIST_SECONDS_PER_CALL - (now - _last_anilist_call_at)
        if wait > 0:
            time.sleep(wait)
    _last_anilist_call_at = time.monotonic()


_MEDIA_QUERY = """
query ($mediaId: Int) {
  Media(id: $mediaId) {
    title { romaji english native }
    coverImage { large }
    bannerImage
    description(asHtml: false)
    genres
    episodes
    duration
    idMal
    studios(isMain: true) { nodes { id name } }
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
      edges {
        role
        node { id name { full } }
        voiceActors(language: JAPANESE) { id name { full } }
      }
    }
    relations {
      edges {
        node {
          id
          idMal
          format
          title { romaji english native }
        }
      }
    }
  }
}
"""

_AIRING_SCHEDULE_QUERY = """
query ($mediaId: Int) {
  Media(id: $mediaId) {
    episodes
    airingSchedule {
      nodes { episode airingAt }
    }
  }
}
"""

# A.21 (2026-08-09) — only these AniList `format` values represent an
# actual anime show worth a `show_relation` edge/stub at all; the same
# relations list can include manga/novel source material, which §5.1's
# show model has no place for (LCARS only tracks anime/TV/movies).
ANIME_RELATION_FORMATS = frozenset({"TV", "TV_SHORT", "MOVIE", "SPECIAL", "OVA", "ONA"})


class AniListError(Exception):
    pass


class AniListAuthError(AniListError):
    """Specifically a 401 — the token is missing/invalid/expired, as
    opposed to a rate limit, network blip, or GraphQL validation error
    — same distinction Data's own anilist.py draws, for the same
    reason: callers can catch this specifically to prompt re-running
    `lcars anilist-login`, without string-matching error text."""


def authorize_url(client_id: str) -> str:
    return (
        f"{AUTHORIZE_URL}?client_id={client_id}&redirect_uri={PIN_REDIRECT_URI}&response_type=code"
    )


def _graphql_request(
    query: str, variables: dict, token: str | None, client: httpx.Client | None
) -> dict:
    """Shared error handling for every GraphQL call this module makes
    (fetch_media, save_media_list_entry) — connect/timeout/HTTP/
    GraphQL-error-body/401 all handled once.

    2026-08-13 — also the single choke point every real call passes
    through, so `_throttle_anilist_call()` lives here rather than in
    each caller (see that function's own comment for why)."""
    _throttle_anilist_call()
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        try:
            response = client.post(
                GRAPHQL_URL,
                json={"query": query, "variables": variables},
                headers=headers,
            )
        except httpx.ConnectError as e:
            raise AniListError("Could not connect to AniList") from e
        except httpx.TimeoutException as e:
            raise AniListError("Timed out talking to AniList") from e

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if payload and payload.get("errors"):
            messages = "; ".join(e.get("message", "unknown error") for e in payload["errors"])
            if response.status_code == 401:
                raise AniListAuthError(
                    f"AniList rejected the token — it may have expired; "
                    f"re-run `lcars anilist-login` ({messages})"
                )
            raise AniListError(f"AniList GraphQL error: {messages}")
        if response.status_code == 401:
            raise AniListAuthError(
                "AniList rejected the token (401 Unauthorized) — it may have "
                "expired; re-run `lcars anilist-login`"
            )
        if response.status_code >= 400:
            raise AniListError(f"AniList returned an error: HTTP {response.status_code}")

        return payload["data"]
    finally:
        if owns_client:
            client.close()


def fetch_media(anilist_id: int, client: httpx.Client | None = None) -> dict | None:
    """The raw `Media` object (or None if AniList has no such id) for
    `anilist_id` — title variants, cover/banner, synopsis, genres,
    episode count, average episode `duration` (minutes — A.19, fills
    `show.duration_minutes` for anime; TMDB covers everything else,
    see tmdb_client.py), the companion MAL id, main studio(s), and
    voice cast. Raises AniListError on a network/HTTP/GraphQL-level failure
    rather than returning None — callers (metadata.py) need to tell
    "no such id" apart from "AniList was unreachable" (§3's best-effort
    policy treats them differently: the latter is worth a retry, the
    former isn't)."""
    data = _graphql_request(_MEDIA_QUERY, {"mediaId": anilist_id}, token=None, client=client)
    return data["Media"]


def fetch_airing_schedule(anilist_id: int, client: httpx.Client | None = None) -> dict | None:
    """§5.2/§6.7, B.4 — a season's own `airingSchedule` (or None if
    AniList has no such id): `{"episodes": int|None, "nodes": [...]}`
    — `nodes` is `episode`/`airingAt` per entry, the *full* schedule
    (not `notYetAired`-filtered) so already-aired episodes get
    reconciled too, not just upcoming ones (§6.7's "reconciliation"
    framing, confirmed with the user). A separate, lighter query from
    fetch_media() above — metadata.py's own `_reconcile_air_dates`
    calls this once per season, not once per show, so the heavier
    cast/relations payload fetch_media() carries would be pure waste
    here. Verified live against three real AniList Media entries
    before being written: `episode` is always 1-based *for that
    specific Media entry* — a split-cour sequel season restarts at 1,
    it does not continue the previous cour's broadcast count —
    matching `season.anilist_id`'s own per-season crosswalk (§5.5)
    directly onto `episode.episode` (also season-scoped), no numbering
    offset needed.

    `episodes` (that Media entry's own total episode count) exists so
    a caller can detect a genuinely different, real failure mode: a
    single TVDB season spanning *multiple* AniList Media entries (e.g.
    Attack on Titan's own Season 3, one 22-episode TVDB season across
    two 12/10-episode AniList entries — confirmed live, not
    hypothetical). `season.anilist_id` can only ever point at one of
    those, so per-episode matching is only valid when LCARS's own
    episode count for that season doesn't exceed this number —
    metadata.py's own `_reconcile_air_dates` is what actually acts on
    this, not this function."""
    data = _graphql_request(
        _AIRING_SCHEDULE_QUERY, {"mediaId": anilist_id}, token=None, client=client
    )
    media = data["Media"]
    if media is None:
        return None
    return {"episodes": media.get("episodes"), "nodes": media["airingSchedule"]["nodes"]}


_MY_LIST_STATUS_QUERY = """
query ($mediaId: Int) {
  Media(id: $mediaId) {
    mediaListEntry { status }
  }
}
"""


def fetch_my_list_status(
    token: str, anilist_id: int, client: httpx.Client | None = None
) -> str | None:
    """B.11d — the authenticated viewer's own current list status for
    `anilist_id` (`mediaListEntry` on a `Media` query resolves to the
    token's own viewer, AniList's standard behavior — same shape
    fetch_media()'s query would need `token` added to if it ever
    wanted this). A one-time bootstrap read used only when backfilling
    a show LCARS has never tracked before (confirmed with the user,
    2026-08-10): seeds the new show's initial LCARS status, not an
    ongoing sync — LCARS stays the source of truth for every push/pull
    after this. Returns None if AniList has no such id, or the viewer
    has no list entry for it at all (never added to their list)."""
    data = _graphql_request(
        _MY_LIST_STATUS_QUERY, {"mediaId": anilist_id}, token=token, client=client
    )
    media = data["Media"]
    if media is None:
        return None
    entry = media.get("mediaListEntry")
    return entry["status"] if entry else None


_VIEWER_QUERY = "query { Viewer { id } }"


def fetch_viewer_id(token: str, client: httpx.Client | None = None) -> int:
    """B.11d — the authenticated viewer's own numeric AniList user id.
    `MediaListCollection` (fetch_my_anime_list() below) requires an
    explicit `userId` argument — unlike `Media.mediaListEntry`
    (fetch_my_list_status() above), there's no "just the token's own
    viewer" implicit mode for a list-collection query. Live-verified
    against the user's real AniList account before being written."""
    data = _graphql_request(_VIEWER_QUERY, {}, token=token, client=client)
    return data["Viewer"]["id"]


_MY_ANIME_LIST_QUERY = """
query ($userId: Int) {
  MediaListCollection(userId: $userId, type: ANIME) {
    lists {
      entries {
        status
        progress
        media { id format title { romaji } }
      }
    }
  }
}
"""


def fetch_my_anime_list(token: str, client: httpx.Client | None = None) -> list[dict]:
    """B.11d — the viewer's entire AniList anime list in one call
    (`type: ANIME` — manga is never requested at all, LCARS has no
    place for it, §5.1), grouped by status/custom-list on AniList's
    side but flattened here into one list, deduplicated by media id
    (an entry appearing in more than one of the viewer's own custom
    lists would otherwise be double-counted — verified live: this
    particular account has none, but nothing guarantees that for every
    account). `format` can be null (verified live — a real entry on
    this account has one); callers decide the fallback, this just
    passes it through as-is.

    `progress` added 2026-08-12 (B.15) — the reconciliation gap found
    live the same day (§6.9/reverse-sync note, `BUILD_PLAN.md`'s
    parked drift-detection bullet): a show's episode-watched state in
    LCARS can silently diverge from what AniList actually shows,
    entirely independent of `status` above. Purely additive — every
    existing caller (`show_backfill.py`) reads this dict by key and
    ignores keys it doesn't ask for, confirmed before adding this."""
    viewer_id = fetch_viewer_id(token, client=client)
    data = _graphql_request(_MY_ANIME_LIST_QUERY, {"userId": viewer_id}, token=token, client=client)
    by_id: dict[int, dict] = {}
    for lst in data["MediaListCollection"]["lists"]:
        for entry in lst["entries"]:
            media = entry["media"]
            by_id[media["id"]] = {
                "anilist_id": media["id"],
                "format": media["format"],
                "status": entry["status"],
                "progress": entry.get("progress") or 0,
                "title": media["title"]["romaji"],
            }
    return list(by_id.values())


_LATEST_ACTIVITY_QUERY = """
query ($userId: Int) {
  Page(page: 1, perPage: 1) {
    activities(userId: $userId, type: ANIME_LIST, sort: ID_DESC) {
      ... on ListActivity { id createdAt }
    }
  }
}
"""


def fetch_latest_activity_marker(
    token: str, viewer_id: int, client: httpx.Client | None = None
) -> tuple[int, int] | None:
    """B.5.3 — the single most recent `ANIME_LIST` activity-feed entry's
    `(id, createdAt)`, or None if the viewer has none at all. The seed
    value for `poll_anilist_activity`'s (watch_reconcile.py) very first
    call: confirmed live against the user's real account (2026-08-13)
    that activity history runs 5000+ entries deep, so a first poll must
    seed straight to "everything up to right now" rather than walking
    that whole history — same seed-and-skip shape
    `availability.py`'s own `_poll_sonarr` already established for a
    never-before-polled service, one cheap `perPage: 1` call instead of
    availability's own "just seed to wall-clock now," since AniList's
    activity feed has no equivalent of "now" beyond its own latest real
    entry.

    `sort: ID_DESC` — confirmed via schema introspection this is the
    only descending option AniList's `ActivitySort` enum offers
    (`ID`/`ID_DESC`/`PINNED`, not `ID_ASC`/`ID_DESC` as might be
    assumed); `fetch_activity_feed` below uses plain `ID` (ascending)
    for its own incremental walk."""
    data = _graphql_request(
        _LATEST_ACTIVITY_QUERY, {"userId": viewer_id}, token=token, client=client
    )
    activities = data["Page"]["activities"]
    if not activities:
        return None
    latest = activities[0]
    return (latest["id"], latest["createdAt"])


_ACTIVITY_FEED_QUERY = """
query ($userId: Int, $since: Int, $page: Int) {
  Page(page: $page, perPage: 50) {
    pageInfo { hasNextPage }
    activities(userId: $userId, type: ANIME_LIST, createdAt_greater: $since, sort: ID) {
      ... on ListActivity { id createdAt }
    }
  }
}
"""


def fetch_activity_feed(
    token: str,
    viewer_id: int,
    since_id: int,
    since_created_at: int,
    client: httpx.Client | None = None,
) -> list[dict]:
    """B.5.3 — every `ANIME_LIST` activity-feed entry strictly newer
    than `(since_id, since_created_at)`, oldest-first, as
    `{"id": int, "created_at": int}` dicts. Deliberately doesn't fetch
    `status`/`progress`/`media` — this function is only ever used as a
    cheap "did anything change" trigger (watch_reconcile.py's
    `poll_anilist_activity`), which reuses the existing, already-tested
    `reconcile_watch_progress()` to actually apply anything rather than
    parsing AniList's activity `status`/`progress` strings itself.

    `createdAt_greater` is queried as `since_created_at - 1`, not
    `since_created_at` directly — confirmed live (2026-08-13, real
    account) that several real activities can share one `createdAt`
    (whole-second resolution); a strict server-side `greater than` on
    the timestamp alone risks silently skipping same-second entries
    this caller hasn't actually seen yet. The `id > since_id` filter
    below is what actually excludes already-processed entries — `id`
    is confirmed monotonically increasing with creation order under
    `sort: ID`, so it's the real cursor; `created_at` only narrows the
    server-side query cheaply.

    Walks every page while AniList reports `hasNextPage` — real-world
    cadence confirmed live is bursty in minutes during an active
    watching session, hours apart otherwise, so more than one page
    (50 entries) landing between polls should be rare, but isn't
    assumed impossible."""
    since_query = max(0, since_created_at - 1)
    new_activities: list[dict] = []
    page = 1
    while True:
        data = _graphql_request(
            _ACTIVITY_FEED_QUERY,
            {"userId": viewer_id, "since": since_query, "page": page},
            token=token,
            client=client,
        )
        page_data = data["Page"]
        for activity in page_data["activities"]:
            if activity["id"] > since_id:
                new_activities.append({"id": activity["id"], "created_at": activity["createdAt"]})
        if not page_data["pageInfo"]["hasNextPage"]:
            break
        page += 1
    return new_activities


def exchange_code(
    client_id: str,
    client_secret: str,
    code: str,
    client: httpx.Client | None = None,
) -> str:
    """The authorization code from `authorize_url()`'s PIN page ->
    a real access token. Used once, interactively, by `lcars
    anilist-login` (cli.py) — not part of any resolver's own request
    path."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.post(
                TOKEN_URL,
                json={
                    "grant_type": "authorization_code",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": PIN_REDIRECT_URI,
                    "code": code,
                },
                headers={"Accept": "application/json"},
            )
        except httpx.ConnectError as e:
            raise AniListError("Could not connect to AniList") from e
        except httpx.TimeoutException as e:
            raise AniListError("Timed out talking to AniList") from e

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            if payload and ("error" in payload or "message" in payload):
                detail = payload.get("message") or payload.get("error")
                raise AniListError(f"AniList rejected the code exchange: {detail}")
            raise AniListError(f"AniList returned an error: HTTP {response.status_code}")

        if not payload or "access_token" not in payload:
            raise AniListError("AniList's token response didn't include an access_token")
        return payload["access_token"]
    finally:
        if owns_client:
            client.close()


def save_media_list_entry(
    token: str,
    anilist_id: int,
    status: str | None = None,
    score: float | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """§6.1/§6.8 — the actual push. `status`/`score` are each omitted
    from the mutation's variables (not just passed as null) unless
    explicitly given — same reasoning as Data's own
    save_media_list_entry(): GraphQL treats an explicit null for an
    optional argument as "unset this", not "leave it alone", so a
    score-only push must never accidentally reset status (and vice
    versa)."""
    fields = []
    variables: dict[str, object] = {"mediaId": anilist_id}
    if status is not None:
        fields.append("$status: MediaListStatus")
        variables["status"] = status
    if score is not None:
        fields.append("$score: Float")
        variables["score"] = score
    var_defs = ", ".join(["$mediaId: Int", *fields])
    args = ", ".join(
        ["mediaId: $mediaId"]
        + (["status: $status"] if status is not None else [])
        + (["score: $score"] if score is not None else [])
    )
    mutation = f"""
    mutation ({var_defs}) {{
      SaveMediaListEntry({args}) {{
        status
        score
      }}
    }}
    """
    data = _graphql_request(mutation, variables, token=token, client=client)
    return data["SaveMediaListEntry"]
