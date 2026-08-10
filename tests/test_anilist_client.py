"""AniList metadata fetch + push — SCOPE.md §5.1/§5.4/§5.5/§6.1/§6.8,
BUILD_PLAN.md A.8/A.9.

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
        self.last_headers = None
        self.last_json = None

    def post(self, url, json=None, headers=None):
        self.calls += 1
        self.last_json = json
        self.last_headers = headers
        if json is not None:
            self.last_variables = json.get("variables")
        if self._error is not None:
            raise self._error
        return self._response

    def close(self):
        pass


class _SequencedFakeClient:
    """Like _FakeClient above, but for fetch_my_anime_list() (B.11d),
    which makes two real calls (Viewer, then MediaListCollection) —
    a plain single-response fake can't stand in for that."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def post(self, url, json=None, headers=None):
        self.calls.append(json)
        return self._responses.pop(0)

    def close(self):
        pass


# --- fetch_media (A.8) -------------------------------------------------------


def test_fetch_media_returns_the_media_object():
    media = {"title": {"romaji": "Golden Kamuy"}, "idMal": 999}
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": media}}))
    result = anilist_client.fetch_media(123, client=fake)
    assert result == media
    assert fake.last_variables == {"mediaId": 123}
    assert fake.last_headers == {}  # unauthenticated — no token for this call


def test_fetch_media_returns_none_for_an_unknown_id():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": None}}))
    assert anilist_client.fetch_media(999999, client=fake) is None


def test_fetch_media_raises_on_graphql_errors():
    fake = _FakeClient(response=_FakeResponse(payload={"errors": [{"message": "Invalid mediaId"}]}))
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


# --- fetch_airing_schedule (B.4) ----------------------------------------------


def test_fetch_airing_schedule_returns_episode_count_and_nodes():
    nodes = [{"episode": 1, "airingAt": 1700000000}, {"episode": 2, "airingAt": 1700604800}]
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Media": {"episodes": 12, "airingSchedule": {"nodes": nodes}}}}
        )
    )
    result = anilist_client.fetch_airing_schedule(123, client=fake)
    assert result == {"episodes": 12, "nodes": nodes}
    assert fake.last_variables == {"mediaId": 123}


def test_fetch_airing_schedule_returns_none_for_an_unknown_id():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": None}}))
    assert anilist_client.fetch_airing_schedule(999999, client=fake) is None


def test_fetch_airing_schedule_raises_on_graphql_errors():
    fake = _FakeClient(response=_FakeResponse(payload={"errors": [{"message": "Invalid mediaId"}]}))
    with pytest.raises(anilist_client.AniListError, match="Invalid mediaId"):
        anilist_client.fetch_airing_schedule(123, client=fake)


# --- fetch_my_list_status (B.11d) ----------------------------------------------


def test_fetch_my_list_status_returns_the_viewer_status():
    payload = {"data": {"Media": {"mediaListEntry": {"status": "CURRENT"}}}}
    fake = _FakeClient(response=_FakeResponse(payload=payload))
    result = anilist_client.fetch_my_list_status("tok", 123, client=fake)
    assert result == "CURRENT"
    assert fake.last_variables == {"mediaId": 123}
    assert fake.last_headers == {"Authorization": "Bearer tok"}  # authenticated, unlike fetch_media


def test_fetch_my_list_status_returns_none_with_no_list_entry():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"Media": {"mediaListEntry": None}}})
    )
    assert anilist_client.fetch_my_list_status("tok", 123, client=fake) is None


def test_fetch_my_list_status_returns_none_for_an_unknown_id():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": None}}))
    assert anilist_client.fetch_my_list_status("tok", 999999, client=fake) is None


def test_fetch_my_list_status_raises_on_graphql_errors():
    fake = _FakeClient(response=_FakeResponse(payload={"errors": [{"message": "Invalid mediaId"}]}))
    with pytest.raises(anilist_client.AniListError, match="Invalid mediaId"):
        anilist_client.fetch_my_list_status("tok", 123, client=fake)


# --- fetch_viewer_id / fetch_my_anime_list (B.11d) ------------------------------


def test_fetch_viewer_id_returns_the_numeric_id():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Viewer": {"id": 24011}}}))
    assert anilist_client.fetch_viewer_id("tok", client=fake) == 24011


def test_fetch_my_anime_list_flattens_every_list_into_one():
    viewer_response = _FakeResponse(payload={"data": {"Viewer": {"id": 24011}}})
    collection_response = _FakeResponse(
        payload={
            "data": {
                "MediaListCollection": {
                    "lists": [
                        {
                            "entries": [
                                {
                                    "status": "COMPLETED",
                                    "media": {"id": 101, "format": "TV", "title": {"romaji": "A"}},
                                }
                            ]
                        },
                        {
                            "entries": [
                                {
                                    "status": "CURRENT",
                                    "media": {
                                        "id": 102,
                                        "format": "MOVIE",
                                        "title": {"romaji": "B"},
                                    },
                                }
                            ]
                        },
                    ]
                }
            }
        }
    )
    fake = _SequencedFakeClient([viewer_response, collection_response])
    result = anilist_client.fetch_my_anime_list("tok", client=fake)
    assert result == [
        {"anilist_id": 101, "format": "TV", "status": "COMPLETED", "title": "A"},
        {"anilist_id": 102, "format": "MOVIE", "status": "CURRENT", "title": "B"},
    ]
    # userId came from the Viewer call's own id, not hardcoded
    assert fake.calls[1]["variables"] == {"userId": 24011}


def test_fetch_my_anime_list_deduplicates_an_entry_appearing_in_two_lists():
    viewer_response = _FakeResponse(payload={"data": {"Viewer": {"id": 24011}}})
    entry = {
        "status": "COMPLETED",
        "media": {"id": 101, "format": "TV", "title": {"romaji": "A"}},
    }
    collection_response = _FakeResponse(
        payload={
            "data": {"MediaListCollection": {"lists": [{"entries": [entry]}, {"entries": [entry]}]}}
        }
    )
    fake = _SequencedFakeClient([viewer_response, collection_response])
    result = anilist_client.fetch_my_anime_list("tok", client=fake)
    assert len(result) == 1


# --- OAuth (A.9) --------------------------------------------------------------


def test_authorize_url_includes_client_id_and_pin_redirect():
    url = anilist_client.authorize_url("42")
    assert "client_id=42" in url
    assert "redirect_uri=https://anilist.co/api/v2/oauth/pin" in url
    assert "response_type=code" in url


def test_exchange_code_returns_the_access_token():
    fake = _FakeClient(response=_FakeResponse(payload={"access_token": "tok-123"}))
    token = anilist_client.exchange_code("cid", "csecret", "authcode", client=fake)
    assert token == "tok-123"
    assert fake.last_json["code"] == "authcode"
    assert fake.last_json["client_id"] == "cid"


def test_exchange_code_raises_with_detail_on_rejection():
    fake = _FakeClient(response=_FakeResponse(status_code=400, payload={"error": "invalid_grant"}))
    with pytest.raises(anilist_client.AniListError, match="invalid_grant"):
        anilist_client.exchange_code("cid", "csecret", "badcode", client=fake)


def test_exchange_code_raises_if_no_access_token_in_response():
    fake = _FakeClient(response=_FakeResponse(payload={"unexpected": "shape"}))
    with pytest.raises(anilist_client.AniListError, match="access_token"):
        anilist_client.exchange_code("cid", "csecret", "code", client=fake)


# --- save_media_list_entry (A.9 push) -----------------------------------------


def test_save_media_list_entry_sends_both_status_and_score():
    entry = {"status": "CURRENT", "score": 85.0}
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"SaveMediaListEntry": entry}}))
    result = anilist_client.save_media_list_entry(
        "tok", 123, status="CURRENT", score=17.0, client=fake
    )
    assert result == {"status": "CURRENT", "score": 85.0}
    assert fake.last_variables == {"mediaId": 123, "status": "CURRENT", "score": 17.0}
    assert fake.last_headers == {"Authorization": "Bearer tok"}


def test_save_media_list_entry_score_only_omits_status_variable():
    """§6.1/§6.8 — an explicit null for an optional GraphQL argument
    means "unset this", not "leave it alone", so a score-only push
    must never even send a status variable."""
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"score": 85.0}}})
    )
    anilist_client.save_media_list_entry("tok", 123, score=17.0, client=fake)
    assert "status" not in fake.last_variables
    assert fake.last_variables["score"] == 17.0


def test_save_media_list_entry_status_only_omits_score_variable():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"status": "DROPPED"}}})
    )
    anilist_client.save_media_list_entry("tok", 123, status="DROPPED", client=fake)
    assert "score" not in fake.last_variables
    assert fake.last_variables["status"] == "DROPPED"


def test_save_media_list_entry_raises_ani_list_auth_error_on_401():
    fake = _FakeClient(
        response=_FakeResponse(status_code=401, payload={"errors": [{"message": "Invalid token"}]})
    )
    with pytest.raises(anilist_client.AniListAuthError, match="anilist-login"):
        anilist_client.save_media_list_entry("bad-tok", 123, score=17.0, client=fake)
