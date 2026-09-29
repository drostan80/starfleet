"""Stage 10 (rebuild_writes): the write list is the difference between what LCARS ends with
and what the lists hold, only what differs, never a skipped level, with what the lists do by
themselves flagged."""

import pytest

from lcars import rebuild, rebuild_writes
from lcars import rebuild_writes as rw


def level(lid, status, progress, **ids):
    return {"id": lid, "show": "s-aaaaaa", "title": lid, "status": status, "progress": progress,
            "ids": ids}


def entry(status, progress, total=None):
    return {"status": status, "progress": progress, "total": total, "updated_at": "t",
            "title": "x"}


def test_only_the_fields_that_differ_are_written():
    lists = {"anilist": {1: entry("watching", 3), 2: entry("completed", 12, 12)}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "watching", 5, anilist=1),
                                 level("b", "completed", 12, anilist=2)], lists)
    assert plan["writes"] == [{"level": "a", "show": "s-aaaaaa", "title": "a",
                               "service": "anilist", "id": 1, "kind": "change",
                               "fields": {"progress": 5},
                               "before": {"status": "watching", "progress": 3}}]


def test_progress_goes_down_too():
    lists = {"anilist": {1: entry("watching", 9)}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "watching", 4, anilist=1)], lists)
    assert plan["writes"][0]["fields"] == {"progress": 4}


def test_an_entry_the_list_lacks_is_an_add_a_skipped_level_is_never_written():
    lists = {"anilist": {}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "planned", 0, anilist=7, mal=8),
                                 level("b", "skipped", 0, anilist=9)], lists)
    assert [(w["service"], w["id"], w["kind"]) for w in plan["writes"]] == [
        ("anilist", 7, "add"), ("mal", 8, "add")]


def test_a_completed_write_that_the_list_would_end_elsewhere_is_reviewed_without_progress():
    # Mushoku 146065: the level holds 12, AniList counts 13 (a special is its episode 0)
    lists = {"anilist": {5: entry("watching", 12, 13)}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "completed", 12, anilist=5)], lists)
    assert plan["writes"][0]["fields"] == {"status": "completed"}
    assert len(plan["reviews"]) == 1 and "13" in plan["reviews"][0]["why"]


def test_watching_at_the_total_is_reviewed():
    lists = {"anilist": {5: entry("watching", 3, 12)}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "watching", 12, anilist=5)], lists)
    assert len(plan["reviews"]) == 1 and plan["writes"] == []  # nothing else to write


def test_entries_no_tracked_level_holds_are_listed_only():
    lists = {"anilist": {1: entry("watching", 1), 2: entry("dropped", 0)}, "mal": {}}
    plan = rw.plan_level_writes([level("a", "watching", 1, anilist=1)], lists)
    assert plan["untracked"] == {"anilist": [2], "mal": []}


def test_the_stage_refuses_to_run_unless_writes_are_captured(tmp_path):
    (tmp_path / "inputs").mkdir()
    run = rebuild.Run(tmp_path / "run", tmp_path / "snap.db", tmp_path / "live.db",
                      tmp_path / "inputs", stage="writes")
    run.dir.mkdir()
    with pytest.raises(rebuild.RebuildError, match="never sent"):
        rebuild_writes.stage_writes(run)  # the suite runs with writes on `send`
