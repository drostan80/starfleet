"""End-to-end: real GraphQL requests (httpx.ASGITransport, no bound
port) against the real ASGI app, over a real migrated SQLite database.
BUILD_PLAN.md A.3's vertical slice — Show/Episode/WatchEvent + their
core mutations, plus the bearer-token/X-LCARS-Client plumbing.
"""

import os
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from lcars import db
from lcars.server import build_app

BEARER_TOKEN = "test-token-123"  # noqa: S105 (test fixture, not a real secret)


@pytest.fixture
def migrated_db(tmp_path) -> Path:
    db_path = tmp_path / "lcars_test.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{db_path}"},
        check=True,
        capture_output=True,
    )
    return db_path


@pytest.fixture
async def client(migrated_db):
    db.connect(migrated_db)
    app = build_app(BEARER_TOKEN)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
    db.close()


def auth_headers(client_name: str | None = "data") -> dict:
    headers = {"Authorization": f"Bearer {BEARER_TOKEN}"}
    if client_name is not None:
        headers["X-LCARS-Client"] = client_name
    return headers


async def gql(client: httpx.AsyncClient, query: str, variables: dict | None = None, **kw) -> dict:
    resp = await client.post("/", json={"query": query, "variables": variables or {}}, **kw)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "errors" not in body, body["errors"]
    return body["data"]


async def add_show(client, **overrides) -> dict:
    input_ = {
        "mediaShape": "EPISODIC",
        "trackingSpace": "ANIME",
        "titleRomaji": "Konosuba",
        "primaryTitle": "ROMAJI",
        **overrides,
    }
    data = await gql(
        client,
        """
        mutation($input: AddShowInput!) {
          addShow(input: $input) {
            id displayTitle status tracked score mediaShape trackingSpace
          }
        }
        """,
        {"input": input_},
        headers=auth_headers(),
    )
    return data["addShow"]


# --- auth --------------------------------------------------------------


async def test_missing_bearer_token_rejected(client):
    resp = await client.post("/", json={"query": "{ shows { edges { node { id } } } }"})
    assert resp.status_code == 401


async def test_wrong_bearer_token_rejected(client):
    resp = await client.post(
        "/",
        json={"query": "{ shows { edges { node { id } } } }"},
        headers={"Authorization": "Bearer wrong-token"},
    )
    assert resp.status_code == 401


async def test_correct_bearer_token_accepted(client):
    resp = await client.post(
        "/", json={"query": "{ shows { edges { node { id } } } }"}, headers=auth_headers()
    )
    assert resp.status_code == 200


async def test_mutation_without_client_header_rejected(client):
    show = await add_show(client)
    resp = await client.post(
        "/",
        json={
            "query": 'mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }',
            "variables": {"id": show["id"]},
        },
        headers=auth_headers(client_name=None),
    )
    # Ariadne returns 400 (not 200-with-errors) when the top-level mutation
    # field itself errors out to null data — confirmed against a real
    # request rather than assumed, see this test's own history.
    assert resp.status_code == 400
    body = resp.json()
    assert "errors" in body
    assert "X-LCARS-Client" in body["errors"][0]["message"]


# --- addShow / show lifecycle ------------------------------------------


async def test_add_show_then_fetch_by_id(client):
    show = await add_show(client)
    assert show["id"].startswith("s-")
    assert show["displayTitle"] == "Konosuba"
    assert show["status"] == "PLANNED"
    assert show["tracked"] is True
    assert show["mediaShape"] == "EPISODIC"
    assert show["trackingSpace"] == "ANIME"

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { id displayTitle } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["id"] == show["id"]


async def test_add_show_display_title_follows_primary_title(client):
    show = await add_show(
        client, titleRomaji="Konosuba", titleEnglish="KonoSuba", primaryTitle="ENGLISH"
    )
    assert show["displayTitle"] == "KonoSuba"


async def test_add_show_rejects_mismatched_primary_title(client):
    resp = await client.post(
        "/",
        json={
            "query": """
                mutation($input: AddShowInput!) { addShow(input: $input) { id } }
            """,
            "variables": {
                "input": {
                    "mediaShape": "EPISODIC",
                    "trackingSpace": "ANIME",
                    "primaryTitle": "ENGLISH",  # no titleEnglish provided
                }
            },
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body


async def test_add_show_with_external_ids_creates_crosswalk_rows(client):
    show = await add_show(client, anilistId=12345, tvdbId=67890)
    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { externalIds { edges { node { service externalId url } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    links = {e["node"]["service"]: e["node"] for e in data["show"]["externalIds"]["edges"]}
    assert links["anilist"]["externalId"] == "12345"
    assert links["anilist"]["url"] == "https://anilist.co/anime/12345"
    assert links["tvdb"]["externalId"] == "67890"


@pytest.mark.parametrize(
    ("media_shape", "expected_url"),
    [
        ("MOVIE", "https://www.themoviedb.org/movie/555"),
        ("EPISODIC", "https://www.themoviedb.org/tv/555"),
    ],
)
async def test_tmdb_url_depends_on_media_shape(client, media_shape, expected_url):
    """Audit-pass fix: a single tmdb URL template would have produced a
    wrong link for whichever media_shape it wasn't written for — §5.4
    itself notes both movies and episodic shows can carry a TMDB id."""
    show = await add_show(client, mediaShape=media_shape, tmdbId=555)
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { externalIds { edges { node { url } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    urls = [e["node"]["url"] for e in data["show"]["externalIds"]["edges"]]
    assert urls == [expected_url]


# --- setStatus / setScore / setTracked + history --------------------------


async def test_set_status_updates_and_records_history(client):
    show = await add_show(client)
    data = await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id status } }",
        {"id": show["id"]},
        headers=auth_headers("holodeck"),
    )
    assert data["setStatus"]["status"] == "WATCHING"

    history = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            statusHistory {
              edges { node { previousStatus newStatus changedBy show { id } } }
            }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    entries = history["show"]["statusHistory"]["edges"]
    assert len(entries) == 1
    assert entries[0]["node"]["previousStatus"] == "PLANNED"
    assert entries[0]["node"]["newStatus"] == "WATCHING"
    assert entries[0]["node"]["changedBy"] == "holodeck"
    # StatusChange.show found unbound in the 2026-08-08 audit pass — no
    # earlier test had ever requested it, only the scalar fields above.
    assert entries[0]["node"]["show"]["id"] == show["id"]


@pytest.mark.parametrize(
    ("input_score", "expected"),
    [
        (15.1, 15.0),  # rounds to nearest quarter-point
        (15.13, 15.25),
        (-5.0, 0.0),  # clamps below range
        (25.0, 20.0),  # clamps above range
    ],
)
async def test_set_score_clamps_and_rounds_silently(client, input_score, expected):
    show = await add_show(client)
    data = await gql(
        client,
        "mutation($id: ID!, $s: Float!) { setScore(showId: $id, score: $s) { score } }",
        {"id": show["id"], "s": input_score},
        headers=auth_headers(),
    )
    assert data["setScore"]["score"] == expected


async def test_set_tracked_records_history(client):
    show = await add_show(client)
    data = await gql(
        client,
        "mutation($id: ID!) { setTracked(showId: $id, tracked: false) { tracked } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["setTracked"]["tracked"] is False

    history = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            trackedHistory {
              edges { node { previousTracked newTracked show { id } } }
            }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    entries = history["show"]["trackedHistory"]["edges"]
    assert len(entries) == 1
    assert entries[0]["node"]["previousTracked"] is True
    assert entries[0]["node"]["newTracked"] is False
    # TrackedChange.show found unbound in the same audit pass as StatusChange.show.
    assert entries[0]["node"]["show"]["id"] == show["id"]


async def test_score_history_includes_show(client):
    show = await add_show(client)
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 15) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    history = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { scoreHistory { edges { node { newScore show { id } } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    entries = history["show"]["scoreHistory"]["edges"]
    assert len(entries) == 1
    assert entries[0]["node"]["newScore"] == 15
    # ScoreChange.show found unbound in the same audit pass as StatusChange.show.
    assert entries[0]["node"]["show"]["id"] == show["id"]


# --- addWatchEvent / markEpisodeSkipped ------------------------------------


async def _insert_episode(
    migrated_db: Path, show_id: str, episode_id: str = "e-tst001", kind: str = "regular"
) -> str:
    """Episodes aren't addable via the API yet (A.8's on-demand fetch,
    not built here) — insert directly for this test's purposes."""
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)
        VALUES (
            ?, ?, 1, 1, ?, 'unwatched',
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (episode_id, show_id, kind),
    )
    conn.commit()
    return episode_id


async def test_add_watch_event_marks_episode_watched(client, migrated_db):
    show = await add_show(client)
    await _insert_episode(migrated_db, show["id"])

    data = await gql(
        client,
        """
        mutation($id: ID!) {
          addWatchEvent(showId: $id, season: 1, episode: 1) { id season episode }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["addWatchEvent"]["season"] == 1

    episode_state = db.get_connection().execute(
        "SELECT state FROM episode WHERE id = 'e-tst001'"
    ).fetchone()
    assert episode_state["state"] == "watched"


async def test_add_watch_event_for_movie_has_null_season_episode(client):
    show = await add_show(
        client, mediaShape="MOVIE", titleRomaji="A Standalone Film", primaryTitle="ROMAJI"
    )
    data = await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id) { id season episode } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["addWatchEvent"]["season"] is None
    assert data["addWatchEvent"]["episode"] is None


async def test_mark_episode_skipped(client, migrated_db):
    show = await add_show(client)
    episode_id = await _insert_episode(migrated_db, show["id"])
    data = await gql(
        client,
        "mutation($id: ID!) { markEpisodeSkipped(episodeId: $id) { state } }",
        {"id": episode_id},
        headers=auth_headers(),
    )
    assert data["markEpisodeSkipped"]["state"] == "SKIPPED"


async def _insert_episode_range(migrated_db: Path, show_id: str, season: int, episodes: range):
    conn = db.get_connection()
    for n in episodes:
        conn.execute(
            """
            INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)
            VALUES (
                ?, ?, ?, ?, 'regular', 'unwatched',
                '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
            )
            """,
            (f"e-s{season}e{n:03d}", show_id, season, n),
        )
    conn.commit()


async def test_delete_watch_event_reverts_episode_state_when_last_one(client, migrated_db):
    show = await add_show(client)
    await _insert_episode(migrated_db, show["id"])
    added = await gql(
        client,
        'mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }',
        {"id": show["id"]},
        headers=auth_headers(),
    )
    watch_event_id = added["addWatchEvent"]["id"]

    result = await gql(
        client,
        "mutation($id: ID!) { deleteWatchEvent(watchEventId: $id) }",
        {"id": watch_event_id},
        headers=auth_headers(),
    )
    assert result["deleteWatchEvent"] is True

    episode_state = db.get_connection().execute(
        "SELECT state FROM episode WHERE id = 'e-tst001'"
    ).fetchone()
    assert episode_state["state"] == "unwatched"


async def test_delete_watch_event_keeps_state_watched_if_rewatch_remains(client, migrated_db):
    show = await add_show(client)
    await _insert_episode(migrated_db, show["id"])
    first = await gql(
        client,
        'mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }',
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        'mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }',
        {"id": show["id"]},
        headers=auth_headers(),
    )

    await gql(
        client,
        "mutation($id: ID!) { deleteWatchEvent(watchEventId: $id) }",
        {"id": first["addWatchEvent"]["id"]},
        headers=auth_headers(),
    )

    episode_state = db.get_connection().execute(
        "SELECT state FROM episode WHERE id = 'e-tst001'"
    ).fetchone()
    assert episode_state["state"] == "watched"


async def test_delete_watch_event_for_movie_has_no_episode_to_revert(client):
    show = await add_show(
        client, mediaShape="MOVIE", titleRomaji="A Standalone Film", primaryTitle="ROMAJI"
    )
    added = await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    result = await gql(
        client,
        "mutation($id: ID!) { deleteWatchEvent(watchEventId: $id) }",
        {"id": added["addWatchEvent"]["id"]},
        headers=auth_headers(),
    )
    assert result["deleteWatchEvent"] is True


async def test_mark_season_watched_creates_one_watch_event_per_episode(client, migrated_db):
    show = await add_show(client)
    await _insert_episode_range(migrated_db, show["id"], season=1, episodes=range(1, 4))

    data = await gql(
        client,
        """
        mutation($id: ID!) {
          markSeasonWatched(showId: $id, season: 1) { season episode }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    watched = sorted(e["episode"] for e in data["markSeasonWatched"])
    assert watched == [1, 2, 3]

    states = db.get_connection().execute(
        "SELECT state FROM episode WHERE show_id = ? AND season = 1", (show["id"],)
    ).fetchall()
    assert all(s["state"] == "watched" for s in states)


async def test_mark_episode_range_watched(client, migrated_db):
    show = await add_show(client)
    await _insert_episode_range(migrated_db, show["id"], season=1, episodes=range(1, 6))

    data = await gql(
        client,
        """
        mutation($id: ID!) {
          markEpisodeRangeWatched(showId: $id, season: 1, fromEpisode: 2, toEpisode: 4) {
            episode
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    watched = sorted(e["episode"] for e in data["markEpisodeRangeWatched"])
    assert watched == [2, 3, 4]

    rows = db.get_connection().execute(
        "SELECT episode, state FROM episode WHERE show_id = ? AND season = 1", (show["id"],)
    ).fetchall()
    states = {
        row["episode"]: row["state"]
        for row in rows
    }
    assert states == {1: "unwatched", 2: "watched", 3: "watched", 4: "watched", 5: "unwatched"}


# --- episode field overrides (§5.2) -----------------------------------------


async def test_set_episode_air_date_records_history(client, migrated_db):
    show = await add_show(client)
    episode_id = await _insert_episode(migrated_db, show["id"])

    data = await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeAirDate(episodeId: $id, airDateUtc: "2026-09-01T12:00:00Z") {
            airDateUtc airDateSource
          }
        }
        """,
        {"id": episode_id},
        headers=auth_headers("captains_log"),
    )
    assert data["setEpisodeAirDate"]["airDateUtc"] == "2026-09-01T12:00:00Z"
    assert data["setEpisodeAirDate"]["airDateSource"] == "MANUAL"

    history = await gql(
        client,
        """
        query($id: ID!) {
          episode(id: $id) {
            airDateHistory {
              edges { node { newAirDateUtc newSource changedBy episode { id } } }
            }
          }
        }
        """,
        {"id": episode_id},
        headers=auth_headers(),
    )
    entries = history["episode"]["airDateHistory"]["edges"]
    assert len(entries) == 1
    assert entries[0]["node"]["newAirDateUtc"] == "2026-09-01T12:00:00Z"
    assert entries[0]["node"]["newSource"] == "MANUAL"
    assert entries[0]["node"]["changedBy"] == "captains_log"
    # AirDateChange.episode found unbound in the same audit pass as
    # StatusChange.show (Query.episode was needed to even reach it here).
    assert entries[0]["node"]["episode"]["id"] == episode_id


async def test_set_episode_air_date_requires_client_header(client, migrated_db):
    show = await add_show(client)
    episode_id = await _insert_episode(migrated_db, show["id"])
    resp = await client.post(
        "/",
        json={
            "query": (
                'mutation($id: ID!) { setEpisodeAirDate(episodeId: $id, '
                'airDateUtc: "2026-09-01T12:00:00Z") { id } }'
            ),
            "variables": {"id": episode_id},
        },
        headers=auth_headers(client_name=None),
    )
    assert "errors" in resp.json()


async def test_set_episode_runtime_override(client, migrated_db):
    show = await add_show(client)
    episode_id = await _insert_episode(migrated_db, show["id"])
    data = await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeRuntimeOverride(episodeId: $id, runtimeMinutes: 45) { runtimeMinutes }
        }
        """,
        {"id": episode_id},
        headers=auth_headers(),
    )
    assert data["setEpisodeRuntimeOverride"]["runtimeMinutes"] == 45


# --- external link management (§5.4) ----------------------------------------


LINK_SHOW_EXTERNAL_ID = """
    mutation($id: ID!, $externalId: String!, $url: String!) {
      linkShowExternalId(showId: $id, service: "tmdb", externalId: $externalId, url: $url) {
        service externalId url
      }
    }
"""


async def test_link_and_unlink_show_external_id(client):
    show = await add_show(client)

    linked = await gql(
        client,
        LINK_SHOW_EXTERNAL_ID,
        {"id": show["id"], "externalId": "999", "url": "https://example/999"},
        headers=auth_headers(),
    )
    assert linked["linkShowExternalId"]["externalId"] == "999"

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { externalIds { edges { node { show { id } } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # ShowExternalId.show found unbound in the 2026-08-08 audit pass — no
    # earlier test had ever requested it.
    assert data["show"]["externalIds"]["edges"][0]["node"]["show"]["id"] == show["id"]

    # calling again with the same service upserts, not duplicates
    relinked = await gql(
        client,
        LINK_SHOW_EXTERNAL_ID,
        {"id": show["id"], "externalId": "1000", "url": "https://example/1000"},
        headers=auth_headers(),
    )
    assert relinked["linkShowExternalId"]["externalId"] == "1000"

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { externalIds { edges { node { service } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert len(data["show"]["externalIds"]["edges"]) == 1

    unlinked = await gql(
        client,
        'mutation($id: ID!) { unlinkShowExternalId(showId: $id, service: "tmdb") }',
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert unlinked["unlinkShowExternalId"] is True

    after = await gql(
        client,
        "query($id: ID!) { show(id: $id) { externalIds { edges { node { service } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert after["show"]["externalIds"]["edges"] == []


# --- pagination wiring (logic itself is tested in test_pagination.py) -----


async def test_shows_by_status_filters_and_paginates(client):
    for i in range(3):
        await add_show(client, titleRomaji=f"Watching Show {i}")
    show = await add_show(client, titleRomaji="Dropped Show")
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: DROPPED) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )

    data = await gql(
        client,
        "query { showsByStatus(statuses: [PLANNED]) { edges { node { displayTitle } } } }",
        headers=auth_headers(),
    )
    titles = {e["node"]["displayTitle"] for e in data["showsByStatus"]["edges"]}
    assert titles == {"Watching Show 0", "Watching Show 1", "Watching Show 2"}


# --- id-mapper manual overrides (§5.5) --------------------------------------


async def test_set_show_id_mapping_creates_then_updates(client):
    show = await add_show(client)

    created = await gql(
        client,
        """
        mutation($id: ID!) {
          setShowIdMapping(showId: $id, tvdbId: 111, anilistId: 222) {
            id tvdbId anilistId source matched manualOverride
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    mapping = created["setShowIdMapping"]
    assert mapping["tvdbId"] == 111
    assert mapping["anilistId"] == 222
    assert mapping["source"] == "MANUAL"
    assert mapping["matched"] is True
    assert mapping["manualOverride"] is True

    updated = await gql(
        client,
        """
        mutation($id: ID!) {
          setShowIdMapping(showId: $id, tvdbId: 999, anilistId: 222) { id tvdbId }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # same underlying row (upsert), not a second one
    assert updated["setShowIdMapping"]["id"] == mapping["id"]
    assert updated["setShowIdMapping"]["tvdbId"] == 999

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { idMapping { tvdbId } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["idMapping"]["tvdbId"] == 999


async def test_set_episode_numbering_scheme(client):
    show = await add_show(client)
    data = await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeNumberingScheme(showId: $id, scheme: ABSOLUTE) { scheme source }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["setEpisodeNumberingScheme"]["scheme"] == "ABSOLUTE"
    assert data["setEpisodeNumberingScheme"]["source"] == "MANUAL"


async def test_set_episode_movie_link_both_directions_queryable(client, migrated_db):
    tv_show = await add_show(client, titleRomaji="Some Series")
    movie_show = await add_show(
        client, mediaShape="MOVIE", titleRomaji="Some Series: The Movie"
    )
    bonus_episode_id = await _insert_episode(
        migrated_db, tv_show["id"], episode_id="e-bmovi1", kind="bonus_movie"
    )

    data = await gql(
        client,
        """
        mutation($e: ID!, $s: ID!) {
          setEpisodeMovieLink(episodeId: $e, movieShowId: $s) { matched source }
        }
        """,
        {"e": bonus_episode_id, "s": movie_show["id"]},
        headers=auth_headers(),
    )
    assert data["setEpisodeMovieLink"]["matched"] is True

    forward = await gql(
        client,
        "query($id: ID!) { episode(id: $id) { linkedMovieShow { id } } }",
        {"id": bonus_episode_id},
        headers=auth_headers(),
    )
    assert forward["episode"]["linkedMovieShow"]["id"] == movie_show["id"]

    backward = await gql(
        client,
        "query($id: ID!) { show(id: $id) { linkedFromEpisode { id } } }",
        {"id": movie_show["id"]},
        headers=auth_headers(),
    )
    assert backward["show"]["linkedFromEpisode"]["id"] == bonus_episode_id


# --- pending_review (§5.6) ---------------------------------------------------


async def _insert_pending_review(migrated_db: Path) -> str:
    """No mutation creates pending_review rows yet (that's automated
    derivation, A.4/A.5) — insert directly for this test's purposes."""
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO pending_review
            (id, entity_type, entity_id, field, proposed_value_chain, source, created_at)
        VALUES ('r-test01', 'show', 's-doesnt-matter', 'status', '["watching"]',
                'sonarr_sync', '2026-08-08T00:00:00Z')
        """
    )
    conn.commit()
    return "r-test01"


async def test_resolve_pending_review(client, migrated_db):
    review_id = await _insert_pending_review(migrated_db)
    data = await gql(
        client,
        """
        mutation($id: ID!) {
          resolvePendingReview(id: $id, resolutionNote: "looks right") {
            resolvedByClient resolutionNote resolvedAt
          }
        }
        """,
        {"id": review_id},
        headers=auth_headers("captains_log"),
    )
    result = data["resolvePendingReview"]
    assert result["resolvedByClient"] == "CAPTAINS_LOG"
    assert result["resolutionNote"] == "looks right"
    assert result["resolvedAt"] is not None


async def test_resolve_pending_review_rejects_non_interactive_client(client, migrated_db):
    review_id = await _insert_pending_review(migrated_db)
    resp = await client.post(
        "/",
        json={
            "query": 'mutation($id: ID!) { resolvePendingReview(id: $id) { id } }',
            "variables": {"id": review_id},
        },
        headers=auth_headers("sonarr_sync"),
    )
    body = resp.json()
    assert "errors" in body
    assert "cannot resolve" in body["errors"][0]["message"]


async def test_pending_reviews_query_defaults_to_unresolved_only(client, migrated_db):
    review_id = await _insert_pending_review(migrated_db)

    before = await gql(
        client, "{ pendingReviews { edges { node { id } } } }", headers=auth_headers()
    )
    assert [e["node"]["id"] for e in before["pendingReviews"]["edges"]] == [review_id]

    await gql(
        client,
        'mutation($id: ID!) { resolvePendingReview(id: $id) { id } }',
        {"id": review_id},
        headers=auth_headers("data"),
    )

    after_default = await gql(
        client, "{ pendingReviews { edges { node { id } } } }", headers=auth_headers()
    )
    assert after_default["pendingReviews"]["edges"] == []

    after_all = await gql(
        client,
        "{ pendingReviews(includeResolved: true) { edges { node { id } } } }",
        headers=auth_headers(),
    )
    assert [e["node"]["id"] for e in after_all["pendingReviews"]["edges"]] == [review_id]


# --- audit-pass fix: show.hardDeleteRequestedAt (§6.11) --------------------
#
# Confirms the column this field depends on actually exists — caught missing
# during a full audit pass despite BUILD_PLAN.md having claimed it was
# already added; see migration 4509892cd91b.


async def test_hard_delete_requested_at_field_resolves(client):
    show = await add_show(client)
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { hardDeleteRequestedAt } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["hardDeleteRequestedAt"] is None


# --- ShowServicePresence / Person / Studio / CastCredit / StudioCredit (§5.4/§5.8) --
#
# No mutations exist for any of these — externally-populated metadata
# (§5.8), not client-created. Rows are inserted directly here, the same way
# episodes are elsewhere in this file, standing in for the on-demand-fetch/
# background-poll machinery that doesn't exist yet (A.8/Phase B).


def _insert_show_service_presence(show_id: str, service: str, present: bool) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO show_service_presence (id, show_id, service, present, checked_at)"
        " VALUES (?, ?, ?, ?, '2026-08-08T00:00:00Z')",
        (f"a-{service[:6]:0<6}", show_id, service, int(present)),
    )
    conn.commit()


def _insert_person(person_id: str, name: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO person (id, name, created_at) VALUES (?, ?, '2026-08-08T00:00:00Z')",
        (person_id, name),
    )
    conn.commit()


def _insert_studio(studio_id: str, name: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO studio (id, name, created_at) VALUES (?, ?, '2026-08-08T00:00:00Z')",
        (studio_id, name),
    )
    conn.commit()


def _insert_cast_credit(show_id: str, person_id: str, role_type: str, character_name=None):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO show_person (show_id, person_id, role_type, character_name)"
        " VALUES (?, ?, ?, ?)",
        (show_id, person_id, role_type, character_name),
    )
    conn.commit()


def _insert_studio_credit(show_id: str, studio_id: str, role_type: str):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO show_studio (show_id, studio_id, role_type) VALUES (?, ?, ?)",
        (show_id, studio_id, role_type),
    )
    conn.commit()


async def test_show_service_presence(client):
    show = await add_show(client)
    _insert_show_service_presence(show["id"], "radarr", True)
    _insert_show_service_presence(show["id"], "sonarr", False)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            servicePresence { edges { node { service present show { id } } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    presence = {e["node"]["service"]: e["node"] for e in data["show"]["servicePresence"]["edges"]}
    assert presence["radarr"]["present"] is True
    assert presence["sonarr"]["present"] is False
    assert presence["radarr"]["show"]["id"] == show["id"]


async def test_cast_credit_both_directions(client):
    show = await add_show(client, titleRomaji="Konosuba")
    _insert_person("p-actor1", "Jane Voice")
    _insert_cast_credit(show["id"], "p-actor1", "voice_actor", character_name="Megumin")

    from_show = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            cast { edges { node { roleType characterName person { id name } } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    credit = from_show["show"]["cast"]["edges"][0]["node"]
    assert credit["roleType"] == "VOICE_ACTOR"
    assert credit["characterName"] == "Megumin"
    assert credit["person"]["name"] == "Jane Voice"

    from_person = await gql(
        client,
        """
        query($id: ID!) {
          person(id: $id) {
            name
            credits { edges { node { characterName show { id } } } }
          }
        }
        """,
        {"id": "p-actor1"},
        headers=auth_headers(),
    )
    reverse_credit = from_person["person"]["credits"]["edges"][0]["node"]
    assert reverse_credit["characterName"] == "Megumin"
    assert reverse_credit["show"]["id"] == show["id"]


async def test_studio_credit_both_directions(client):
    show = await add_show(client)
    _insert_studio("d-studio", "Great Animation Studio")
    _insert_studio_credit(show["id"], "d-studio", "studio")

    from_show = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            studioCredits { edges { node { roleType studio { id name } } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    credit = from_show["show"]["studioCredits"]["edges"][0]["node"]
    assert credit["roleType"] == "STUDIO"
    assert credit["studio"]["name"] == "Great Animation Studio"

    from_studio = await gql(
        client,
        """
        query($id: ID!) {
          studio(id: $id) {
            name
            credits { edges { node { roleType show { id } } } }
          }
        }
        """,
        {"id": "d-studio"},
        headers=auth_headers(),
    )
    reverse_credit = from_studio["studio"]["credits"]["edges"][0]["node"]
    assert reverse_credit["roleType"] == "STUDIO"
    assert reverse_credit["show"]["id"] == show["id"]


async def test_people_and_studios_top_level_queries(client):
    _insert_person("p-listme", "Someone")
    _insert_studio("d-listme", "Some Studio")

    people = await gql(
        client, "{ people { edges { node { id name } } } }", headers=auth_headers()
    )
    assert any(e["node"]["id"] == "p-listme" for e in people["people"]["edges"])

    studios = await gql(
        client, "{ studios { edges { node { id name } } } }", headers=auth_headers()
    )
    assert any(e["node"]["id"] == "d-listme" for e in studios["studios"]["edges"])


# --- show_relation / franchise / franchise_member / next_up_override (§5.9) --
#
# show_relation and franchise itself have no mutations (auto-derived, not
# client-created, same as person/studio) — inserted directly here.
# franchise_member and next_up_override DO have manual-override mutations.


def _insert_show_relation(show_id: str, related_show_id: str) -> None:
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO show_relation (show_id, related_show_id, created_at)"
        " VALUES (?, ?, '2026-08-08T00:00:00Z')",
        (show_id, related_show_id),
    )
    conn.commit()


def _insert_franchise(franchise_id: str, name: str) -> None:
    conn = db.get_connection()
    conn.execute("INSERT INTO franchise (id, name) VALUES (?, ?)", (franchise_id, name))
    conn.commit()


async def test_related_shows_reads_both_directions(client):
    show_a = await add_show(client, titleRomaji="Show A")
    show_b = await add_show(client, titleRomaji="Show B")
    show_c = await add_show(client, titleRomaji="Show C")
    # A -> B (A is the source), C -> A (A is the target) — both should
    # surface in A's relatedShows despite being opposite directions.
    _insert_show_relation(show_a["id"], show_b["id"])
    _insert_show_relation(show_c["id"], show_a["id"])

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { relatedShows { edges { node { id } } } } }",
        {"id": show_a["id"]},
        headers=auth_headers(),
    )
    related_ids = {e["node"]["id"] for e in data["show"]["relatedShows"]["edges"]}
    assert related_ids == {show_b["id"], show_c["id"]}


async def test_franchise_member_order_create_then_update(client):
    show = await add_show(client)
    _insert_franchise("f-testfr", "Test Franchise")

    created = await gql(
        client,
        """
        mutation($f: ID!, $s: ID!) {
          setFranchiseMemberOrder(franchiseId: $f, showId: $s, sortOrder: 1) {
            sortOrder franchise { id name } show { id }
          }
        }
        """,
        {"f": "f-testfr", "s": show["id"]},
        headers=auth_headers(),
    )
    entry = created["setFranchiseMemberOrder"]
    assert entry["sortOrder"] == 1
    assert entry["franchise"]["name"] == "Test Franchise"
    assert entry["show"]["id"] == show["id"]

    updated = await gql(
        client,
        """
        mutation($f: ID!, $s: ID!) {
          setFranchiseMemberOrder(franchiseId: $f, showId: $s, sortOrder: 2) { sortOrder }
        }
        """,
        {"f": "f-testfr", "s": show["id"]},
        headers=auth_headers(),
    )
    assert updated["setFranchiseMemberOrder"]["sortOrder"] == 2

    # confirms upsert, not a duplicate row
    members = await gql(
        client,
        "query($id: ID!) { franchise(id: $id) { members { edges { node { sortOrder } } } } }",
        {"id": "f-testfr"},
        headers=auth_headers(),
    )
    assert len(members["franchise"]["members"]["edges"]) == 1
    assert members["franchise"]["members"]["edges"][0]["node"]["sortOrder"] == 2

    from_show = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { franchiseMemberships { edges { node { sortOrder } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert from_show["show"]["franchiseMemberships"]["edges"][0]["node"]["sortOrder"] == 2


async def test_franchise_member_order_requires_existing_franchise_and_show(client):
    show = await add_show(client)
    resp = await client.post(
        "/",
        json={
            "query": (
                'mutation($s: ID!) { setFranchiseMemberOrder(franchiseId: "f-nosuch", '
                "showId: $s, sortOrder: 1) { sortOrder } }"
            ),
            "variables": {"s": show["id"]},
        },
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_next_up_order_create_then_update(client):
    show = await add_show(client)

    created = await gql(
        client,
        """
        mutation($id: ID!) {
          setNextUpOrder(showId: $id, sortOrder: 5) { sortOrder show { id } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert created["setNextUpOrder"]["sortOrder"] == 5
    assert created["setNextUpOrder"]["show"]["id"] == show["id"]

    updated = await gql(
        client,
        'mutation($id: ID!) { setNextUpOrder(showId: $id, sortOrder: 9) { sortOrder } }',
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert updated["setNextUpOrder"]["sortOrder"] == 9

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { nextUpOverride { sortOrder } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # confirms upsert, not a duplicate row
    assert data["show"]["nextUpOverride"]["sortOrder"] == 9
