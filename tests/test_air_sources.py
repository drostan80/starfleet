"""Air-date candidates and the per-season schedule choice (air_sources.py, 2026-10-04), the
locks it puts on the overwriting writers, and the AniDB limits of the per-show refresh."""

import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from lcars import air_sources, anidb, animeschedule, metadata, syoboi, util

NOW = "2026-10-04T10:00:00Z"
FUTURE = "2999-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "air.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True,
        capture_output=True,
    )
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    yield c
    c.close()


def _show(c, show_id="s-air001", space="anime"):
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
        " status, created_at, updated_at)"
        " VALUES (?, 'episodic', ?, 'Air Test', 'romaji', 'watching', ?, ?)",
        (show_id, space, NOW, NOW),
    )
    return show_id


def _season(c, show_id, season_id="z-air001", number=1, status="watching", anilist_id=None):
    c.execute(
        "INSERT INTO season (id, show_id, season_number, part_number, source, kind, status,"
        " anilist_id, created_at, updated_at)"
        " VALUES (?, ?, ?, 1, 'manual', 'tvdb_season', ?, ?, ?, ?)",
        (season_id, show_id, number, status, anilist_id, NOW, NOW),
    )
    return season_id


def _episode(c, show_id, season_id, n, date=None, source=None, raw=None, season=1):
    eid = f"e-air{n:03d}"
    c.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, air_date_source,"
        " air_date_raw_sonarr, sonarr_season, sonarr_episode, season_id, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, 'regular', ?, ?, ?, ?, ?, ?, ?, ?)",
        (eid, show_id, season, n, date, source, raw, season, n, season_id, NOW, NOW),
    )
    return eid


def _ext(c, show_id, service, external_id):
    c.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES (?, ?, ?, '', ?)",
        (show_id, service, external_id, NOW),
    )


def _row(c, episode_id):
    return c.execute("SELECT * FROM episode WHERE id = ?", (episode_id,)).fetchone()


def _candidates(c, episode_id):
    return {
        (r["source"], r["channel"]): r["air_date_utc"]
        for r in c.execute(
            "SELECT * FROM episode_air_candidate WHERE episode_id = ?", (episode_id,)
        )
    }


def _two_station_show(c):
    """Anime S1 E1: Sonarr raw, TVmaze, AniDB (date only), two Syoboi stations (one deleted
    broadcast on a third that must be ignored)."""
    show = _show(c)
    z = _season(c, show)
    e = _episode(c, show, z, 1, "2026-10-04T14:45:00Z", "sonarr", raw="2026-10-04T14:45:00Z")
    _ext(c, show, "tvmaze", "900")
    _ext(c, show, "anidb", "1000")
    _ext(c, show, "syoboi", "100")
    c.execute(
        "INSERT INTO tvmaze_episode (tvmaze_show_id, season, episode, airdate, airstamp,"
        " airtime, fetched_at)"
        " VALUES (900, 1, 1, '2026-10-04', '2026-10-04T14:50:00Z', '14:50', ?)",
        (NOW,)
    )
    c.execute(
        "INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, airdate, fetched_at)"
        " VALUES (1000, 1, 1, '2026-10-04', ?)", (NOW,)
    )
    c.execute(
        "INSERT INTO episode_anidb_mapping (episode_id, anidb_anime_id, anidb_season, anidb_epno,"
        " created_at) VALUES (?, 1000, 1, 1, ?)", (e, NOW)
    )
    for pid, chid, utc, deleted in [
        (1, 7, "2026-10-04T15:00:00Z", 0),
        (2, 7, "2026-10-11T15:00:00Z", 0),  # same station, later rerun: first broadcast wins
        (3, 20, "2026-10-05T12:00:00Z", 0),
        (4, 99, "2026-10-01T00:00:00Z", 1),  # deleted
    ]:
        c.execute(
            "INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, deleted, fetched_at)"
            " VALUES (?, 100, ?, 1, ?, ?, ?)", (pid, chid, utc, deleted, NOW)
        )
    c.commit()
    return show, z, e


# ── candidates ────────────────────────────────────────────────────────────


def test_collect_lists_every_stored_source_and_one_schedule_per_station(conn):
    show, _z, e = _two_station_show(conn)
    counts = air_sources.collect_candidates(conn, show)
    assert counts == {"sonarr": 1, "tvmaze": 1, "anidb": 1, "syoboi": 2}
    assert _candidates(conn, e) == {
        ("sonarr", ""): "2026-10-04T14:45:00Z",
        ("tvmaze", ""): "2026-10-04T14:50:00Z",
        ("anidb", ""): "2026-10-04T00:00:00Z",
        ("syoboi", "7"): "2026-10-04T15:00:00Z",
        ("syoboi", "20"): "2026-10-05T12:00:00Z",
    }


def test_collect_is_repeatable_and_keeps_the_live_candidates(conn):
    show, _z, e = _two_station_show(conn)
    air_sources.record_candidate(conn, e, "anilist", "", "2026-10-04T14:46:00Z")
    air_sources.collect_candidates(conn, show)
    air_sources.collect_candidates(conn, show)
    found = _candidates(conn, e)
    assert len(found) == 6
    assert found[("anilist", "")] == "2026-10-04T14:46:00Z"


def test_a_source_that_dropped_an_episode_leaves_no_stale_candidate(conn):
    show, _z, e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    conn.execute("DELETE FROM syoboi_program WHERE chid = 20")
    air_sources.collect_candidates(conn, show)
    assert ("syoboi", "20") not in _candidates(conn, e)


def test_schedules_for_show_groups_by_source_and_station_with_station_names(conn):
    show, z, _e = _two_station_show(conn)
    conn.execute(
        "INSERT INTO syoboi_channel (chid, name, fetched_at) VALUES (20, 'AT-X', ?)", (NOW,)
    )
    air_sources.collect_candidates(conn, show)
    [season] = air_sources.schedules_for_show(conn, show)
    assert season["season_id"] == z and season["current_source"] == "sonarr"
    labels = {o["label"] for o in season["options"]}
    assert "Syoboi (Japanese TV) · AT-X" in labels
    assert "Syoboi (Japanese TV) · ch 7" in labels  # a station we hold no name for
    anidb_option = next(o for o in season["options"] if o["source"] == "anidb")
    assert anidb_option["date_only"] is True
    sonarr_option = next(o for o in season["options"] if o["source"] == "sonarr")
    assert sonarr_option["in_use"] is True and sonarr_option["chosen"] is False


# ── the choice ────────────────────────────────────────────────────────────


def test_choosing_a_schedule_writes_its_dates_and_records_the_change(conn):
    show, z, e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    stats = air_sources.set_choice(conn, z, "syoboi", "20", changed_by="web")
    row = _row(conn, e)
    assert (row["air_date_utc"], row["air_date_source"]) == ("2026-10-05T12:00:00Z", "syoboi")
    assert stats["applied"] == 1
    change = conn.execute("SELECT * FROM air_date_change WHERE episode_id = ?", (e,)).fetchone()
    assert (change["previous_air_date_utc"], change["new_air_date_utc"]) == (
        "2026-10-04T14:45:00Z", "2026-10-05T12:00:00Z")
    assert (change["previous_source"], change["new_source"], change["changed_by"]) == (
        "sonarr", "syoboi", "web")
    [season] = air_sources.schedules_for_show(conn, show)
    assert season["chosen_source"] == "syoboi" and season["chosen_channel"] == "20"


def test_a_manual_date_outranks_a_chosen_schedule(conn):
    show, z, e = _two_station_show(conn)
    conn.execute("UPDATE episode SET air_date_source = 'manual' WHERE id = ?", (e,))
    air_sources.collect_candidates(conn, show)
    stats = air_sources.set_choice(conn, z, "tvmaze")
    assert stats["kept_manual"] == 1
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T14:45:00Z"


def test_choosing_a_schedule_nobody_has_is_refused(conn):
    show, z, _e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    with pytest.raises(ValueError, match="no syoboi schedule"):
        air_sources.set_choice(conn, z, "syoboi", "555")


def test_an_episode_the_chosen_schedule_has_no_date_for_keeps_its_date(conn):
    show, z, e = _two_station_show(conn)
    e2 = _episode(conn, show, z, 2, "2026-10-11T14:45:00Z", "sonarr")
    air_sources.collect_candidates(conn, show)  # candidates exist for episode 1 only
    stats = air_sources.set_choice(conn, z, "tvmaze")
    assert stats["no_date"] == 1
    assert _row(conn, e2)["air_date_utc"] == "2026-10-11T14:45:00Z"
    assert _row(conn, e)["air_date_source"] == "tvmaze"


def test_clearing_the_choice_keeps_the_dates_and_unlocks_the_season(conn):
    show, z, e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    air_sources.set_choice(conn, z, "tvmaze")
    assert air_sources.episode_is_locked(conn, e)
    air_sources.clear_choice(conn, z)
    assert not air_sources.episode_is_locked(conn, e)
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T14:50:00Z"


def test_reapplying_after_a_schedule_moves_follows_the_chosen_source(conn):
    show, z, e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    air_sources.set_choice(conn, z, "syoboi", "7")
    conn.execute("UPDATE syoboi_program SET st_time_utc = '2026-10-04T16:00:00Z' WHERE pid = 1")
    air_sources.collect_candidates(conn, show)
    assert air_sources.apply_all_choices(conn, show)["applied"] == 1
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T16:00:00Z"


def test_a_season_that_is_not_followed_is_not_rewritten(conn):
    show, z, e = _two_station_show(conn)
    air_sources.collect_candidates(conn, show)
    conn.execute("UPDATE season SET status = 'skipped' WHERE id = ?", (z,))
    stats = air_sources.set_choice(conn, z, "tvmaze")
    assert stats["applied"] == 0
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T14:45:00Z"


# ── the locks on the overwriting writers ──────────────────────────────────


def _stub_anilist(monkeypatch, nodes, episodes=13):
    from lcars import anilist_client

    monkeypatch.setattr(
        anilist_client, "fetch_airing_schedule", lambda *_a, **_k: {"episodes": episodes,
                                                                     "nodes": nodes}
    )


def test_anilist_reconcile_records_a_candidate_and_leaves_a_chosen_season_alone(conn, monkeypatch):
    show = _show(conn)
    z = _season(conn, show, anilist_id=555)
    e = _episode(conn, show, z, 1, "2026-10-04T14:45:00Z", "anilist")
    conn.commit()
    _stub_anilist(monkeypatch, [{"episode": 1, "airingAt": 1791125100}])
    monkeypatch.setattr(util, "unix_to_iso", lambda _ts: "2026-10-04T14:30:00Z")

    air_sources.record_candidate(conn, e, "tvmaze", "", "2026-10-04T14:50:00Z")
    conn.execute(
        "INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
        " VALUES (?, 'tvmaze', '', ?)", (z, NOW))
    metadata._reconcile_air_dates(conn, {"id": show})
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T14:45:00Z"  # locked: not overwritten
    assert _candidates(conn, e)[("anilist", "")] == "2026-10-04T14:30:00Z"  # but remembered

    air_sources.clear_choice(conn, z)
    metadata._reconcile_air_dates(conn, {"id": show})
    assert _row(conn, e)["air_date_utc"] == "2026-10-04T14:30:00Z"  # unlocked: earlier wins


def test_syoboi_rewire_leaves_a_chosen_season_alone(conn):
    show, z, e = _two_station_show(conn)
    conn.execute("UPDATE episode SET air_date_utc = '2026-12-01T00:00:00Z' WHERE id = ?", (e,))
    air_sources.collect_candidates(conn, show)
    conn.execute(
        "INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
        " VALUES (?, 'tvmaze', '', ?)", (z, NOW))
    assert syoboi.rewire_airdates(conn, dry_run=True)["would_update"] == 0
    air_sources.clear_choice(conn, z)
    assert syoboi.rewire_airdates(conn, dry_run=True)["would_update"] == 1


def test_animeschedule_records_a_candidate_and_leaves_a_chosen_season_alone(conn):
    show = _show(conn)
    z = _season(conn, show)
    e = _episode(conn, show, z, 1, FUTURE, "sonarr")
    conn.execute(
        "INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
        " VALUES (?, 'sonarr', '', ?)", (z, NOW))
    item = {"episode": 1, "air_date_utc": "2026-10-04T14:46:00Z", "title": "Air Test"}
    assert animeschedule._apply_or_flag(conn, show, item) == "unchanged"
    assert _row(conn, e)["air_date_utc"] == FUTURE
    assert _candidates(conn, e)[("animeschedule", "")] == "2026-10-04T14:46:00Z"
    air_sources.clear_choice(conn, z)
    assert animeschedule._apply_or_flag(conn, show, item) == "updated"


# ── AniDB limits of the per-show refresh ──────────────────────────────────


@pytest.fixture
def anidb_state(monkeypatch):
    monkeypatch.setattr(anidb, "_banned_until", 0.0)
    calls = []

    def fake_fetch(aid, client=None):
        calls.append(aid)
        return [{"anidb_season": 1, "anidb_epno": 1, "title_en": "x", "title_ja": None,
                 "title_romaji": None, "airdate": "2026-10-04", "length_minutes": 24}]

    monkeypatch.setattr(anidb, "fetch_anime_episodes", fake_fetch)
    return calls


def test_refresh_fetches_an_anime_and_will_not_ask_again_within_a_day(conn, anidb_state):
    first = anidb.refresh_anime_now(conn, [1000])
    assert first["fetched"] == 1 and anidb_state == [1000]
    again = anidb.refresh_anime_now(conn, [1000])
    assert again["fetched"] == 0 and again["recent"] == 1 and anidb_state == [1000]


def test_refresh_does_nothing_while_the_ban_back_off_runs(conn, anidb_state, monkeypatch):
    monkeypatch.setattr(anidb, "_banned_until", time.time() + 3600)
    stats = anidb.refresh_anime_now(conn, [1000])
    assert stats["fetched"] == 0 and "banned" in stats["refused"] and anidb_state == []


def test_refresh_stops_at_the_daily_cap(conn, anidb_state, monkeypatch):
    monkeypatch.setattr(anidb, "ANIDB_DAILY_CAP", 1)
    stats = anidb.refresh_anime_now(conn, [1000, 1001])
    assert stats["fetched"] == 1 and anidb_state == [1000]
    assert "daily limit" in anidb.refresh_anime_now(conn, [1002])["refused"]


def test_one_click_fetches_at_most_a_batch_and_says_how_many_are_left(conn, anidb_state):
    stats = anidb.refresh_anime_now(conn, list(range(2000, 2012)))  # a franchise: 12 entries
    assert stats["fetched"] == anidb.ANIDB_REFRESH_MAX_PER_CLICK == len(anidb_state)
    assert stats["left"] == 12 - anidb.ANIDB_REFRESH_MAX_PER_CLICK
    again = anidb.refresh_anime_now(conn, list(range(2000, 2012)))  # next click: the next batch
    assert again["recent"] == anidb.ANIDB_REFRESH_MAX_PER_CLICK
    assert not set(anidb_state[:5]) & set(anidb_state[5:])


def test_a_ban_during_the_refresh_starts_the_back_off(conn, monkeypatch):
    monkeypatch.setattr(anidb, "_banned_until", 0.0)
    monkeypatch.setattr(anidb, "fetch_anime_episodes", lambda *_a, **_k: "BANNED")
    stats = anidb.refresh_anime_now(conn, [1000, 1001])
    assert stats["banned"] is True and anidb._banned_until > time.time()
    monkeypatch.setattr(anidb, "_banned_until", 0.0)


def test_anidb_ids_behind_a_show_come_from_its_own_id_and_from_tvdb(conn):
    show = _show(conn)
    _ext(conn, show, "anidb", "1000")
    _ext(conn, show, "tvdb", "777")
    conn.execute(
        "INSERT INTO anime_list_entry (anidb_id, tvdb_id, fetched_at) VALUES (1001, '777', ?)",
        (NOW,),
    )
    assert anidb.anidb_ids_for_show(conn, show) == [1000, 1001]
