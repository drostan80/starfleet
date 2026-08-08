"""Sonarr metadata fetch — SCOPE.md §5.1/§5.2, BUILD_PLAN.md A.8.

Pure client-layer coverage: no real network calls, an injected fake
httpx.Client stands in throughout.
"""

import httpx
import pytest

from lcars import sonarr_client


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://sonarr.example/api/v3/series")
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


def test_series_by_tvdb_id_returns_the_match():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 42, "title": "Golden Kamuy"}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    result = client.series_by_tvdb_id(67890)
    assert result == {"id": 42, "title": "Golden Kamuy"}
    assert fake.calls == [("series", {"tvdbId": 67890})]


def test_series_by_tvdb_id_returns_none_when_not_in_library():
    fake = _FakeClient(response=_FakeResponse(payload=[]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    assert client.series_by_tvdb_id(67890) is None


def test_episodes_returns_the_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"episodeNumber": 1}, {"episodeNumber": 2}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    result = client.episodes(42)
    assert len(result) == 2
    assert fake.calls == [("episode", {"seriesId": 42})]


def test_raises_specific_message_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="rejected the API key"):
        client.series_by_tvdb_id(1)


def test_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="Could not connect"):
        client.series_by_tvdb_id(1)


def test_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="Timed out"):
        client.series_by_tvdb_id(1)
