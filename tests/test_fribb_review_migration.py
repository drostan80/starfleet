"""Migration c2d3e4f5a6b7: open Fribb id reviews are rewritten to say what happened (user 10-07)."""

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _alembic(path, *args):
    subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, check=True,
                   env={**os.environ, "LCARS_DATABASE_URL": f"sqlite:///{path}"},
                   capture_output=True)


def test_open_fribb_id_reviews_become_a_message_with_confirm(tmp_path):
    path = tmp_path / "m.db"
    _alembic(path, "upgrade", "head")
    _alembic(path, "downgrade", "b1c2d3e4f5a6")
    c = sqlite3.connect(path)
    c.execute("INSERT INTO show (id, media_shape, tracking_space, title_romaji, primary_title,"
              " status, created_at, updated_at) VALUES ('s-mig001', 'episodic', 'anime', 'M',"
              " 'romaji', 'watching', 'x', 'x')")
    c.execute("INSERT INTO season (id, show_id, season_number, part_number, source, kind, status,"
              " created_at, updated_at) VALUES ('z-mig001', 's-mig001', 1, 1, 'fribb',"
              " 'tvdb_season', 'watching', 'x', 'x')")
    for rid, field, prev, chain, resolved in (
        ("r-mig001", "mal_id", None, '["62922"]', None),             # open, added
        ("r-mig002", "anilist_id", 11, '["22"]', None),              # open, changed
        ("r-mig003", "mal_id", None, '["62922"]', "2026-10-01T00:00:00Z"),   # resolved: untouched
        ("r-mig004", "anilist_id", None, '["dataset unreachable"]', None),   # not an id: untouched
    ):
        c.execute("INSERT INTO pending_review (id, entity_type, entity_id, field, previous_value,"
                  " proposed_value_chain, source, created_at, resolved_at) VALUES (?, 'season',"
                  " 'z-mig001', ?, ?, ?, 'fribb', 'x', ?)", (rid, field, prev, chain, resolved))
    c.commit()
    c.close()
    _alembic(path, "upgrade", "head")
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    rows = {r["id"]: r for r in c.execute("SELECT * FROM pending_review")}
    assert json.loads(rows["r-mig001"]["proposed_value_chain"]) == [
        "Fribb added MAL ID 62922 for this season"]
    assert json.loads(rows["r-mig001"]["choices"]) == [{"id": "confirm", "label": "Confirm"}]
    assert rows["r-mig001"]["show_id"] == "s-mig001"
    assert json.loads(rows["r-mig002"]["proposed_value_chain"]) == [
        "Fribb changed this season's AniList ID from 11 to 22"]
    assert rows["r-mig003"]["proposed_value_chain"] == '["62922"]'
    assert rows["r-mig003"]["choices"] is None
    assert rows["r-mig004"]["proposed_value_chain"] == '["dataset unreachable"]'
