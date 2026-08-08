"""AniList metadata fetch — SCOPE.md §5.1/§5.4/§5.5, BUILD_PLAN.md A.8.

Public, unauthenticated GraphQL endpoint — fetching a `Media` by id for
display metadata isn't tied to a specific viewer, same as Data's own
real `search_anime()` (`~/repos/data/src/data/anilist.py`), which this
module's error-handling shape is ported from (its `AniListClient`
itself, used for the *authenticated* mediaListEntry/write paths, isn't
relevant here — A.8 never pushes to a user's list, only reads public
Media data). Adapted to LCARS's own sync execution model (§11.2) — a
sync `httpx.Client`, not Data's `httpx.AsyncClient`.
"""

import httpx

GRAPHQL_URL = "https://graphql.anilist.co"

_MEDIA_QUERY = """
query ($mediaId: Int) {
  Media(id: $mediaId) {
    title { romaji english native }
    coverImage { large }
    bannerImage
    description(asHtml: false)
    genres
    episodes
    idMal
    studios(isMain: true) { nodes { id name } }
    characters(sort: [ROLE, RELEVANCE], perPage: 25) {
      edges {
        role
        node { id name { full } }
        voiceActors(language: JAPANESE) { id name { full } }
      }
    }
  }
}
"""


class AniListError(Exception):
    pass


def fetch_media(anilist_id: int, client: httpx.Client | None = None) -> dict | None:
    """The raw `Media` object (or None if AniList has no such id) for
    `anilist_id` — title variants, cover/banner, synopsis, genres,
    episode count, the companion MAL id, main studio(s), and voice
    cast. Raises AniListError on a network/HTTP/GraphQL-level failure
    rather than returning None — callers (metadata.py) need to tell
    "no such id" apart from "AniList was unreachable" (§3's best-effort
    policy treats them differently: the latter is worth a retry, the
    former isn't)."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.post(
                GRAPHQL_URL, json={"query": _MEDIA_QUERY, "variables": {"mediaId": anilist_id}}
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
            raise AniListError(f"AniList GraphQL error: {messages}")
        if response.status_code >= 400:
            raise AniListError(f"AniList returned an error: HTTP {response.status_code}")

        return payload["data"]["Media"]
    finally:
        if owns_client:
            client.close()
