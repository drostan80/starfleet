"""End-to-end: real GraphQL requests (httpx.ASGITransport, no bound
port) against the real ASGI app, over a real migrated SQLite database.
BUILD_PLAN.md A.3's vertical slice — Show/Episode/WatchEvent + their
core mutations, plus the bearer-token/X-LCARS-Client plumbing.
"""

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from lcars import (
    anilist_client,
    config,
    db,
    export_import,
    fribb,
    mal_client,
    radarr_client,
    sonarr_client,
    tmdb_client,
    util,
)
from lcars.server import build_app

BEARER_TOKEN = "test-token-123"


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
    # B.4 — same reasoning: metadata.py's _reconcile_air_dates calls this
    # once per season with a real anilist_id, so any test that overrides
    # fetch_media above to return real data (setting season 1's anilist_id
    # via _upsert_season) would otherwise make a real network call here
    # too. Tests exercising the real reconciliation behavior re-monkeypatch
    # this themselves (see the "AniList airingSchedule reconciliation"
    # test section below).
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    # A.20 — same reasoning as the AniList stub above: _fetch_sonarr now
    # reconciles any newly-discovered season against the Fribb dataset
    # immediately (season_mapping.reconcile_season), so every pre-A.20
    # Sonarr-fetch test that links a tvdb id would otherwise make a real
    # network call too. Tests exercising real Fribb matching already
    # override this via their own _patch_fribb_dataset() call below.
    monkeypatch.setattr(fribb, "load_dataset", lambda: [])
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
            "query": "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }",
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
    "duration": 24,
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
        posterUrl bannerUrl synopsis genresRaw totalEpisodes durationMinutes
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


class _FakeTmdbClient:
    def __init__(self, tvdb_to_tmdb=None, movie_runtime=None, tv_runtime=None, error=None):
        self._tvdb_to_tmdb = tvdb_to_tmdb or {}
        self._movie_runtime = movie_runtime
        self._tv_runtime = tv_runtime
        self._error = error
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        pass

    def find_by_tvdb_id(self, tvdb_id):
        self.calls.append(("find_by_tvdb_id", tvdb_id))
        if self._error is not None:
            raise self._error
        return self._tvdb_to_tmdb.get(tvdb_id)

    def movie_runtime(self, tmdb_id):
        self.calls.append(("movie_runtime", tmdb_id))
        if self._error is not None:
            raise self._error
        return self._movie_runtime

    def tv_episode_runtime(self, tmdb_id):
        self.calls.append(("tv_episode_runtime", tmdb_id))
        if self._error is not None:
            raise self._error
        return self._tv_runtime


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
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    show = await add_show(client, anilistId=12345)

    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    result = data["show"]
    assert result["posterUrl"] == "https://anilist.co/img/cover.jpg"
    assert result["bannerUrl"] == "https://anilist.co/img/banner.jpg"
    assert result["synopsis"] == "A gold rush story."
    assert result["genresRaw"] == ["Action", "Adventure"]
    assert result["totalEpisodes"] == 12
    assert result["durationMinutes"] == 24

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
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    await add_show(client, anilistId=111, titleRomaji="Show One")
    await add_show(client, anilistId=222, titleRomaji="Show Two")

    data = await gql(
        client,
        "query { people { edges { node { id } } } studios { edges { node { id } } } }",
        headers=auth_headers(),
    )
    assert len(data["people"]["edges"]) == 1
    assert len(data["studios"]["edges"]) == 1


FAKE_ANILIST_MEDIA_WITH_RELATIONS = {
    **FAKE_ANILIST_MEDIA,
    "relations": {
        "edges": [
            {
                "node": {
                    "id": 333,
                    "idMal": 433,
                    "format": "TV",
                    "title": {"romaji": "Golden Kamuy 2", "english": None, "native": None},
                }
            },
            {
                "node": {
                    "id": 444,
                    "idMal": None,
                    "format": "MOVIE",
                    "title": {"romaji": "Golden Kamuy Movie", "english": None, "native": None},
                }
            },
            {
                "node": {
                    "id": 999,
                    "idMal": None,
                    "format": "MANGA",
                    "title": {"romaji": "Golden Kamuy (manga)", "english": None, "native": None},
                }
            },
        ]
    },
}


async def test_add_show_anilist_fetch_creates_relation_stub_shows(client, monkeypatch):
    """A.21 (2026-08-09 consolidation audit) — show_relation.related_show_id
    is a real, non-nullable FK; a relation to a show LCARS has never seen
    must auto-create a tracked=false stub (§5.1's own promotion-target
    framing), not fail or silently drop the edge. The MANGA-format relation
    must be skipped entirely — not a show LCARS can ever track."""
    monkeypatch.setattr(
        anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA_WITH_RELATIONS
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    show = await add_show(client, anilistId=111)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            relatedShows {
              edges { node { displayTitle mediaShape trackingSpace tracked status } }
            }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    related = {e["node"]["displayTitle"]: e["node"] for e in data["show"]["relatedShows"]["edges"]}
    assert set(related) == {"Golden Kamuy 2", "Golden Kamuy Movie"}  # manga relation excluded
    assert related["Golden Kamuy 2"]["mediaShape"] == "EPISODIC"
    assert related["Golden Kamuy 2"]["trackingSpace"] == "ANIME"
    assert related["Golden Kamuy 2"]["tracked"] is False
    assert related["Golden Kamuy 2"]["status"] == "PLANNED"
    assert related["Golden Kamuy Movie"]["mediaShape"] == "MOVIE"

    stub_data = await gql(
        client,
        """
        query {
          shows(first: 10) {
            edges { node { displayTitle externalIds { edges { node { service externalId } } } } }
          }
        }
        """,
        headers=auth_headers(),
    )
    stub = next(
        e["node"]
        for e in stub_data["shows"]["edges"]
        if e["node"]["displayTitle"] == "Golden Kamuy 2"
    )
    links = {e["node"]["service"]: e["node"]["externalId"] for e in stub["externalIds"]["edges"]}
    assert links["anilist"] == "333"
    assert links["mal"] == "433"


async def test_add_show_anilist_fetch_relation_reuses_existing_show(client, monkeypatch):
    """When the related AniList id is already a real, tracked show in
    LCARS, the edge must point at that show — no duplicate stub."""
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    existing = await add_show(client, anilistId=333, titleRomaji="Golden Kamuy 2")

    monkeypatch.setattr(
        anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA_WITH_RELATIONS
    )
    show = await add_show(client, anilistId=111)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { relatedShows { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    related_ids = {e["node"]["id"] for e in data["show"]["relatedShows"]["edges"]}
    assert existing["id"] in related_ids

    all_shows = await gql(
        client,
        "query { shows(first: 10) { edges { node { id } } } }",
        headers=auth_headers(),
    )
    assert len(all_shows["shows"]["edges"]) == 3  # existing + the new show + the one real stub


async def test_add_show_promotes_an_existing_untracked_stub_instead_of_duplicating(
    client, monkeypatch
):
    """B.11d follow-up, real bug found in the live backfill run: a
    relation walk (A.21, previous test) can auto-create a tracked=false
    stub for an id a *later*, independent addShow/backfill call also
    targets. That second call must promote the existing stub in place
    (SCOPE.md §5.1's own documented path) — not insert a second `show`
    row for the same AniList id (confirmed live: 86 such collision
    pairs, one show's own show_external_id.anilist_id shared by two
    separate show rows)."""
    monkeypatch.setattr(
        anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA_WITH_RELATIONS
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    await add_show(client, anilistId=111)

    before = await gql(
        client, "query { shows(first: 10) { edges { node { id } } } }", headers=auth_headers()
    )
    stub_data = await gql(
        client,
        """
        query {
          shows(first: 10) {
            edges {
              node {
                id displayTitle tracked
                externalIds { edges { node { service externalId } } }
              }
            }
          }
        }
        """,
        headers=auth_headers(),
    )
    stub = next(
        e["node"]
        for e in stub_data["shows"]["edges"]
        if e["node"]["displayTitle"] == "Golden Kamuy 2"
    )
    assert stub["tracked"] is False

    promoted = await add_show(
        client, anilistId=333, tvdbId=98765, titleRomaji="Golden Kamuy 2 (direct add)"
    )
    assert promoted["id"] == stub["id"]  # same row, not a new one
    assert promoted["tracked"] is True

    after = await gql(
        client, "query { shows(first: 10) { edges { node { id } } } }", headers=auth_headers()
    )
    assert len(after["shows"]["edges"]) == len(before["shows"]["edges"])  # no new row appeared

    links_data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { externalIds { edges { node { service externalId } } } }
        }
        """,
        {"id": stub["id"]},
        headers=auth_headers(),
    )
    links = {
        e["node"]["service"]: e["node"]["externalId"]
        for e in links_data["show"]["externalIds"]["edges"]
    }
    assert links["anilist"] == "333"  # carried over from the stub, untouched
    assert links["tvdb"] == "98765"  # added by the promoting call, the stub never had one


async def test_add_show_rejects_a_duplicate_external_id_already_tracked(client):
    """The other half of the same fix: a match against an
    already-tracked show (not an untracked stub) is a genuine
    duplicate-add attempt, rejected outright rather than silently
    creating a second row for it."""
    await add_show(client, anilistId=555, titleRomaji="Already Tracked")
    resp = await client.post(
        "/",
        json={
            "query": """
                mutation($input: AddShowInput!) {
                  addShow(input: $input) { id }
                }
            """,
            "variables": {
                "input": {
                    "mediaShape": "EPISODIC",
                    "trackingSpace": "ANIME",
                    "titleRomaji": "Already Tracked Again",
                    "primaryTitle": "ROMAJI",
                    "anilistId": 555,
                }
            },
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "already" in body["errors"][0]["message"].lower()

    data = await gql(
        client, "query { shows(first: 10) { edges { node { id } } } }", headers=auth_headers()
    )
    assert len(data["shows"]["edges"]) == 1  # the rejected attempt created nothing


FAKE_ANILIST_MEDIA_WITH_MAL_COLLIDING_RELATIONS = {
    **FAKE_ANILIST_MEDIA,
    "relations": {
        "edges": [
            {
                "node": {
                    "id": 501,
                    "idMal": 601,  # same MAL id as the edge below — real AniList shape,
                    "format": "TV",  # e.g. Ao Haru Ride PAGE.13 / unwritten, both mal 24151
                    "title": {"romaji": "Split Part A", "english": None, "native": None},
                }
            },
            {
                "node": {
                    "id": 502,
                    "idMal": 601,
                    "format": "TV",
                    "title": {"romaji": "Split Part B", "english": None, "native": None},
                }
            },
        ]
    },
}


async def test_add_show_anilist_fetch_relations_sharing_one_mal_id_reuse_one_stub(
    client, monkeypatch
):
    """B.11d/B.11e follow-up, real bug found in the second deployed
    backfill run: two AniList relation entries can be genuinely
    distinct Media (different anilist_id) while AniList reports the
    *same* idMal for both — the old anilist_id-only existing-show check
    let each one create its own stub, producing two show rows sharing
    one mal_id. The second edge must reuse the first edge's own stub
    instead of creating a duplicate."""
    monkeypatch.setattr(
        anilist_client,
        "fetch_media",
        lambda *a, **kw: FAKE_ANILIST_MEDIA_WITH_MAL_COLLIDING_RELATIONS,
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    show = await add_show(client, anilistId=111)

    stub_data = await gql(
        client,
        """
        query {
          shows(first: 10) {
            edges { node { displayTitle externalIds { edges { node { service externalId } } } } }
          }
        }
        """,
        headers=auth_headers(),
    )
    stubs = [
        e["node"]
        for e in stub_data["shows"]["edges"]
        if e["node"]["displayTitle"] in ("Split Part A", "Split Part B")
    ]
    assert len(stubs) == 1  # not two — the second edge reused the first's own stub
    links = {
        e["node"]["service"]: e["node"]["externalId"] for e in stubs[0]["externalIds"]["edges"]
    }
    assert links["anilist"] == "501"  # the first edge's own id, created first
    assert links["mal"] == "601"

    related = await gql(
        client,
        "query($id: ID!) { show(id: $id) { relatedShows { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert len(related["show"]["relatedShows"]["edges"]) == 1  # both edges point at one show


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


async def test_add_show_sonarr_fetch_creates_season_rows_and_sets_episode_season_id(
    client, monkeypatch
):
    """A.20 (2026-08-09 consolidation audit) — the real gap: episodes
    arriving from Sonarr for a season number never seen before must get
    a `season` row (§5.5) and their own `episode.season_id` set, not just
    an episode row with no season entity behind it."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)  # tvdb_id 555, seasons 1+2

    def _ep(season, number):
        return {"seasonNumber": season, "episodeNumber": number, "airDateUtc": None, "runtime": 24}

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1, 1), _ep(2, 1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            seasons { edges { node { seasonNumber anilistId malId source } } }
            episodes { edges { node { season seasonEntity { seasonNumber anilistId } } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    seasons = {s["node"]["seasonNumber"]: s["node"] for s in data["show"]["seasons"]["edges"]}
    assert seasons[1]["anilistId"] == 111
    assert seasons[2]["anilistId"] == 222
    assert seasons[1]["source"] == "FRIBB"

    episodes = {e["node"]["season"]: e["node"] for e in data["show"]["episodes"]["edges"]}
    assert episodes[1]["seasonEntity"]["anilistId"] == 111
    assert episodes[2]["seasonEntity"]["anilistId"] == 222


async def test_add_show_sonarr_fetch_leaves_manual_override_season_untouched(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)
    await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 999, malId: 999) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )

    fake._episodes = [_ep(1)]  # simulate a later refetch discovering season 1's episodes
    await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { seasons { edges { node { anilistId manualOverride } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    season = data["show"]["seasons"]["edges"][0]["node"]
    assert season["anilistId"] == 999  # untouched by the Fribb dataset's own 111
    assert season["manualOverride"] is True


async def test_add_show_sonarr_fetch_backfills_season_id_on_preexisting_episode(
    client, monkeypatch
):
    """Simulates data written before A.20 existed: an episode row with
    season_id already NULL, from a first fetch with no Fribb dataset
    reachable at all. A later refetch (dataset now reachable) must
    backfill season_id onto that same row, not just new ones."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    def _raise():
        raise RuntimeError("no cache, no network")

    monkeypatch.setattr(fribb, "load_dataset", lambda: _raise())
    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { seasonEntity { id } } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # unmatched, not null — season row exists even without a Fribb candidate
    assert data["show"]["episodes"]["edges"][0]["node"]["seasonEntity"] is not None

    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)
    conn = db.get_connection()
    conn.execute("UPDATE episode SET season_id = NULL WHERE show_id = ?", (show["id"],))
    conn.execute("DELETE FROM season WHERE show_id = ?", (show["id"],))
    conn.commit()
    await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { episodes { edges { node { seasonEntity { anilistId } } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["episodes"]["edges"][0]["node"]["seasonEntity"]["anilistId"] == 111


async def test_add_show_sonarr_fetch_fribb_failure_still_creates_season_and_opens_review(
    client, monkeypatch
):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))

    def _raise():
        raise RuntimeError("dataset unreachable")

    monkeypatch.setattr(fribb, "load_dataset", _raise)

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { seasons { edges { node { id source matched } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    season = data["show"]["seasons"]["edges"][0]["node"]
    assert season["source"] == "UNMATCHED"
    assert season["matched"] is False
    reviews = await _pending_reviews_for(client, season["id"])
    assert any(
        r["field"] == "anilist_id" and "dataset unreachable" in r["proposedValueChain"][-1]
        for r in reviews
    )


# --- source-fact capture: absolute_number + kind (§5.2, A.25) ---------------
#
# Both are *capture*, not behavior: nothing reads `kind` to decide anything,
# and nextUp orders by air date precisely so a source platform's filing
# convention can't drive watch order.


async def test_sonarr_fetch_captures_absolute_episode_number(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=[])
    eps = [
        {
            "seasonNumber": 1,
            "episodeNumber": 1,
            "absoluteEpisodeNumber": 1,
            "airDateUtc": None,
            "runtime": None,
        },
        {
            "seasonNumber": 2,
            "episodeNumber": 1,
            "absoluteEpisodeNumber": 13,
            "airDateUtc": None,
            "runtime": None,
        },
    ]
    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "anime"}, episodes=eps)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { episodes { edges { node { season absoluteNumber } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    by_season = {e["node"]["season"]: e["node"] for e in data["show"]["episodes"]["edges"]}
    assert by_season[1]["absoluteNumber"] == 1
    assert by_season[2]["absoluteNumber"] == 13


async def test_sonarr_fetch_backfills_absolute_number_on_existing_episode(client, monkeypatch):
    """A refetch fills a column that was empty, without disturbing
    anything a human may have set on that row."""
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=[])
    bare = {"seasonNumber": 1, "episodeNumber": 1, "airDateUtc": None, "runtime": None}
    fake = _FakeSonarrClient(series={"id": 42}, episodes=[bare])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    fake._episodes = [{**bare, "absoluteEpisodeNumber": 7}]
    await gql(
        client,
        "mutation($i:ID!){ refreshShowMetadata(showId:$i){ id } }",
        {"i": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { absoluteNumber } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["episodes"]["edges"][0]["node"]["absoluteNumber"] == 7


async def test_sonarr_fetch_captures_season_zero_as_special_kind(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=[])

    def _ep(season, number):
        return {
            "seasonNumber": season,
            "episodeNumber": number,
            "airDateUtc": None,
            "runtime": None,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(0, 1), _ep(1, 1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { season kind } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    by_season = {e["node"]["season"]: e["node"]["kind"] for e in data["show"]["episodes"]["edges"]}
    assert by_season == {0: "SPECIAL", 1: "REGULAR"}


async def test_set_episode_kind_overrides_the_captured_value(client, migrated_db):
    """Sonarr can't tell special from ova/bonus_movie; `kind` was
    read-only across the whole API until this mutation existed."""
    show = await add_show(client)
    _insert_next_up_episode(migrated_db, "e-knd001", show["id"], season=0, episode=1)
    data = await gql(
        client,
        "mutation($i: ID!) { setEpisodeKind(episodeId: $i, kind: BONUS_MOVIE) { id kind } }",
        {"i": "e-knd001"},
        headers=auth_headers(),
    )
    assert data["setEpisodeKind"]["kind"] == "BONUS_MOVIE"


async def test_set_episode_kind_rejects_unknown_episode(client):
    resp = await client.post(
        "/",
        json={
            "query": 'mutation { setEpisodeKind(episodeId: "e-nope00", kind: OVA) { id } }',
        },
        headers=auth_headers(),
    )
    assert "no such episode" in resp.text


# --- episode-numbering-scheme automatic derivation (§5.5, A.22) -------------


async def test_sonarr_fetch_derives_absolute_scheme_from_series_type(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=[])

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "anime"}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodeNumberingMapping { scheme source matched } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    mapping = data["show"]["episodeNumberingMapping"]
    assert mapping["scheme"] == "ABSOLUTE"
    assert mapping["source"] == "SONARR"
    assert mapping["matched"] is True


async def test_sonarr_fetch_derives_absolute_scheme_from_absolute_episode_number(
    client, monkeypatch
):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=[])
    ep = {
        "seasonNumber": 1,
        "episodeNumber": 1,
        "absoluteEpisodeNumber": 13,
        "airDateUtc": None,
        "runtime": None,
    }
    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "standard"}, episodes=[ep])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodeNumberingMapping { scheme } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["episodeNumberingMapping"]["scheme"] == "ABSOLUTE"


async def test_sonarr_fetch_derives_season_episode_scheme_by_default(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=[])

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "standard"}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodeNumberingMapping { scheme } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["episodeNumberingMapping"]["scheme"] == "SEASON_EPISODE"


async def test_sonarr_fetch_never_overwrites_manual_numbering_scheme(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    _patch_fribb_dataset(monkeypatch, dataset=[])

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": None}

    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "anime"}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)
    await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeNumberingScheme(showId: $id, scheme: SEASON_EPISODE) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )

    fake._episodes = [_ep(2)]  # a later fetch would otherwise re-derive ABSOLUTE
    await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodeNumberingMapping { scheme source } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    mapping = data["show"]["episodeNumberingMapping"]
    assert mapping["scheme"] == "SEASON_EPISODE"
    assert mapping["source"] == "MANUAL"


async def test_anime_show_with_only_tvdb_id_still_gets_anilist_metadata(client, monkeypatch):
    """A.24 — the gap this closes: Data's bridge (A.17) never sends an
    anilistId, so every anime show it added landed permanently bare, even
    though Fribb resolves the right id one table away. LCARS now resolves
    the show-level link itself, before the fetch that needs it."""
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)  # tvdb 555 -> anilist 111
    fetched = []

    def _spy(anilist_id, *a, **kw):
        fetched.append(anilist_id)
        return FAKE_ANILIST_MEDIA

    monkeypatch.setattr(anilist_client, "fetch_media", _spy)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    fake = _FakeSonarrClient(series={"id": 42}, episodes=[])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)

    show = await add_show(client, trackingSpace="ANIME", tvdbId=555)  # no anilistId

    assert fetched == [111], "AniList should have been fetched with the Fribb-resolved id"
    data = await gql(
        client,
        """
        query($id: ID!) { show(id: $id) {
            synopsis posterUrl
            externalIds { edges { node { service externalId } } }
        } }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["synopsis"] == "A gold rush story."
    assert data["show"]["posterUrl"] == "https://anilist.co/img/cover.jpg"
    edges = data["show"]["externalIds"]["edges"]
    links = {e["node"]["service"]: e["node"]["externalId"] for e in edges}
    assert links["anilist"] == "111"  # §5.1's mandate now actually satisfied


async def test_anime_show_never_overwrites_a_caller_supplied_anilist_id(client, monkeypatch):
    """A human-supplied id outranks a dataset lookup (§3 principle 6)."""
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)  # would resolve tvdb 555 -> 111
    fetched = []
    monkeypatch.setattr(
        anilist_client,
        "fetch_media",
        lambda aid, *a, **kw: fetched.append(aid) or FAKE_ANILIST_MEDIA,
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    show = await add_show(client, trackingSpace="ANIME", tvdbId=555, anilistId=777)
    assert fetched == [777]
    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { externalIds { edges { node { service externalId } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    edges = data["show"]["externalIds"]["edges"]
    links = {e["node"]["service"]: e["node"]["externalId"] for e in edges}
    assert links["anilist"] == "777"


async def test_already_bare_anime_show_recovers_on_refresh(client, monkeypatch):
    """The case that actually motivated A.24 — not a fresh add, but a
    show *already sitting in the database* bare, as every anime show
    Data bridged in before this fix does. It must recover on the next
    `refreshShowMetadata`, with no re-add and no manual id entry."""
    _patch_fribb_dataset(monkeypatch, dataset=[])  # nothing resolvable yet
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: None)
    show = await add_show(client, trackingSpace="ANIME", tvdbId=555)

    bare = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { synopsis externalIds { edges { node { service } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert bare["show"]["synopsis"] is None
    services = {e["node"]["service"] for e in bare["show"]["externalIds"]["edges"]}
    assert services == {"tvdb"}, "precondition: the show really is bare"

    # Now the dataset knows it (a refreshed Fribb download, or simply the
    # first fetch that reaches the network) and AniList answers.
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)  # tvdb 555 -> 111
    fetched = []
    monkeypatch.setattr(
        anilist_client,
        "fetch_media",
        lambda aid, *a, **kw: fetched.append(aid) or FAKE_ANILIST_MEDIA,
    )
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    await gql(
        client,
        "mutation($i:ID!){ refreshShowMetadata(showId:$i){ id } }",
        {"i": show["id"]},
        headers=auth_headers(),
    )

    assert fetched == [111]
    healed = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            synopsis posterUrl
            externalIds { edges { node { service externalId } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert healed["show"]["synopsis"] == "A gold rush story."
    assert healed["show"]["posterUrl"] == "https://anilist.co/img/cover.jpg"
    links = {
        e["node"]["service"]: e["node"]["externalId"]
        for e in healed["show"]["externalIds"]["edges"]
    }
    assert links["anilist"] == "111"


async def test_anime_show_with_no_resolvable_anilist_id_opens_a_review(client, monkeypatch):
    """§5.1 mandates the link; §3 principle 1 says flag, never gate —
    hard-rejecting would break Data's bridge on every anime add."""
    _patch_fribb_dataset(monkeypatch, dataset=[])  # no match for anything
    show = await add_show(client, trackingSpace="ANIME", tvdbId=555)
    reviews = await _pending_reviews_for(client, show["id"])
    assert any(r["field"] == "anilist_id" for r in reviews), reviews
    # ...and the show still exists and is usable, not rejected
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["id"] == show["id"]


async def test_sonarr_fetch_skips_season_zero_entirely(client, monkeypatch):
    """Season 0 is Sonarr/TVDB's specials bucket (§5.2), not a season with
    a cross-service identity — A.20 originally reconciled it like any
    other, creating one permanently unresolvable pending_review per show
    with specials. No season row, no review, season_id stays NULL."""
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=FAKE_FRIBB_DATASET)

    def _ep(season, number):
        return {
            "seasonNumber": season,
            "episodeNumber": number,
            "airDateUtc": None,
            "runtime": None,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(0, 1), _ep(1, 1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        """
        query($id: ID!) { show(id: $id) {
            seasons { edges { node { seasonNumber } } }
            episodes { edges { node { season seasonEntity { seasonNumber } } } }
        } }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    numbers = {e["node"]["seasonNumber"] for e in data["show"]["seasons"]["edges"]}
    assert numbers == {1}, "no season row should exist for season 0"
    by_season = {e["node"]["season"]: e["node"] for e in data["show"]["episodes"]["edges"]}
    assert by_season[0]["seasonEntity"] is None  # specials carry no season identity
    assert by_season[1]["seasonEntity"]["seasonNumber"] == 1

    all_reviews = await gql(
        client,
        "query { pendingReviews { edges { node { entityType field } } } }",
        headers=auth_headers(),
    )
    season_reviews = [
        e["node"]
        for e in all_reviews["pendingReviews"]["edges"]
        if e["node"]["entityType"] == "season"
    ]
    assert season_reviews == [], f"season 0 should generate no review noise: {season_reviews}"


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

    # §6.7, B.6 — the same failure that opened the pending_review above
    # also recorded a service_health entry, through metadata._guarded's
    # own choke point.
    health_data = await gql(client, SERVICE_HEALTH_QUERY, headers=auth_headers())
    anilist_health = next(e for e in health_data["serviceHealth"] if e["service"] == "ANILIST")
    assert anilist_health["status"] == "UNREACHABLE"
    assert anilist_health["lastErrorMessage"] == "Could not connect"

    # whatever was unreachable is back — retry via refreshShowMetadata,
    # the manual-fix-by-user path (confirmed 2026-08-08)
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    refreshed = await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { posterUrl } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert refreshed["refreshShowMetadata"]["posterUrl"] == "https://anilist.co/img/cover.jpg"

    # §6.7, B.6 — and the recovery flips it back to OK, clearing the error.
    health_data = await gql(client, SERVICE_HEALTH_QUERY, headers=auth_headers())
    anilist_health = next(e for e in health_data["serviceHealth"] if e["service"] == "ANILIST")
    assert anilist_health["status"] == "OK"
    assert anilist_health["lastErrorMessage"] is None


# --- TMDB duration fetch (A.19, §5.1 duration_minutes gap) -------------------
#
# Default fixture config has no tmdb_api_key, so every test above this
# section never touches this path — same "not configured = no-op" gate
# Sonarr/Radarr already use. These explicitly set tmdb_api_key to exercise it.


async def test_add_show_tmdb_fetch_populates_movie_duration(client, monkeypatch):
    config.set_current(config.Config(tmdb_api_key="key"))
    fake = _FakeTmdbClient(movie_runtime=112)
    monkeypatch.setattr(tmdb_client, "TmdbClient", lambda *a, **kw: fake)
    show = await add_show(client, mediaShape="MOVIE", trackingSpace="TV", tmdbId=555)

    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["durationMinutes"] == 112
    assert fake.calls == [("movie_runtime", 555)]  # already had a tmdb id — no bridge needed


async def test_add_show_tmdb_fetch_resolves_tvdb_bridge_for_episodic_show(client, monkeypatch):
    config.set_current(config.Config(tmdb_api_key="key"))
    fake = _FakeTmdbClient(tvdb_to_tmdb={67890: 42}, tv_runtime=22)
    monkeypatch.setattr(tmdb_client, "TmdbClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) {
            durationMinutes
            externalIds { edges { node { service externalId url } } }
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["durationMinutes"] == 22
    assert fake.calls == [("find_by_tvdb_id", 67890), ("tv_episode_runtime", 42)]

    external_ids = {e["node"]["service"]: e["node"] for e in data["show"]["externalIds"]["edges"]}
    assert external_ids["tmdb"]["externalId"] == "42"
    assert external_ids["tmdb"]["url"] == "https://www.themoviedb.org/tv/42"


async def test_add_show_tmdb_fetch_no_op_when_tvdb_has_no_tmdb_match(client, monkeypatch):
    config.set_current(config.Config(tmdb_api_key="key"))
    fake = _FakeTmdbClient(tvdb_to_tmdb={})  # no match
    monkeypatch.setattr(tmdb_client, "TmdbClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)

    data = await gql(client, SHOW_METADATA_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["durationMinutes"] is None
    assert fake.calls == [("find_by_tvdb_id", 67890)]  # never got to tv_episode_runtime


async def test_add_show_tmdb_fetch_skipped_for_anime(client, monkeypatch):
    """AniList already covers duration for anime — TMDB should never be
    called at all for a tracking_space=anime show, regardless of
    media_shape (an anime movie still goes through AniList, §5.1)."""
    config.set_current(config.Config(tmdb_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda *a, **kw: None)
    fake = _FakeTmdbClient(movie_runtime=112)
    monkeypatch.setattr(tmdb_client, "TmdbClient", lambda *a, **kw: fake)
    await add_show(client, anilistId=12345)
    assert fake.calls == []


# --- AniList airingSchedule reconciliation (§5.2/§6.7, B.4) -----------------
# Rides the exact same daily/on-demand cadence as the rest of metadata.py's
# fetch_and_populate — no new mutation/due-query, so these are exercised
# through the same addShow/refreshShowMetadata paths as everything above.


async def test_anilist_air_date_reconciliation_corrects_a_sonarr_seeded_date(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 12,
            "nodes": [{"episode": 1, "airingAt": 1735689600}],
        },
    )

    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2020-01-01T00:00:00Z",
            "runtime": 24,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, anilistId=12345, tvdbId=67890)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { episodes { edges { node { id episode airDateUtc airDateSource } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    ep = data["show"]["episodes"]["edges"][0]["node"]
    assert ep["airDateUtc"] == "2025-01-01T00:00:00Z"  # AniList's own value, not Sonarr's
    assert ep["airDateSource"] == "ANILIST"
    # pending_review entries here are keyed by the *episode's* own id
    # (entity_type="episode"), not the show's — §5.6's own per-row shape.
    reviews = await _pending_reviews_for(client, ep["id"])
    reviews = [r for r in reviews if r["field"] == "air_date_utc"]
    assert len(reviews) == 1
    assert reviews[0]["source"] == "anilist"
    assert reviews[0]["previousValue"] == "2020-01-01T00:00:00Z"


async def test_anilist_air_date_reconciliation_never_overwrites_a_manual_date(client, monkeypatch):
    """Revised 2026-08-09, directly on the user's own reasoning: only a
    genuine reschedule signal (animeschedule.net, B.5, not built yet)
    should override a date the user deliberately set — AniList/Sonarr
    repeatedly re-asserting stale data over an already-fixed value is
    the "stubborn weekly rewrite" failure mode the user flagged.
    Manual gets the same hard-gate protection season.manual_override
    already gives season mapping (§3 principle 6) — no write, no
    pending_review at all, not just logged for later review."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 12,
            "nodes": [{"episode": 1, "airingAt": 1735689600}],
        },
    )

    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2020-01-01T00:00:00Z",
            "runtime": 24,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, anilistId=12345, tvdbId=67890)
    episode_id = (
        await gql(
            client,
            "query($id: ID!) { show(id: $id) { episodes { edges { node { id } } } } }",
            {"id": show["id"]},
            headers=auth_headers(),
        )
    )["show"]["episodes"]["edges"][0]["node"]["id"]
    await gql(
        client,
        "mutation($id: ID!, $d: DateTime!) {"
        " setEpisodeAirDate(episodeId: $id, airDateUtc: $d) { airDateSource } }",
        {"id": episode_id, "d": "2030-06-01T00:00:00Z"},
        headers=auth_headers(),
    )
    # addShow's own inline fetch already reconciled sonarr -> anilist once
    # (correctly — the episode was still 'sonarr'-sourced at that point) —
    # capture the chain length *after* the manual override, so the
    # assertion below only checks nothing *new* got added on top of it.
    before = [
        r for r in await _pending_reviews_for(client, episode_id) if r["field"] == "air_date_utc"
    ]

    await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        "query($id: ID!) {"
        " show(id: $id) { episodes { edges { node { airDateUtc airDateSource } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    ep = data["show"]["episodes"]["edges"][0]["node"]
    assert ep["airDateUtc"] == "2030-06-01T00:00:00Z"  # the manual value survives, untouched
    assert ep["airDateSource"] == "MANUAL"
    after = [
        r for r in await _pending_reviews_for(client, episode_id) if r["field"] == "air_date_utc"
    ]
    assert after == before  # refreshShowMetadata added nothing new


async def test_anilist_air_date_reconciliation_never_overwrites_an_animeschedule_date(
    client, monkeypatch
):
    """§6.7/B.8 — closing a real gap B.5 exposed: this function predates
    B.5 and only ever guarded 'manual'; an animeschedule-sourced date
    (a genuine reschedule signal, ranked *above* AniList in §6.7's
    priority order) must survive an AniList reconciliation pass just
    as a manual one does — no write, no new pending_review."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 12,
            "nodes": [{"episode": 1, "airingAt": 1735689600}],
        },
    )

    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2020-01-01T00:00:00Z",
            "runtime": 24,
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, anilistId=12345, tvdbId=67890)
    episode_id = (
        await gql(
            client,
            "query($id: ID!) { show(id: $id) { episodes { edges { node { id } } } } }",
            {"id": show["id"]},
            headers=auth_headers(),
        )
    )["show"]["episodes"]["edges"][0]["node"]["id"]

    conn = db.get_connection()
    conn.execute(
        "UPDATE episode SET air_date_utc = ?, air_date_source = 'animeschedule' WHERE id = ?",
        ("2030-06-01T00:00:00Z", episode_id),
    )
    conn.commit()
    before = [
        r for r in await _pending_reviews_for(client, episode_id) if r["field"] == "air_date_utc"
    ]

    await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        "query($id: ID!) {"
        " show(id: $id) { episodes { edges { node { airDateUtc airDateSource } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    ep = data["show"]["episodes"]["edges"][0]["node"]
    assert ep["airDateUtc"] == "2030-06-01T00:00:00Z"  # the animeschedule value survives
    assert ep["airDateSource"] == "ANIMESCHEDULE"
    after = [
        r for r in await _pending_reviews_for(client, episode_id) if r["field"] == "air_date_utc"
    ]
    assert after == before  # refreshShowMetadata added nothing new


async def test_anilist_air_date_reconciliation_is_a_no_op_when_unchanged(client, monkeypatch):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 12,
            "nodes": [{"episode": 1, "airingAt": 1735689600}],
        },
    )

    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2025-01-01T00:00:00Z",
            "runtime": 24,  # already AniList's own value
        }

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, anilistId=12345, tvdbId=67890)
    data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { episodes { edges { node { id } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    episode_id = data["show"]["episodes"]["edges"][0]["node"]["id"]

    all_reviews = await _pending_reviews_for(client, episode_id)
    reviews = [r for r in all_reviews if r["field"] == "air_date_utc"]
    assert reviews == []


async def test_anilist_air_date_reconciliation_skips_a_season_with_no_anilist_id(
    client, monkeypatch
):
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    # fetch_media returns None (default fixture behavior) — season 1 never
    # gets an anilist_id at all, so the reconciliation loop has nothing to
    # iterate; fetch_airing_schedule must never even be called.
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda *a, **kw: calls.append(1) or [],
    )

    def _ep(number):
        return {"seasonNumber": 1, "episodeNumber": number, "airDateUtc": None, "runtime": 24}

    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(1)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    await add_show(client, anilistId=12345)
    assert calls == []


async def test_anilist_air_date_reconciliation_skips_a_season_that_spans_multiple_anilist_entries(
    client, monkeypatch
):
    """Real, confirmed-live scenario, not hypothetical: Attack on
    Titan's own Season 3 is one 22-episode TVDB season split across two
    separate AniList Media entries (12 + 10 episodes). season.anilist_id
    can only point at one of them, so if LCARS's own episode count for
    the season exceeds that entry's own reported total, per-episode
    matching is unsafe — skip the whole season, open a pending_review
    on the *season* (not each episode) instead of silently writing
    dates from the wrong cour."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 10,  # this Media entry only covers 10 episodes...
            "nodes": [{"episode": n, "airingAt": 1735689600 + n * 86400} for n in range(1, 11)],
        },
    )

    def _ep(number):
        return {
            "seasonNumber": 1,
            "episodeNumber": number,
            "airDateUtc": "2020-01-01T00:00:00Z",
            "runtime": 24,
        }

    # ...but LCARS has 22 episodes for this season (the real, full TVDB split).
    fake = _FakeSonarrClient(series={"id": 42}, episodes=[_ep(n) for n in range(1, 23)])
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, anilistId=12345, tvdbId=67890)

    data = await gql(
        client,
        "query($id: ID!) {"
        " show(id: $id) { episodes { edges { node { episode airDateUtc airDateSource } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    # None of the 22 episodes were touched — no partial/wrong-cour writes.
    for edge in data["show"]["episodes"]["edges"]:
        assert edge["node"]["airDateUtc"] == "2020-01-01T00:00:00Z"
        assert edge["node"]["airDateSource"] == "SONARR"
    seasons = await gql(
        client,
        "query($id: ID!) { show(id: $id) { seasons { edges { node { id seasonNumber } } } } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    season_id = seasons["show"]["seasons"]["edges"][0]["node"]["id"]
    all_reviews = await _pending_reviews_for(client, season_id)
    reviews = [r for r in all_reviews if r["field"] == "anilist_id"]
    assert len(reviews) == 1
    assert "22" in reviews[0]["proposedValueChain"][0]
    assert "10" in reviews[0]["proposedValueChain"][0]


async def test_anilist_air_date_reconciliation_skips_an_unfetched_episode(client, monkeypatch):
    """AniList reports an episode LCARS has no row for at all (Sonarr
    hasn't fetched it yet) — skipped gracefully, not an error."""
    config.set_current(config.Config(sonarr_url="http://sonarr:8989", sonarr_api_key="key"))
    monkeypatch.setattr(anilist_client, "fetch_media", lambda *a, **kw: FAKE_ANILIST_MEDIA)
    monkeypatch.setattr(
        anilist_client,
        "fetch_airing_schedule",
        lambda anilist_id, *a, **kw: {
            "episodes": 12,
            "nodes": [{"episode": 99, "airingAt": 1735689600}],
        },
    )
    fake = _FakeSonarrClient(series={"id": 42}, episodes=[])  # Sonarr has nothing yet
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    await add_show(client, anilistId=12345, tvdbId=67890)  # must not raise
    # No episode row exists at all here, so there's no entity id to filter
    # pending_review by — check globally instead that no episode-level
    # air_date_utc entry got created from anything at all.
    reviews = await gql(
        client,
        "query { pendingReviews { edges { node { entityType field } } } }",
        headers=auth_headers(),
    )
    air_date_reviews = [
        r["node"]
        for r in reviews["pendingReviews"]["edges"]
        if r["node"]["entityType"] == "episode" and r["node"]["field"] == "air_date_utc"
    ]
    assert air_date_reviews == []


async def test_add_show_tmdb_fetch_skips_silently_when_not_configured(client):
    """Default fixture: no tmdb_api_key at all — confirms this is
    treated the same as "not linked", not a failure worth a
    pending_review, same as Sonarr/Radarr's own equivalent test."""
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)
    reviews = await _pending_reviews_for(client, show["id"])
    assert reviews == []


async def test_add_show_tmdb_fetch_failure_logs_pending_review(client, monkeypatch):
    config.set_current(config.Config(tmdb_api_key="key"))
    fake = _FakeTmdbClient(error=tmdb_client.TmdbError("Could not connect to TMDB"))
    monkeypatch.setattr(tmdb_client, "TmdbClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=67890)

    reviews = await _pending_reviews_for(client, show["id"])
    assert len(reviews) == 1
    assert reviews[0]["source"] == "tmdb"
    assert reviews[0]["proposedValueChain"] == ["Could not connect to TMDB"]


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


# --- AniList score/status push (§6.1/§6.8, A.9) -----------------------------
#
# Default fixture config has no anilist_access_token, so every push is a
# silent no-op there — these tests explicitly configure one to exercise the
# real push path, mocking anilist_client.save_media_list_entry throughout
# (no real network calls).


async def _link_season_anilist(client, show_id, season_number, anilist_id):
    data = await gql(
        client,
        """
        mutation($id: ID!, $season: Int!, $anilistId: Int!) {
          setSeasonMapping(showId: $id, seasonNumber: $season, anilistId: $anilistId) { id }
        }
        """,
        {"id": show_id, "season": season_number, "anilistId": anilist_id},
        headers=auth_headers(),
    )
    return data["setSeasonMapping"]["id"]


def _authenticated_config():
    return config.Config(anilist_access_token="tok-123")


async def test_set_score_pushes_show_score_to_every_linked_season(client, monkeypatch):
    config.set_current(_authenticated_config())
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "save_media_list_entry",
        lambda token, anilist_id, **kw: calls.append((token, anilist_id, kw)),
    )
    show = await add_show(client)
    await _link_season_anilist(client, show["id"], 1, 111)
    await _link_season_anilist(client, show["id"], 2, 222)

    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert len(calls) == 2
    pushed_ids = {c[1] for c in calls}
    assert pushed_ids == {111, 222}
    for token, _anilist_id, kw in calls:
        assert token == "tok-123"
        assert kw == {"score": 85.0}  # ×5, §6.1


async def test_set_score_no_push_when_not_authenticated(client, monkeypatch):
    called = []
    monkeypatch.setattr(
        anilist_client, "save_media_list_entry", lambda *a, **kw: called.append(True)
    )
    show = await add_show(client)
    await _link_season_anilist(client, show["id"], 1, 111)
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert called == []


async def test_set_season_score_pushes_only_that_season(client, monkeypatch):
    config.set_current(_authenticated_config())
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "save_media_list_entry",
        lambda token, anilist_id, **kw: calls.append((anilist_id, kw)),
    )
    show = await add_show(client)
    season1 = await _link_season_anilist(client, show["id"], 1, 111)
    await _link_season_anilist(client, show["id"], 2, 222)

    data = await gql(
        client,
        "mutation($id: ID!, $s: Float!) { setSeasonScore(seasonId: $id, score: $s) { score } }",
        {"id": season1, "s": 18.0},
        headers=auth_headers(),
    )
    assert data["setSeasonScore"]["score"] == 18.0
    assert calls == [(111, {"score": 90.0})]  # only season 1, not season 2


async def test_set_season_score_overrides_show_score_fallback(client, monkeypatch):
    config.set_current(_authenticated_config())
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "save_media_list_entry",
        lambda token, anilist_id, **kw: calls.append((anilist_id, kw)),
    )
    show = await add_show(client)
    season1 = await _link_season_anilist(client, show["id"], 1, 111)
    await _link_season_anilist(client, show["id"], 2, 222)  # no own score — falls back

    await gql(
        client,
        "mutation($id: ID!, $s: Float!) { setSeasonScore(seasonId: $id, score: $s) { score } }",
        {"id": season1, "s": 12.0},
        headers=auth_headers(),
    )
    calls.clear()

    # show-level score push: season 1 keeps its own 12.0 (-> 60.0), season 2
    # has no own score so falls back to the new show-level value (-> 100.0)
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 20) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    pushed = dict(calls)
    assert pushed[111] == {"score": 60.0}
    assert pushed[222] == {"score": 100.0}


async def test_set_status_pushes_to_every_linked_season(client, monkeypatch):
    config.set_current(_authenticated_config())
    calls = []
    monkeypatch.setattr(
        anilist_client,
        "save_media_list_entry",
        lambda token, anilist_id, **kw: calls.append((anilist_id, kw)),
    )
    show = await add_show(client)
    await _link_season_anilist(client, show["id"], 1, 111)

    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: COMPLETED) { status } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert calls == [(111, {"status": "COMPLETED"})]


async def test_anilist_push_failure_logs_pending_review_against_season(client, monkeypatch):
    config.set_current(_authenticated_config())

    def _raise(*a, **kw):
        raise anilist_client.AniListError("Timed out talking to AniList")

    monkeypatch.setattr(anilist_client, "save_media_list_entry", _raise)
    show = await add_show(client)
    season_id = await _link_season_anilist(client, show["id"], 1, 111)

    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    reviews = await _pending_reviews_for(client, season_id)
    assert len(reviews) == 1
    assert reviews[0]["entityType"] == "season"
    assert reviews[0]["field"] == "anilist_push"
    assert reviews[0]["source"] == "anilist"


# --- MAL score/status push (§6.1/§6.9, B.10) --------------------------------
#
# Same shape as the AniList push tests directly above — default fixture
# config has no mal_access_token, so every push is a silent no-op there;
# these explicitly configure one and mock mal_client.update_my_list_status
# throughout (no real network calls).


async def _link_season_mal(client, show_id, season_number, mal_id):
    data = await gql(
        client,
        """
        mutation($id: ID!, $season: Int!, $malId: Int!) {
          setSeasonMapping(showId: $id, seasonNumber: $season, malId: $malId) { id }
        }
        """,
        {"id": show_id, "season": season_number, "malId": mal_id},
        headers=auth_headers(),
    )
    return data["setSeasonMapping"]["id"]


def _mal_authenticated_config():
    return config.Config(mal_access_token="mal-tok-123")


async def test_set_score_pushes_show_score_to_every_mal_linked_season(client, monkeypatch):
    config.set_current(_mal_authenticated_config())
    calls = []
    monkeypatch.setattr(
        mal_client,
        "update_my_list_status",
        lambda token, mal_id, **kw: calls.append((token, mal_id, kw)),
    )
    show = await add_show(client)
    await _link_season_mal(client, show["id"], 1, 111)
    await _link_season_mal(client, show["id"], 2, 222)

    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert len(calls) == 2
    pushed_ids = {c[1] for c in calls}
    assert pushed_ids == {111, 222}
    for token, _mal_id, kw in calls:
        assert token == "mal-tok-123"
        # 17 ÷ 2 = 8.5 -> round() to 8 (Python's round-half-to-even — same
        # plain round() convention setScore's own quarter-point clamp
        # already uses, §6.1, not a new rounding rule introduced here).
        assert kw == {"score": 8}


async def test_set_score_no_mal_push_when_not_authenticated(client, monkeypatch):
    called = []
    monkeypatch.setattr(mal_client, "update_my_list_status", lambda *a, **kw: called.append(True))
    show = await add_show(client)
    await _link_season_mal(client, show["id"], 1, 111)
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert called == []


async def test_set_season_score_pushes_only_that_season_to_mal(client, monkeypatch):
    config.set_current(_mal_authenticated_config())
    calls = []
    monkeypatch.setattr(
        mal_client, "update_my_list_status", lambda token, mal_id, **kw: calls.append((mal_id, kw))
    )
    show = await add_show(client)
    season1 = await _link_season_mal(client, show["id"], 1, 111)
    await _link_season_mal(client, show["id"], 2, 222)

    await gql(
        client,
        "mutation($id: ID!, $s: Float!) { setSeasonScore(seasonId: $id, score: $s) { score } }",
        {"id": season1, "s": 18.0},
        headers=auth_headers(),
    )
    assert calls == [(111, {"score": 9})]  # 18 ÷ 2 = 9 exactly, only season 1


async def test_set_status_pushes_to_every_mal_linked_season(client, monkeypatch):
    config.set_current(_mal_authenticated_config())
    calls = []
    monkeypatch.setattr(
        mal_client, "update_my_list_status", lambda token, mal_id, **kw: calls.append((mal_id, kw))
    )
    show = await add_show(client)
    await _link_season_mal(client, show["id"], 1, 111)

    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: COMPLETED) { status } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert calls == [(111, {"status": "completed"})]


async def test_set_status_never_pushes_is_rewatching_or_num_times_rewatched_to_mal(
    client, monkeypatch
):
    # §6.8/§6.9 — rewatching never auto-toggles either MAL field; setStatus's
    # own push call simply never has a way to send them at all.
    config.set_current(_mal_authenticated_config())
    calls = []
    monkeypatch.setattr(
        mal_client, "update_my_list_status", lambda token, mal_id, **kw: calls.append(kw)
    )
    show = await add_show(client)
    await _link_season_mal(client, show["id"], 1, 111)
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { status } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert "is_rewatching" not in calls[0]
    assert "num_times_rewatched" not in calls[0]


async def test_set_status_maps_every_real_show_status_value_to_mal_without_crashing(
    client, monkeypatch
):
    # Every show.status CHECK-constraint value must have a _STATUS_TO_MAL
    # entry — a bare dict lookup would KeyError and lose the local write
    # otherwise (caught in review before commit, not hypothetical).
    config.set_current(_mal_authenticated_config())
    calls = []
    monkeypatch.setattr(
        mal_client, "update_my_list_status", lambda token, mal_id, **kw: calls.append(kw)
    )
    show = await add_show(client)
    await _link_season_mal(client, show["id"], 1, 111)
    for status in ("WATCHING", "PLANNED", "PAUSED", "COMPLETED", "DROPPED"):
        data = await gql(
            client,
            "mutation($id: ID!, $s: ShowStatus!) { setStatus(showId: $id, status: $s) { status } }",
            {"id": show["id"], "s": status},
            headers=auth_headers(),
        )
        assert data["setStatus"]["status"] == status  # the local write always succeeds
    assert len(calls) == 5


async def test_mal_push_failure_logs_pending_review_against_season(client, monkeypatch):
    config.set_current(_mal_authenticated_config())

    def _raise(*a, **kw):
        raise mal_client.MALError("Timed out talking to MyAnimeList")

    monkeypatch.setattr(mal_client, "update_my_list_status", _raise)
    show = await add_show(client)
    season_id = await _link_season_mal(client, show["id"], 1, 111)

    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    reviews = await _pending_reviews_for(client, season_id)
    assert len(reviews) == 1
    assert reviews[0]["entityType"] == "season"
    assert reviews[0]["field"] == "mal_push"
    assert reviews[0]["source"] == "mal"


# --- refreshMalTokenIfDue (§6.9, B.10) ----------------------------------------


REFRESH_MAL_TOKEN_IF_DUE = "mutation { refreshMalTokenIfDue { refreshed } }"


async def test_refresh_mal_token_if_due_no_op_when_not_configured(client):
    data = await gql(client, REFRESH_MAL_TOKEN_IF_DUE, headers=auth_headers())
    assert data["refreshMalTokenIfDue"] == {"refreshed": False}


async def test_refresh_mal_token_if_due_no_op_when_not_yet_due(client, monkeypatch):
    config.set_current(
        config.Config(
            mal_client_id="cid",
            mal_refresh_token="ref-1",
            mal_token_refreshed_at=util.now_utc_iso(),  # just refreshed
        )
    )
    called = []
    monkeypatch.setattr(mal_client, "refresh_access_token", lambda *a, **kw: called.append(True))
    data = await gql(client, REFRESH_MAL_TOKEN_IF_DUE, headers=auth_headers())
    assert data["refreshMalTokenIfDue"] == {"refreshed": False}
    assert called == []


async def test_refresh_mal_token_if_due_refreshes_and_persists_when_stale(client, monkeypatch):
    cfg = config.Config(
        mal_client_id="cid",
        mal_client_secret="csecret",
        mal_refresh_token="stale-ref",
        mal_token_refreshed_at=util.utc_iso_offset(-8),  # 8 days ago, past the 7-day gate
    )
    config.set_current(cfg)
    monkeypatch.setattr(
        mal_client,
        "refresh_access_token",
        lambda client_id, client_secret, refresh_token: ("new-acc", "new-ref"),
    )
    saved = []
    monkeypatch.setattr(
        config, "save_mal_tokens", lambda access, refresh: saved.append((access, refresh))
    )
    data = await gql(client, REFRESH_MAL_TOKEN_IF_DUE, headers=auth_headers())
    assert data["refreshMalTokenIfDue"] == {"refreshed": True}
    assert saved == [("new-acc", "new-ref")]
    # the live in-memory singleton is updated too, not just persisted via
    # save_mal_tokens() — the exact bug this mutation's own docstring flags
    # and fixes deliberately (config.get_current() is a process-wide
    # singleton; a later push this same process makes must see the new
    # token immediately, not after a restart).
    assert cfg.mal_access_token == "new-acc"
    assert cfg.mal_refresh_token == "new-ref"


async def test_refresh_mal_token_if_due_failure_records_service_health(client, monkeypatch):
    config.set_current(
        config.Config(
            mal_client_id="cid",
            mal_refresh_token="ref-1",
            mal_token_refreshed_at=util.utc_iso_offset(-8),
        )
    )

    def _raise(*a, **kw):
        raise mal_client.MALAuthError("refresh_token expired")

    monkeypatch.setattr(mal_client, "refresh_access_token", _raise)
    data = await gql(client, REFRESH_MAL_TOKEN_IF_DUE, headers=auth_headers())
    assert data["refreshMalTokenIfDue"] == {"refreshed": False}
    health = await gql(client, SERVICE_HEALTH_QUERY, headers=auth_headers())
    mal_health = next(e for e in health["serviceHealth"] if e["service"] == "MAL")
    assert mal_health["status"] == "UNREACHABLE"


# --- paced/catch-up mode (§6.2, A.10) ----------------------------------------
#
# Reuses _insert_episode_with_air_date, defined once further down in this
# file (originally for the episodesAiringSoon tests) — same helper, same
# (migrated_db, episode_id, show_id, air_date_utc) signature.

# GraphQL argument defaults (cadenceDays: Int = 7, schema.graphql) only
# apply when the argument is entirely omitted from the query document —
# NOT when a variable resolves to null (that sends an explicit null, which
# overrides the default). So ENABLE_PACED_MODE_DEFAULT omits the argument
# outright, and ENABLE_PACED_MODE_WITH_CADENCE always supplies a real value.
ENABLE_PACED_MODE_DEFAULT = """
    mutation($id: ID!) {
      enablePacedMode(showId: $id) { id pacedCadenceDays pacedNextDate }
    }
"""

ENABLE_PACED_MODE_WITH_CADENCE = """
    mutation($id: ID!, $cadence: Int!) {
      enablePacedMode(showId: $id, cadenceDays: $cadence) {
        id pacedCadenceDays pacedNextDate
      }
    }
"""

DISABLE_PACED_MODE = """
    mutation($id: ID!) {
      disablePacedMode(showId: $id) { id pacedCadenceDays pacedNextDate }
    }
"""

SHOW_PACED_MODE_QUERY = """
    query($id: ID!) { show(id: $id) { id pacedCadenceDays pacedNextDate } }
"""


async def test_enable_paced_mode_defaults_cadence_to_seven(client):
    show = await add_show(client)  # no episodes at all — non-airing
    data = await gql(client, ENABLE_PACED_MODE_DEFAULT, {"id": show["id"]}, headers=auth_headers())
    assert data["enablePacedMode"]["pacedCadenceDays"] == 7


async def test_enable_paced_mode_with_custom_cadence(client):
    show = await add_show(client)
    data = await gql(
        client,
        ENABLE_PACED_MODE_WITH_CADENCE,
        {"id": show["id"], "cadence": 3},
        headers=auth_headers(),
    )
    assert data["enablePacedMode"]["pacedCadenceDays"] == 3


async def test_enable_paced_mode_rejects_a_show_with_no_air_date_yet(client, migrated_db):
    show = await add_show(client)
    _insert_episode_with_air_date(migrated_db, "e-airng1", show["id"], None)
    resp = await client.post(
        "/",
        json={"query": ENABLE_PACED_MODE_DEFAULT, "variables": {"id": show["id"]}},
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "unreleased episodes" in body["errors"][0]["message"]


async def test_enable_paced_mode_rejects_a_show_with_a_future_episode(client, migrated_db):
    show = await add_show(client)
    _insert_episode_with_air_date(migrated_db, "e-airng2", show["id"], "2099-01-01T00:00:00Z")
    resp = await client.post(
        "/",
        json={"query": ENABLE_PACED_MODE_DEFAULT, "variables": {"id": show["id"]}},
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_enable_paced_mode_allows_a_fully_aired_show(client, migrated_db):
    show = await add_show(client)
    _insert_episode_with_air_date(migrated_db, "e-past01", show["id"], "2020-01-01T00:00:00Z")
    data = await gql(client, ENABLE_PACED_MODE_DEFAULT, {"id": show["id"]}, headers=auth_headers())
    assert data["enablePacedMode"]["pacedCadenceDays"] == 7


async def test_disable_paced_mode_clears_cadence(client):
    show = await add_show(client)
    await gql(client, ENABLE_PACED_MODE_DEFAULT, {"id": show["id"]}, headers=auth_headers())
    data = await gql(client, DISABLE_PACED_MODE, {"id": show["id"]}, headers=auth_headers())
    assert data["disablePacedMode"]["pacedCadenceDays"] is None
    assert data["disablePacedMode"]["pacedNextDate"] is None


async def test_paced_next_date_is_null_without_a_watch_event(client):
    show = await add_show(client)
    data = await gql(client, ENABLE_PACED_MODE_DEFAULT, {"id": show["id"]}, headers=auth_headers())
    assert data["enablePacedMode"]["pacedNextDate"] is None


async def test_paced_next_date_is_null_when_not_in_paced_mode(client, migrated_db):
    show = await add_show(client)
    _insert_episode_with_air_date(migrated_db, "e-past02", show["id"], "2020-01-01T00:00:00Z")
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, SHOW_PACED_MODE_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["pacedNextDate"] is None


async def test_paced_next_date_is_latest_watch_event_plus_cadence(client, migrated_db):
    show = await add_show(client)
    _insert_episode_with_air_date(migrated_db, "e-past03", show["id"], "2020-01-01T00:00:00Z")
    _insert_episode_with_air_date(
        migrated_db, "e-past04", show["id"], "2020-01-08T00:00:00Z", episode=2
    )
    await gql(
        client,
        ENABLE_PACED_MODE_WITH_CADENCE,
        {"id": show["id"], "cadence": 7},
        headers=auth_headers(),
    )
    await gql(
        client,
        """
        mutation($id: ID!, $watchedAt: DateTime!) {
          addWatchEvent(showId: $id, season: 1, episode: 1, watchedAt: $watchedAt) { id }
        }
        """,
        {"id": show["id"], "watchedAt": "2026-01-01T00:00:00Z"},
        headers=auth_headers(),
    )
    data = await gql(client, SHOW_PACED_MODE_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data["show"]["pacedNextDate"] == "2026-01-08T00:00:00Z"

    # a second, later watch recomputes from that new latest watch — adaptive,
    # not pre-baked (§6.2)
    await gql(
        client,
        """
        mutation($id: ID!, $watchedAt: DateTime!) {
          addWatchEvent(showId: $id, season: 1, episode: 2, watchedAt: $watchedAt) { id }
        }
        """,
        {"id": show["id"], "watchedAt": "2026-01-10T00:00:00Z"},
        headers=auth_headers(),
    )
    data2 = await gql(client, SHOW_PACED_MODE_QUERY, {"id": show["id"]}, headers=auth_headers())
    assert data2["show"]["pacedNextDate"] == "2026-01-17T00:00:00Z"


# --- cross-show next-up (§6.4, A.11) -----------------------------------------


def _insert_next_up_episode(
    migrated_db,
    episode_id,
    show_id,
    *,
    season=1,
    episode=1,
    state="unwatched",
    air_date_utc="2026-01-01T00:00:00Z",
    available=True,
):
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode
            (id, show_id, season, episode, kind, state, air_date_utc,
             available_via_sonarr, created_at, updated_at)
        VALUES (?, ?, ?, ?, 'regular', ?, ?, ?, '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z')
        """,
        (
            episode_id,
            show_id,
            season,
            episode,
            state,
            air_date_utc,
            # B.3 — available_via_sonarr is now 3-state, not boolean.
            "available" if available else "unavailable",
        ),
    )
    conn.commit()


NEXT_UP_QUERY = """
    query {
      nextUp {
        edges { node { show { id displayTitle } episode { id season episode } } }
      }
    }
"""


async def _add_watching_show(client, **overrides) -> dict:
    """addShow always creates a PLANNED show (resolve_add_show's own
    hardcoded initial status) — nextUp's default candidate set is
    WATCHING-status shows, so most of these tests need one explicitly
    marked as such."""
    show = await add_show(client, **overrides)
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    return show


async def test_next_up_includes_watching_show_with_available_episode(client, migrated_db):
    show = await _add_watching_show(client, titleRomaji="Watching Show")
    _insert_next_up_episode(migrated_db, "e-nu0001", show["id"])
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["nextUp"]["edges"]}
    assert show["id"] in show_ids


async def test_next_up_excludes_a_show_with_no_unwatched_available_episode(client, migrated_db):
    show = await add_show(client, titleRomaji="Nothing To Watch")
    # watched already — shouldn't count
    _insert_next_up_episode(migrated_db, "e-nu0002", show["id"], state="watched")
    # not locally available — shouldn't count either
    _insert_next_up_episode(migrated_db, "e-nu0003", show["id"], episode=2, available=False)
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["nextUp"]["edges"]}
    assert show["id"] not in show_ids


async def test_next_up_excludes_a_planned_show(client, migrated_db):
    show = await add_show(client, titleRomaji="Just Planned")
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: PLANNED) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    _insert_next_up_episode(migrated_db, "e-nu0004", show["id"])
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["nextUp"]["edges"]}
    assert show["id"] not in show_ids


async def test_next_up_includes_a_paced_show_regardless_of_status(client, migrated_db):
    show = await add_show(client, titleRomaji="Paced Show")
    _insert_next_up_episode(
        migrated_db, "e-nu0005", show["id"], air_date_utc="2020-01-01T00:00:00Z"
    )
    await gql(
        client,
        "mutation($id: ID!) { enablePacedMode(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["nextUp"]["edges"]}
    assert show["id"] in show_ids


async def test_next_up_defaults_to_soonest_available_first(client, migrated_db):
    show_a = await _add_watching_show(client, titleRomaji="Airs Later")
    show_b = await _add_watching_show(client, titleRomaji="Airs Sooner")
    _insert_next_up_episode(
        migrated_db, "e-nu0006", show_a["id"], air_date_utc="2026-06-01T00:00:00Z"
    )
    _insert_next_up_episode(
        migrated_db, "e-nu0007", show_b["id"], air_date_utc="2026-01-01T00:00:00Z"
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    order = [e["node"]["show"]["id"] for e in data["nextUp"]["edges"]]
    assert order.index(show_b["id"]) < order.index(show_a["id"])


async def test_next_up_manual_override_wins_over_the_default_order(client, migrated_db):
    show_a = await _add_watching_show(client, titleRomaji="Airs Sooner")
    show_b = await _add_watching_show(client, titleRomaji="Airs Later, But Pinned")
    _insert_next_up_episode(
        migrated_db, "e-nu0008", show_a["id"], air_date_utc="2026-01-01T00:00:00Z"
    )
    _insert_next_up_episode(
        migrated_db, "e-nu0009", show_b["id"], air_date_utc="2026-06-01T00:00:00Z"
    )
    await gql(
        client,
        "mutation($id: ID!) { setNextUpOrder(showId: $id, sortOrder: 1) { sortOrder } }",
        {"id": show_b["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    order = [e["node"]["show"]["id"] for e in data["nextUp"]["edges"]]
    assert order.index(show_b["id"]) < order.index(show_a["id"])


async def test_next_up_picks_the_earliest_unwatched_episode_per_show(client, migrated_db):
    show = await _add_watching_show(client, titleRomaji="Multi Episode")
    _insert_next_up_episode(migrated_db, "e-nu0010", show["id"], episode=1, state="watched")
    _insert_next_up_episode(migrated_db, "e-nu0011", show["id"], episode=2)
    _insert_next_up_episode(migrated_db, "e-nu0012", show["id"], episode=3)
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    entries = data["nextUp"]["edges"]
    entry = next(e["node"] for e in entries if e["node"]["show"]["id"] == show["id"])
    assert entry["episode"]["episode"] == 2


async def test_next_up_picks_by_air_date_not_season_number(client, migrated_db):
    """§6.4 states one default — soonest-available-first — and it governs
    the intra-show pick too. A.11 used `season ASC, episode ASC` there,
    which imported Sonarr/TVDB's season-0-is-specials filing convention
    into internal behavior: a special outranked the actual premiere.
    Internal air date is the source of truth; how a source platform
    files an episode has no bearing on what to watch next."""
    show = await _add_watching_show(client, titleRomaji="Has Specials")
    # A season-0 special that aired *after* the premiere, and the premiere.
    _insert_next_up_episode(
        migrated_db,
        "e-nu0100",
        show["id"],
        season=0,
        episode=1,
        air_date_utc="2026-03-01T00:00:00Z",
    )
    _insert_next_up_episode(
        migrated_db,
        "e-nu0101",
        show["id"],
        season=1,
        episode=1,
        air_date_utc="2026-01-01T00:00:00Z",
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    entry = next(
        e["node"] for e in data["nextUp"]["edges"] if e["node"]["show"]["id"] == show["id"]
    )
    assert entry["episode"]["id"] == "e-nu0101"  # the premiere, not the special


async def test_next_up_offers_a_special_when_it_genuinely_aired_first(client, migrated_db):
    """The converse, and why air-date order needs no `kind` taxonomy: a
    pre-air special really is next when it really aired first."""
    show = await _add_watching_show(client, titleRomaji="Pre-air Special")
    _insert_next_up_episode(
        migrated_db,
        "e-nu0102",
        show["id"],
        season=0,
        episode=1,
        air_date_utc="2025-12-01T00:00:00Z",
    )
    _insert_next_up_episode(
        migrated_db,
        "e-nu0103",
        show["id"],
        season=1,
        episode=1,
        air_date_utc="2026-01-01T00:00:00Z",
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    entry = next(
        e["node"] for e in data["nextUp"]["edges"] if e["node"]["show"]["id"] == show["id"]
    )
    assert entry["episode"]["id"] == "e-nu0102"


async def test_next_up_unknown_air_date_sorts_after_known_ones(client, migrated_db):
    show = await _add_watching_show(client, titleRomaji="Unknown Date")
    _insert_next_up_episode(
        migrated_db,
        "e-nu0104",
        show["id"],
        season=1,
        episode=1,
        air_date_utc=None,
    )
    _insert_next_up_episode(
        migrated_db,
        "e-nu0105",
        show["id"],
        season=2,
        episode=1,
        air_date_utc="2026-01-01T00:00:00Z",
    )
    data = await gql(client, NEXT_UP_QUERY, headers=auth_headers())
    entry = next(
        e["node"] for e in data["nextUp"]["edges"] if e["node"]["show"]["id"] == show["id"]
    )
    assert entry["episode"]["id"] == "e-nu0105"  # known date wins over unknown


async def test_next_up_is_paginated(client, migrated_db):
    show_a = await _add_watching_show(client, titleRomaji="Show A")
    show_b = await _add_watching_show(client, titleRomaji="Show B")
    _insert_next_up_episode(
        migrated_db, "e-nu0013", show_a["id"], air_date_utc="2026-01-01T00:00:00Z"
    )
    _insert_next_up_episode(
        migrated_db, "e-nu0014", show_b["id"], air_date_utc="2026-02-01T00:00:00Z"
    )
    data = await gql(
        client,
        "query { nextUp(first: 1) { edges { node { show { id } } } pageInfo { hasNextPage } } }",
        headers=auth_headers(),
    )
    assert len(data["nextUp"]["edges"]) == 1
    assert data["nextUp"]["pageInfo"]["hasNextPage"] is True


# --- dueForMetadataRefresh (§6.7, B.1) --------------------------------------

DUE_FOR_METADATA_REFRESH_QUERY = """
    query { dueForMetadataRefresh { edges { node { id } } } }
"""

SHOW_METADATA_REFRESH_QUERY = """
    query($id: ID!) { show(id: $id) { id metadataLastRefreshedAt } }
"""


def _set_metadata_last_refreshed_at(migrated_db: Path, show_id: str, value: str | None) -> None:
    conn = db.get_connection()
    conn.execute("UPDATE show SET metadata_last_refreshed_at = ? WHERE id = ?", (value, show_id))
    conn.commit()


async def test_due_for_metadata_refresh_excludes_a_non_watching_show(client, migrated_db):
    show = await add_show(client, titleRomaji="Just Planned")  # addShow default: PLANNED
    _insert_episode_with_air_date(migrated_db, "e-due001", show["id"], None)  # airing
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert show["id"] not in ids_


async def test_due_for_metadata_refresh_excludes_a_fully_aired_watching_show(client, migrated_db):
    show = await _add_watching_show(client, titleRomaji="Fully Aired")
    _insert_episode_with_air_date(migrated_db, "e-due002", show["id"], "2020-01-01T00:00:00Z")
    # Isolate the airing check from the "already refreshed today" one — addShow's own
    # inline fetch already stamped this at creation, above.
    _set_metadata_last_refreshed_at(migrated_db, show["id"], None)
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert show["id"] not in ids_


async def test_due_for_metadata_refresh_excludes_a_show_already_refreshed_today(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Refreshed Today")
    _insert_episode_with_air_date(migrated_db, "e-due003", show["id"], None)  # airing
    _set_metadata_last_refreshed_at(migrated_db, show["id"], util.now_utc_iso())
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert show["id"] not in ids_


async def test_due_for_metadata_refresh_includes_a_never_refreshed_watching_airing_show(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Never Refreshed")
    _insert_episode_with_air_date(migrated_db, "e-due004", show["id"], None)  # airing
    _set_metadata_last_refreshed_at(migrated_db, show["id"], None)
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert show["id"] in ids_


async def test_due_for_metadata_refresh_includes_a_show_refreshed_yesterday(client, migrated_db):
    show = await _add_watching_show(client, titleRomaji="Refreshed Yesterday")
    _insert_episode_with_air_date(migrated_db, "e-due005", show["id"], "2099-01-01T00:00:00Z")
    yesterday = (datetime.now(UTC) - timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _set_metadata_last_refreshed_at(migrated_db, show["id"], yesterday)
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert show["id"] in ids_


async def test_due_for_metadata_refresh_excludes_a_watching_movie(client, migrated_db):
    # Deliberate, documented consequence of reusing _show_is_airing (A.10)
    # verbatim (SCOPE.md §11.2's B.1 note): a movie has no episode rows at
    # all, so it's always "non-airing" — a watching movie therefore never
    # qualifies for the daily background pass, regardless of status or how
    # long ago (or never) it was refreshed.
    movie = await add_show(
        client, mediaShape="MOVIE", trackingSpace="TV", titleRomaji="A Watched Film"
    )
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }",
        {"id": movie["id"]},
        headers=auth_headers(),
    )
    _set_metadata_last_refreshed_at(migrated_db, movie["id"], None)  # never refreshed either
    data = await gql(client, DUE_FOR_METADATA_REFRESH_QUERY, headers=auth_headers())
    ids_ = {e["node"]["id"] for e in data["dueForMetadataRefresh"]["edges"]}
    assert movie["id"] not in ids_


async def test_refresh_show_metadata_stamps_metadata_last_refreshed_at(client, migrated_db):
    show = await add_show(client)
    before = await gql(
        client, SHOW_METADATA_REFRESH_QUERY, {"id": show["id"]}, headers=auth_headers()
    )
    assert before["show"]["metadataLastRefreshedAt"] is not None  # addShow's own inline fetch
    _set_metadata_last_refreshed_at(migrated_db, show["id"], None)
    after = await gql(
        client,
        "mutation($id: ID!) { refreshShowMetadata(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert after["refreshShowMetadata"]["id"] == show["id"]
    data = await gql(
        client, SHOW_METADATA_REFRESH_QUERY, {"id": show["id"]}, headers=auth_headers()
    )
    assert data["show"]["metadataLastRefreshedAt"] is not None


# --- dueForSeasonReconciliation (§5.5, B.2) ---------------------------------

DUE_FOR_SEASON_RECONCILIATION_QUERY = """
    query {
      dueForSeasonReconciliation {
        edges { node { id seasonNumber show { id } } }
      }
    }
"""


def _insert_season(
    migrated_db: Path,
    season_id: str,
    show_id: str,
    season_number: int,
    last_reconciled_at: str | None = None,
) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO season
            (id, show_id, season_number, source, matched, manual_override,
             last_reconciled_at, created_at, updated_at)
        VALUES (?, ?, ?, 'unmatched', 0, 0, ?, '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')
        """,
        (season_id, show_id, season_number, last_reconciled_at),
    )
    conn.commit()


async def test_due_for_season_reconciliation_excludes_a_non_watching_shows_season(
    client, migrated_db
):
    show = await add_show(client, titleRomaji="Just Planned")  # addShow default: PLANNED
    _insert_episode_with_air_date(migrated_db, "e-dsr001", show["id"], None)  # airing
    _insert_season(migrated_db, "z-dsr001", show["id"], 1)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert show["id"] not in show_ids


async def test_due_for_season_reconciliation_excludes_a_fully_aired_watching_shows_season(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Fully Aired")
    _insert_episode_with_air_date(migrated_db, "e-dsr002", show["id"], "2020-01-01T00:00:00Z")
    _insert_season(migrated_db, "z-dsr002", show["id"], 1)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    show_ids = {e["node"]["show"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert show["id"] not in show_ids


async def test_due_for_season_reconciliation_excludes_a_season_reconciled_this_week(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Reconciled Recently")
    _insert_episode_with_air_date(migrated_db, "e-dsr003", show["id"], None)  # airing
    recent = (datetime.now(UTC) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_season(migrated_db, "z-dsr003", show["id"], 1, last_reconciled_at=recent)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    season_ids = {e["node"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert "z-dsr003" not in season_ids


async def test_due_for_season_reconciliation_includes_a_never_reconciled_season(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Never Reconciled")
    _insert_episode_with_air_date(migrated_db, "e-dsr004", show["id"], None)  # airing
    _insert_season(migrated_db, "z-dsr004", show["id"], 1, last_reconciled_at=None)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    season_ids = {e["node"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert "z-dsr004" in season_ids


async def test_due_for_season_reconciliation_includes_a_season_reconciled_eight_days_ago(
    client, migrated_db
):
    show = await _add_watching_show(client, titleRomaji="Stale Reconciliation")
    _insert_episode_with_air_date(migrated_db, "e-dsr005", show["id"], None)  # airing
    stale = (datetime.now(UTC) - timedelta(days=8)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _insert_season(migrated_db, "z-dsr005", show["id"], 1, last_reconciled_at=stale)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    season_ids = {e["node"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert "z-dsr005" in season_ids


async def test_due_for_season_reconciliation_includes_every_season_of_an_airing_show(
    client, migrated_db
):
    # Confirmed directly, 2026-08-09: "whole show airing -> all its seasons
    # weekly" — an already-finished earlier season of a still-airing show is
    # included too, not just the specific season currently airing.
    show = await _add_watching_show(client, titleRomaji="Multi-Season Airing Show")
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, state,"
        " created_at, updated_at)"
        " VALUES ('e-dsr006', ?, 2, 1, 'regular', NULL, 'unwatched',"
        " '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')",
        (show["id"],),
    )
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, state,"
        " created_at, updated_at)"
        " VALUES ('e-dsr007', ?, 1, 1, 'regular', '2020-01-01T00:00:00Z', 'unwatched',"
        " '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')",
        (show["id"],),
    )
    conn.commit()
    _insert_season(migrated_db, "z-dsr006", show["id"], 1, last_reconciled_at=None)
    _insert_season(migrated_db, "z-dsr007", show["id"], 2, last_reconciled_at=None)
    data = await gql(client, DUE_FOR_SEASON_RECONCILIATION_QUERY, headers=auth_headers())
    season_ids = {e["node"]["id"] for e in data["dueForSeasonReconciliation"]["edges"]}
    assert "z-dsr006" in season_ids  # season 1, already finished, still swept
    assert "z-dsr007" in season_ids  # season 2, the one actually airing


# --- full-text search (§6.5, A.12) -------------------------------------------

SEARCH_QUERY = """
    query($q: String!) {
      search(query: $q) { edges { node { id displayTitle } } }
    }
"""


async def test_search_matches_romaji_title(client):
    show = await add_show(client, titleRomaji="Golden Kamuy")
    await add_show(client, titleRomaji="Unrelated Show")
    data = await gql(client, SEARCH_QUERY, {"q": "Kamuy"}, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["search"]["edges"]}
    assert ids == {show["id"]}


async def test_search_matches_english_and_native_title_variants(client):
    show = await add_show(
        client,
        titleRomaji="Kimi no Na wa",
        titleEnglish="Your Name",
        titleNative="君の名は",
    )
    data_en = await gql(client, SEARCH_QUERY, {"q": "Your Name"}, headers=auth_headers())
    assert {e["node"]["id"] for e in data_en["search"]["edges"]} == {show["id"]}

    data_native = await gql(client, SEARCH_QUERY, {"q": "君の名は"}, headers=auth_headers())
    assert {e["node"]["id"] for e in data_native["search"]["edges"]} == {show["id"]}


async def test_search_matches_synopsis(client, migrated_db):
    show = await add_show(client, titleRomaji="Mystery Show")
    conn = db.get_connection()
    conn.execute(
        "UPDATE show SET synopsis = ? WHERE id = ?",
        ("A gold rush story in Hokkaido.", show["id"]),
    )
    conn.commit()
    data = await gql(client, SEARCH_QUERY, {"q": "Hokkaido"}, headers=auth_headers())
    assert {e["node"]["id"] for e in data["search"]["edges"]} == {show["id"]}


async def test_search_is_case_insensitive(client):
    show = await add_show(client, titleRomaji="Golden Kamuy")
    data = await gql(client, SEARCH_QUERY, {"q": "golden kamuy"}, headers=auth_headers())
    assert {e["node"]["id"] for e in data["search"]["edges"]} == {show["id"]}


async def test_search_no_match_returns_empty(client):
    await add_show(client, titleRomaji="Golden Kamuy")
    data = await gql(client, SEARCH_QUERY, {"q": "Nonexistent Title Xyz"}, headers=auth_headers())
    assert data["search"]["edges"] == []


async def test_search_is_paginated(client):
    await add_show(client, titleRomaji="Search Match One")
    await add_show(client, titleRomaji="Search Match Two")
    data = await gql(
        client,
        'query { search(query: "Search Match", first: 1) { edges { node { id } } '
        "pageInfo { hasNextPage } } }",
        headers=auth_headers(),
    )
    assert len(data["search"]["edges"]) == 1
    assert data["search"]["pageInfo"]["hasNextPage"] is True


# --- stats (§6.6, A.13) -------------------------------------------------------
#
# show.duration_minutes has no mutation to set it anywhere in the API yet (a
# real, pre-existing gap — see BUILD_PLAN.md's A.13 entry) — set directly via
# SQL for these tests, same as any other not-yet-mutable column this session.

STATS_QUERY = """
    query {
      stats {
        totalShows totalEpisodesWatched hoursWatched
        scoreDistribution { score count }
      }
    }
"""


async def test_stats_total_shows_counts_only_tracked(client):
    await add_show(client, titleRomaji="Tracked Show")
    untracked = await add_show(client, titleRomaji="Untracked Show")
    await gql(
        client,
        "mutation($id: ID!) { setTracked(showId: $id, tracked: false) { id } }",
        {"id": untracked["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, STATS_QUERY, headers=auth_headers())
    assert data["stats"]["totalShows"] == 1


async def test_stats_total_episodes_watched(client, migrated_db):
    show = await add_show(client)
    _insert_next_up_episode(migrated_db, "e-st0001", show["id"], episode=1, state="unwatched")
    _insert_next_up_episode(migrated_db, "e-st0002", show["id"], episode=2, state="unwatched")
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, STATS_QUERY, headers=auth_headers())
    assert data["stats"]["totalEpisodesWatched"] == 1


async def test_stats_hours_watched_uses_episode_override_or_show_default(client, migrated_db):
    conn = db.get_connection()
    show_a = await add_show(client, titleRomaji="Has Show Default")
    conn.execute("UPDATE show SET duration_minutes = 24 WHERE id = ?", (show_a["id"],))
    conn.commit()
    _insert_next_up_episode(migrated_db, "e-st0003", show_a["id"], state="unwatched")
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show_a["id"]},
        headers=auth_headers(),
    )

    show_b = await add_show(client, titleRomaji="Has Episode Override")
    _insert_next_up_episode(migrated_db, "e-st0004", show_b["id"], state="unwatched")
    await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeRuntimeOverride(episodeId: $id, runtimeMinutes: 90) { id }
        }
        """,
        {"id": "e-st0004"},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show_b["id"]},
        headers=auth_headers(),
    )

    data = await gql(client, STATS_QUERY, headers=auth_headers())
    # 24 (show_a's fallback) + 90 (show_b's own override) = 114 minutes = 1.9 hours
    assert data["stats"]["hoursWatched"] == pytest.approx(114 / 60)


async def test_stats_hours_watched_includes_movies(client, migrated_db):
    conn = db.get_connection()
    movie = await add_show(client, mediaShape="MOVIE", trackingSpace="TV", titleRomaji="A Movie")
    conn.execute("UPDATE show SET duration_minutes = 120 WHERE id = ?", (movie["id"],))
    conn.commit()
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id) { id } }",
        {"id": movie["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, STATS_QUERY, headers=auth_headers())
    assert data["stats"]["hoursWatched"] == pytest.approx(2.0)


async def test_stats_score_distribution_excludes_null_and_groups_by_value(client):
    show_a = await add_show(client, titleRomaji="Score A")
    show_b = await add_show(client, titleRomaji="Score B")
    await add_show(client, titleRomaji="Unscored")  # no score set — excluded
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show_a["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 17) { score } }",
        {"id": show_b["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, STATS_QUERY, headers=auth_headers())
    buckets = {b["score"]: b["count"] for b in data["stats"]["scoreDistribution"]}
    assert buckets == {17.0: 2}


async def test_stats_lifetime_metrics_survive_untracking(client, migrated_db):
    show = await add_show(client, titleRomaji="Will Be Untracked")
    _insert_next_up_episode(migrated_db, "e-st0005", show["id"], state="unwatched")
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 15) { score } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setTracked(showId: $id, tracked: false) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(client, STATS_QUERY, headers=auth_headers())
    assert data["stats"]["totalShows"] == 0  # no longer counted as a current show
    assert data["stats"]["totalEpisodesWatched"] == 1  # watching it already happened
    buckets = {b["score"]: b["count"] for b in data["stats"]["scoreDistribution"]}
    assert buckets == {15.0: 1}  # scoring it already happened too


# --- deletion policy (§6.11, A.14) -------------------------------------------


async def test_soft_delete_sets_tracked_false_and_status_stays(client):
    show = await add_show(client)
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: COMPLETED) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    data = await gql(
        client,
        "mutation($id: ID!) { softDeleteShow(showId: $id) { tracked status } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["softDeleteShow"]["tracked"] is False
    assert data["softDeleteShow"]["status"] == "COMPLETED"  # untouched

    history = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { trackedHistory { edges { node { newTracked } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert history["show"]["trackedHistory"]["edges"][-1]["node"]["newTracked"] is False


async def test_request_hard_delete_requires_soft_delete_first(client):
    show = await add_show(client)  # still tracked
    resp = await client.post(
        "/",
        json={
            "query": "mutation($id: ID!) { requestHardDelete(showId: $id) { id } }",
            "variables": {"id": show["id"]},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "soft-deleted first" in body["errors"][0]["message"]


async def test_request_then_cancel_hard_delete(client):
    show = await add_show(client)
    await gql(
        client,
        "mutation($id: ID!) { softDeleteShow(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    requested = await gql(
        client,
        "mutation($id: ID!) { requestHardDelete(showId: $id) { hardDeleteRequestedAt } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert requested["requestHardDelete"]["hardDeleteRequestedAt"] is not None

    cancelled = await gql(
        client,
        "mutation($id: ID!) { cancelHardDelete(showId: $id) { hardDeleteRequestedAt tracked } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert cancelled["cancelHardDelete"]["hardDeleteRequestedAt"] is None
    assert cancelled["cancelHardDelete"]["tracked"] is False  # only the timer is reversed


async def test_confirm_hard_delete_rejects_before_delay_elapses(client):
    show = await add_show(client)
    await gql(
        client,
        "mutation($id: ID!) { softDeleteShow(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { requestHardDelete(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    resp = await client.post(
        "/",
        json={
            "query": (
                "mutation($id: ID!, $t: String!) "
                "{ confirmHardDelete(showId: $id, retypedTitle: $t) }"
            ),
            "variables": {"id": show["id"], "t": show["displayTitle"]},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "24-hour delay" in body["errors"][0]["message"]


async def test_confirm_hard_delete_rejects_without_a_pending_request(client):
    show = await add_show(client)
    await gql(
        client,
        "mutation($id: ID!) { softDeleteShow(showId: $id) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    resp = await client.post(
        "/",
        json={
            "query": (
                "mutation($id: ID!, $t: String!) "
                "{ confirmHardDelete(showId: $id, retypedTitle: $t) }"
            ),
            "variables": {"id": show["id"], "t": show["displayTitle"]},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "no pending hard-delete request" in body["errors"][0]["message"]


async def _soft_delete_request_and_backdate(client, migrated_db, show_id):
    await gql(
        client,
        "mutation($id: ID!) { softDeleteShow(showId: $id) { id } }",
        {"id": show_id},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { requestHardDelete(showId: $id) { id } }",
        {"id": show_id},
        headers=auth_headers(),
    )
    conn = db.get_connection()
    conn.execute(
        "UPDATE show SET hard_delete_requested_at = '2020-01-01T00:00:00Z' WHERE id = ?",
        (show_id,),
    )
    conn.commit()


async def test_confirm_hard_delete_rejects_wrong_retyped_title(client, migrated_db):
    show = await add_show(client, titleRomaji="Exact Title")
    await _soft_delete_request_and_backdate(client, migrated_db, show["id"])
    resp = await client.post(
        "/",
        json={
            "query": (
                "mutation($id: ID!, $t: String!) "
                "{ confirmHardDelete(showId: $id, retypedTitle: $t) }"
            ),
            "variables": {"id": show["id"], "t": "Wrong Title"},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "doesn't match" in body["errors"][0]["message"]


async def test_confirm_hard_delete_cascades_across_every_related_table(client, migrated_db):
    conn = db.get_connection()
    show = await add_show(client, titleRomaji="Doomed Show", anilistId=999, tvdbId=888)

    _insert_next_up_episode(migrated_db, "e-hd0001", show["id"], episode=1, state="unwatched")
    _insert_next_up_episode(migrated_db, "e-hd0002", show["id"], episode=2, state="unwatched")
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setScore(showId: $id, score: 15) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeAirDate(episodeId: $id, airDateUtc: "2026-01-01T00:00:00Z") { id }
        }
        """,
        {"id": "e-hd0002"},
        headers=auth_headers(),
    )
    await gql(
        client,
        """
        mutation($id: ID!) {
          setSeasonMapping(showId: $id, seasonNumber: 1, anilistId: 111) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        """
        mutation($id: ID!) {
          setEpisodeNumberingScheme(showId: $id, scheme: SEASON_EPISODE) { id }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        """
        mutation($id: ID!) {
          refreshShowServicePresence(
            showId: $id, service: "sonarr", candidateTitles: ["Doomed Show"]
          ) {
            id
          }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    tag = await gql(
        client, 'mutation { createTag(name: "hd-test-tag") { id } }', headers=auth_headers()
    )
    await gql(
        client,
        "mutation($s: ID!, $t: ID!) { addShowTag(showId: $s, tagId: $t) { id } }",
        {"s": show["id"], "t": tag["createTag"]["id"]},
        headers=auth_headers(),
    )
    conn.execute("INSERT INTO franchise (id, name) VALUES ('f-hdtest', 'HD Test Franchise')")
    conn.commit()
    await gql(
        client,
        """
        mutation($f: ID!, $s: ID!) {
          setFranchiseMemberOrder(franchiseId: $f, showId: $s, sortOrder: 1) { show { id } }
        }
        """,
        {"f": "f-hdtest", "s": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { setNextUpOrder(showId: $id, sortOrder: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )

    other_show = await add_show(client, titleRomaji="Related Show")
    conn.execute(
        "INSERT INTO show_relation (show_id, related_show_id, created_at)"
        " VALUES (?, ?, '2026-08-08T00:00:00Z')",
        (show["id"], other_show["id"]),
    )
    conn.execute(
        "INSERT INTO show_relation (show_id, related_show_id, created_at)"
        " VALUES (?, ?, '2026-08-08T00:00:00Z')",
        (other_show["id"], show["id"]),
    )
    conn.commit()

    movie_show = await add_show(
        client, mediaShape="MOVIE", trackingSpace="TV", titleRomaji="Linked Movie"
    )
    await gql(
        client,
        "mutation($e: ID!, $m: ID!) { setEpisodeMovieLink(episodeId: $e, movieShowId: $m) { id } }",
        {"e": "e-hd0002", "m": movie_show["id"]},
        headers=auth_headers(),
    )

    other_episodic = await add_show(client, titleRomaji="Other Episodic")
    _insert_next_up_episode(
        migrated_db, "e-hd0003", other_episodic["id"], episode=1, state="unwatched"
    )
    await gql(
        client,
        "mutation($e: ID!, $m: ID!) { setEpisodeMovieLink(episodeId: $e, movieShowId: $m) { id } }",
        {"e": "e-hd0003", "m": show["id"]},
        headers=auth_headers(),
    )

    conn.execute(
        "INSERT INTO pending_review"
        " (id, entity_type, entity_id, field, proposed_value_chain, source, created_at)"
        " VALUES ('r-hdtst1', 'show', ?, 'metadata_fetch', '[\"err\"]', 'anilist',"
        " '2026-08-08T00:00:00Z')",
        (show["id"],),
    )
    season_row = conn.execute("SELECT id FROM season WHERE show_id = ?", (show["id"],)).fetchone()
    conn.execute(
        "INSERT INTO pending_review"
        " (id, entity_type, entity_id, field, proposed_value_chain, source, created_at)"
        " VALUES ('r-hdtst2', 'season', ?, 'anilist_push', '[\"err\"]', 'anilist',"
        " '2026-08-08T00:00:00Z')",
        (season_row["id"],),
    )
    conn.commit()

    await _soft_delete_request_and_backdate(client, migrated_db, show["id"])
    result = await gql(
        client,
        "mutation($id: ID!, $t: String!) { confirmHardDelete(showId: $id, retypedTitle: $t) }",
        {"id": show["id"], "t": "Doomed Show"},
        headers=auth_headers(),
    )
    assert result["confirmHardDelete"] is True

    assert conn.execute("SELECT 1 FROM show WHERE id = ?", (show["id"],)).fetchone() is None
    for table, column in [
        ("episode", "show_id"),
        ("watch_event", "show_id"),
        ("season", "show_id"),
        ("show_external_id", "show_id"),
        ("show_service_presence", "show_id"),
        ("episode_numbering_mapping", "show_id"),
        ("status_change", "show_id"),
        ("score_change", "show_id"),
        ("tracked_change", "show_id"),
        ("show_tag", "show_id"),
        ("franchise_member", "show_id"),
        ("next_up_override", "show_id"),
    ]:
        row = conn.execute(f"SELECT 1 FROM {table} WHERE {column} = ?", (show["id"],)).fetchone()
        assert row is None, f"{table} still has a row for the deleted show"

    assert (
        conn.execute(
            "SELECT 1 FROM show_relation WHERE show_id = ? OR related_show_id = ?",
            (show["id"], show["id"]),
        ).fetchone()
        is None
    )
    assert (
        conn.execute("SELECT 1 FROM episode_movie_link WHERE episode_id = 'e-hd0002'").fetchone()
        is None
    )
    other_link = conn.execute(
        "SELECT movie_show_id FROM episode_movie_link WHERE episode_id = 'e-hd0003'"
    ).fetchone()
    assert other_link is not None
    assert other_link["movie_show_id"] is None
    assert (
        conn.execute("SELECT 1 FROM pending_review WHERE id IN ('r-hdtst1', 'r-hdtst2')").fetchone()
        is None
    )
    assert conn.execute("SELECT 1 FROM show WHERE id = ?", (other_show["id"],)).fetchone()
    assert conn.execute("SELECT 1 FROM show WHERE id = ?", (movie_show["id"],)).fetchone()
    assert conn.execute("SELECT 1 FROM show WHERE id = ?", (other_episodic["id"],)).fetchone()
    assert (
        conn.execute("SELECT 1 FROM tag WHERE id = ?", (tag["createTag"]["id"],)).fetchone()
        is not None
    )
    assert conn.execute("SELECT 1 FROM franchise WHERE id = 'f-hdtest'").fetchone() is not None


# --- data export/import (§6.12, A.15) ----------------------------------------
#
# The actual data-movement logic (24-table round trip, dependency ordering,
# generated-column exclusion, rollback-on-failure) is covered thoroughly in
# tests/test_export_import.py against two real separate databases — this
# file only confirms exportData/importData are wired correctly through
# GraphQL itself (string in/out, error wrapping).


async def test_export_data_returns_valid_json_with_all_tables(client):
    await add_show(client, titleRomaji="Exportable Show")
    data = await gql(client, "{ exportData }", headers=auth_headers())
    payload = json.loads(data["exportData"])
    assert payload["schema_version"] == export_import.SCHEMA_VERSION
    assert set(payload["tables"]) == set(export_import.EXPORT_IMPORT_TABLES)
    assert len(payload["tables"]["show"]) == 1


async def test_import_data_rejects_schema_version_mismatch(client):
    bad_export = json.dumps({"schema_version": 999, "tables": {}})
    resp = await client.post(
        "/",
        json={
            "query": "mutation($j: String!) { importData(json: $j) { schemaVersion } }",
            "variables": {"j": bad_export},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "schema_version mismatch" in body["errors"][0]["message"]


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
    """No direct "add one episode" mutation exists (A.8's on-demand
    fetch populates episodes from Sonarr, not a manual single-episode
    mutation) — insert directly for this test's purposes."""
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

    episode_state = (
        db.get_connection().execute("SELECT state FROM episode WHERE id = 'e-tst001'").fetchone()
    )
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
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
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

    episode_state = (
        db.get_connection().execute("SELECT state FROM episode WHERE id = 'e-tst001'").fetchone()
    )
    assert episode_state["state"] == "unwatched"


async def test_delete_watch_event_keeps_state_watched_if_rewatch_remains(client, migrated_db):
    show = await add_show(client)
    await _insert_episode(migrated_db, show["id"])
    first = await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    await gql(
        client,
        "mutation($id: ID!) { addWatchEvent(showId: $id, season: 1, episode: 1) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )

    await gql(
        client,
        "mutation($id: ID!) { deleteWatchEvent(watchEventId: $id) }",
        {"id": first["addWatchEvent"]["id"]},
        headers=auth_headers(),
    )

    episode_state = (
        db.get_connection().execute("SELECT state FROM episode WHERE id = 'e-tst001'").fetchone()
    )
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

    states = (
        db.get_connection()
        .execute("SELECT state FROM episode WHERE show_id = ? AND season = 1", (show["id"],))
        .fetchall()
    )
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

    rows = (
        db.get_connection()
        .execute(
            "SELECT episode, state FROM episode WHERE show_id = ? AND season = 1", (show["id"],)
        )
        .fetchall()
    )
    states = {row["episode"]: row["state"] for row in rows}
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
                "mutation($id: ID!) { setEpisodeAirDate(episodeId: $id, "
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
        e["node"]["service"]: e["node"]["present"] for e in data["show"]["servicePresence"]["edges"]
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
        e["node"]["seasonNumber"]: e["node"]["anilistId"] for e in data["show"]["seasons"]["edges"]
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
    movie_show = await add_show(client, mediaShape="MOVIE", titleRomaji="Some Series: The Movie")
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
            "query": "mutation($id: ID!) { resolvePendingReview(id: $id) { id } }",
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
        "mutation($id: ID!) { resolvePendingReview(id: $id) { id } }",
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

    people = await gql(client, "{ people { edges { node { id name } } } }", headers=auth_headers())
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
        "mutation($id: ID!) { setNextUpOrder(showId: $id, sortOrder: 9) { sortOrder } }",
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
            "query": "mutation($id: ID!) { deleteTag(tagId: $id) }",
            "variables": {"id": tag_id},
        },
        headers=auth_headers(),
    )
    assert "errors" in resp.json()


async def test_tags_top_level_query(client):
    await gql(client, 'mutation { createTag(name: "list-me") { id } }', headers=auth_headers())
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
        json={"query": ('mutation { updateFilterPreset(id: "q-nosuch", name: "X") { id } }')},
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
    migrated_db: Path, episode_id: str, show_id: str, air_date_utc: str | None, episode: int = 1
) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode
            (id, show_id, season, episode, kind, air_date_utc, state, created_at, updated_at)
        VALUES (
            ?, ?, 1, ?, 'regular', ?, 'unwatched',
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (episode_id, show_id, episode, air_date_utc),
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


# --- episodesInRange (§8, B.11c) --------------------------------------------
#
# Unlike episodesAiringSoon (future-only, relative to server "now"),
# built for Data's own calendar render path (B.11f) — a client-side
# calendar with no floor on how far back it can page.


async def test_episodes_in_range_filters_by_explicit_window(client, migrated_db):
    show_a = await add_show(client, titleRomaji="Show A")
    show_b = await add_show(client, titleRomaji="Show B")
    show_c = await add_show(client, titleRomaji="Show C")
    show_d = await add_show(client, titleRomaji="Show D")

    _insert_episode_with_air_date(migrated_db, "e-before", show_a["id"], _iso(-10))  # before window
    _insert_episode_with_air_date(migrated_db, "e-inside", show_b["id"], _iso(-1))  # inside window
    _insert_episode_with_air_date(migrated_db, "e-after0", show_c["id"], _iso(10))  # after window
    _insert_episode_with_air_date(migrated_db, "e-nodate", show_d["id"], None)  # unknown air date

    start = _iso(-3)
    end = _iso(3)
    data = await gql(
        client,
        "query($start: DateTime!, $end: DateTime!) {"
        " episodesInRange(start: $start, end: $end) { edges { node { id } } } }",
        variables={"start": start, "end": end},
        headers=auth_headers(),
    )
    ids = {e["node"]["id"] for e in data["episodesInRange"]["edges"]}
    assert ids == {"e-inside"}


async def test_episodes_in_range_can_query_purely_in_the_past_windows(client, migrated_db):
    # No "now" floor at all — episodesAiringSoon can't do this, exactly
    # the gap episodesInRange exists to close (Data's calendar_nav
    # step_back() has no floor either).
    show = await add_show(client, titleRomaji="Old Show")
    _insert_episode_with_air_date(migrated_db, "e-oldold", show["id"], _iso(-100))

    data = await gql(
        client,
        "query($start: DateTime!, $end: DateTime!) {"
        " episodesInRange(start: $start, end: $end) { edges { node { id } } } }",
        variables={"start": _iso(-101), "end": _iso(-99)},
        headers=auth_headers(),
    )
    ids = {e["node"]["id"] for e in data["episodesInRange"]["edges"]}
    assert ids == {"e-oldold"}


async def test_episodes_in_range_end_bound_is_exclusive(client, migrated_db):
    # Half-open [start, end) — matches Data's own calendar_nav
    # CalendarState.date_range() convention (start <= local.date() < end).
    show = await add_show(client, titleRomaji="Edge Show")
    boundary = _iso(3)
    _insert_episode_with_air_date(migrated_db, "e-bound1", show["id"], boundary)

    data = await gql(
        client,
        "query($start: DateTime!, $end: DateTime!) {"
        " episodesInRange(start: $start, end: $end) { edges { node { id } } } }",
        variables={"start": _iso(0), "end": boundary},
        headers=auth_headers(),
    )
    assert data["episodesInRange"]["edges"] == []


async def test_episodes_in_range_is_empty_with_no_matching_episodes(client, migrated_db):
    # pageInfo too, not just edges — same regression class B.9's own
    # backlog test locked in (pagination.py's own docstring documents a
    # real bug where pageInfo went silently null with no end-to-end test
    # ever having queried a pageInfo sub-field).
    data = await gql(
        client,
        "query($start: DateTime!, $end: DateTime!) {"
        " episodesInRange(start: $start, end: $end) {"
        " edges { node { id } } pageInfo { hasNextPage startCursor } } }",
        variables={"start": _iso(0), "end": _iso(1)},
        headers=auth_headers(),
    )
    assert data["episodesInRange"]["edges"] == []
    assert data["episodesInRange"]["pageInfo"]["hasNextPage"] is False


# --- backlog (§6.3, B.9) -----------------------------------------------------
#
# LCARS-side scope only, confirmed with the user 2026-08-09: Query.backlog
# (this section) binds now; the Data-side calendar-native counter line and
# mark-watched-clears-oldest interaction are deferred to B.11, when Data's
# calendar switches to LCARS-backed reads generally, rather than building
# them against Data's current local computation (its own CLAUDE.md) and
# redoing them then.


def _insert_backlog_episode(
    migrated_db: Path,
    episode_id: str,
    show_id: str,
    air_date_utc: str | None,
    episode: int = 1,
    state: str = "unwatched",
    available_via_sonarr: str = "unavailable",
) -> None:
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO episode
            (id, show_id, season, episode, kind, air_date_utc, state,
             available_via_sonarr, created_at, updated_at)
        VALUES (
            ?, ?, 1, ?, 'regular', ?, ?, ?,
            '2026-08-08T00:00:00Z', '2026-08-08T00:00:00Z'
        )
        """,
        (episode_id, show_id, episode, air_date_utc, state, available_via_sonarr),
    )
    conn.commit()


async def _watching_show(client, **overrides) -> dict:
    show = await add_show(client, **overrides)
    await gql(
        client,
        "mutation($id: ID!) { setStatus(showId: $id, status: WATCHING) { id } }",
        {"id": show["id"]},
        headers=auth_headers(),
    )
    return show


BACKLOG_QUERY = "{ backlog { edges { node { id } } } }"


async def test_backlog_includes_an_unwatched_available_episode_of_a_watching_airing_show(
    client, migrated_db
):
    show = await _watching_show(client, titleRomaji="Airing Show")
    # airing: one aired episode (backlog candidate) plus one still in the future
    _insert_backlog_episode(
        migrated_db, "e-bl0001", show["id"], _iso(-1), episode=1, available_via_sonarr="available"
    )
    _insert_backlog_episode(migrated_db, "e-bl0002", show["id"], _iso(3), episode=2)

    data = await gql(client, BACKLOG_QUERY, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["backlog"]["edges"]}
    assert ids == {"e-bl0001"}


async def test_backlog_excludes_a_watched_episode(client, migrated_db):
    show = await _watching_show(client, titleRomaji="Airing Show")
    _insert_backlog_episode(
        migrated_db,
        "e-bl0003",
        show["id"],
        _iso(-1),
        episode=1,
        state="watched",
        available_via_sonarr="available",
    )
    _insert_backlog_episode(
        migrated_db, "e-bl0004", show["id"], _iso(3), episode=2
    )  # keeps it airing

    data = await gql(client, BACKLOG_QUERY, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["backlog"]["edges"]}
    assert "e-bl0003" not in ids


async def test_backlog_excludes_a_downloading_not_yet_available_episode(client, migrated_db):
    show = await _watching_show(client, titleRomaji="Airing Show")
    _insert_backlog_episode(
        migrated_db,
        "e-bl0005",
        show["id"],
        _iso(-1),
        episode=1,
        available_via_sonarr="downloading",
    )
    _insert_backlog_episode(migrated_db, "e-bl0006", show["id"], _iso(3), episode=2)

    data = await gql(client, BACKLOG_QUERY, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["backlog"]["edges"]}
    assert "e-bl0005" not in ids


async def test_backlog_excludes_episodes_of_a_non_watching_show(client, migrated_db):
    show = await add_show(client, titleRomaji="Planned Show")  # default status: planned
    _insert_backlog_episode(
        migrated_db, "e-bl0007", show["id"], _iso(-1), episode=1, available_via_sonarr="available"
    )
    _insert_backlog_episode(migrated_db, "e-bl0008", show["id"], _iso(3), episode=2)

    data = await gql(client, BACKLOG_QUERY, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["backlog"]["edges"]}
    assert "e-bl0007" not in ids


async def test_backlog_excludes_a_fully_released_non_airing_watching_show(client, migrated_db):
    # A completed-but-still-bingeing (paced-mode-style) show: watching, but
    # every episode already aired — §6.2's own _show_is_airing predicate,
    # reused verbatim here, says this show is not airing.
    show = await _watching_show(client, titleRomaji="Fully Released Show")
    _insert_backlog_episode(
        migrated_db, "e-bl0009", show["id"], _iso(-10), episode=1, available_via_sonarr="available"
    )

    data = await gql(client, BACKLOG_QUERY, headers=auth_headers())
    ids = {e["node"]["id"] for e in data["backlog"]["edges"]}
    assert "e-bl0009" not in ids


async def test_backlog_is_empty_with_no_watching_airing_shows(client, migrated_db):
    # No watching+airing shows at all — the resolver's own short-circuit
    # branch (pagination.paginate over a "1 = 0" where clause). Queries
    # pageInfo too, not just edges — pagination.py's own docstring documents
    # a real bug (pageInfo silently null) that went unnoticed for exactly
    # this reason: no end-to-end test had ever queried a pageInfo sub-field.
    await add_show(client, titleRomaji="Planned Show")  # planned, no episodes at all
    data = await gql(
        client,
        "{ backlog { edges { node { id } } pageInfo { hasNextPage startCursor } } }",
        headers=auth_headers(),
    )
    assert data["backlog"]["edges"] == []
    assert data["backlog"]["pageInfo"]["hasNextPage"] is False


# --- absolute_number synthesis (§5.2, A.25) ---------------------------------


async def test_absolute_number_synthesis_numbers_specials_between_regulars(client, monkeypatch):
    """§5.2: 'a special airing between S1E12 and S2E1 becomes 12.1; a
    second one before S2E1 becomes 12.2'."""
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=[])
    eps = [
        {
            "seasonNumber": 1,
            "episodeNumber": 12,
            "absoluteEpisodeNumber": 12,
            "airDateUtc": "2026-01-01T00:00:00Z",
            "runtime": None,
        },
        {
            "seasonNumber": 0,
            "episodeNumber": 1,
            "airDateUtc": "2026-01-05T00:00:00Z",
            "runtime": None,
        },
        {
            "seasonNumber": 0,
            "episodeNumber": 2,
            "airDateUtc": "2026-01-09T00:00:00Z",
            "runtime": None,
        },
        {
            "seasonNumber": 2,
            "episodeNumber": 1,
            "absoluteEpisodeNumber": 13,
            "airDateUtc": "2026-02-01T00:00:00Z",
            "runtime": None,
        },
    ]
    fake = _FakeSonarrClient(series={"id": 42, "seriesType": "anime"}, episodes=eps)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { episodes { edges { node { season episode absoluteNumber } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    got = {
        (e["node"]["season"], e["node"]["episode"]): e["node"]["absoluteNumber"]
        for e in data["show"]["episodes"]["edges"]
    }
    assert got[(1, 12)] == 12  # source value, untouched
    assert got[(0, 1)] == 12.1  # first special after absolute 12
    assert got[(0, 2)] == 12.2  # second one
    assert got[(2, 1)] == 13  # source value, untouched


async def test_absolute_number_synthesis_recomputes_when_a_special_is_inserted(client, monkeypatch):
    """Indices are positional, so a newly-discovered special landing
    between two existing ones must renumber the later one."""
    config.set_current(config.Config(sonarr_url="http://s:8989", sonarr_api_key="k"))
    _patch_fribb_dataset(monkeypatch, dataset=[])
    base = [
        {
            "seasonNumber": 1,
            "episodeNumber": 1,
            "absoluteEpisodeNumber": 1,
            "airDateUtc": "2026-01-01T00:00:00Z",
            "runtime": None,
        },
        {
            "seasonNumber": 0,
            "episodeNumber": 9,
            "airDateUtc": "2026-01-20T00:00:00Z",
            "runtime": None,
        },
    ]
    fake = _FakeSonarrClient(series={"id": 42}, episodes=base)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    show = await add_show(client, trackingSpace="TV", tvdbId=555)

    # A special that aired *earlier* shows up on a later fetch.
    fake._episodes = base + [
        {
            "seasonNumber": 0,
            "episodeNumber": 8,
            "airDateUtc": "2026-01-10T00:00:00Z",
            "runtime": None,
        },
    ]
    await gql(
        client,
        "mutation($i:ID!){ refreshShowMetadata(showId:$i){ id } }",
        {"i": show["id"]},
        headers=auth_headers(),
    )

    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { episodes { edges { node { season episode absoluteNumber } } } }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    got = {
        (e["node"]["season"], e["node"]["episode"]): e["node"]["absoluteNumber"]
        for e in data["show"]["episodes"]["edges"]
    }
    assert got[(1, 1)] == 1
    assert got[(0, 8)] == 1.1  # earlier-airing special takes the first slot
    assert got[(0, 9)] == 1.2  # the pre-existing one renumbered behind it


# --- file availability polling (§5.2/§6.7, B.3) — GraphQL wiring only; the ---
# --- actual poll/reconcile logic has its own dedicated test_availability.py --

POLL_FILE_AVAILABILITY = """
    mutation { pollFileAvailability { episodesUpdated showsUpdated } }
"""

BACKFILL_FILE_AVAILABILITY = """
    mutation { backfillFileAvailability { episodesUpdated showsUpdated } }
"""

AUDIT_LOCAL_FILES = """
    mutation {
      auditLocalFiles {
        episodesCorrected
        showsCorrected
        orphanFiles { showId path parsedSeason parsedEpisode }
        untrackedShows { service title externalId path }
      }
    }
"""

RECOMMENDED_INTERVAL_QUERY = """
    query { recommendedAvailabilityPollIntervalSeconds }
"""

POLL_ANIME_SCHEDULE = """
    mutation { pollAnimeSchedule { episodesUpdated flagged } }
"""

POLL_LOCAL_SERVICE_PRESENCE = """
    mutation { pollLocalServicePresence { showsUpdated } }
"""

POLL_CATALOG_SERVICE_PRESENCE = """
    mutation { pollCatalogServicePresence { showsUpdated } }
"""

RECONCILE_EPISODE_MOVIE_LINKS = """
    mutation {
      reconcileEpisodeMovieLinks { matched flagged unmatched availabilitySynced }
    }
"""


async def test_poll_file_availability_returns_zero_with_nothing_configured(client):
    # No Sonarr/Radarr credentials in the client fixture's default config (§4.9's
    # own "not configured, same as not linked" guard) — a clean no-op, not an error.
    data = await gql(client, POLL_FILE_AVAILABILITY, headers=auth_headers())
    assert data["pollFileAvailability"] == {"episodesUpdated": 0, "showsUpdated": 0}


async def test_backfill_file_availability_wiring_returns_zero_with_nothing_configured(client):
    # Same not-configured no-op guard applies to the manual backfill mutation —
    # its own event-processing/checkpoint-ignoring logic is test_availability.py's
    # job (test_backfill_sonarr_ignores_an_existing_checkpoint_and_walks_full_history
    # et al.); this only locks in that the mutation is wired to availability.py's
    # backfill_file_availability(), not poll_file_availability().
    data = await gql(client, BACKFILL_FILE_AVAILABILITY, headers=auth_headers())
    assert data["backfillFileAvailability"] == {"episodesUpdated": 0, "showsUpdated": 0}


async def test_poll_local_service_presence_returns_zero_with_no_tracked_shows(client):
    # Actual rollup logic is test_service_presence.py's job; this only locks
    # in that the mutation is wired to service_presence.refresh_local_presence().
    data = await gql(client, POLL_LOCAL_SERVICE_PRESENCE, headers=auth_headers())
    assert data["pollLocalServicePresence"] == {"showsUpdated": 0}


async def test_poll_catalog_service_presence_returns_zero_with_nothing_configured(client):
    # Same not-configured no-op guard as pollFileAvailability — actual
    # catalog-matching logic is test_service_presence.py's job.
    data = await gql(client, POLL_CATALOG_SERVICE_PRESENCE, headers=auth_headers())
    assert data["pollCatalogServicePresence"] == {"showsUpdated": 0}


async def test_reconcile_episode_movie_links_returns_zero_with_no_bonus_movie_episodes(client):
    # Actual derivation/availability-sync logic is test_episode_movie_link.py's
    # job; this only locks in that the mutation is wired to
    # episode_movie_link.reconcile_episode_movie_links() with the right shape.
    data = await gql(client, RECONCILE_EPISODE_MOVIE_LINKS, headers=auth_headers())
    assert data["reconcileEpisodeMovieLinks"] == {
        "matched": 0,
        "flagged": 0,
        "unmatched": 0,
        "availabilitySynced": 0,
    }


async def test_audit_local_files_wiring_returns_empty_with_nothing_configured(client):
    # Same not-configured no-op guard — the actual reconciliation/discovery logic
    # (including the filesystem-reading orphan pass) is test_local_audit.py's job;
    # this only locks in that the mutation is wired to local_audit.audit_local_files()
    # and that its nested OrphanFile/UntrackedRemoteShow types resolve through real
    # GraphQL with correct camelCase field names.
    data = await gql(client, AUDIT_LOCAL_FILES, headers=auth_headers())
    assert data["auditLocalFiles"] == {
        "episodesCorrected": 0,
        "showsCorrected": 0,
        "orphanFiles": [],
        "untrackedShows": [],
    }


PREVIEW_SHOW_BACKFILL = """
    query {
      previewShowBackfill { service title externalId trackingSpace mediaShape }
    }
"""

BACKFILL_UNTRACKED_SHOWS = """
    mutation {
      backfillUntrackedShows {
        created { showId service title }
        promoted { showId service title }
        failed { service title error }
      }
    }
"""


async def test_preview_show_backfill_wiring_returns_empty_with_nothing_configured(client):
    # Same not-configured no-op guard as auditLocalFiles — the actual
    # classification/computation logic is test_show_backfill.py's job; this
    # only locks in that the query is wired to show_backfill.preview_backfill()
    # and that BackfillPreviewItem resolves through real GraphQL with correct
    # camelCase field names/enum types.
    data = await gql(client, PREVIEW_SHOW_BACKFILL, headers=auth_headers())
    assert data["previewShowBackfill"] == []


async def test_backfill_untracked_shows_wiring_returns_empty_with_nothing_configured(client):
    # Same guard — actual creation/throttle/status-seed logic is
    # test_show_backfill.py's job; this only locks in that the mutation is
    # wired to show_backfill.backfill_untracked_shows() with the right shape.
    data = await gql(client, BACKFILL_UNTRACKED_SHOWS, headers=auth_headers())
    assert data["backfillUntrackedShows"] == {"created": [], "promoted": [], "failed": []}


POLL_UNTRACKED_SHOWS = """
    mutation { pollUntrackedShows { found newFindings resolvedFindings } }
"""

UNTRACKED_SHOW_FINDINGS = """
    query {
      untrackedShowFindings(first: 10) {
        edges {
          node {
            id service title externalId path trackingSpace mediaShape
            firstSeenAt lastSeenAt
          }
        }
      }
    }
"""


async def test_poll_untracked_shows_wiring_is_a_clean_no_op_with_nothing_configured(client):
    # Same guard as previewShowBackfill/backfillUntrackedShows above — the
    # actual reconciliation logic is test_untracked_sweep.py's job; this
    # only locks in that the mutation is wired to
    # untracked_sweep.sweep_untracked_shows() with the right shape.
    data = await gql(client, POLL_UNTRACKED_SHOWS, headers=auth_headers())
    assert data["pollUntrackedShows"] == {"found": 0, "newFindings": 0, "resolvedFindings": 0}

    findings = await gql(client, UNTRACKED_SHOW_FINDINGS, headers=auth_headers())
    assert findings["untrackedShowFindings"]["edges"] == []


async def test_untracked_show_findings_resolves_a_persisted_row_through_real_graphql(client):
    # Inserted directly (not via pollUntrackedShows) — this is purely
    # about UntrackedShowFindingConnection's own wiring (enum typing,
    # camelCase, nullable path), not the sweep's reconciliation logic.
    db.get_connection().execute(
        "INSERT INTO untracked_show_finding"
        " (id, service, external_id, title, path, tracking_space, media_shape,"
        "  first_seen_at, last_seen_at, created_at, updated_at)"
        " VALUES ('u-abc123', 'sonarr', '111', 'Found Show', '/data/anime/Found Show',"
        "  'anime', 'episodic', '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z',"
        "  '2026-08-10T00:00:00Z', '2026-08-10T00:00:00Z')"
    )
    db.get_connection().commit()

    data = await gql(client, UNTRACKED_SHOW_FINDINGS, headers=auth_headers())
    nodes = [e["node"] for e in data["untrackedShowFindings"]["edges"]]
    assert nodes == [
        {
            "id": "u-abc123",
            "service": "sonarr",
            "title": "Found Show",
            "externalId": "111",
            "path": "/data/anime/Found Show",
            "trackingSpace": "ANIME",
            "mediaShape": "EPISODIC",
            "firstSeenAt": "2026-08-10T00:00:00Z",
            "lastSeenAt": "2026-08-10T00:00:00Z",
        }
    ]


async def test_poll_anime_schedule_returns_zero_with_no_candidate_shows(client):
    # No watching+actively-airing anime shows in this fresh DB — candidates
    # is empty, so poll_anime_schedule() returns early without ever calling
    # animeschedule_client.fetch_raw_feed() (no real network call here). The
    # actual matching/reconciliation logic is test_animeschedule.py's job;
    # this only locks in that the mutation is wired to
    # animeschedule.poll_anime_schedule() with correct camelCase field names.
    data = await gql(client, POLL_ANIME_SCHEDULE, headers=auth_headers())
    assert data["pollAnimeSchedule"] == {"episodesUpdated": 0, "flagged": 0}


async def test_recommended_availability_poll_interval_defaults_to_baseline(client):
    data = await gql(client, RECOMMENDED_INTERVAL_QUERY, headers=auth_headers())
    assert data["recommendedAvailabilityPollIntervalSeconds"] == 3600


# --- per-integration service-health tracking (§6.7, B.6) ---------------------

SERVICE_HEALTH_QUERY = """
    query {
      serviceHealth {
        service status lastCheckedAt lastSuccessAt lastErrorMessage
      }
    }
"""


async def test_service_health_returns_unknown_for_every_tracked_service_by_default(client):
    # A fresh DB, nothing ever contacted — one entry per TrackedService,
    # not an empty list, all UNKNOWN.
    data = await gql(client, SERVICE_HEALTH_QUERY, headers=auth_headers())
    entries = data["serviceHealth"]
    assert {e["service"] for e in entries} == {
        "SONARR",
        "RADARR",
        "ANILIST",
        "ANIMESCHEDULE",
        "MAL",
    }
    assert all(e["status"] == "UNKNOWN" for e in entries)
    assert all(e["lastCheckedAt"] is None for e in entries)


async def test_episode_availability_fields_resolve_through_real_graphql(client, migrated_db):
    # Locks in the AvailabilityStatus enum's own DB<->GraphQL value mapping
    # (resolvers.py's ENUMS list) — a real gap class this project has hit before
    # (a missing _enum() registration is invisible to schema validation, only
    # surfaces at actual query time).
    show = await add_show(client, titleRomaji="Availability Fields")
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, state,"
        " available_via_sonarr, file_path_sonarr, created_at, updated_at)"
        " VALUES ('e-avf001', ?, 1, 1, 'regular', 'unwatched', 'available', '/data/x.mkv',"
        " '2026-08-09T00:00:00Z', '2026-08-09T00:00:00Z')",
        (show["id"],),
    )
    conn.commit()
    data = await gql(
        client,
        """
        query($id: ID!) {
          episode(id: $id) {
            availableViaSonarr availableViaRadarr filePathSonarr filePathRadarr availableLocally
          }
        }
        """,
        {"id": "e-avf001"},
        headers=auth_headers(),
    )
    assert data["episode"]["availableViaSonarr"] == "AVAILABLE"
    assert data["episode"]["availableViaRadarr"] == "UNAVAILABLE"
    assert data["episode"]["filePathSonarr"] == "/data/x.mkv"
    assert data["episode"]["filePathRadarr"] is None
    assert data["episode"]["availableLocally"] is True


# --- B.14 cross-service show-merge --------------------------------------

POLL_SHOW_MERGES = """
    mutation { pollShowMerges { candidatesFound merged } }
"""

SHOW_MERGES = """
    query {
      showMerges(first: 10) {
        edges {
          node {
            id matchedOn manifest mergedAt reversedAt reversedByClient
            winnerShow { id primaryTitle tracked }
            loserShow { id primaryTitle tracked }
          }
        }
      }
    }
"""


async def test_poll_show_merges_wiring_is_a_clean_no_op_with_nothing_to_merge(client):
    # Actual detection/merge logic is test_show_merge.py's job — this only
    # locks in that the mutation is wired to show_merge.sweep_show_merges()
    # with the right shape.
    data = await gql(client, POLL_SHOW_MERGES, headers=auth_headers())
    assert data["pollShowMerges"] == {"candidatesFound": 0, "merged": 0}

    merges = await gql(client, SHOW_MERGES, headers=auth_headers())
    assert merges["showMerges"]["edges"] == []


async def test_poll_show_merges_and_show_merges_query_resolve_through_real_graphql(
    client, migrated_db
):
    winner = await add_show(client, titleRomaji="Mebius Dust", trackingSpace="ANIME")
    loser = await add_show(
        client, titleRomaji="Mebius Dust", trackingSpace="TV", mediaShape="EPISODIC"
    )
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'anilist', '108992', 'https://x', 'x')",
        (winner["id"],),
    )
    conn.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, 'tvdb', '111', 'https://x', 'x')",
        (loser["id"],),
    )
    conn.commit()

    poll_data = await gql(client, POLL_SHOW_MERGES, headers=auth_headers())
    assert poll_data["pollShowMerges"] == {"candidatesFound": 1, "merged": 1}

    merges = await gql(client, SHOW_MERGES, headers=auth_headers())
    nodes = [e["node"] for e in merges["showMerges"]["edges"]]
    assert len(nodes) == 1
    node = nodes[0]
    assert node["winnerShow"]["id"] == winner["id"]
    assert node["winnerShow"]["tracked"] is True
    assert node["loserShow"]["id"] == loser["id"]
    assert node["loserShow"]["tracked"] is False
    assert "Mebius Dust" in node["matchedOn"]
    assert any("show_external_id" in line for line in node["manifest"])
    assert node["mergedAt"] is not None
    assert node["reversedAt"] is None
    assert node["reversedByClient"] is None


async def test_reverse_show_merge_requires_a_client_header(client, migrated_db):
    winner = await add_show(client, titleRomaji="Reverse Me", trackingSpace="ANIME")
    loser = await add_show(
        client, titleRomaji="Reverse Me", trackingSpace="TV", mediaShape="EPISODIC"
    )
    from lcars import show_merge

    merge_id = show_merge.merge_shows(db.get_connection(), winner["id"], loser["id"], "test")
    resp = await client.post(
        "/",
        json={
            "query": "mutation($id: ID!) { reverseShowMerge(id: $id) { id } }",
            "variables": {"id": merge_id},
        },
        headers=auth_headers(client_name=None),
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "errors" in body
    assert "X-LCARS-Client" in body["errors"][0]["message"]


async def test_reverse_show_merge_rejects_an_unknown_id(client):
    resp = await client.post(
        "/",
        json={
            "query": "mutation($id: ID!) { reverseShowMerge(id: $id) { id } }",
            "variables": {"id": "y-ghost1"},
        },
        headers=auth_headers(),
    )
    body = resp.json()
    assert "errors" in body
    assert "no such show_merge" in body["errors"][0]["message"]


async def test_reverse_show_merge_restores_the_loser_through_real_graphql(client, migrated_db):
    winner = await add_show(client, titleRomaji="Restore Me", trackingSpace="ANIME")
    loser = await add_show(
        client, titleRomaji="Restore Me", trackingSpace="TV", mediaShape="EPISODIC"
    )
    from lcars import show_merge

    merge_id = show_merge.merge_shows(db.get_connection(), winner["id"], loser["id"], "test")

    data = await gql(
        client,
        "mutation($id: ID!) { reverseShowMerge(id: $id) { id reversedAt reversedByClient } }",
        {"id": merge_id},
        headers=auth_headers("data"),
    )
    result = data["reverseShowMerge"]
    assert result["id"] == merge_id
    assert result["reversedAt"] is not None
    assert result["reversedByClient"] == "data"

    show_data = await gql(
        client,
        "query($id: ID!) { show(id: $id) { tracked } }",
        {"id": loser["id"]},
        headers=auth_headers(),
    )
    assert show_data["show"]["tracked"] is True


async def test_show_availability_fields_resolve_through_real_graphql(client, migrated_db):
    show = await add_show(
        client, mediaShape="MOVIE", trackingSpace="TV", titleRomaji="Availability Movie"
    )
    conn = db.get_connection()
    conn.execute("UPDATE show SET available_via_radarr = 'downloading' WHERE id = ?", (show["id"],))
    conn.commit()
    data = await gql(
        client,
        """
        query($id: ID!) {
          show(id: $id) { availableViaRadarr filePathRadarr availableLocally }
        }
        """,
        {"id": show["id"]},
        headers=auth_headers(),
    )
    assert data["show"]["availableViaRadarr"] == "DOWNLOADING"
    assert data["show"]["filePathRadarr"] is None
    assert data["show"]["availableLocally"] is False
