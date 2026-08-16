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
            # B.21 — real content attached (not just a bare status), so
            # _post's own validation-error-detail extraction (a real
            # e.response.json() call) has something real to parse in
            # tests, not just this fake's own separate .json() method.
            response = httpx.Response(self.status_code, request=request, json=self._payload)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )

    def json(self):
        return self._payload


class _FakeClient:
    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    def get(self, path, params=None):
        self.calls.append(("GET", path, params))
        if self._error is not None:
            raise self._error
        return self._response

    def post(self, path, json=None):
        self.calls.append(("POST", path, json))
        if self._error is not None:
            raise self._error
        return self._response

    def put(self, path, json=None):
        self.calls.append(("PUT", path, json))
        if self._error is not None:
            raise self._error
        return self._response


def test_series_by_tvdb_id_returns_the_match():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 42, "title": "Golden Kamuy"}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    result = client.series_by_tvdb_id(67890)
    assert result == {"id": 42, "title": "Golden Kamuy"}
    assert fake.calls == [("GET", "series", {"tvdbId": 67890})]


def test_series_by_tvdb_id_returns_none_when_not_in_library():
    fake = _FakeClient(response=_FakeResponse(payload=[]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    assert client.series_by_tvdb_id(67890) is None


def test_episodes_returns_the_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"episodeNumber": 1}, {"episodeNumber": 2}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    result = client.episodes(42)
    assert len(result) == 2
    assert fake.calls == [("GET", "episode", {"seriesId": 42})]


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


# --- B.21 — search/add/update ------------------------------------------------


def test_lookup_series_returns_the_candidate_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"tvdbId": 421855, "title": "x"}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    result = client.lookup_series("tvdb:421855")
    assert result == [{"tvdbId": 421855, "title": "x"}]
    assert fake.calls == [("GET", "series/lookup", {"term": "tvdb:421855"})]


def test_root_folders_returns_the_raw_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"path": "/data/media/anime"}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    assert client.root_folders() == [{"path": "/data/media/anime"}]
    assert fake.calls == [("GET", "rootfolder", None)]


def test_quality_profiles_returns_the_raw_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 9, "name": "[Anime] Remux-1080p"}]))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    assert client.quality_profiles() == [{"id": 9, "name": "[Anime] Remux-1080p"}]
    assert fake.calls == [("GET", "qualityprofile", None)]


def test_add_series_posts_the_payload():
    fake = _FakeClient(response=_FakeResponse(payload={"id": 42, "title": "x"}))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    payload = {"tvdbId": 421855, "title": "x", "rootFolderPath": "/data/media/anime"}
    result = client.add_series(payload)
    assert result == {"id": 42, "title": "x"}
    assert fake.calls == [("POST", "series", payload)]


def test_update_series_puts_the_full_object_to_its_own_id():
    fake = _FakeClient(response=_FakeResponse(payload={"id": 42, "monitored": False}))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    series = {"id": 42, "monitored": False, "seasons": []}
    result = client.update_series(series)
    assert result == {"id": 42, "monitored": False}
    assert fake.calls == [("PUT", "series/42", series)]


def test_add_series_surfaces_sonarr_validation_error_detail():
    """A 400 on add comes back as a JSON array of {propertyName,
    errorMessage} objects, not a single message — the detail should be
    surfaced, not just the bare HTTP status."""
    error_body = [
        {"propertyName": "RootFolderPath", "errorMessage": "Root folder does not exist"}
    ]
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload=error_body))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="Root folder does not exist"):
        client.add_series({"tvdbId": 1})


def test_add_series_falls_back_to_bare_status_with_no_parseable_detail():
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload=None))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="HTTP 400"):
        client.add_series({"tvdbId": 1})


def test_add_series_raises_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="rejected the API key"):
        client.add_series({"tvdbId": 1})


def test_add_series_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="Could not connect"):
        client.add_series({"tvdbId": 1})


def test_update_series_raises_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = sonarr_client.SonarrClient("http://sonarr:8989", "key", client=fake)
    with pytest.raises(sonarr_client.SonarrError, match="rejected the API key"):
        client.update_series({"id": 1})
