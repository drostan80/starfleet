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
                                    "progress": 12,
                                    "score": 85,
                                    "media": {"id": 101, "format": "TV", "title": {"romaji": "A"}},
                                }
                            ]
                        },
                        {
                            "entries": [
                                {
                                    "status": "CURRENT",
                                    "progress": 0,
                                    "score": 0,
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
        {
            "anilist_id": 101,
            "format": "TV",
            "status": "COMPLETED",
            "progress": 12,
            "score": 85,
            "title": "A",
        },
        {
            "anilist_id": 102,
            "format": "MOVIE",
            "status": "CURRENT",
            "progress": 0,
            "score": 0,
            "title": "B",
        },
    ]
    # userId came from the Viewer call's own id, not hardcoded
    assert fake.calls[1]["variables"] == {"userId": 24011}


def test_fetch_my_anime_list_treats_a_null_progress_as_zero():
    viewer_response = _FakeResponse(payload={"data": {"Viewer": {"id": 24011}}})
    collection_response = _FakeResponse(
        payload={
            "data": {
                "MediaListCollection": {
                    "lists": [
                        {
                            "entries": [
                                {
                                    "status": "PLANNING",
                                    "progress": None,
                                    "media": {"id": 101, "format": "TV", "title": {"romaji": "A"}},
                                }
                            ]
                        }
                    ]
                }
            }
        }
    )
    fake = _SequencedFakeClient([viewer_response, collection_response])
    result = anilist_client.fetch_my_anime_list("tok", client=fake)
    assert result[0]["progress"] == 0


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


def test_fetch_my_anime_list_passes_score_through_including_zero():
    # AniList returns 0 (not null) for an unscored entry — the score
    # importer relies on both a real score and that 0 arriving verbatim.
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
                                    "progress": 12,
                                    "score": 90,
                                    "media": {"id": 1, "format": "TV", "title": {"romaji": "A"}},
                                },
                                {
                                    "status": "CURRENT",
                                    "progress": 3,
                                    "score": 0,
                                    "media": {"id": 2, "format": "TV", "title": {"romaji": "B"}},
                                },
                            ]
                        }
                    ]
                }
            }
        }
    )
    fake = _SequencedFakeClient([viewer_response, collection_response])
    entries = anilist_client.fetch_my_anime_list("tok", client=fake)
    assert {e["anilist_id"]: e["score"] for e in entries} == {1: 90, 2: 0}


def test_fetch_score_format_returns_the_viewer_format():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Viewer": {"mediaListOptions": {"scoreFormat": "POINT_100"}}}}
        )
    )
    assert anilist_client.fetch_score_format("tok", client=fake) == "POINT_100"


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


def test_save_media_list_entry_progress_only_omits_other_variables():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"progress": 3}}})
    )
    anilist_client.save_media_list_entry("tok", 123, progress=3, client=fake)
    assert fake.last_variables == {"mediaId": 123, "progress": 3}


def test_save_media_list_entry_sends_repeat_and_status_for_rewatch():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"SaveMediaListEntry": {"status": "REPEATING", "repeat": 2}}}
        )
    )
    anilist_client.save_media_list_entry("tok", 123, status="REPEATING", repeat=2, client=fake)
    assert fake.last_variables == {"mediaId": 123, "status": "REPEATING", "repeat": 2}


def test_save_media_list_entry_sends_started_at_as_fuzzy_date():
    """archive/todo.md:1013 — the actual gap: `startedAt`/`completedAt`
    take AniList's `FuzzyDateInput` shape (plain year/month/day ints),
    not a scalar like every other param above. LCARS's own
    `season.started_at` is a full ISO-8601 UTC TEXT timestamp; this
    confirms the conversion, not just that the caller's own string got
    forwarded unchanged."""
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"id": 1}}})
    )
    anilist_client.save_media_list_entry(
        "tok", 123, started_at="2026-08-16T09:00:00Z", client=fake
    )
    assert fake.last_variables == {
        "mediaId": 123,
        "startedAt": {"year": 2026, "month": 8, "day": 16},
    }


def test_save_media_list_entry_sends_completed_at_as_fuzzy_date():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"id": 1}}})
    )
    anilist_client.save_media_list_entry(
        "tok", 123, completed_at="2026-08-19T00:00:00Z", client=fake
    )
    assert fake.last_variables == {
        "mediaId": 123,
        "completedAt": {"year": 2026, "month": 8, "day": 19},
    }


def test_save_media_list_entry_score_only_omits_started_at_and_completed_at():
    """Same "omit, don't null" guard as every other param — a score-only
    push (the common case) must never send startedAt/completedAt at all,
    which would unset them on AniList."""
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"SaveMediaListEntry": {"score": 85.0}}})
    )
    anilist_client.save_media_list_entry("tok", 123, score=17.0, client=fake)
    assert "startedAt" not in fake.last_variables
    assert "completedAt" not in fake.last_variables


# --- fetch_my_list_entry_id / delete_media_list_entry (write-mirror gap #4) ---


def test_fetch_my_list_entry_id_returns_the_real_entry_id():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Media": {"mediaListEntry": {"id": 5001}}}}
        )
    )
    result = anilist_client.fetch_my_list_entry_id("tok", 123, client=fake)
    assert result == 5001
    assert fake.last_variables == {"mediaId": 123}


def test_fetch_my_list_entry_id_none_when_never_added():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"Media": {"mediaListEntry": None}}})
    )
    assert anilist_client.fetch_my_list_entry_id("tok", 123, client=fake) is None


def test_fetch_my_list_entry_id_none_when_media_missing():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": None}}))
    assert anilist_client.fetch_my_list_entry_id("tok", 123, client=fake) is None


def test_delete_media_list_entry_sends_the_entry_id_not_the_media_id():
    fake = _FakeClient(
        response=_FakeResponse(payload={"data": {"DeleteMediaListEntry": {"deleted": True}}})
    )
    result = anilist_client.delete_media_list_entry("tok", 5001, client=fake)
    assert result is True
    assert fake.last_variables == {"id": 5001}


def test_delete_media_list_entry_raises_ani_list_auth_error_on_401():
    fake = _FakeClient(
        response=_FakeResponse(status_code=401, payload={"errors": [{"message": "Invalid token"}]})
    )
    with pytest.raises(anilist_client.AniListAuthError, match="anilist-login"):
        anilist_client.delete_media_list_entry("bad-tok", 5001, client=fake)


# --- _throttle_anilist_call (2026-08-13, the "Too many requests" fix) -------
# The repo-wide `tests/conftest.py` autouse fixture neuters the real
# `time.sleep` for every other test in this file (and everywhere else);
# these tests undo that locally to exercise the throttle itself.


def test_throttle_sleeps_the_remaining_gap_when_called_too_soon(monkeypatch):
    monkeypatch.setattr(anilist_client, "_last_anilist_call_at", 99.0)
    times = iter([100.0, 100.0])  # elapsed-check read, then the recorded call time
    monkeypatch.setattr(anilist_client.time, "monotonic", lambda: next(times))
    slept = []
    monkeypatch.setattr(anilist_client.time, "sleep", lambda s: slept.append(s))

    anilist_client._throttle_anilist_call()

    assert slept == [pytest.approx(anilist_client._ANILIST_SECONDS_PER_CALL - 1.0)]
    assert anilist_client._last_anilist_call_at == 100.0


def test_throttle_does_not_sleep_once_enough_time_has_already_passed(monkeypatch):
    monkeypatch.setattr(anilist_client, "_last_anilist_call_at", 100.0)
    monkeypatch.setattr(anilist_client.time, "monotonic", lambda: 500.0)
    slept = []
    monkeypatch.setattr(anilist_client.time, "sleep", lambda s: slept.append(s))

    anilist_client._throttle_anilist_call()

    assert slept == []
    assert anilist_client._last_anilist_call_at == 500.0


def test_throttle_never_sleeps_on_the_very_first_call_this_process(monkeypatch):
    monkeypatch.setattr(anilist_client, "_last_anilist_call_at", None)
    slept = []
    monkeypatch.setattr(anilist_client.time, "sleep", lambda s: slept.append(s))

    anilist_client._throttle_anilist_call()

    assert slept == []
    assert anilist_client._last_anilist_call_at is not None


def test_graphql_request_invokes_the_throttle(monkeypatch):
    """Confirms the throttle is actually wired into the one real choke
    point every caller passes through, not just unit-tested in
    isolation."""
    called = []
    monkeypatch.setattr(anilist_client, "_throttle_anilist_call", lambda: called.append(1))
    media = {"title": {"romaji": "Golden Kamuy"}}
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Media": media}}))
    anilist_client.fetch_media(123, client=fake)
    assert called == [1]


# --- fetch_latest_activity_marker / fetch_activity_feed (B.5.3) --------


def test_fetch_latest_activity_marker_returns_id_and_created_at():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Page": {"activities": [{"id": 999, "createdAt": 1700000000}]}}}
        )
    )
    result = anilist_client.fetch_latest_activity_marker("tok", 24011, client=fake)
    assert result == (999, 1700000000)
    assert fake.last_variables == {"userId": 24011}


def test_fetch_latest_activity_marker_returns_none_for_an_empty_account():
    fake = _FakeClient(response=_FakeResponse(payload={"data": {"Page": {"activities": []}}}))
    result = anilist_client.fetch_latest_activity_marker("tok", 24011, client=fake)
    assert result is None


def test_fetch_activity_feed_returns_new_activities_oldest_first():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={
                "data": {
                    "Page": {
                        "pageInfo": {"hasNextPage": False},
                        "activities": [
                            {"id": 101, "createdAt": 1700000100},
                            {"id": 102, "createdAt": 1700000200},
                        ],
                    }
                }
            }
        )
    )
    result = anilist_client.fetch_activity_feed(
        "tok", 24011, since_id=0, since_created_at=0, client=fake
    )
    assert result == [
        {"id": 101, "created_at": 1700000100},
        {"id": 102, "created_at": 1700000200},
    ]


def test_fetch_activity_feed_filters_out_already_seen_ids_sharing_a_second():
    """The real reason id, not just createdAt, is the cursor — confirmed
    live: several real activities can share one createdAt second."""
    fake = _FakeClient(
        response=_FakeResponse(
            payload={
                "data": {
                    "Page": {
                        "pageInfo": {"hasNextPage": False},
                        "activities": [
                            {"id": 100, "createdAt": 1700000100},  # already seen
                            {"id": 101, "createdAt": 1700000100},  # new, same second
                        ],
                    }
                }
            }
        )
    )
    result = anilist_client.fetch_activity_feed(
        "tok", 24011, since_id=100, since_created_at=1700000100, client=fake
    )
    assert result == [{"id": 101, "created_at": 1700000100}]


def test_fetch_activity_feed_queries_a_one_second_safety_margin():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Page": {"pageInfo": {"hasNextPage": False}, "activities": []}}}
        )
    )
    anilist_client.fetch_activity_feed(
        "tok", 24011, since_id=100, since_created_at=1700000100, client=fake
    )
    assert fake.last_variables["since"] == 1700000099


def test_fetch_activity_feed_never_queries_a_negative_since():
    fake = _FakeClient(
        response=_FakeResponse(
            payload={"data": {"Page": {"pageInfo": {"hasNextPage": False}, "activities": []}}}
        )
    )
    anilist_client.fetch_activity_feed("tok", 24011, since_id=0, since_created_at=0, client=fake)
    assert fake.last_variables["since"] == 0


def test_fetch_activity_feed_walks_every_page():
    fake = _SequencedFakeClient(
        [
            _FakeResponse(
                payload={
                    "data": {
                        "Page": {
                            "pageInfo": {"hasNextPage": True},
                            "activities": [{"id": 101, "createdAt": 1700000100}],
                        }
                    }
                }
            ),
            _FakeResponse(
                payload={
                    "data": {
                        "Page": {
                            "pageInfo": {"hasNextPage": False},
                            "activities": [{"id": 102, "createdAt": 1700000200}],
                        }
                    }
                }
            ),
        ]
    )
    result = anilist_client.fetch_activity_feed(
        "tok", 24011, since_id=0, since_created_at=0, client=fake
    )
    assert [a["id"] for a in result] == [101, 102]
    assert fake.calls[0]["variables"]["page"] == 1
    assert fake.calls[1]["variables"]["page"] == 2
