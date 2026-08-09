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

import httpx

GRAPHQL_URL = "https://graphql.anilist.co"
AUTHORIZE_URL = "https://anilist.co/api/v2/oauth/authorize"
TOKEN_URL = "https://anilist.co/api/v2/oauth/token"
# AniList's hosted "pin" page — see Data's own anilist.py for the full
# history of why this redirect (not a local callback server, not the
# Implicit Grant) is the one that actually works; unchanged here.
PIN_REDIRECT_URI = "https://anilist.co/api/v2/oauth/pin"

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
    GraphQL-error-body/401 all handled once."""
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
