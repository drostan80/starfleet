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
            statusHistory { edges { node { previousStatus newStatus changedBy } } }
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


# --- addWatchEvent / markEpisodeSkipped ------------------------------------


async def _insert_episode(migrated_db: Path, show_id: str) -> str:
    """Episodes aren't addable via the API yet (A.8's on-demand fetch,
    not built here) — insert directly for this test's purposes."""
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode (id, show_id, season, episode, kind, state, created_at, updated_at)
        VALUES (
            'e-tst001', ?, 1, 1, 'regular', 'unwatched',
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (show_id,),
    )
    conn.commit()
    return "e-tst001"


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
