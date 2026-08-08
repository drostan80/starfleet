"""AniList metadata fetch — SCOPE.md §5.1/§5.4/§5.5, BUILD_PLAN.md A.8.

Pure client-layer coverage: no real network calls, an injected fake
httpx.Client stands in throughout — same pattern as
tests/test_fribb.py's _FakeClient.
"""

import httpx
import pytest

from lcars import anilist_client


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
        self.calls = 0
        self.last_variables = None

    def post(self, url, json):
        self.calls += 1
        self.last_variables = json.get("variables")
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        pass


def test_fetch_media_returns_the_media_object():
    media = {"title": {"romaji": "Golden Kamuy"}, "idMal": 999}
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": media}}))
    result = anilist_client.fetch_media(123, client=fake)
    assert result == media
    assert fake.last_variables == {"mediaId": 123}


def test_fetch_media_returns_none_for_an_unknown_id():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": None}}))
    assert anilist_client.fetch_media(999999, client=fake) is None


def test_fetch_media_raises_on_graphql_errors():
    fake = _FakeClient(
        response=_FakeResponse(payload={"errors": [{"message": "Invalid mediaId"}]})
    )
    with pytest.raises(anilist_client.AniListError, match="Invalid mediaId"):
        anilist_client.fetch_media(123, client=fake)


def test_fetch_media_raises_on_http_error_status():
    fake = _FakeClient(response=_FakeResponse(status_code=500, payload=None))
    with pytest.raises(anilist_client.AniListError, match="HTTP 500"):
        anilist_client.fetch_media(123, client=fake)


def test_fetch_media_raises_on_connect_error():
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    with pytest.raises(anilist_client.AniListError, match="Could not connect"):
        anilist_client.fetch_media(123, client=fake)


def test_fetch_media_raises_on_timeout():
    fake = _FakeClient(error=httpx.TimeoutException("boom"))
    with pytest.raises(anilist_client.AniListError, match="Timed out"):
        anilist_client.fetch_media(123, client=fake)
