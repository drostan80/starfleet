"""Jellyfin: find shows and movies, read a user's played marks, write them (2026-10-10).

Only what `jellyfin_sync` needs. Verified against the running server's own OpenAPI document
(Jellyfin 12.1.0): the key goes in an `Authorization: MediaBrowser Token="<key>"` header; played
marks are `POST` / `DELETE /UserPlayedItems/{itemId}?userId=&datePlayed=`; the series listing has
no filter by provider id in this version, so `/Items` is read whole (with `ProviderIds`) and
matched here.

Writes go through `_post` / `_delete`, which respect the external-writes capture mode like the
Sonarr, Radarr, AniList and MAL writes do.
"""

import httpx


class JellyfinError(Exception):
    pass


class JellyfinClient:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = 30.0,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(
            base_url=self.base_url,
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "JellyfinClient":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def _send(self, method: str, path: str, params: dict | None = None):
        try:
            response = self._client.request(method, path, params=params)
            response.raise_for_status()
        except httpx.ConnectError as e:
            raise JellyfinError(f"Could not connect to Jellyfin at {self.base_url}") from e
        except httpx.HTTPStatusError as e:
            status = e.response.status_code
            if status in (401, 403):
                raise JellyfinError(f"Jellyfin rejected the API key (HTTP {status})") from e
            raise JellyfinError(f"Jellyfin returned an error: HTTP {status}") from e
        except httpx.TimeoutException as e:
            raise JellyfinError(f"Timed out talking to Jellyfin at {self.base_url}") from e
        return response

    def _get(self, path: str, params: dict | None = None):
        return self._send("GET", path, params).json()

    def _post(self, path: str, params: dict | None = None) -> None:
        from lcars import external_writes

        if external_writes.capturing():  # recorded, not sent (PLAN-CODE 9.0)
            external_writes.arr_write("jellyfin", "POST", path, None, params)
            return
        self._send("POST", path, params)

    def _delete(self, path: str, params: dict | None = None) -> None:
        from lcars import external_writes

        if external_writes.capturing():
            external_writes.arr_write("jellyfin", "DELETE", path, None, params)
            return
        self._send("DELETE", path, params)

    # ── reads ────────────────────────────────────────────────────────────────

    def user_id(self, user: str) -> str:
        """The id of the Jellyfin user by name (or the id itself)."""
        users = self._get("/Users")
        for u in users:
            if user.lower() in (str(u.get("Name", "")).lower(), str(u.get("Id", "")).lower()):
                return u["Id"]
        raise JellyfinError(f"no Jellyfin user named {user!r}")

    def _listing(self, item_type: str, user_id: str, played_data: bool) -> list[dict]:
        items, start = [], 0
        while True:
            page = self._get("/Items", {
                "includeItemTypes": item_type, "recursive": "true", "userId": user_id,
                "fields": "ProviderIds", "enableUserData": "true" if played_data else "false",
                "enableImages": "false", "limit": 500, "startIndex": start,
            })
            batch = page.get("Items", [])
            items += batch
            start += len(batch)
            if not batch or start >= page.get("TotalRecordCount", start):
                return items

    def all_series(self, user_id: str) -> list[dict]:
        """Every series with its ProviderIds ({"Tvdb": "...", "Tmdb": "..."})."""
        return self._listing("Series", user_id, played_data=False)

    def all_movies(self, user_id: str) -> list[dict]:
        """Every movie with its ProviderIds and the user's played mark (`UserData.Played`)."""
        return self._listing("Movie", user_id, played_data=True)

    def series_episodes(self, series_id: str, user_id: str) -> list[dict]:
        """A series' episodes with ParentIndexNumber / IndexNumber and `UserData.Played`."""
        return self._get(f"/Shows/{series_id}/Episodes", {
            "userId": user_id, "enableUserData": "true", "fields": "ProviderIds",
        }).get("Items", [])

    # ── writes ───────────────────────────────────────────────────────────────

    def mark_played(self, item_id: str, user_id: str, date_played: str | None = None) -> None:
        params = {"userId": user_id}
        if date_played:
            params["datePlayed"] = date_played
        self._post(f"/UserPlayedItems/{item_id}", params)

    def mark_unplayed(self, item_id: str, user_id: str) -> None:
        self._delete(f"/UserPlayedItems/{item_id}", {"userId": user_id})
