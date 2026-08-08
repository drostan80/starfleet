"""Radarr metadata fetch — SCOPE.md §5.1, BUILD_PLAN.md A.8.

Pure client-layer coverage: no real network calls, an injected fake
httpx.Client stands in throughout. Same shape as test_sonarr_client.py
— radarr_client.py is a fresh build (no aniq/Data reference exists),
but follows the same *arr-family REST conventions sonarr_client.py
already validated against Data's own real client.
"""

import httpx
import pytest

from lcars import radarr_client


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://radarr.example/api/v3/movie")
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=httpx.Response(
                    self.status_code, request=request
                )
            )

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if self._error is not None:
            raise self._error
        return self._response


def test_movie_by_tmdb_id_returns_the_match():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 42, "title": "A Silent Voice"}]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    result = client.movie_by_tmdb_id(555)
    assert result == {"id": 42, "title": "A Silent Voice"}
    assert fake.calls == [("movie", {"tmdbId": 555})]


def test_movie_by_tmdb_id_returns_none_when_not_in_library():
    fake = _FakeClient(response=_FakeResponse(payload=[]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    assert client.movie_by_tmdb_id(555) is None


def test_raises_specific_message_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="rejected the API key"):
        client.movie_by_tmdb_id(1)


def test_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="Could not connect"):
        client.movie_by_tmdb_id(1)


def test_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="Timed out"):
        client.movie_by_tmdb_id(1)
