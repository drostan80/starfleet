"""MyAnimeList OAuth2/PKCE + score/status push — SCOPE.md §5.5/§6.1/§6.9,
BUILD_PLAN.md B.10.

**Registration, verified live 2026-08-10**: unlike AniList (which reuses
Data/aniq's already-registered app), MAL had never been registered in
this project — the user registered a fresh app at
`myanimelist.net/apiconfig` specifically for LCARS, App Type "Other".
**Real correction to SCOPE.md §6.9's original assumption, found live
during registration, not from docs**: §6.9 assumed "client_id +
client_secret on approval" the same shape as AniList's app. MAL's
"Other" app type is a PKCE **public client** and issues no
`client_secret` at all (that's not a UI bug or a hidden field — it's
the standard OAuth2 public-client shape PKCE exists to support:
`code_verifier`/`code_challenge` replace a secret's role for a client
that can't securely store one). `client_secret` stays a config field
here (some app types do get one, and MAL's token endpoint accepts it
if present) but every function in this module treats it as optional —
omitted from a request entirely when not set, never assumed present
the way `anilist_client.py`'s functions do.

Same sync-`httpx.Client`, same connect/timeout/HTTP-status/auth-error
handling shape as `anilist_client.py` — but MAL's real API is REST +
form-encoded bodies (`application/x-www-form-urlencoded`), not
GraphQL/JSON, so every request here uses `data=`, not `json=`.

PKCE specifics, confirmed against MAL's own docs (§6.9's research,
re-verified 2026-08-10 against the live `myanimelist.net/apiconfig`
reference page): `plain` method only (no S256) — the `code_challenge`
sent to `/authorize` is the *same string* as the `code_verifier` later
sent to `/token`, 43-128 characters. `generate_code_verifier()` uses
`secrets.token_urlsafe(64)` (86 chars — comfortably mid-range, no
truncation/padding edge cases to worry about).

**Refresh-token rotation, confirmed live 2026-08-10, not left as a doc
ambiguity**: MAL's own written docs don't clearly state whether the
`refresh_token` grant issues a genuinely new token or just echoes the
one just used, so this was verified directly rather than assumed —
called `refresh_access_token()` for real against the user's own live
tokens and diffed the result: **it does rotate**, a different
`refresh_token` came back from the one sent. That means the proactive
renewal job (`refreshMalTokenIfDue`, resolvers.py) genuinely does
reset the 1-month clock on every successful call, not just mint a
fresh access token — BUILD_PLAN.md's own "build the renewal job now"
premise holds. `refresh_access_token()` still returns whatever
`refresh_token` the response actually contains (falling back to the
token just used if a response ever omitted one) — belt-and-braces
correctness for the rotating case now confirmed as the real one, not
a hedge against genuine uncertainty. `config.save_mal_tokens()`
persists both values on every refresh so the rotated token is never
silently dropped.
"""

import secrets

import httpx

AUTHORIZE_URL = "https://myanimelist.net/v1/oauth2/authorize"
TOKEN_URL = "https://myanimelist.net/v1/oauth2/token"
API_BASE_URL = "https://api.myanimelist.net/v2"
# Registered 2026-08-10 against the user's own MAL app — picked to avoid
# colliding with services already running locally (qBittorrent's own
# default WebUI port is 8080). Nothing needs to actually listen here: MAL
# redirects the browser to this address with ?code=... in the URL, which
# is read straight out of the address bar and pasted in, same "no local
# callback server" shape anilist_client.py's own PIN_REDIRECT_URI uses.
REDIRECT_URI = "http://localhost:1701/"


class MALError(Exception):
    pass


class MALAuthError(MALError):
    """A 401 from the my_list_status push (access token invalid/expired
    beyond what the periodic refresh job should ever let happen), or a
    refresh-grant rejection (the refresh_token itself has expired —
    genuinely needs a fresh interactive `lcars mal-login`, not just
    another refresh attempt). Same reasoning anilist_client.py's own
    AniListAuthError gives: callers can catch this specifically rather
    than string-matching."""


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def authorize_url(client_id: str, code_verifier: str) -> str:
    return (
        f"{AUTHORIZE_URL}?response_type=code&client_id={client_id}"
        f"&code_challenge={code_verifier}&code_challenge_method=plain"
        f"&redirect_uri={REDIRECT_URI}"
    )


def _token_request(data: dict, client: httpx.Client | None) -> dict:
    """Shared POST-to-/token handling for both the authorization_code
    and refresh_token grants below — connect/timeout/HTTP-error/missing-
    token-field all handled once, same shape anilist_client.py's own
    _graphql_request gives its two GraphQL call sites."""
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.post(TOKEN_URL, data=data, headers={"Accept": "application/json"})
        except httpx.ConnectError as e:
            raise MALError("Could not connect to MyAnimeList") from e
        except httpx.TimeoutException as e:
            raise MALError("Timed out talking to MyAnimeList") from e

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code >= 400:
            detail = None
            if payload:
                detail = payload.get("message") or payload.get("error")
            if response.status_code in (400, 401):
                raise MALAuthError(
                    f"MyAnimeList rejected the request — {detail or f'HTTP {response.status_code}'}"
                    "; re-run `lcars mal-login` to reauthorize"
                )
            raise MALError(f"MyAnimeList returned an error: HTTP {response.status_code}")

        if not payload or "access_token" not in payload:
            raise MALError("MyAnimeList's token response didn't include an access_token")
        return payload
    finally:
        if owns_client:
            client.close()


def exchange_code(
    client_id: str,
    client_secret: str | None,
    code: str,
    code_verifier: str,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """The authorization code from `authorize_url()`'s redirect -> a
    real (access_token, refresh_token) pair. Used once, interactively,
    by `lcars mal-login` (cli.py). `client_secret` omitted from the
    request entirely when not set — MAL's own "Other" public-client app
    type issues none at all (module docstring)."""
    data = {
        "client_id": client_id,
        "grant_type": "authorization_code",
        "code": code,
        "code_verifier": code_verifier,
        "redirect_uri": REDIRECT_URI,
    }
    if client_secret:
        data["client_secret"] = client_secret
    payload = _token_request(data, client)
    return payload["access_token"], payload["refresh_token"]


def refresh_access_token(
    client_id: str,
    client_secret: str | None,
    refresh_token: str,
    client: httpx.Client | None = None,
) -> tuple[str, str]:
    """`(access_token, refresh_token)` — the refresh_token returned is
    whatever the response actually contains, falling back to the one
    just used if the response omits it (module docstring's own note on
    MAL's ambiguous rotation behavior — correct under either reading)."""
    data = {
        "client_id": client_id,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }
    if client_secret:
        data["client_secret"] = client_secret
    payload = _token_request(data, client)
    return payload["access_token"], payload.get("refresh_token", refresh_token)


_ANIME_DETAIL_FIELDS = (
    "title,main_picture,synopsis,num_episodes,average_episode_duration,"
    "genres,alternative_titles,start_date,end_date,status,media_type"
)


def fetch_anime_details(
    mal_id: int,
    client_id: str,
    client: httpx.Client | None = None,
) -> dict | None:
    """Public metadata for one anime — `GET /v2/anime/{id}?fields=...`,
    authenticated by `X-MAL-CLIENT-ID` header only (no user OAuth token
    needed). Returns the raw MAL response dict on success, None on 404
    (genuinely nonexistent id). Raises `MALError` on network/server
    errors so the caller can log and handle as appropriate.

    Built for AniList→MAL source failover (2026-09-05): when AniList is
    unreachable, `metadata._fetch_mal_fallback` uses this to fill scalar
    show fields (poster, synopsis, duration) from MAL instead.
    Deliberately does NOT require a user access token — public data is
    available to any registered app's client_id, same as AniList's own
    `fetch_media` which needs no auth either.  The caller does **not**
    record `service_health` for MAL from this path — the public API and
    the OAuth push path are independent failure domains (see
    `_fetch_mal_fallback`'s own docstring).

    Field mapping notes for callers:
    - `average_episode_duration` is in **seconds** (MAL's convention);
      LCARS's `duration_minutes` is minutes — caller must divide by 60.
    - `main_picture.large` is a poster; MAL has no banner equivalent.
    - `genres` uses MAL's own vocabulary, which differs from AniList's.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.get(
                f"{API_BASE_URL}/anime/{mal_id}?fields={_ANIME_DETAIL_FIELDS}",
                headers={"X-MAL-CLIENT-ID": client_id},
            )
        except httpx.ConnectError as e:
            raise MALError("Could not connect to MyAnimeList") from e
        except httpx.TimeoutException as e:
            raise MALError("Timed out talking to MyAnimeList") from e

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json().get("message")
            except (ValueError, AttributeError):
                detail = None
            raise MALError(
                f"MyAnimeList returned an error: HTTP {response.status_code}"
                + (f" ({detail})" if detail else "")
            )
        return response.json()
    finally:
        if owns_client:
            client.close()


_SEASONAL_FIELDS = (
    "id,title,main_picture,synopsis,num_episodes,average_episode_duration,"
    "genres,alternative_titles,start_date,status,media_type,studios"
)


def fetch_seasonal_anime(
    year: int,
    season: str,
    client_id: str,
    page: int = 1,
    per_page: int = 50,
    client: httpx.Client | None = None,
) -> dict:
    """Public seasonal anime listing — ``GET /v2/anime/season/{year}/{season}``,
    authenticated by ``X-MAL-CLIENT-ID`` header only (no user OAuth token).

    MAL's seasonal endpoint uses cursor-based pagination via ``offset`` +
    ``limit`` (not AniList's ``page`` numbers), so ``page`` is translated
    here: page 1 → offset 0, page 2 → offset ``per_page``, etc.

    Returns ``{"data": [...], "paging": {...}}`` — caller maps the MAL
    shapes. ``nsfw=true`` mirrors AniList's unfiltered seasonal query and
    ``fetch_my_list``'s own convention.

    Built for AniList→MAL browse fallback (2026-09-05): when AniList's
    seasonal fetch is unreachable, ``browse.py`` calls this instead.
    """
    offset = (page - 1) * per_page
    url = (
        f"{API_BASE_URL}/anime/season/{year}/{season.lower()}"
        f"?fields={_SEASONAL_FIELDS}"
        f"&sort=anime_num_list_users&limit={per_page}&offset={offset}"
        f"&nsfw=true"
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=15.0)
    try:
        try:
            response = client.get(url, headers={"X-MAL-CLIENT-ID": client_id})
        except httpx.ConnectError as e:
            raise MALError("Could not connect to MyAnimeList") from e
        except httpx.TimeoutException as e:
            raise MALError("Timed out talking to MyAnimeList") from e

        if response.status_code >= 400:
            try:
                detail = response.json().get("message")
            except (ValueError, AttributeError):
                detail = None
            raise MALError(
                f"MyAnimeList returned an error: HTTP {response.status_code}"
                + (f" ({detail})" if detail else "")
            )
        return response.json()
    finally:
        if owns_client:
            client.close()


def search_anime(
    query: str,
    client_id: str,
    limit: int = 10,
    client: httpx.Client | None = None,
) -> list[dict]:
    """Public anime search — ``GET /v2/anime?q=...``, authenticated by
    ``X-MAL-CLIENT-ID`` header only. Returns up to ``limit`` results.

    MAL requires ``q`` to be at least 3 characters. Raises ``MALError``
    if the query is too short or on network/HTTP errors.

    Built for AniList→MAL search fallback (2026-09-05): when AniList's
    ``searchMedia`` is unreachable, ``resolvers.py`` calls this instead.
    """
    if len(query) < 3:
        raise MALError("MAL search requires at least 3 characters")
    url = (
        f"{API_BASE_URL}/anime?q={query}"
        f"&fields={_SEASONAL_FIELDS}"
        f"&limit={limit}&nsfw=true"
    )
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.get(url, headers={"X-MAL-CLIENT-ID": client_id})
        except httpx.ConnectError as e:
            raise MALError("Could not connect to MyAnimeList") from e
        except httpx.TimeoutException as e:
            raise MALError("Timed out talking to MyAnimeList") from e

        if response.status_code >= 400:
            try:
                detail = response.json().get("message")
            except (ValueError, AttributeError):
                detail = None
            raise MALError(
                f"MyAnimeList returned an error: HTTP {response.status_code}"
                + (f" ({detail})" if detail else "")
            )
        payload = response.json()
        return [row["node"] for row in payload.get("data", []) if row.get("node")]
    finally:
        if owns_client:
            client.close()


def update_my_list_status(
    token: str,
    mal_id: int,
    status: str | None = None,
    score: int | None = None,
    num_watched_episodes: int | None = None,
    client: httpx.Client | None = None,
) -> dict:
    """§6.1/§6.9 — the actual push, `PATCH /v2/anime/{mal_id}/my_list_
    status`, form-encoded. `status`/`score` each omitted from the
    request body entirely unless explicitly given, same "an unset
    field is 'leave it alone', not 'clear it'" reasoning
    anilist_client.py's own save_media_list_entry() already
    establishes — a score-only push must never accidentally reset
    status (and vice versa). `is_rewatching`/`num_times_rewatched` are
    deliberately never sent here at all (§6.8/§6.9: rewatching never
    auto-toggles either).

    `num_watched_episodes` added 2026-08-26 (MAL bidirectional sync) —
    same omit-unless-given shape; MAL's own field name for episode
    progress, the counterpart to AniList's `progress`."""
    data: dict[str, object] = {}
    if status is not None:
        data["status"] = status
    if score is not None:
        data["score"] = score
    if num_watched_episodes is not None:
        data["num_watched_episodes"] = num_watched_episodes
    owns_client = client is None
    client = client or httpx.Client(timeout=10.0)
    try:
        try:
            response = client.patch(
                f"{API_BASE_URL}/anime/{mal_id}/my_list_status",
                data=data,
                headers={"Authorization": f"Bearer {token}"},
            )
        except httpx.ConnectError as e:
            raise MALError("Could not connect to MyAnimeList") from e
        except httpx.TimeoutException as e:
            raise MALError("Timed out talking to MyAnimeList") from e

        try:
            payload = response.json()
        except ValueError:
            payload = None

        if response.status_code == 401:
            raise MALAuthError(
                "MyAnimeList rejected the access token (401 Unauthorized) — it may "
                "have expired; the periodic refresh job should catch this "
                "automatically, but if it persists re-run `lcars mal-login`"
            )
        if response.status_code >= 400:
            detail = (payload or {}).get("message") if payload else None
            raise MALError(
                f"MyAnimeList returned an error: HTTP {response.status_code}"
                + (f" ({detail})" if detail else "")
            )
        return payload or {}
    finally:
        if owns_client:
            client.close()


def fetch_my_list(token: str, client: httpx.Client | None = None) -> list[dict]:
    """The authenticated user's entire MAL anime list — the read side of
    the MAL bidirectional sync (2026-08-26), counterpart to
    `anilist_client.fetch_my_anime_list`. `GET /users/@me/animelist`
    with `fields=list_status` (which yields status/score/
    num_episodes_watched), following `paging.next` to the end (MAL caps
    a page at 1000; a personal list can exceed it, so paging isn't
    optional). Returns one flat dict per entry:
    `{mal_id, status, score, num_watched_episodes}` — `status`/`score`
    left in MAL's own vocabulary/scale (caller normalizes), matching
    `fetch_my_anime_list`'s pass-through convention.

    MAL has no activity-feed equivalent to AniList's, so there's no cheap
    "did anything change" pre-check here — the caller polls the whole
    list on a cadence and diffs (mal_reconcile.py)."""
    owns_client = client is None
    client = client or httpx.Client(timeout=30.0)
    url = f"{API_BASE_URL}/users/@me/animelist?fields=list_status&limit=1000&nsfw=true"
    entries: list[dict] = []
    try:
        while url:
            try:
                response = client.get(url, headers={"Authorization": f"Bearer {token}"})
            except httpx.ConnectError as e:
                raise MALError("Could not connect to MyAnimeList") from e
            except httpx.TimeoutException as e:
                raise MALError("Timed out talking to MyAnimeList") from e
            if response.status_code == 401:
                raise MALAuthError(
                    "MyAnimeList rejected the access token (401 Unauthorized) — it may "
                    "have expired; the periodic refresh job should catch this automatically"
                )
            if response.status_code >= 400:
                raise MALError(f"MyAnimeList returned an error: HTTP {response.status_code}")
            payload = response.json()
            for row in payload.get("data", []):
                node = row.get("node") or {}
                status_obj = row.get("list_status") or {}
                if node.get("id") is None:
                    continue
                entries.append(
                    {
                        "mal_id": node["id"],
                        "status": status_obj.get("status"),
                        "score": status_obj.get("score"),
                        "num_watched_episodes": status_obj.get("num_episodes_watched") or 0,
                    }
                )
            url = (payload.get("paging") or {}).get("next")
        return entries
    finally:
        if owns_client:
            client.close()
