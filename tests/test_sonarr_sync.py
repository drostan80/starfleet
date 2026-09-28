"""LCARS → Sonarr per season (PLAN-CODE phase 6) — RULEBOOK R5.5–R5.9."""

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import config, sonarr_client, sonarr_sync

NOW = "2026-01-01T00:00:00Z"


class FakeSonarr:
    def __init__(self, series):
        self.series = series
        self.updates, self.monitored = [], []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def series_by_tvdb_id(self, tvdb_id):
        return self.series

    def update_series(self, series):
        self.updates.append({e["seasonNumber"]: e["monitored"] for e in series["seasons"]})

    def episodes(self, series_id):
        return [
            {"id": 11, "seasonNumber": 2, "airDateUtc": "2020-01-01T00:00:00Z"},
            {"id": 12, "seasonNumber": 2, "airDateUtc": "2099-01-01T00:00:00Z"},
            {"id": 13, "seasonNumber": 2, "airDateUtc": None},
        ]

    def monitor_episodes(self, ids, monitored):
        self.monitored.append((sorted(ids), monitored))


@pytest.fixture
def conn(tmp_path, monkeypatch):
    path = tmp_path / "s.db"
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parent.parent,
        env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
        check=True, capture_output=True,
    )
    c = sqlite3.connect(path)
    c.execute(
        "INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title, status,"
        " created_at, updated_at) VALUES ('s-sona01', 'episodic', 'tv', 'S', 'romaji',"
        " 'watching', ?, ?)", (NOW, NOW))
    c.execute(
        "INSERT INTO show_external_id (show_id, service, external_id, url, created_at)"
        " VALUES ('s-sona01', 'tvdb', '42', '', ?)", (NOW,))
    for n in (1, 2, 3):
        c.execute(
            "INSERT INTO season (id, show_id, season_number, source, status, created_at,"
            " updated_at) VALUES (?, 's-sona01', ?, 'manual', 'planned', ?, ?)",
            (f"z-sona0{n}", n, NOW, NOW))
    config.set_current(config.Config(sonarr_url="http://s", sonarr_api_key="k"))
    yield c
    config.set_current(config.Config())
    c.close()


def _fake(monkeypatch, series):
    fake = FakeSonarr(series)
    monkeypatch.setattr(sonarr_client, "SonarrClient", lambda *a, **kw: fake)
    return fake


def _series():
    return {"id": 7, "monitored": True,
            "seasons": [{"seasonNumber": n, "monitored": True} for n in (0, 1, 2, 3)]}


def test_planned_season_monitors_its_future_episodes_only(conn, monkeypatch):
    fake = _fake(monkeypatch, _series())
    sonarr_sync.apply(conn, [("z-sona02", "skipped", "planned")])
    assert fake.updates[-1][2] is True
    assert fake.monitored == [([12, 13], True), ([11], False)]  # R2.12


def test_dropping_a_season_unmonitors_it_and_later_ones_only(conn, monkeypatch):
    fake = _fake(monkeypatch, _series())
    sonarr_sync.apply(conn, [("z-sona02", "watching", "dropped")])
    assert fake.updates[-1] == {0: True, 1: True, 2: False, 3: False}  # R5.8


def test_completed_changes_nothing(conn, monkeypatch):
    fake = _fake(monkeypatch, _series())
    sonarr_sync.apply(conn, [("z-sona02", "watching", "completed")])
    assert fake.updates == []  # R5.7


def test_sonarr_add_earlier_skipped_latest_planned(conn, monkeypatch):
    fake = _fake(monkeypatch, _series())
    sonarr_sync.apply(conn, [("z-sona01", None, "skipped"), ("z-sona02", None, "skipped"),
                             ("z-sona03", None, "planned")])
    assert fake.updates[-1] == {0: True, 1: False, 2: False, 3: True}  # R5.3


def test_a_show_not_in_sonarr_is_never_added(conn, monkeypatch):
    fake = _fake(monkeypatch, None)
    sonarr_sync.apply(conn, [("z-sona02", "skipped", "planned")])
    assert fake.updates == []  # R5.9
