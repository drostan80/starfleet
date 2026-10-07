"""Air-date precision, refreshed candidates and the selection rules (RULEBOOK R1.0b / R1.0e,
user 2026-10-07): a time is an instant, a date is a calendar day; every source's schedule is kept
fresh and diffed; the earliest timed candidate is the pick, applied when a candidate changes or
appears; TV follows Sonarr; a chosen schedule wins, filling only the dates it lacks with the
earliest candidate; icons say when a schedule moved by more than two hours."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import air_sources, air_time, airdate_priority, metadata, status_rules, syoboi, tvmaze

NOW = "2026-10-04T10:00:00Z"
SOON = "2999-01-01T00:00:00Z"


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "prec.db"
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


def _show(c, space="anime", show_id="s-prc001", status="watching"):
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " created_at, updated_at) VALUES (?, 'episodic', ?, 'Prec', 'romaji', ?, ?, ?)",
        (show_id, space, status, NOW, NOW),
    )
    c.execute(
        "INSERT INTO season (id, show_id, season_number, part_number, source, kind, status,"
        " created_at, updated_at) VALUES ('z-prc001', ?, 1, 1, 'manual', 'tvdb_season', 'watching',"
        " ?, ?)", (show_id, NOW, NOW),
    )
    return show_id


def _episode(c, show_id, n, date=None, source=None, precision=None, local=None, raw=None):
    eid = f"e-prc{n:03d}"
    c.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, air_date_source,"
        " air_precision, air_local_date, air_date_raw_sonarr, sonarr_season, sonarr_episode,"
        " season_id, created_at, updated_at)"
        " VALUES (?, ?, 1, ?, 'regular', ?, ?, ?, ?, ?, 1, ?, 'z-prc001', ?, ?)",
        (eid, show_id, n, date, source, precision, local, raw, n, NOW, NOW),
    )
    return eid


def _row(c, eid):
    return c.execute("SELECT * FROM episode WHERE id = ?", (eid,)).fetchone()


def _cand(c, eid, source, air, precision="time", local=None, channel=""):
    """A live-read candidate (AniList, animeschedule) — kept as recorded."""
    air_sources.record_candidate(c, eid, source, channel, air, precision, local)


def _ext(c, show, service, external_id):
    c.execute("INSERT OR IGNORE INTO show_external_id (show_id, service, external_id, url,"
              " created_at) VALUES (?, ?, ?, '', ?)", (show, service, external_id, NOW))


def _sonarr(c, eid, utc, precision="time", local=None):
    """Sonarr's own raw date — what its candidate is rebuilt from."""
    c.execute("UPDATE episode SET air_date_raw_sonarr = ?, air_raw_sonarr_precision = ?,"
              " air_raw_sonarr_local_date = ? WHERE id = ?", (utc, precision, local, eid))


def _tvmaze(c, show, n, airdate, airstamp, airtime):
    _ext(c, show, "tvmaze", "900")
    c.execute("INSERT OR REPLACE INTO tvmaze_episode (tvmaze_show_id, season, episode, airdate,"
              " airstamp, airtime, fetched_at) VALUES (900, 1, ?, ?, ?, ?, ?)",
              (n, airdate, airstamp, airtime, NOW))


def _anidb(c, show, eid, n, airdate):
    _ext(c, show, "anidb", "1000")
    c.execute("INSERT OR REPLACE INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno,"
              " airdate, fetched_at) VALUES (1000, 1, ?, ?, ?)", (n, airdate, NOW))
    c.execute("INSERT OR IGNORE INTO episode_anidb_mapping (episode_id, anidb_anime_id,"
              " anidb_season, anidb_epno, created_at) VALUES (?, 1000, 1, ?, ?)", (eid, n, NOW))


def _syoboi(c, show, eid, n, chid, utc):
    _ext(c, show, "syoboi", "100")
    _anidb(c, show, eid, n, "2026-10-01")
    c.execute("INSERT OR REPLACE INTO syoboi_program (pid, tid, chid, count, st_time_utc, deleted,"
              " fetched_at) VALUES (?, 100, ?, ?, ?, 0, ?)", (n * 100 + chid, chid, n, utc, NOW))


# ── time zones and the "aired at" moment ─────────────────────────────────────────────────────────


def test_end_of_the_local_day_is_japan_for_anime_and_the_latest_us_zone_otherwise():
    assert air_time.aired_at("2026-10-05", anime=True) == "2026-10-05T15:00:00Z"  # 24:00 JST
    assert air_time.aired_at("2026-10-05", anime=False) == "2026-10-06T08:00:00Z"  # 24:00 UTC-8


def test_a_late_night_japanese_broadcast_is_the_next_day_in_japan_not_in_utc():
    # 25:00 JST on 5 Oct = 4 Oct 16:00Z: AniDB's date for it is the 5th
    assert air_time.jst_date("2026-10-04T16:00:00Z").isoformat() == "2026-10-05"
    assert air_time.day_in_japan("2026-10-04T16:00:00Z", None, None).isoformat() == "2026-10-05"
    # a date-only value keeps its own day whatever its stored instant
    assert air_time.day_in_japan("2026-10-05T00:00:00Z", "date", "2026-10-05").isoformat() \
        == "2026-10-05"


def test_the_sql_end_of_day_is_the_python_one(conn):
    show = _show(conn)
    other = "s-prc002"
    conn.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
                 " status, created_at, updated_at) VALUES (?, 'episodic', 'tv', 'T', 'romaji',"
                 " 'watching', ?, ?)", (other, NOW, NOW))
    for sid, anime in ((show, True), (other, False)):
        got = conn.execute(
            f"SELECT {air_time.aired_at_from_date_sql(repr('2026-10-05'), repr(sid))}"
        ).fetchone()[0]
        assert got == air_time.aired_at("2026-10-05", anime)


def test_aired_at_expression_is_the_end_of_day_for_a_date_and_the_time_for_a_time(conn):
    show = _show(conn)
    d = _episode(conn, show, 1, "2026-10-05T00:00:00Z", "tvdb", "date", "2026-10-05")
    conn.execute("UPDATE episode SET air_aired_at = ? WHERE id = ?",
                 (air_time.aired_at("2026-10-05", True), d))
    t = _episode(conn, show, 2, "2026-10-05T10:00:00Z", "anilist")
    sql = f"SELECT id, {air_time.aired_at_sql()} AS at FROM episode"
    got = {r["id"]: r["at"] for r in conn.execute(sql)}
    assert got[d] == "2026-10-05T15:00:00Z" and got[t] == "2026-10-05T10:00:00Z"


def test_a_date_only_episode_is_not_unaired_after_its_day_but_is_during_it(conn):
    # completing a season marks unaired episodes watched only after a warning (R2.7): a date-only
    # episode of today is still unaired until its local day has ended
    show = _show(conn)
    today = status_rules.util.now_utc_iso()[:10]
    e = _episode(conn, show, 1, f"{today}T00:00:00Z", "tvdb", "date", today)
    conn.execute("UPDATE episode SET air_aired_at = ? WHERE id = ?",
                 (air_time.aired_at(today, False), e))
    with pytest.raises(status_rules.NeedsConfirmation):
        status_rules.set_level_status(conn, "z-prc001", "completed", "test")


# ── should_apply with precision ──────────────────────────────────────────────────────────────────


def test_a_date_never_replaces_a_time():
    assert not airdate_priority.should_apply(
        "anidb", "2026-10-04T00:00:00Z", "anilist", "2026-10-04T14:45:00Z", "date", "time")


def test_a_time_replaces_a_date_within_three_days_whatever_the_order():
    assert airdate_priority.should_apply(
        "anilist", "2026-10-05T14:45:00Z", "tvdb", "2026-10-05T00:00:00Z", "time", "date")
    # even when later than the date's midnight (the usual case: the time is during the day)
    assert airdate_priority.should_apply(
        "syoboi", "2026-10-07T01:00:00Z", "anidb", "2026-10-05T00:00:00Z", "time", "date")
    # outside the window it is a different slot: ordinary rules (a different source, later: no)
    assert not airdate_priority.should_apply(
        "syoboi", "2026-10-12T01:00:00Z", "anidb", "2026-10-05T00:00:00Z", "time", "date")


def test_a_time_a_few_minutes_from_a_date_still_upgrades_it():
    assert airdate_priority.should_apply(
        "anilist", "2026-10-05T00:03:00Z", "tvdb", "2026-10-05T00:00:00Z", "time", "date")


# ── candidates: kept fresh, the first value remembered, changes logged ───────────────────────────


def test_a_candidate_keeps_the_first_value_and_logs_each_later_change(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    first = air_sources.record_candidate(conn, e, "syoboi", "7", "2026-10-12T15:00:00Z")
    assert first["old"] is None
    assert air_sources.record_candidate(conn, e, "syoboi", "7", "2026-10-12T15:00:00Z") is None
    ev = air_sources.record_candidate(conn, e, "syoboi", "7", "2026-10-19T15:00:00Z")
    assert ev["old"] == "2026-10-12T15:00:00Z"
    c = conn.execute("SELECT * FROM episode_air_candidate").fetchone()
    assert c["first_air_date_utc"] == "2026-10-12T15:00:00Z"
    log = conn.execute("SELECT * FROM air_candidate_change").fetchall()
    assert [(r["previous_air_date_utc"], r["new_air_date_utc"]) for r in log] \
        == [("2026-10-12T15:00:00Z", "2026-10-19T15:00:00Z")]


def test_correcting_a_candidate_from_before_the_baseline_is_not_the_source_changing_its_mind(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    conn.execute("INSERT INTO episode_air_candidate (episode_id, source, channel, air_date_utc,"
                 " fetched_at) VALUES (?, 'sonarr', '', '2011-11-01T13:00:00Z', ?)", (e, NOW))
    air_sources.record_candidate(conn, e, "sonarr", "", "2026-10-04T16:00:00Z")  # Sonarr refreshed
    c = conn.execute("SELECT * FROM episode_air_candidate").fetchone()
    assert c["first_air_date_utc"] == "2026-10-04T16:00:00Z"
    assert conn.execute("SELECT COUNT(*) FROM air_candidate_change").fetchone()[0] == 0
    assert air_sources.air_change_flags(conn, e)["details"] == []


def test_icons_other_when_an_unfollowed_source_moved_chosen_when_the_followed_one_did(conn):
    show = _show(conn)
    e = _episode(conn, show, 1, "2026-10-12T15:00:00Z", "syoboi")
    _cand(conn, e, "syoboi", "2026-10-12T15:00:00Z", channel="7")
    _cand(conn, e, "anilist", "2026-10-12T15:00:00Z")
    _cand(conn, e, "syoboi", "2026-10-19T15:00:00Z", channel="7")   # a week later
    flags = air_sources.air_change_flags(conn, e)
    assert flags["other"] and not flags["chosen"]       # nothing chosen: every source is "other"
    assert flags["details"][0]["label"].startswith("Syoboi")
    air_sources.set_choice(conn, "z-prc001", "syoboi", "7")
    flags = air_sources.air_change_flags(conn, e)
    assert flags["chosen"] and not flags["other"]            # the followed schedule moved
    _cand(conn, e, "anilist", "2026-10-12T20:00:00Z")        # 5 h: another source, over two hours
    flags = air_sources.air_change_flags(conn, e)
    assert flags["chosen"] and flags["other"]


def test_a_change_of_two_hours_or_less_shows_no_icon(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    _cand(conn, e, "anilist", "2026-10-12T15:00:00Z")
    _cand(conn, e, "anilist", "2026-10-12T17:00:00Z")        # exactly two hours
    assert air_sources.air_change_flags(conn, e) == {"chosen": False, "other": False, "details": []}


# ── the automatic rule over the candidates ───────────────────────────────────────────────────────


def _anime_with_syoboi(conn, stored="2026-10-12T15:00:00Z"):
    show = _show(conn)
    e = _episode(conn, show, 1, stored, "syoboi")
    for service, ext in (("syoboi", "100"), ("anidb", "1000")):
        conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                     " VALUES (?, ?, ?, '', ?)", (show, service, ext, NOW))
    conn.execute("INSERT INTO anidb_episode (anidb_anime_id, anidb_season, anidb_epno, fetched_at)"
                 " VALUES (1000, 1, 1, ?)", (NOW,))
    conn.execute("INSERT INTO episode_anidb_mapping (episode_id, anidb_anime_id, anidb_season,"
                 " anidb_epno, created_at) VALUES (?, 1000, 1, 1, ?)", (e, NOW))
    conn.execute("INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, deleted,"
                 " fetched_at) VALUES (1, 100, 7, 1, ?, 0, ?)", (stored, NOW))
    return show, e


def test_a_source_that_moves_later_is_not_undone_by_a_stale_earlier_one(conn):
    """Earliest is how a date is picked at the onset, not a hard rule: Syoboi moves the 12th to
    the 19th, AniList still says the 12th; the 19th must hold, refresh after refresh."""
    show, e = _anime_with_syoboi(conn)
    _cand(conn, e, "anilist", "2026-10-12T15:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_utc"] == "2026-10-12T15:00:00Z"
    conn.execute("UPDATE syoboi_program SET st_time_utc = '2026-10-19T15:00:00Z'")
    for _ in range(3):
        air_sources.refresh_show_schedules(conn, show)
        row = _row(conn, e)
        assert (row["air_date_utc"], row["air_date_source"]) == ("2026-10-19T15:00:00Z", "syoboi")


def test_an_empty_date_takes_the_earliest_timed_candidate_never_a_date_only_one(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    _anidb(conn, show, e, 1, "2026-10-03")                       # a day, earlier
    _cand(conn, e, "anilist", "2026-10-04T14:45:00Z")
    _syoboi(conn, show, e, 1, 7, "2026-10-04T15:30:00Z")
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert (row["air_date_utc"], row["air_date_source"], row["air_precision"]) \
        == ("2026-10-04T14:45:00Z", "anilist", None)


def test_with_only_a_date_known_the_episode_holds_the_date_and_its_end_of_day(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    _anidb(conn, show, e, 1, "2026-10-05")
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert row["air_precision"] == "date" and row["air_local_date"] == "2026-10-05"
    assert row["air_date_utc"] == "2026-10-05T00:00:00Z"
    assert row["air_aired_at"] == "2026-10-05T15:00:00Z"  # anime: the end of the Japanese day


def test_a_time_arriving_for_a_date_only_episode_upgrades_it(conn):
    show = _show(conn)
    e = _episode(conn, show, 1)
    _anidb(conn, show, e, 1, "2026-10-05")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_precision"] == "date"
    _syoboi(conn, show, e, 1, 7, "2026-10-04T16:00:00Z")            # 25:00 JST on the 5th
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert row["air_date_utc"] == "2026-10-04T16:00:00Z" and row["air_precision"] is None
    assert row["air_local_date"] is None and row["air_aired_at"] is None


def test_a_manual_date_is_never_touched(conn):
    show = _show(conn)
    e = _episode(conn, show, 1, "2026-10-09T12:00:00Z", "manual")
    _cand(conn, e, "anilist", "2026-10-04T14:45:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_utc"] == "2026-10-09T12:00:00Z"


def test_a_long_aired_anime_episode_keeps_its_date_when_a_source_moves(conn):
    show = _show(conn)
    e = _episode(conn, show, 1, "2025-01-05T15:30:00Z", "syoboi")
    _cand(conn, e, "sonarr", "2025-01-05T15:30:00Z")
    air_sources.refresh_show_schedules(conn, show)
    ev = air_sources.record_candidate(conn, e, "sonarr", "", "2025-01-05T15:00:00Z")
    air_sources.auto_apply(conn, show, [ev])
    assert _row(conn, e)["air_date_utc"] == "2025-01-05T15:30:00Z"


# ── TV: Sonarr wins ──────────────────────────────────────────────────────────────────────────────


def test_a_tv_show_follows_sonarr_even_over_an_earlier_tvmaze(conn):
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1, "2026-10-04T13:00:00Z", "tvmaze")
    _tvmaze(conn, show, 1, "2026-10-04", "2026-10-04T13:00:00Z", "13:00")
    _sonarr(conn, e, "2026-10-05T01:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert (row["air_date_utc"], row["air_date_source"]) == ("2026-10-05T01:00:00Z", "sonarr")
    # and it follows Sonarr when Sonarr moves it
    _sonarr(conn, e, "2026-10-12T01:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_utc"] == "2026-10-12T01:00:00Z"


def test_a_tv_show_uses_tvmaze_only_when_sonarr_has_no_date(conn):
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    _tvmaze(conn, show, 1, "2026-10-04", "2026-10-04T13:00:00Z", "13:00")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_source"] == "tvmaze"


def test_a_weekly_streamer_with_a_real_sonarr_time_keeps_it_though_tvmaze_has_none(conn):
    """Reacher / Strange New Worlds (Prime Video / Paramount+, airTime 03:00): the files land
    0.2-0.5 h after Sonarr's time, so TVmaze having no air time does not make Sonarr's a date."""
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    _sonarr(conn, e, "2026-10-08T07:00:00Z", "time", "2026-10-08")
    _tvmaze(conn, show, 1, "2026-10-08", "2026-10-08T12:00:00Z", "")    # TVmaze: noon placeholder
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert (row["air_date_utc"], row["air_date_source"], row["air_precision"]) \
        == ("2026-10-08T07:00:00Z", "sonarr", None)
    candidates = {r["source"]: r["precision"] for r in conn.execute(
        "SELECT source, precision FROM episode_air_candidate")}
    assert candidates == {"sonarr": "time", "tvmaze": "date"}


def test_a_tv_series_filed_at_midnight_is_a_date_but_an_anime_at_midnight_is_a_time():
    """Last Seen (Apple TV, airTime 00:00, files land ~01:20Z) is a release day; a Tokyo MX slot
    at 00:00 JST is a real time."""
    ep = {"airDateUtc": "2026-10-07T04:00:00Z", "airDate": "2026-10-07"}
    assert metadata._sonarr_air_fields({"airTime": "00:00"}, ep)[1] == "date"
    assert metadata._sonarr_air_fields({"airTime": "00:00"}, ep, anime=True)[1] == "time"
    assert metadata._sonarr_air_fields({"airTime": "03:00"}, ep)[1] == "time"


def test_a_source_gaining_an_air_time_has_not_moved_its_schedule(conn):
    """TVmaze's noon placeholder (a date) becoming a real 21:00 must not light the ! icon."""
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    _tvmaze(conn, show, 1, "2026-10-07", "2026-10-07T12:00:00Z", "")
    air_sources.refresh_show_schedules(conn, show)
    _tvmaze(conn, show, 1, "2026-10-07", "2026-10-08T01:00:00Z", "21:00")
    air_sources.refresh_show_schedules(conn, show)
    c = conn.execute("SELECT * FROM episode_air_candidate WHERE source = 'tvmaze'").fetchone()
    assert c["precision"] == "time" and c["first_air_date_utc"] == "2026-10-08T01:00:00Z"
    assert air_sources.air_change_flags(conn, e)["details"] == []
    # a real move afterwards still shows
    _tvmaze(conn, show, 1, "2026-10-14", "2026-10-15T01:00:00Z", "21:00")
    air_sources.refresh_show_schedules(conn, show)
    assert air_sources.air_change_flags(conn, e)["other"] is True


def test_a_schedule_an_open_wrong_entry_review_distrusts_is_never_applied_automatically(conn):
    """Slime S4 shape: AniList's schedule is months off. It stays a candidate to see and choose,
    but fills no empty date — not by the automatic rule, not as a chosen season's fallback."""
    from lcars import pending_review

    show = _show(conn)
    e = _episode(conn, show, 1)
    _cand(conn, e, "anilist", "2026-04-03T14:00:00Z")                 # months from the truth
    _anidb(conn, show, e, 1, "2024-04-05")                            # the only trustworthy source
    pending_review.open_or_extend(conn, "season", "z-prc001", "anilist_id", "anilist", None,
                                  "AniList media 1's airing schedule is ~700 days from Sonarr's")
    air_sources.refresh_show_schedules(conn, show)
    assert (_row(conn, e)["air_date_source"], _row(conn, e)["air_date_utc"]) \
        == ("anidb", "2024-04-05T00:00:00Z")
    assert conn.execute("SELECT COUNT(*) FROM episode_air_candidate WHERE source = 'anilist'"
                        ).fetchone()[0] == 1                           # still there to choose
    # once answered the schedule is trusted again
    conn.execute("UPDATE pending_review SET resolved_at = 'x'")
    e2 = _episode(conn, show, 2)
    _cand(conn, e2, "anilist", "2026-04-10T14:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e2)["air_date_source"] == "anilist"


def test_tvmaze_with_an_air_time_stays_a_time(conn):
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES (?, 'tvmaze', '900', '', ?)", (show, NOW))
    conn.execute("INSERT INTO tvmaze_episode (tvmaze_show_id, season, episode, airdate, airstamp,"
                 " airtime, fetched_at) VALUES (900, 1, 1, '2026-10-05', '2026-10-05T01:00:00Z',"
                 " '21:00', ?)", (NOW,))
    air_sources.refresh_show_schedules(conn, show)
    row = _row(conn, e)
    assert row["air_precision"] is None and row["air_date_utc"] == "2026-10-05T01:00:00Z"


# ── a chosen schedule ────────────────────────────────────────────────────────────────────────────


def test_a_chosen_schedule_wins_and_every_refresh_keeps_following_it(conn):
    show, e = _anime_with_syoboi(conn, stored="2026-10-07T16:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    _cand(conn, e, "anilist", "2026-10-04T16:05:00Z")          # earlier, but not the chosen one
    air_sources.set_choice(conn, "z-prc001", "syoboi", "7")
    for _ in range(2):
        air_sources.refresh_show_schedules(conn, show)
        assert _row(conn, e)["air_date_utc"] == "2026-10-07T16:00:00Z"
    # a missed week: Syoboi moves it, the chosen schedule moves the date with it
    conn.execute("UPDATE syoboi_program SET st_time_utc = '2026-10-14T16:00:00Z'")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_utc"] == "2026-10-14T16:00:00Z"


def test_in_a_chosen_season_an_empty_date_takes_the_earliest_candidate_until_the_source_has_one(
    conn,
):
    show = _show(conn)
    e1 = _episode(conn, show, 1, "2026-10-07T16:00:00Z", "syoboi")
    e2 = _episode(conn, show, 2)
    _syoboi(conn, show, e1, 1, 7, "2026-10-07T16:00:00Z")
    _cand(conn, e2, "anilist", "2026-10-14T16:05:00Z")
    _sonarr(conn, e2, "2026-10-14T16:00:00Z")
    air_sources.collect_candidates(conn, show)
    stats = air_sources.set_choice(conn, "z-prc001", "syoboi", "7")
    assert stats["fallback"] == 1
    assert (_row(conn, e2)["air_date_utc"], _row(conn, e2)["air_date_source"]) \
        == ("2026-10-14T16:00:00Z", "sonarr")                 # the earliest timed candidate
    # the chosen source now has a date for it: from here on that is the date
    _syoboi(conn, show, e2, 2, 7, "2026-10-14T16:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e2)["air_date_source"] == "syoboi"
    # and an episode that already has a date keeps it when the chosen source has none
    e3 = _episode(conn, show, 3, "2026-10-21T12:00:00Z", "anilist")
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e3)["air_date_utc"] == "2026-10-21T12:00:00Z"


def test_the_empty_date_fillers_leave_a_chosen_season_to_its_own_rule(conn):
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES (?, 'tvmaze', '900', '', ?)", (show, NOW))
    conn.execute("INSERT INTO tvmaze_episode (tvmaze_show_id, season, episode, airdate, airstamp,"
                 " airtime, fetched_at) VALUES (900, 1, 1, '2026-10-05', '2026-10-05T01:00:00Z',"
                 " '21:00', ?)", (NOW,))
    conn.execute("INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
                 " VALUES ('z-prc001', 'sonarr', '', ?)", (NOW,))
    assert tvmaze.fill_airdate_gaps(conn) == 0
    assert _row(conn, e)["air_date_utc"] is None
    conn.execute("DELETE FROM season_air_choice")
    assert tvmaze.fill_airdate_gaps(conn) == 1


def test_the_tvmaze_filler_stamps_a_date_when_tvmaze_has_no_air_time(conn):
    show = _show(conn, space="tv")
    e = _episode(conn, show, 1)
    conn.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
                 " VALUES (?, 'tvmaze', '900', '', ?)", (show, NOW))
    conn.execute("INSERT INTO tvmaze_episode (tvmaze_show_id, season, episode, airdate, airstamp,"
                 " airtime, fetched_at) VALUES (900, 1, 1, '2026-10-07',"
                 " '2026-10-07T12:00:00Z', '', ?)", (NOW,))
    assert tvmaze.fill_airdate_gaps(conn) == 1
    row = _row(conn, e)
    assert (row["air_date_utc"], row["air_precision"], row["air_local_date"]) \
        == ("2026-10-07T00:00:00Z", "date", "2026-10-07")
    assert row["air_aired_at"] == "2026-10-08T08:00:00Z"


def test_the_syoboi_rewire_leaves_a_chosen_season_alone_and_upgrades_a_date(conn):
    show, e = _anime_with_syoboi(conn, stored="2026-10-05T00:00:00Z")
    conn.execute("UPDATE episode SET air_date_source = 'anidb', air_precision = 'date',"
                 " air_local_date = '2026-10-05', air_aired_at = '2026-10-05T15:00:00Z'"
                 " WHERE id = ?", (e,))
    conn.execute("UPDATE syoboi_program SET st_time_utc = '2026-10-04T16:00:00Z'")
    conn.execute("INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
                 " VALUES ('z-prc001', 'anilist', '', ?)", (NOW,))
    assert syoboi.rewire_airdates(conn)["updated"] == 0
    conn.execute("DELETE FROM season_air_choice")
    assert syoboi.rewire_airdates(conn)["updated"] == 1
    row = _row(conn, e)
    assert row["air_date_utc"] == "2026-10-04T16:00:00Z" and row["air_precision"] is None
    assert row["air_aired_at"] is None


# ── what Sonarr's own air time says ─────────────────────────────────────────────────────────────


def test_sonarrs_time_is_real_only_when_the_series_has_an_air_time():
    ep = {"airDateUtc": "2026-10-07T04:00:00Z", "airDate": "2026-10-07"}
    assert metadata._sonarr_air_fields({"airTime": "21:00"}, ep) \
        == ("2026-10-07T04:00:00Z", "time", "2026-10-07")
    assert metadata._sonarr_air_fields({"airTime": ""}, ep) \
        == ("2026-10-07T04:00:00Z", "date", "2026-10-07")
    assert metadata._sonarr_air_fields({}, {"airDateUtc": None}) == (None, "date", None)


def test_a_sonarr_date_months_from_a_curated_one_is_not_applied_over_it(conn):
    """Kanojo no Tomodachi: Sonarr's placeholder dates were 15 years early — 'earliest' must not
    let them replace Syoboi's; a plausible earlier Sonarr date (a few days) still does."""
    show, e = _anime_with_syoboi(conn, stored="2026-10-07T16:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    _sonarr(conn, e, "2011-11-01T13:00:00Z")
    air_sources.refresh_show_schedules(conn, show)
    assert (_row(conn, e)["air_date_utc"], _row(conn, e)["air_date_source"]) \
        == ("2026-10-07T16:00:00Z", "syoboi")
    _sonarr(conn, e, "2026-10-04T16:00:00Z")                      # a plausible, earlier schedule
    air_sources.refresh_show_schedules(conn, show)
    assert _row(conn, e)["air_date_source"] == "sonarr"


# ── a season that has aired fully is history (user 10-07) ────────────────────────────────────────


def _aired_season(conn, show, *, days_ago=40):
    """Season 1 of `show`: three episodes, all dated, the last aired `days_ago` days ago."""
    from lcars import util

    ids = []
    for n in (1, 2, 3):
        when = util.utc_iso_offset(-(days_ago + (3 - n) * 7))
        eid = _episode(conn, show, n, when, "syoboi")
        conn.execute("UPDATE episode SET title = ? WHERE id = ?", (f"Episode {n}", eid))
        ids.append(eid)
    return ids


def test_a_season_is_finished_when_every_episode_is_dated_and_the_last_aired_over_14_days_ago(conn):
    show = _show(conn)
    _aired_season(conn, show, days_ago=20)
    assert air_sources.finished_seasons(conn, show) == {1}
    assert not air_sources.has_airing_season(conn, show)
    from lcars import util

    e4 = _episode(conn, show, 4, util.utc_iso_offset(-3), "syoboi")   # one aired 3 days ago
    conn.execute("UPDATE episode SET title = 'Episode 4' WHERE id = ?", (e4,))
    assert air_sources.finished_seasons(conn, show) == set()
    assert air_sources.has_airing_season(conn, show)
    conn.execute("UPDATE episode SET air_date_utc = NULL WHERE episode = 4")  # or one undated
    assert air_sources.finished_seasons(conn, show) == set()


def test_a_finished_season_is_not_refreshed_and_nothing_in_it_changes(conn):
    show = _show(conn)
    e1, e2, e3 = _aired_season(conn, show)
    before = {r["id"]: r["air_date_utc"] for r in conn.execute("SELECT * FROM episode")}
    _sonarr(conn, e2, "2020-01-01T10:00:00Z")                  # a source says otherwise
    _cand(conn, e2, "anilist", "2020-01-01T10:00:00Z")
    result = air_sources.refresh_show_schedules(conn, show)
    assert result["candidates"] == 0 and result["events"] == 0
    assert {r["id"]: r["air_date_utc"] for r in conn.execute("SELECT * FROM episode")} == before
    # and a chosen schedule does not re-apply there either (only the user's own click does)
    air_sources.collect_candidates(conn, show)
    conn.execute("INSERT INTO season_air_choice (season_id, source, channel, chosen_at)"
                 " VALUES ('z-prc001', 'anilist', '', ?)", (NOW,))
    assert air_sources.apply_all_choices(conn, show)["applied"] == 0
    assert air_sources.apply_choice(conn, "z-prc001")["applied"] >= 1   # the explicit action works


def test_only_the_airing_season_of_a_show_is_refreshed(conn):
    from lcars import util

    show = _show(conn)
    old = _aired_season(conn, show)                                  # season 1, long aired
    conn.execute("INSERT INTO season (id, show_id, season_number, part_number, source, kind,"
                 " status, created_at, updated_at) VALUES ('z-prc002', ?, 2, 1, 'manual',"
                 " 'tvdb_season', 'watching', ?, ?)", (show, NOW, NOW))
    new = "e-prc101"
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, air_date_source,"
        " sonarr_season, sonarr_episode, season_id, created_at, updated_at)"
        " VALUES (?, ?, 2, 1, 'regular', ?, 'sonarr', 2, 1, 'z-prc002', ?, ?)",
        (new, show, util.utc_iso_offset(5), NOW, NOW))
    _sonarr(conn, old[0], "2019-01-01T00:00:00Z")
    _sonarr(conn, new, util.utc_iso_offset(6))
    air_sources.refresh_show_schedules(conn, show)
    held = {r["episode_id"] for r in conn.execute("SELECT episode_id FROM episode_air_candidate")}
    assert new in held and old[0] not in held                # candidates only for the airing season


def test_anilist_is_not_called_for_a_season_that_has_aired_fully(conn, monkeypatch):
    from lcars import anilist_client

    show = _show(conn)
    _aired_season(conn, show)
    conn.execute("UPDATE season SET anilist_id = 777 WHERE id = 'z-prc001'")
    calls = []
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule",
                        lambda anilist_id, *a, **kw: calls.append(anilist_id))
    metadata._reconcile_air_dates(conn, {"id": show})
    assert calls == []


def test_a_planned_show_with_every_season_aired_has_nothing_to_refresh(conn, monkeypatch):
    show = _show(conn, status="planned")
    _aired_season(conn, show)
    read = []
    monkeypatch.setattr(metadata, "_read_sonarr", lambda *a, **kw: read.append(1))
    metadata.refresh_schedules_only(conn, show)
    assert read == []                                        # no Sonarr, no AniList, no TVmaze call
    assert conn.execute("SELECT metadata_last_refreshed_at FROM show WHERE id = ?",
                        (show,)).fetchone()[0] is not None


def test_the_syoboi_rewire_leaves_a_finished_season_alone(conn):
    show, e = _anime_with_syoboi(conn, stored="2024-01-05T15:30:00Z")   # long ago
    conn.execute("UPDATE syoboi_program SET st_time_utc = '2024-01-05T15:00:00Z'")  # earlier
    conn.execute("UPDATE episode SET air_date_source = 'anidb' WHERE id = ?", (e,))
    assert syoboi.rewire_airdates(conn, only_airing=True)["updated"] == 0
    assert syoboi.rewire_airdates(conn)["updated"] == 1                # the unrestricted one


# ── announced seasons: planned, "TBA", whatever date a wrong source gave them ────────────────────


def _planned_season_two(conn, show, first_date=None, anilist_id=None):
    conn.execute("INSERT INTO season (id, show_id, season_number, part_number, source, kind,"
                 " status, anilist_id, created_at, updated_at) VALUES ('z-prc002', ?, 2, 1,"
                 " 'manual', 'tvdb_season', 'planned', ?, ?, ?)", (show, anilist_id, NOW, NOW))
    conn.execute(
        "INSERT INTO episode (id, show_id, season, episode, kind, air_date_utc, air_date_source,"
        " title, season_id, sonarr_season, sonarr_episode, created_at, updated_at)"
        " VALUES ('e-prc201', ?, 2, 1, 'regular', ?, ?, 'TBA', 'z-prc002', 2, 1, ?, ?)",
        (show, first_date, "anilist" if first_date else None, NOW, NOW))


def test_a_planned_season_is_targeted_whatever_date_it_wrongly_holds(conn):
    """The 13 copied dates (user 10-07): S2 is planned and holds S1's premiere date from long ago.
    A planned season is a future season: it is refreshed (weekly), not treated as history."""
    from lcars import util

    show = _show(conn)
    _aired_season(conn, show)                                        # S1: watching, long aired
    _planned_season_two(conn, show, util.utc_iso_offset(-100))
    assert air_sources.season_states(conn, show) == {1: "history", 2: "planned"}
    assert air_sources.finished_seasons(conn, show) == {1}
    assert air_sources.has_airing_season(conn, show)
    assert air_sources.refresh_cadence(conn, show) == "weekly"
    conn.execute("UPDATE episode SET air_date_utc = ? WHERE id = 'e-prc201'",
                 (util.utc_iso_offset(3),))                          # a season airing now: daily
    assert air_sources.season_states(conn, show)[2] == "airing"
    assert air_sources.refresh_cadence(conn, show) == "daily"
    conn.execute("UPDATE episode SET air_date_utc = NULL WHERE id = 'e-prc201'")   # undated: airing
    assert air_sources.season_states(conn, show)[2] == "airing"


def test_a_season_that_is_not_planned_and_has_aired_is_history(conn):
    from lcars import util

    show = _show(conn)
    _aired_season(conn, show)
    _planned_season_two(conn, show, util.utc_iso_offset(-100))
    conn.execute("UPDATE season SET status = 'completed' WHERE id = 'z-prc002'")
    assert air_sources.season_states(conn, show) == {1: "history", 2: "history"}
    assert air_sources.refresh_cadence(conn, show) is None


def test_a_show_whose_every_season_is_titled_and_aired_has_no_cadence(conn):
    show = _show(conn)
    _aired_season(conn, show)
    assert air_sources.refresh_cadence(conn, show) is None


# ── chronology (R1.6) ────────────────────────────────────────────────────────────────────────────


def test_a_season_does_not_start_before_the_one_before_it_ended(conn):
    from lcars import util

    show = _show(conn)
    _aired_season(conn, show, days_ago=40)                           # S1 ends 40 days ago
    assert air_sources.starts_before_previous_season_ended(
        conn, show, 2, util.utc_iso_offset(-120))                    # S1's premiere date: a copy
    assert not air_sources.starts_before_previous_season_ended(
        conn, show, 2, util.utc_iso_offset(60))                      # a real sequel
    assert not air_sources.starts_before_previous_season_ended(
        conn, show, 1, "2000-01-01T00:00:00Z")                       # a first season has no prior


def _timestamp(iso):
    import datetime as dt

    return int(dt.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp())


def test_anilist_does_not_write_season_ones_schedule_onto_a_placeholder_season(conn, monkeypatch):
    """The 13 clones: a schedule that is S1's must not date S2E1, nor become a candidate."""
    from lcars import anilist_client

    show = _show(conn)
    _aired_season(conn, show, days_ago=40)
    _planned_season_two(conn, show, anilist_id=999)
    first = conn.execute("SELECT air_date_utc FROM episode WHERE id = 'e-prc001'").fetchone()[0]
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda i, *a, **kw: {
        "episodes": 12, "nodes": [{"episode": 1, "airingAt": _timestamp(first)}]})
    metadata._reconcile_air_dates(conn, {"id": show})
    assert conn.execute("SELECT air_date_utc FROM episode WHERE id = 'e-prc201'"
                        ).fetchone()[0] is None
    assert conn.execute("SELECT COUNT(*) FROM episode_air_candidate WHERE episode_id = 'e-prc201'"
                        ).fetchone()[0] == 0


def test_anilist_does_not_write_a_schedule_for_an_id_another_level_holds(conn, monkeypatch):
    from lcars import anilist_client

    show = _show(conn)
    conn.execute("UPDATE season SET anilist_id = 435 WHERE id = 'z-prc001'")
    _planned_season_two(conn, show, anilist_id=435)
    calls = []
    monkeypatch.setattr(anilist_client, "fetch_airing_schedule", lambda i, *a, **kw: (
        calls.append(i), {"episodes": 20, "nodes": [{"episode": 1, "airingAt": 5000000000}]})[1])
    metadata._reconcile_air_dates(conn, {"id": show})
    assert calls == []                                               # no call for either level


def test_provisional_episodes_are_not_made_when_the_season_starts_before_the_last_one_ended(conn):
    """Mahou Shoujo ni Akogarete: S2E1 carries S1's premiere date, so S1's Syoboi run 'fit' S2."""
    from lcars import provisional_episodes, util

    show = _show(conn, status="planned")
    _aired_season(conn, show, days_ago=40)
    first = conn.execute("SELECT air_date_utc FROM episode WHERE id = 'e-prc001'").fetchone()[0]
    _planned_season_two(conn, show, first)
    for service, ext in (("syoboi", "100"), ("anidb", "1000")):
        _ext(conn, show, service, ext)
    for n in range(1, 9):                        # Syoboi's broadcasts of S1's run, weekly
        when = util.unix_to_iso(_timestamp(first) + (n - 1) * 7 * 86400)
        conn.execute("INSERT INTO syoboi_program (pid, tid, chid, count, st_time_utc, ed_time_utc,"
                     " deleted, fetched_at) VALUES (?, 100, 7, ?, ?, ?, 0, ?)",
                     (n, n, when, when, NOW))
    result = provisional_episodes.sync_show(conn, show)
    assert result["created"] == 0
    assert conn.execute("SELECT COUNT(*) FROM episode WHERE provisional = 1").fetchone()[0] == 0
