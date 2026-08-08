"""SCOPE.md §5.1, BUILD_PLAN.md A.19 — pure client-layer coverage, no
real network calls, an injected fake httpx.Client stands in throughout
(same shape as test_radarr_client.py/test_sonarr_client.py). Unlike
those two, TMDB's own by-id endpoints return a real 404 for an unknown
id rather than 200+empty-list, so the fake response models status_code
directly rather than raise_for_status()."""

import httpx
import pytest

from lcars import tmdb_client


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload

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


def test_find_by_tvdb_id_returns_the_match():
    fake = _FakeClient(response=_FakeResponse(payload={"tv_results": [{"id": 42}]}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.find_by_tvdb_id(67890) == 42
    assert fake.calls == [("find/67890", {"external_source": "tvdb_id", "api_key": "key"})]


def test_find_by_tvdb_id_returns_none_when_no_tv_match():
    fake = _FakeClient(response=_FakeResponse(payload={"tv_results": []}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.find_by_tvdb_id(67890) is None


def test_movie_runtime_returns_the_value():
    fake = _FakeClient(response=_FakeResponse(payload={"runtime": 112}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.movie_runtime(555) == 112
    assert fake.calls == [("movie/555", {"api_key": "key"})]


def test_movie_runtime_none_when_unset():
    fake = _FakeClient(response=_FakeResponse(payload={"runtime": None}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.movie_runtime(555) is None


def test_tv_episode_runtime_returns_the_last_entry():
    # TMDB's own list-shaped field (runtime can change across a long
    # show's history) — the most recent entry wins, see the client's
    # own docstring.
    fake = _FakeClient(response=_FakeResponse(payload={"episode_run_time": [24, 22]}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.tv_episode_runtime(999) == 22


def test_tv_episode_runtime_none_when_empty():
    fake = _FakeClient(response=_FakeResponse(payload={"episode_run_time": []}))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.tv_episode_runtime(999) is None


def test_returns_none_on_404():
    fake = _FakeClient(response=_FakeResponse(status_code=404))
    client = tmdb_client.TmdbClient("key", client=fake)
    assert client.movie_runtime(555) is None
    assert client.find_by_tvdb_id(1) is None


def test_raises_specific_message_on_401():
    fake = _FakeClient(response=_FakeResponse(status_code=401))
    client = tmdb_client.TmdbClient("key", client=fake)
    with pytest.raises(tmdb_client.TmdbError, match="rejected the API key"):
        client.movie_runtime(555)


def test_raises_on_other_http_error():
    fake = _FakeClient(response=_FakeResponse(status_code=500))
    client = tmdb_client.TmdbClient("key", client=fake)
    with pytest.raises(tmdb_client.TmdbError, match="HTTP 500"):
        client.movie_runtime(555)


def test_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    client = tmdb_client.TmdbClient("key", client=fake)
    with pytest.raises(tmdb_client.TmdbError, match="Could not connect"):
        client.movie_runtime(555)


def test_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    client = tmdb_client.TmdbClient("key", client=fake)
    with pytest.raises(tmdb_client.TmdbError, match="Timed out"):
        client.movie_runtime(555)
