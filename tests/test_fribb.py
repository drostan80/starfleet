"""Fribb/anime-lists dataset matching — SCOPE.md §5.5, BUILD_PLAN.md A.4.

Pure-function coverage for lcars/fribb.py. The mutation that actually
wires this into `season` rows (reconcileSeasonMapping) is covered
end-to-end in tests/test_server.py, same split as ids.py/test_ids.py
vs. the id-generating mutations themselves.
"""

import json
import os
import time

import httpx
import pytest

from lcars import fribb

SAMPLE = [
    {"tvdb_id": 100, "anilist_id": 1, "mal_id": 1, "season": {"tvdb": 1}},
    {"tvdb_id": 100, "anilist_id": 2, "mal_id": 2, "season": {"tvdb": 2}},
    {"tvdb_id": 200, "anilist_id": 3, "mal_id": "unknown", "season": {"tvdb": 1}},
    {"tvdb_id": 300, "anilist_id": "unknown"},  # filtered — no real anilist_id
    {"anilist_id": 4},  # filtered — no tvdb_id at all
]


def test_build_tvdb_index_skips_entries_missing_tvdb_or_anilist_id():
    index = fribb.build_tvdb_index(SAMPLE)
    assert set(index) == {100, 200}
    assert len(index[100]) == 2
    assert len(index[200]) == 1


def test_resolve_season_candidate_single_candidate_short_circuits():
    """Deliberately does NOT check the single candidate's season tag —
    real production behavior ported verbatim from Data's mapping.py,
    see fribb.py's own docstring for the full history/reasoning."""
    index = fribb.build_tvdb_index(SAMPLE)
    candidate = fribb.resolve_season_candidate(index, 200, season_number=99)
    assert candidate["anilist_id"] == 3


def test_resolve_season_candidate_disambiguates_multiple_by_season_number():
    index = fribb.build_tvdb_index(SAMPLE)
    c1 = fribb.resolve_season_candidate(index, 100, season_number=1)
    c2 = fribb.resolve_season_candidate(index, 100, season_number=2)
    assert c1["anilist_id"] == 1
    assert c2["anilist_id"] == 2


def test_resolve_season_candidate_ambiguous_season_number_returns_none():
    index = fribb.build_tvdb_index(SAMPLE)
    assert fribb.resolve_season_candidate(index, 100, season_number=99) is None


def test_resolve_season_candidate_unknown_tvdb_id_returns_none():
    index = fribb.build_tvdb_index(SAMPLE)
    assert fribb.resolve_season_candidate(index, 999, season_number=1) is None


def test_extract_ids():
    assert fribb.extract_ids({"anilist_id": 1, "mal_id": 2}) == (1, 2)
    assert fribb.extract_ids({"anilist_id": 1, "mal_id": "unknown"}) == (1, None)
    assert fribb.extract_ids(None) == (None, None)


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeClient:
    """Stands in for httpx.Client — load_dataset accepts an injected
    client precisely so tests never make a real network call."""

    def __init__(self, data=None, error=None):
        self._data = data
        self._error = error
        self.calls = 0

    def get(self, url):
        self.calls += 1
        if self._error is not None:
            raise self._error
        return _FakeResponse(self._data)

    def close(self):
        pass


def _backdate(path, seconds_ago):
    old = time.time() - seconds_ago
    os.utime(path, (old, old))


def test_load_dataset_downloads_and_caches_on_first_call(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    fake = _FakeClient(data=SAMPLE)
    result = fribb.load_dataset(cache_path=cache_path, client=fake)
    assert result == SAMPLE
    assert fake.calls == 1
    assert json.loads(cache_path.read_text()) == SAMPLE


def test_load_dataset_uses_fresh_cache_without_a_network_call(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    cache_path.write_text(json.dumps(SAMPLE))
    fake = _FakeClient(data=[{"different": True}])
    result = fribb.load_dataset(cache_path=cache_path, max_age=3600, client=fake)
    assert result == SAMPLE
    assert fake.calls == 0


def test_load_dataset_refetches_once_the_cache_goes_stale(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    cache_path.write_text(json.dumps([{"old": True}]))
    _backdate(cache_path, seconds_ago=1000)
    fake = _FakeClient(data=SAMPLE)
    result = fribb.load_dataset(cache_path=cache_path, max_age=1, client=fake)
    assert result == SAMPLE
    assert fake.calls == 1


def test_load_dataset_falls_back_to_stale_cache_on_network_error(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    cache_path.write_text(json.dumps(SAMPLE))
    _backdate(cache_path, seconds_ago=1000)
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    result = fribb.load_dataset(cache_path=cache_path, max_age=1, client=fake)
    assert result == SAMPLE


def test_load_dataset_raises_on_network_error_with_no_cache_at_all(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    fake = _FakeClient(error=httpx.ConnectError("boom"))
    with pytest.raises(httpx.HTTPError):
        fribb.load_dataset(cache_path=cache_path, client=fake)


# --- parse/index memoization (2026-08-09 consolidation audit) ---------------
#
# A.20 made reconciliation automatic and per-season, so a multi-season show
# re-read and re-parsed the multi-megabyte dataset — and rebuilt its full
# index — once per season, inside a sync resolver (§11.2). Memoized at the
# source rather than at one call site, since B.2's weekly all-shows pass is
# the real hammer.


def test_load_dataset_parses_a_fresh_cache_only_once(tmp_path, monkeypatch):
    cache_path = tmp_path / "anime-lists.json"
    cache_path.write_text(json.dumps(SAMPLE))
    fribb._parse_cache.clear()
    parses = []
    real_loads = json.loads
    monkeypatch.setattr(
        json, "loads", lambda s, *a, **kw: (parses.append(1), real_loads(s, *a, **kw))[1]
    )

    first = fribb.load_dataset(cache_path=cache_path)
    second = fribb.load_dataset(cache_path=cache_path)

    assert first is second, "the same parsed object should be handed back"
    assert len(parses) == 1, f"dataset parsed {len(parses)} times, expected 1"


def test_load_dataset_reparses_once_the_cache_file_changes(tmp_path):
    cache_path = tmp_path / "anime-lists.json"
    cache_path.write_text(json.dumps(SAMPLE))
    fribb._parse_cache.clear()
    first = fribb.load_dataset(cache_path=cache_path)

    os.utime(cache_path, (0, 0))  # a different mtime == a different version
    cache_path.write_text(json.dumps([]))
    second = fribb.load_dataset(cache_path=cache_path)

    assert second == [], "a rewritten cache must not serve the stale parse"
    assert first is not second


def test_build_tvdb_index_is_memoized_per_dataset_object():
    fribb._index_cache.clear()
    dataset = list(SAMPLE)
    first = fribb.build_tvdb_index(dataset)
    second = fribb.build_tvdb_index(dataset)
    assert first is second

    other = list(SAMPLE)  # a genuinely different object -> rebuilt
    assert fribb.build_tvdb_index(other) is not first
