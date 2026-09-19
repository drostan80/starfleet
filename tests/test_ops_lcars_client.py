"""Ops's own GraphQL client — B.1. Same pattern Data's own real
`test_lcars_client.py` (`~/repos/data`) already established for its
sibling client: an injected `httpx.MockTransport`, no real network call.
"""

import json

import httpx
import pytest

from ops.lcars_client import LcarsAuthError, LcarsClient, LcarsError


def _client(handler) -> LcarsClient:
    return LcarsClient("http://lcars:8000", "test-token", transport=httpx.MockTransport(handler))


async def test_refresh_show_metadata_sends_the_right_headers_and_variables():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {"id": "s-abc123"}
        assert request.headers["x-lcars-client"] == "ops"
        assert request.headers["authorization"] == "Bearer test-token"
        return httpx.Response(200, json={"data": {"refreshShowMetadata": {"id": "s-abc123"}}})

    client = _client(handler)
    result = await client.refresh_show_metadata("s-abc123")
    assert result == {"id": "s-abc123"}
    await client.aclose()


async def test_due_for_metadata_refresh_walks_every_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        calls.append(payload["variables"]["after"])
        if payload["variables"]["after"] is None:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "dueForMetadataRefresh": {
                            "edges": [{"node": {"id": "s-page001", "displayTitle": "A"}}],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "dueForMetadataRefresh": {
                        "edges": [{"node": {"id": "s-page002", "displayTitle": "B"}}],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    client = _client(handler)
    shows = await client.due_for_metadata_refresh()
    assert [s["id"] for s in shows] == ["s-page001", "s-page002"]
    assert calls == [None, "cursor-1"]
    await client.aclose()


async def test_due_for_metadata_refresh_empty_result_is_a_clean_no_op():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "dueForMetadataRefresh": {
                        "edges": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    client = _client(handler)
    assert await client.due_for_metadata_refresh() == []
    await client.aclose()


async def test_reconcile_season_mapping_sends_the_right_variables():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {"id": "s-abc123", "season": 2}
        return httpx.Response(200, json={"data": {"reconcileSeasonMapping": {"id": "z-xyz"}}})

    client = _client(handler)
    result = await client.reconcile_season_mapping("s-abc123", 2)
    assert result == {"id": "z-xyz"}
    await client.aclose()


async def test_due_for_season_reconciliation_walks_every_page():
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        calls.append(payload["variables"]["after"])
        if payload["variables"]["after"] is None:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "dueForSeasonReconciliation": {
                            "edges": [
                                {
                                    "node": {
                                        "id": "z-page01",
                                        "seasonNumber": 1,
                                        "show": {"id": "s-a"},
                                    }
                                }
                            ],
                            "pageInfo": {"hasNextPage": True, "endCursor": "cursor-1"},
                        }
                    }
                },
            )
        return httpx.Response(
            200,
            json={
                "data": {
                    "dueForSeasonReconciliation": {
                        "edges": [
                            {"node": {"id": "z-page02", "seasonNumber": 2, "show": {"id": "s-b"}}}
                        ],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    client = _client(handler)
    seasons = await client.due_for_season_reconciliation()
    assert [s["id"] for s in seasons] == ["z-page01", "z-page02"]
    assert calls == [None, "cursor-1"]
    await client.aclose()


async def test_all_seasons_walks_shows_then_each_shows_seasons():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        if "shows" in payload["query"] and "seasons" not in payload["query"]:
            return httpx.Response(
                200,
                json={
                    "data": {
                        "shows": {
                            "edges": [{"node": {"id": "s-a"}}, {"node": {"id": "s-b"}}],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                },
            )
        show_id = payload["variables"]["id"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "show": {
                        "seasons": {
                            "edges": [
                                {
                                    "node": {
                                        "id": f"z-{show_id}",
                                        "seasonNumber": 1,
                                        "show": {"id": show_id},
                                    }
                                }
                            ],
                            "pageInfo": {"hasNextPage": False, "endCursor": None},
                        }
                    }
                }
            },
        )

    client = _client(handler)
    seasons = await client.all_seasons()
    assert {s["id"] for s in seasons} == {"z-s-a", "z-s-b"}
    await client.aclose()


async def test_all_seasons_empty_library_is_a_clean_no_op():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "shows": {
                        "edges": [],
                        "pageInfo": {"hasNextPage": False, "endCursor": None},
                    }
                }
            },
        )

    client = _client(handler)
    assert await client.all_seasons() == []
    await client.aclose()


async def test_poll_file_availability_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200,
            json={"data": {"pollFileAvailability": {"episodesUpdated": 4, "showsUpdated": 1}}},
        )

    client = _client(handler)
    result = await client.poll_file_availability()
    assert result == {"episodesUpdated": 4, "showsUpdated": 1}
    await client.aclose()


async def test_poll_anilist_activity_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200,
            json={
                "data": {
                    "pollAnilistActivity": {
                        "activitiesSeen": 2,
                        "reconcileResult": {
                            "seasonsChecked": 40,
                            "notMatchedOnAnilist": 0,
                            "showsStatusUpdated": 1,
                            "episodesBackfilled": 3,
                            "ambiguousAnilistIdConflicts": 0,
                        },
                    }
                }
            },
        )

    client = _client(handler)
    result = await client.poll_anilist_activity()
    assert result["activitiesSeen"] == 2
    assert result["reconcileResult"]["episodesBackfilled"] == 3
    await client.aclose()


async def test_poll_anilist_activity_returns_null_reconcile_result_on_a_quiet_poll():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"pollAnilistActivity": {"activitiesSeen": 0, "reconcileResult": None}}},
        )

    client = _client(handler)
    result = await client.poll_anilist_activity()
    assert result == {"activitiesSeen": 0, "reconcileResult": None}
    await client.aclose()


async def test_poll_anime_schedule_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200, json={"data": {"pollAnimeSchedule": {"episodesUpdated": 2, "flagged": 1}}}
        )

    client = _client(handler)
    result = await client.poll_anime_schedule()
    assert result == {"episodesUpdated": 2, "flagged": 1}
    await client.aclose()


async def test_poll_local_service_presence_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(200, json={"data": {"pollLocalServicePresence": {"showsUpdated": 3}}})

    client = _client(handler)
    result = await client.poll_local_service_presence()
    assert result == {"showsUpdated": 3}
    await client.aclose()


async def test_poll_catalog_service_presence_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200, json={"data": {"pollCatalogServicePresence": {"showsUpdated": 7}}}
        )

    client = _client(handler)
    result = await client.poll_catalog_service_presence()
    assert result == {"showsUpdated": 7}
    await client.aclose()


async def test_backfill_tvdb_ids_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(200, json={"data": {"backfillTvdbIds": {"showsUpdated": 5}}})

    client = _client(handler)
    result = await client.backfill_tvdb_ids()
    assert result == {"showsUpdated": 5}
    await client.aclose()


async def test_poll_show_merges_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200, json={"data": {"pollShowMerges": {"candidatesFound": 2, "reviewsOpened": 1}}}
        )

    client = _client(handler)
    result = await client.poll_show_merges()
    assert result == {"candidatesFound": 2, "reviewsOpened": 1}
    await client.aclose()


async def test_reconcile_episode_movie_links_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(
            200,
            json={
                "data": {
                    "reconcileEpisodeMovieLinks": {
                        "matched": 1,
                        "flagged": 2,
                        "unmatched": 3,
                        "availabilitySynced": 4,
                    }
                }
            },
        )

    client = _client(handler)
    result = await client.reconcile_episode_movie_links()
    assert result == {"matched": 1, "flagged": 2, "unmatched": 3, "availabilitySynced": 4}
    await client.aclose()


async def test_refresh_mal_token_if_due_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        return httpx.Response(200, json={"data": {"refreshMalTokenIfDue": {"refreshed": True}}})

    client = _client(handler)
    result = await client.refresh_mal_token_if_due()
    assert result == {"refreshed": True}
    await client.aclose()


async def test_backfill_file_availability_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "backfillFileAvailability" in payload["query"]
        return httpx.Response(
            200,
            json={"data": {"backfillFileAvailability": {"episodesUpdated": 9, "showsUpdated": 2}}},
        )

    client = _client(handler)
    result = await client.backfill_file_availability()
    assert result == {"episodesUpdated": 9, "showsUpdated": 2}
    await client.aclose()


async def test_audit_local_files_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "auditLocalFiles" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "auditLocalFiles": {
                        "episodesCorrected": 1,
                        "showsCorrected": 2,
                        "orphanFiles": [
                            {
                                "showId": "s-x",
                                "path": "/x.mkv",
                                "parsedSeason": 1,
                                "parsedEpisode": 2,
                            }
                        ],
                        "untrackedShows": [
                            {"service": "sonarr", "title": "Y", "externalId": 5, "path": "/y"}
                        ],
                    }
                }
            },
        )

    client = _client(handler)
    result = await client.audit_local_files()
    assert result["episodesCorrected"] == 1
    assert result["showsCorrected"] == 2
    assert result["orphanFiles"][0]["path"] == "/x.mkv"
    assert result["untrackedShows"][0]["title"] == "Y"
    await client.aclose()


async def test_reconcile_arr_state_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "reconcileArrState" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "reconcileArrState": {
                        "episodesCorrected": 1,
                        "showsCorrected": 2,
                        "showsCreated": 3,
                        "showsCreateFailed": 0,
                        "pausedShowIds": ["s-1"],
                        "resumedShowIds": ["s-2", "s-3"],
                    }
                }
            },
        )

    client = _client(handler)
    result = await client.reconcile_arr_state()
    assert result["episodesCorrected"] == 1
    assert result["showsCorrected"] == 2
    assert result["showsCreated"] == 3
    assert result["pausedShowIds"] == ["s-1"]
    assert result["resumedShowIds"] == ["s-2", "s-3"]
    await client.aclose()


async def test_preview_show_backfill_sends_no_variables_and_returns_the_list():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "previewShowBackfill" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "previewShowBackfill": [
                        {
                            "service": "sonarr",
                            "title": "Y",
                            "externalId": 5,
                            "trackingSpace": "ANIME",
                            "mediaShape": "EPISODIC",
                        }
                    ]
                }
            },
        )

    client = _client(handler)
    result = await client.preview_show_backfill()
    assert result[0]["title"] == "Y"
    assert result[0]["trackingSpace"] == "ANIME"
    await client.aclose()


async def test_backfill_untracked_shows_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "backfillUntrackedShows" in payload["query"]
        assert "promoted" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "backfillUntrackedShows": {
                        "created": [{"showId": "s-x", "service": "sonarr", "title": "Y"}],
                        "promoted": [{"showId": "s-z", "service": "anilist", "title": "Z"}],
                        "failed": [],
                    }
                }
            },
        )

    client = _client(handler)
    result = await client.backfill_untracked_shows()
    assert result["created"][0]["title"] == "Y"
    assert result["promoted"][0]["title"] == "Z"
    assert result["failed"] == []
    await client.aclose()


async def test_poll_untracked_shows_sends_no_variables_and_returns_the_result():
    def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.read())
        assert payload["variables"] == {}
        assert "pollUntrackedShows" in payload["query"]
        return httpx.Response(
            200,
            json={
                "data": {
                    "pollUntrackedShows": {"found": 5, "newFindings": 1, "resolvedFindings": 2}
                }
            },
        )

    client = _client(handler)
    result = await client.poll_untracked_shows()
    assert result == {"found": 5, "newFindings": 1, "resolvedFindings": 2}
    await client.aclose()


async def test_recommended_availability_poll_interval_seconds_returns_the_int():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": {"recommendedAvailabilityPollIntervalSeconds": 300}}
        )

    client = _client(handler)
    assert await client.recommended_availability_poll_interval_seconds() == 300
    await client.aclose()


async def test_raises_lcars_auth_error_on_401():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={})

    client = _client(handler)
    with pytest.raises(LcarsAuthError):
        await client.refresh_show_metadata("s-abc123")
    await client.aclose()


async def test_raises_lcars_error_on_graphql_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"errors": [{"message": "no such show: s-bad"}]})

    client = _client(handler)
    with pytest.raises(LcarsError, match="no such show"):
        await client.refresh_show_metadata("s-bad")
    await client.aclose()


async def test_raises_on_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    client = _client(handler)
    with pytest.raises(LcarsError, match="Could not connect"):
        await client.refresh_show_metadata("s-abc123")
    await client.aclose()


async def test_raises_lcars_error_on_malformed_200_response():
    # A real gap caught by review, not a test failure: a 200 with no
    # valid JSON body (or JSON with neither "data" nor "errors") used to
    # fall straight into `payload["data"]`, raising a raw TypeError/
    # KeyError that would escape run_once/run_forever's own
    # `except LcarsError` handling and kill the unattended daemon.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="not json")

    client = _client(handler)
    with pytest.raises(LcarsError, match="unexpected response"):
        await client.refresh_show_metadata("s-abc123")
    await client.aclose()


async def test_raises_on_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("boom")

    client = _client(handler)
    with pytest.raises(LcarsError, match="Timed out"):
        await client.refresh_show_metadata("s-abc123")
    await client.aclose()
