"""8.8.5 (user, 2026-10-06): a TVDB link keeps its provenance and is re-checked when Fribb later
has data — the rule is "the stored id is among the TVDB ids Fribb gives for any of the show's
season AniList ids"; Fribb silent = nothing to say; a link you confirmed is never rechecked."""

import itertools
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from lcars import reviews, tvdb_recheck

NOW = "2026-10-06T00:00:00Z"
_n = itertools.count(1)
FRIBB = [
    {"anilist_id": 10, "tvdb_id": 100, "type": "TV", "season": {"tvdb": 1}},
    {"anilist_id": 11, "tvdb_id": 100, "type": "TV", "season": {"tvdb": 2}},
    {"anilist_id": 20, "tvdb_id": 999, "type": "TV", "season": {"tvdb": 1}},
]


@pytest.fixture
def conn(tmp_path):
    path = tmp_path / "recheck.db"
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                   cwd=Path(__file__).resolve().parent.parent,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
                   check=True, capture_output=True)
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


def _show(c, sid, tvdb, source, anilist_ids):
    c.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
              " status, tracked, created_at, updated_at) VALUES (?, 'episodic', 'anime', ?,"
              " 'romaji', 'planned', 1, ?, ?)", (sid, sid, NOW, NOW))
    c.execute("INSERT INTO show_external_id (show_id, service, external_id, url, created_at,"
              " source) VALUES (?, 'tvdb', ?, '', ?, ?)", (sid, str(tvdb), NOW, source))
    for i, a in enumerate(anilist_ids, 1):
        c.execute("INSERT INTO season (id, show_id, season_number, anilist_id, source, status,"
                  " created_at, updated_at) VALUES (?, ?, ?, ?, 'auto', 'planned', ?, ?)",
                  (f"z-{next(_n):06d}", sid, i, a, NOW, NOW))
    c.commit()


def _open(c, sid):
    return c.execute("SELECT * FROM pending_review WHERE entity_id = ? AND field = ? AND"
                     " resolved_at IS NULL", (sid, tvdb_recheck.FIELD)).fetchall()


def test_a_stored_id_that_fribb_confirms_or_cannot_judge_opens_nothing(conn):
    _show(conn, "s-agree1", 100, "tvmaze", [10, 11])   # Fribb: 100 for both seasons
    _show(conn, "s-union1", 100, None, [10, 20])       # 100 is among Fribb's {100, 999}
    _show(conn, "s-silent", 100, None, [30])           # Fribb has nothing for 30
    _show(conn, "s-noids1", 100, None, [])             # no AniList id at all
    assert tvdb_recheck.recheck(conn, FRIBB) == 0


def test_a_disagreement_opens_one_review_with_both_choices_and_not_again(conn):
    _show(conn, "s-clash1", 555, "wikidata", [10])
    assert tvdb_recheck.recheck(conn, FRIBB) == 1
    [r] = _open(conn, "s-clash1")
    assert [c["id"] for c in json.loads(r["choices"])] == ["keep_tvdb", "fix_tvdb"]
    assert json.loads(r["payload"])["fribb_ids"] == [100]
    assert tvdb_recheck.recheck(conn, FRIBB) == 0  # the same answer: not asked twice
    assert len(_open(conn, "s-clash1")) == 1


def test_keep_stamps_the_link_as_yours_and_it_is_never_rechecked(conn):
    _show(conn, "s-clash2", 555, None, [10])
    tvdb_recheck.recheck(conn, FRIBB)
    [r] = _open(conn, "s-clash2")
    reviews.resolve_choice(conn, r["id"], "keep_tvdb", "captains_log", None)
    assert conn.execute("SELECT source FROM show_external_id WHERE show_id = 's-clash2'"
                        ).fetchone()[0] == "you"
    assert tvdb_recheck.recheck(conn, FRIBB) == 0


def test_fix_closes_the_review_and_changes_nothing_and_stays_closed(conn):
    _show(conn, "s-clash3", 555, "sonarr", [10])
    tvdb_recheck.recheck(conn, FRIBB)
    [r] = _open(conn, "s-clash3")
    reviews.resolve_choice(conn, r["id"], "fix_tvdb", "captains_log", None)
    assert conn.execute("SELECT external_id FROM show_external_id WHERE show_id = 's-clash3'"
                        ).fetchone()[0] == "555"
    assert tvdb_recheck.recheck(conn, FRIBB) == 0


def test_a_link_you_confirmed_is_never_rechecked_and_films_are_left_alone(conn):
    _show(conn, "s-yours1", 555, "you", [10])
    conn.execute("UPDATE show SET media_shape = 'movie' WHERE id = 's-yours1'")
    _show(conn, "s-film01", 555, None, [10])
    conn.execute("UPDATE show SET media_shape = 'movie' WHERE id = 's-film01'")
    assert tvdb_recheck.recheck(conn, FRIBB) == 0
