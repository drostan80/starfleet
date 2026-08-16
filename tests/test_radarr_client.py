"""Radarr metadata fetch (+ write, B.21) — SCOPE.md §5.1, BUILD_PLAN.md A.8/B.21.

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
            # B.21 — real content attached, not just a bare status, so
            # _post's own validation-error-detail extraction has
            # something real to parse in tests.
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


def test_movie_by_tmdb_id_returns_the_match():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 42, "title": "A Silent Voice"}]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    result = client.movie_by_tmdb_id(555)
    assert result == {"id": 42, "title": "A Silent Voice"}
    assert fake.calls == [("GET", "movie", {"tmdbId": 555})]


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


# --- B.21 — search/add/update ------------------------------------------------


def test_lookup_movie_returns_the_candidate_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"tmdbId": 27205, "title": "x"}]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    result = client.lookup_movie("tmdb:27205")
    assert result == [{"tmdbId": 27205, "title": "x"}]
    assert fake.calls == [("GET", "movie/lookup", {"term": "tmdb:27205"})]


def test_root_folders_returns_the_raw_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"path": "/data/media/movies"}]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    assert client.root_folders() == [{"path": "/data/media/movies"}]
    assert fake.calls == [("GET", "rootfolder", None)]


def test_quality_profiles_returns_the_raw_list():
    fake = _FakeClient(response=_FakeResponse(payload=[{"id": 7, "name": "HD-1080p"}]))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    assert client.quality_profiles() == [{"id": 7, "name": "HD-1080p"}]
    assert fake.calls == [("GET", "qualityprofile", None)]


def test_add_movie_posts_the_payload():
    fake = _FakeClient(response=_FakeResponse(payload={"id": 42, "title": "x"}))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    payload = {"tmdbId": 27205, "title": "x", "rootFolderPath": "/data/media/movies"}
    result = client.add_movie(payload)
    assert result == {"id": 42, "title": "x"}
    assert fake.calls == [("POST", "movie", payload)]


def test_update_movie_puts_the_full_object_to_its_own_id():
    fake = _FakeClient(response=_FakeResponse(payload={"id": 42, "monitored": False}))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    movie = {"id": 42, "monitored": False}
    result = client.update_movie(movie)
    assert result == {"id": 42, "monitored": False}
    assert fake.calls == [("PUT", "movie/42", movie)]


def test_add_movie_surfaces_radarr_validation_error_detail():
    error_body = [{"propertyName": "RootFolderPath", "errorMessage": "Root folder does not exist"}]
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload=error_body))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="Root folder does not exist"):
        client.add_movie({"tmdbId": 1})


def test_add_movie_falls_back_to_bare_status_with_no_parseable_detail():
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload=None))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="HTTP 400"):
        client.add_movie({"tmdbId": 1})


def test_add_movie_raises_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="rejected the API key"):
        client.add_movie({"tmdbId": 1})


def test_add_movie_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="Could not connect"):
        client.add_movie({"tmdbId": 1})


def test_update_movie_raises_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = radarr_client.RadarrClient("http://radarr:7878", "key", client=fake)
    with pytest.raises(radarr_client.RadarrError, match="rejected the API key"):
        client.update_movie({"id": 1})
