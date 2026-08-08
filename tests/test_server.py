"""End-to-end: real GraphQL requests (httpx.ASGITransport, no bound
port) against the real ASGI app, over a real migrated SQLite database.
BUILD_PLAN.md A.3's vertical slice — Show/Episode/WatchEvent + their
core mutations, plus the bearer-token/X-LCARS-Client plumbing.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from lcars import anilist_client, config, db, fribb, radarr_client, sonarr_client
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
async def client(migrated_db, monkeypatch):
    db.connect(migrated_db)
    # A.8 — no Sonarr/Radarr credentials by default, so metadata.py's own
    # "not configured, same as not linked" guard skips those branches
    # without a test needing to know anything about them; AniList has no
    # such gate (public endpoint, §5.1's mandatory-for-anime link), so it's
    # stubbed directly here instead — every existing pre-A.8 test that adds
    # an anime show would otherwise make a real network call. Tests that
    # want to exercise the real A.8 fetch behavior re-monkeypatch these
    # themselves (see the "on-demand metadata fetch" test section below).
    config.set_current(config.Config())
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)
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


# --- on-demand metadata fetch (§4 Phase A, A.8) ------------------------------
#
# The `client` fixture stubs anilist_client.fetch_media to a no-op and sets
# empty Sonarr/Radarr config by default, so every test above this section
# never touches these paths for real. These tests re-monkeypatch each client
# to exercise the real addShow-triggers-fetch / refreshShowMetadata behavior.

FAKE_ANILIST_MEDIA = {
    "title": {"romaji": "Golden Kamuy"},
    "coverImage": {"large": "https://anilist.co/img/cover.jpg"},
    "bannerImage": "https://anilist.co/img/banner.jpg",
    "description": "A gold rush story.",
    "genres": ["Action", "Adventure"],
    "episodes": 12,
    "idMal": 99999,
    "studios": {"nodes": [{"id": 501, "name": "Geno Studio"}]},
    "characters": {
        "edges": [
            {
                "role": "MAIN",
                "node": {"id": 701, "name": {"full": "Saichi Sugimoto"}},
                "voiceActors": [{"id": 801, "name": {"full": "Kenta Miyake"}}],
            }
        ]
    },
}

SHOW_METADATA_QUERY = """
    query($id: ID!) {
      show(id: $id) {
        posterUrl bannerUrl synopsis genresRaw totalEpisodes
        seasons { edges { node { seasonNumber anilistId malId source manualOverride } } }
        cast { edges { node { roleType characterName person { name } } } }
        studioCredits { edges { node { roleType studio { name } } } }
      }
    }
"""


class _FakeSonarrClient:
    def __init__(self, series=None, episodes=None):
        self._series = series
        self._episodes = episodes or []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def series_by_tvdb_id(self, tvdb_id):
        return self._series

    def episodes(self, series_id):
        return self._episodes


class _FakeRadarrClient:
    def __init__(self, movie=None):
        self._movie = movie

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def movie_by_tmdb_id(self, tmdb_id):
        return self._movie


async def test_add_show_anilist_fetch_populates_metadata_season_and_cast(client, monkeypatch):
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    show = await add_show(client, anilistId=12345)

    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    result = data["show"]
    assert result["posterUrl"] == "https://anilist.co/img/cover.jpg"
    assert result["bannerUrl"] == "https://anilist.co/img/banner.jpg"
    assert result["synopsis"] == "A gold rush story."
    assert result["genresRaw"] == ["Action", "Adventure"]
    assert result["totalEpisodes"] == 12

    seasons = result["seasons"]["edges"]
    assert len(seasons) == 1
    season = seasons[0]["node"]
    assert season["seasonNumber"] == 1
    assert season["anilistId"] == 12345
    assert season["malId"] == 99999
    assert season["source"] == "MANUAL"
    assert season["manualOverride"] is True

    cast = result["cast"]["edges"]
    assert len(cast) == 1
    assert cast[0]["node"]["roleType"] == "VOICE_ACTOR"
    assert cast[0]["node"]["characterName"] == "Saichi Sugimoto"
    assert cast[0]["node"]["person"]["name"] == "Kenta Miyake"

    studios = result["studioCredits"]["edges"]
    assert len(studios) == 1
    assert studios[0]["node"]["roleType"] == "STUDIO"
    assert studios[0]["node"]["studio"]["name"] == "Geno Studio"


async def test_add_show_anilist_fetch_reuses_existing_studio_and_person(client, monkeypatch):
    """Upsert-by-AniList-id (metadata.py's own _link_studio/_link_person) —
    two shows sharing a studio/voice actor shouldn't duplicate either row."""
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    await add_show(client, anilistId=111, titleRomaji="Show One")
    await add_show(client, anilistId=222, titleRomaji="Show Two")

    data = await gql(
        client,
        "query { people { edges { node { id } } } studios { edges { node { id } } } }",
        headers=auth_headers(),
    )
    assert len(data["people"]["edges"]) == 1
    assert len(data["studios"]["edges"]) == 1


async def test_add_show_anilist_fetch_no_media_found_leaves_show_bare(client, monkeypatch):
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)
    show = await add_show(client, anilistId=12345)
    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["posterUrl"] is None
    assert data["show"]["seasons"]["edges"] == []


async def test_add_show_sonarr_fetch_creates_episodes(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2026-01-01T00:00:00Z",
            "runtime": 24,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1), _ep(2)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            episodes { edges { node { season episode airDateUtc runtimeMinutes } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    episodes = data["show"]["episodes"]["edges"]
    assert len(episodes) == 2
    assert {e["node"]["episode"] for e in episodes} == {1, 2}
    assert episodes[0]["node"]["runtimeMinutes"] == 24


async def test_add_show_sonarr_fetch_no_op_when_not_in_sonarr_library(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    fake = _FakeSonarrClient(series=None)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["episodes"]["edges"] == []


async def test_add_show_radarr_fetch_populates_movie_metadata(client, monkeypatch):
    config.set_current(config.Config(radarr_url="http://radarr:7878", radarr_api_key="key"))
    movie = {
        "overview": "A boy and a girl reconnect.",
        "genres": ["Drama", "Romance"],
        "images": [
            {"coverType": "fanart", "remoteUrl": "https://radarr.example/fanart.jpg"},
            {"coverType": "poster", "remoteUrl": "https://radarr.example/poster.jpg"},
        ],
    }
    fake = _FakeRadarrClient(movie=movie)
    monkeypatch.setattr(radarr_client, "RadarrClient", lambda *a, **kw: fake)
    show = await add_show(
        client, mediaShape="MOVIE", trackingSpace="TV", titleRomaji="A Silent Voice", tmdbId=555
    )
    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["posterUrl"] == "https://radarr.example/poster.jpg"
    assert data["show"]["synopsis"] == "A boy and a girl reconnect."
    assert data["show"]["genresRaw"] == ["Drama", "Romance"]


async def test_add_show_metadata_fetch_skips_silently_when_not_configured(client):
    """Default fixture: no Sonarr/Radarr config at all — confirms this is
    treated the same as "not linked", not a failure worth a pending_review."""
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)
    reviews = await _pending_reviews_for(client, show["id"])
    assert reviews == []


async def test_add_show_fetch_failure_logs_pending_review_and_refresh_retries(client, monkeypatch):
    monkeypatch.setattr(
        anilist_client,
        "fetch_media",
        lambda *a, **kw: (_ for _ in ()).throw(anilist_client.AniListError("Could not connect")),
    )
    show = await add_show(client, anilistId=12345)

    bare = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert bare["show"]["posterUrl"] is None

    reviews = await _pending_reviews_for(client, show["id"])
    assert len(reviews) == 1
    assert reviews[0]["entityType"] == "show"
    assert reviews[0]["field"] == "metadata_fetch"
    assert reviews[0]["source"] == "anilist"
    assert reviews[0]["proposedValueChain"] == ["Could not connect"]

    # whatever was unreachable is back — retry via refreshShowMetadata,
    # the manual-fix-by-user path (confirmed 2026-08-08)
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    refreshed = await gql(
        client,
        'mutation($id: ID!) { refreshShowMetadata(showId: $id) { posterUrl } }',
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert refreshed["refreshShowMetadata"]["posterUrl"] == "https://anilist.co/img/cover.jpg"


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


# --- show_service_presence fuzzy matching (§5.4, A.7) -----------------------
#
# fuzzy.best_match's own algorithm is covered in tests/test_fuzzy.py; this
# file covers refreshShowServicePresence's wiring (upsert, which title
# variants get compared, present flag written correctly).

REFRESH_SERVICE_PRESENCE = """
    mutation($id: ID!, $service: String!, $titles: [String!]!) {
      refreshShowServicePresence(showId: $id, service: $service, candidateTitles: $titles) {
        id show { id } service present checkedAt
      }
    }
"""


async def test_refresh_show_service_presence_creates_then_updates(client):
    show = await add_show(client, titleRomaji="Golden Kamuy")

    found = await gql(
        client,
        REFRESH_SERVICE_PRESENCE,
        {"id": show["id"], "service": "sonarr", "titles": ["Golden Kamuy", "Other Show"]},
        headers=auth_headers(),
    )
    presence = found["refreshShowServicePresence"]
    assert presence["present"] is True
    assert presence["service"] == "sonarr"
    assert presence["show"]["id"] == show["id"]

    # a later call with no matching candidate flips it back to absent —
    # passive/self-healing, same row (upsert on show_id+service), not a
    # second one
    absent = await gql(
        client,
        REFRESH_SERVICE_PRESENCE,
        {"id": show["id"], "service": "sonarr", "titles": ["Completely Different Title"]},
        headers=auth_headers(),
    )
    assert absent["refreshShowServicePresence"]["id"] == presence["id"]
    assert absent["refreshShowServicePresence"]["present"] is False


async def test_refresh_show_service_presence_no_candidates_is_absent(client):
    show = await add_show(client, titleRomaji="Golden Kamuy")
    data = await gql(
        client,
        REFRESH_SERVICE_PRESENCE,
        {"id": show["id"], "service": "radarr", "titles": []},
        headers=auth_headers(),
    )
    assert data["refreshShowServicePresence"]["present"] is False


async def test_refresh_show_service_presence_independent_per_service(client):
    show = await add_show(client, titleRomaji="Golden Kamuy")
    await gql(
        client,
        REFRESH_SERVICE_PRESENCE,
        {"id": show["id"], "service": "sonarr", "titles": ["Golden Kamuy"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        REFRESH_SERVICE_PRESENCE,
        {"id": show["id"], "service": "radarr", "titles": []},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { servicePresence { edges { node { service present } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    by_service = {
        e["node"]["service"]: e["node"]["present"]
        for e in data["show"]["servicePresence"]["edges"]
    }
    assert by_service == {"sonarr": True, "radarr": False}


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


async def test_set_season_mapping_creates_then_updates(client):
    """§5.5/A.4: replaces the old show_id_mapping test. Also exercises
    the exact scenario the season entity exists for — two seasons of
    the same show resolving to two different AniList ids, which the
    old show-scoped mapping couldn't represent at all."""
    show = await add_show(client)

    season1 = await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 111, malId: 211) {
            id seasonNumber anilistId malId source matched manualOverride show { id }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    mapping = season1["setSeasonMapping"]
    assert mapping["seasonNumber"] == 1
    assert mapping["anilistId"] == 111
    assert mapping["malId"] == 211
    assert mapping["source"] == "MANUAL"
    assert mapping["matched"] is True
    assert mapping["manualOverride"] is True
    assert mapping["show"]["id"] == show["id"]

    # a second season of the SAME show, a DIFFERENT AniList id — the
    # exact case a single show-level anilist_id column couldn't hold
    season2 = await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 2, anilistId: 222, malId: 222) {
            seasonNumber anilistId
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert season2["setSeasonMapping"]["seasonNumber"] == 2
    assert season2["setSeasonMapping"]["anilistId"] == 222

    updated = await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 999, malId: 211) {
            id anilistId
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # same underlying row (upsert), not a second one
    assert updated["setSeasonMapping"]["id"] == mapping["id"]
    assert updated["setSeasonMapping"]["anilistId"] == 999

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { seasons { edges { node { seasonNumber anilistId } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    seasons = {
        e["node"]["seasonNumber"]: e["node"]["anilistId"]
        for e in data["show"]["seasons"]["edges"]
    }
    assert seasons == {1: 999, 2: 222}


async def test_episode_season_entity_link(client, migrated_db):
    show = await add_show(client)
    season = await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 111, malId: 211) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    season_id = season["setSeasonMapping"]["id"]

    # episodes aren't addable via the API yet (A.8) — link one directly,
    # same convention as every other episode fixture in this file
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode
            (id, show_id, season, season_id, episode, kind, state, created_at, updated_at)
        VALUES (
            'e-season', ?, 1, ?, 1, 'regular', 'unwatched',
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (show["id"], season_id),
    )
    conn.commit()

    data = await gql(
        client,
        "query($id: ID!) { episode(id: $id) { seasonEntity { id anilistId } } }",
        {"id": "e-season"},
        headers=auth_headers(),
    )
    assert data["episode"]["seasonEntity"]["id"] == season_id
    assert data["episode"]["seasonEntity"]["anilistId"] == 111


# --- automatic id-mapper reconciliation (§5.5, A.4) -------------------------
#
# fribb.load_dataset is monkeypatched throughout so these tests never make a
# real network call — the download/cache mechanics themselves are covered by
# tests/test_fribb.py; this file covers reconcileSeasonMapping's own logic
# (tvdb lookup, apply-immediately, manual_override protection, pending_review
# open/skip behavior).

FAKE_FRIBB_DATASET = [
    {"tvdb_id": 555, "anilist_id": 111, "mal_id": 211, "season": {"tvdb": 1}},
    {"tvdb_id": 555, "anilist_id": 222, "mal_id": 222, "season": {"tvdb": 2}},
]


def _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET):
    monkeypatch.setattr(fribb, "load_dataset", lambda: dataset)


async def _link_tvdb(client, show_id, tvdb_id):
    await gql(
        client,
        """
        mutation($id: ID!, $externalId: String!) {
          linkShowExternalId(showId: $id, service: "tvdb", externalId: $externalId, url: "https://x")
            { service }
        }
        """,
        {"id": show_id, "externalId": str(tvdb_id)},
        headers=auth_headers(),
    )


async def _pending_reviews_for(client, entity_id):
    reviews = await gql(
        client,
        """
        query {
          pendingReviews {
            edges { node { entityType entityId field source previousValue proposedValueChain } }
          }
        }
        """,
        headers=auth_headers(),
    )
    return [
        e["node"] for e in reviews["pendingReviews"]["edges"] if e["node"]["entityId"] == entity_id
    ]


RECONCILE_SEASON_MAPPING = """
    mutation($id: ID!, $season: Int!) {
      reconcileSeasonMapping(showId: $id, seasonNumber: $season) {
        id anilistId malId source matched manualOverride
      }
    }
"""


async def _reconcile(client, show_id, season_number):
    data = await gql(
        client,
        RECONCILE_SEASON_MAPPING,
        {"id": show_id, "season": season_number},
        headers=auth_headers(),
    )
    return data["reconcileSeasonMapping"]


async def test_reconcile_season_mapping_no_tvdb_link_logs_no_candidate_review(client, monkeypatch):
    _patch_fribb_dataset(monkeypatch)
    show = await add_show(client)

    season = await _reconcile(client, show["id"], 1)
    assert season["matched"] is False
    assert season["source"] == "UNMATCHED"
    assert season["anilistId"] is None
    assert season["malId"] is None

    reviews = await _pending_reviews_for(client, season["id"])
    assert len(reviews) == 1
    assert reviews[0]["entityType"] == "season"
    assert reviews[0]["field"] == "anilist_id"
    assert reviews[0]["source"] == "fribb"


async def test_reconcile_season_mapping_matches_via_linked_tvdb_id(client, monkeypatch):
    _patch_fribb_dataset(monkeypatch)
    show = await add_show(client)
    await _link_tvdb(client, show["id"], 555)

    season = await _reconcile(client, show["id"], 1)
    assert season["anilistId"] == 111
    assert season["malId"] == 211
    assert season["source"] == "FRIBB"
    assert season["matched"] is True
    assert season["manualOverride"] is False
    assert await _pending_reviews_for(client, season["id"]) == []

    # a second season of the same show resolves independently, via the same
    # tvdb_id disambiguated by season_number — the exact scenario A.4 exists
    # for (the retired show_id_mapping table couldn't hold both at once)
    season2 = await _reconcile(client, show["id"], 2)
    assert season2["anilistId"] == 222

    # re-running the first season against an unchanged dataset is a clean
    # match, not a discrepancy — confirms unchanged re-checks stay silent
    await _reconcile(client, show["id"], 1)
    assert await _pending_reviews_for(client, season["id"]) == []


async def test_reconcile_season_mapping_discrepancy_applies_immediately_and_logs(
    client, monkeypatch
):
    show = await add_show(client)
    await _link_tvdb(client, show["id"], 555)

    _patch_fribb_dataset(
        monkeypatch, [{"tvdb_id": 555, "anilist_id": 111, "mal_id": 211, "season": {"tvdb": 1}}]
    )
    first = await _reconcile(client, show["id"], 1)
    season_id = first["id"]
    assert first["anilistId"] == 111

    # the dataset later disagrees with what's already stored — §3 principle
    # 1: applied immediately, logged for after-the-fact awareness
    _patch_fribb_dataset(
        monkeypatch, [{"tvdb_id": 555, "anilist_id": 999, "mal_id": 211, "season": {"tvdb": 1}}]
    )
    second = await _reconcile(client, show["id"], 1)
    assert second["id"] == season_id
    assert second["anilistId"] == 999

    all_reviews = await _pending_reviews_for(client, season_id)
    reviews = [r for r in all_reviews if r["field"] == "anilist_id"]
    assert len(reviews) == 1
    assert reviews[0]["previousValue"] == "111"
    assert reviews[0]["proposedValueChain"] == ["999"]

    # a THIRD disagreement before the first is ever resolved extends the
    # same entry's value chain rather than opening a duplicate (§5.6)
    _patch_fribb_dataset(
        monkeypatch, [{"tvdb_id": 555, "anilist_id": 777, "mal_id": 211, "season": {"tvdb": 1}}]
    )
    await _reconcile(client, show["id"], 1)
    all_reviews = await _pending_reviews_for(client, season_id)
    reviews = [r for r in all_reviews if r["field"] == "anilist_id"]
    assert len(reviews) == 1  # still one entry, not two
    assert reviews[0]["previousValue"] == "111"  # unchanged — value before the FIRST change
    assert reviews[0]["proposedValueChain"] == ["999", "777"]


async def test_reconcile_season_mapping_never_overwrites_manual_override(client, monkeypatch):
    show = await add_show(client)
    await _link_tvdb(client, show["id"], 555)
    manual = await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 42, malId: 42) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    season_id = manual["setSeasonMapping"]["id"]

    _patch_fribb_dataset(
        monkeypatch, [{"tvdb_id": 555, "anilist_id": 999, "mal_id": 999, "season": {"tvdb": 1}}]
    )
    data = await gql(
        client, RECONCILE_SEASON_MAPPING, {"id": show["id"], "season": 1}, headers=auth_headers()
    )
    season = data["reconcileSeasonMapping"]
    assert season["id"] == season_id
    assert season["anilistId"] == 42  # untouched — manual wins (§3 principle 6)
    assert season["malId"] == 42
    assert season["manualOverride"] is True

    # no review noise for a disagreement against something already manually
    # decided (asked/confirmed 2026-08-08, A.4)
    assert await _pending_reviews_for(client, season_id) == []


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
    """reconcileSeasonMapping (A.4) is the one mutation that creates
    pending_review rows today, and only for season-mapping discrepancies
    — inserting directly here keeps these resolve/query tests about a
    generic row, not coupled to season's own specifics."""
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


# --- custom tags (§5.1) — the only user-created entity in this slice --------


async def test_create_tag_rejects_duplicate_name(client):
    created = await gql(
        client,
        'mutation { createTag(name: "favorites") { id name } }',
        headers=auth_headers(),
    )
    assert created["createTag"]["name"] == "favorites"

    resp = await client.post(
        "/",
        json={"query": 'mutation { createTag(name: "favorites") { id } }'},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "already exists" in body["errors"][0]["message"]


async def test_add_and_remove_show_tag_both_directions_queryable(client):
    show = await add_show(client)
    tag = await gql(
        client, 'mutation { createTag(name: "must-rewatch") { id } }', headers=auth_headers()
    )
    tag_id = tag["createTag"]["id"]

    await gql(
        client,
        "mutation($s: ID!, $t: ID!) { addShowTag(showId: $s, tagId: $t) { id } }",
        {"s": show["id"], "t": tag_id},
        headers=auth_headers(),
    )

    from_show = await gql(
        client,
        "query($id: ID!) { show(id: $id) { tags { edges { node { id name } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert [e["node"]["name"] for e in from_show["show"]["tags"]["edges"]] == ["must-rewatch"]

    from_tag = await gql(
        client,
        "query($id: ID!) { tag(id: $id) { shows { edges { node { id } } } } }",
        {"id": tag_id},
        headers=auth_headers(),
    )
    assert [e["node"]["id"] for e in from_tag["tag"]["shows"]["edges"]] == [show["id"]]

    # adding the same tag twice doesn't duplicate the association
    await gql(
        client,
        "mutation($s: ID!, $t: ID!) { addShowTag(showId: $s, tagId: $t) { id } }",
        {"s": show["id"], "t": tag_id},
        headers=auth_headers(),
    )
    again = await gql(
        client,
        "query($id: ID!) { show(id: $id) { tags { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert len(again["show"]["tags"]["edges"]) == 1

    await gql(
        client,
        "mutation($s: ID!, $t: ID!) { removeShowTag(showId: $s, tagId: $t) { id } }",
        {"s": show["id"], "t": tag_id},
        headers=auth_headers(),
    )
    after_remove = await gql(
        client,
        "query($id: ID!) { show(id: $id) { tags { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert after_remove["show"]["tags"]["edges"] == []


async def test_delete_tag_cascades_from_shows(client):
    show = await add_show(client)
    tag = await gql(
        client, 'mutation { createTag(name: "temp-tag") { id } }', headers=auth_headers()
    )
    tag_id = tag["createTag"]["id"]
    await gql(
        client,
        "mutation($s: ID!, $t: ID!) { addShowTag(showId: $s, tagId: $t) { id } }",
        {"s": show["id"], "t": tag_id},
        headers=auth_headers(),
    )

    result = await gql(
        client,
        "mutation($id: ID!) { deleteTag(tagId: $id) }",
        {"id": tag_id},
        headers=auth_headers(),
    )
    assert result["deleteTag"] is True

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { tags { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["tags"]["edges"] == []

    resp = await client.post(
        "/",
        json={
            "query": 'mutation($id: ID!) { deleteTag(tagId: $id) }',
            "variables": {"id": tag_id},
        },
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_tags_top_level_query(client):
    await gql(
        client, 'mutation { createTag(name: "list-me") { id } }', headers=auth_headers()
    )
    data = await gql(client, "{ tags { edges { node { name } } } }", headers=auth_headers())
    assert "list-me" in [e["node"]["name"] for e in data["tags"]["edges"]]


# --- saved filter presets (§5.10) — the last table in §5's document order ---


async def test_create_filter_preset(client):
    created = await gql(
        client,
        """
        mutation {
          createFilterPreset(name: "Watching Anime", filterJson: "{\\"status\\":\\"WATCHING\\"}") {
            id name filterJson
          }
        }
        """,
        headers=auth_headers(),
    )
    preset = created["createFilterPreset"]
    assert preset["name"] == "Watching Anime"
    assert preset["filterJson"] == '{"status":"WATCHING"}'

    fetched = await gql(
        client,
        "query($id: ID!) { filterPreset(id: $id) { name } }",
        {"id": preset["id"]},
        headers=auth_headers(),
    )
    assert fetched["filterPreset"]["name"] == "Watching Anime"


async def test_create_filter_preset_does_not_require_unique_name(client):
    """Unlike tag.name, filter_preset.name has no UNIQUE constraint
    (migration 7196ca889757) — freely editable, not fixed (§5.10)."""
    first = await gql(
        client,
        'mutation { createFilterPreset(name: "Dup", filterJson: "{}") { id } }',
        headers=auth_headers(),
    )
    second = await gql(
        client,
        'mutation { createFilterPreset(name: "Dup", filterJson: "{}") { id } }',
        headers=auth_headers(),
    )
    assert first["createFilterPreset"]["id"] != second["createFilterPreset"]["id"]


async def test_update_filter_preset_is_a_partial_update(client):
    created = await gql(
        client,
        'mutation { createFilterPreset(name: "Original", filterJson: "{\\"a\\":1}") { id } }',
        headers=auth_headers(),
    )
    preset_id = created["createFilterPreset"]["id"]

    # updating just the name leaves filterJson untouched
    renamed = await gql(
        client,
        """
        mutation($id: ID!) {
          updateFilterPreset(id: $id, name: "Renamed") { name filterJson }
        }
        """,
        {"id": preset_id},
        headers=auth_headers(),
    )
    assert renamed["updateFilterPreset"]["name"] == "Renamed"
    assert renamed["updateFilterPreset"]["filterJson"] == '{"a":1}'

    # updating just filterJson leaves the (already-renamed) name untouched
    rejsoned = await gql(
        client,
        """
        mutation($id: ID!) {
          updateFilterPreset(id: $id, filterJson: "{\\"b\\":2}") { name filterJson }
        }
        """,
        {"id": preset_id},
        headers=auth_headers(),
    )
    assert rejsoned["updateFilterPreset"]["name"] == "Renamed"
    assert rejsoned["updateFilterPreset"]["filterJson"] == '{"b":2}'


async def test_update_filter_preset_requires_existing_id(client):
    resp = await client.post(
        "/",
        json={
            "query": (
                'mutation { updateFilterPreset(id: "q-nosuch", name: "X") { id } }'
            )
        },
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_delete_filter_preset(client):
    created = await gql(
        client,
        'mutation { createFilterPreset(name: "Temp", filterJson: "{}") { id } }',
        headers=auth_headers(),
    )
    preset_id = created["createFilterPreset"]["id"]

    result = await gql(
        client,
        "mutation($id: ID!) { deleteFilterPreset(id: $id) }",
        {"id": preset_id},
        headers=auth_headers(),
    )
    assert result["deleteFilterPreset"] is True

    resp = await client.post(
        "/",
        json={
            "query": "mutation($id: ID!) { deleteFilterPreset(id: $id) }",
            "variables": {"id": preset_id},
        },
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_filter_presets_top_level_query(client):
    await gql(
        client,
        'mutation { createFilterPreset(name: "List Me", filterJson: "{}") { id } }',
        headers=auth_headers(),
    )
    data = await gql(
        client, "{ filterPresets { edges { node { name } } } }", headers=auth_headers()
    )
    assert "List Me" in [e["node"]["name"] for e in data["filterPresets"]["edges"]]


# --- episodesAiringSoon (§8) — the last piece genuinely scoped to A.3 -------
#
# search/stats/nextUp/deletion/export-import each have their own dedicated,
# separately-numbered BUILD_PLAN.md steps (A.11-A.15); backlog is Phase B's
# own step (B.9, matching §6.3's "(Phase B)" label). episodesAiringSoon has
# neither a later dedicated step nor a phase-B data dependency, so it's the
# one remaining piece that was actually A.3's job.


def _iso(offset_days: float) -> str:
    return (datetime.now(UTC) + timedelta(days=offset_days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _insert_episode_with_air_date(
    migrated_db: Path, episode_id: str, show_id: str, air_date_utc: str | None
) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode
            (id, show_id, season, episode, kind, air_date_utc, state, created_at, updated_at)
        VALUES (
            ?, ?, 1, 1, 'regular', ?, 'unwatched',
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (episode_id, show_id, air_date_utc),
    )
    conn.commit()


async def test_episodes_airing_soon_filters_by_date_window(client, migrated_db):
    show_a = await add_show(client, titleRomaji="Show A")
    show_b = await add_show(client, titleRomaji="Show B")
    show_c = await add_show(client, titleRomaji="Show C")
    show_d = await add_show(client, titleRomaji="Show D")

    _insert_episode_with_air_date(migrated_db, "e-past01", show_a["id"], _iso(-1))  # already aired
    _insert_episode_with_air_date(migrated_db, "e-soon01", show_b["id"], _iso(2))  # within window
    _insert_episode_with_air_date(migrated_db, "e-later1", show_c["id"], _iso(10))  # outside window
    _insert_episode_with_air_date(migrated_db, "e-nodate", show_d["id"], None)  # unknown air date

    data = await gql(
        client,
        "{ episodesAiringSoon(days: 3) { edges { node { id } } } }",
        headers=auth_headers(),
    )
    ids = {e["node"]["id"] for e in data["episodesAiringSoon"]["edges"]}
    assert ids == {"e-soon01"}
