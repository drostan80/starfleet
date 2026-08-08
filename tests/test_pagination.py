"""Relay pagination (src/lcars/pagination.py) — BUILD_PLAN.md A.3.

Exercised against a real in-memory SQLite table, forward and backward,
including the hasNextPage/hasPreviousPage bug caught while writing this
(the first version computed them against a WHERE clause that already
excluded the very rows being checked for, making both always False).
"""

import sqlite3

import pytest

from lcars.pagination import decode_cursor, encode_cursor, paginate, paginate_list


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE widget (label TEXT, kind TEXT)")
    for i in range(10):
        c.execute(
            "INSERT INTO widget (label, kind) VALUES (?, ?)",
            (f"widget-{i}", "even" if i % 2 == 0 else "odd"),
        )
    return c


def labels(page: dict) -> list[str]:
    return [e["node"]["label"] for e in page["edges"]]


def test_cursor_roundtrips():
    assert decode_cursor(encode_cursor(42)) == 42


def test_no_args_returns_everything_no_paging(conn):
    page = paginate(conn, "widget", "1 = 1", ())
    assert labels(page) == [f"widget-{i}" for i in range(10)]
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is False


def test_first_page_forward(conn):
    page = paginate(conn, "widget", "1 = 1", (), first=3)
    assert labels(page) == ["widget-0", "widget-1", "widget-2"]
    assert page["page_info"]["has_next_page"] is True
    assert page["page_info"]["has_previous_page"] is False


def test_middle_page_forward_has_both_directions(conn):
    first_page = paginate(conn, "widget", "1 = 1", (), first=3)
    second_page = paginate(
        conn, "widget", "1 = 1", (), first=3, after=first_page["page_info"]["end_cursor"]
    )
    assert labels(second_page) == ["widget-3", "widget-4", "widget-5"]
    assert second_page["page_info"]["has_next_page"] is True
    assert second_page["page_info"]["has_previous_page"] is True


def test_last_page_forward_has_no_next(conn):
    page = paginate(conn, "widget", "1 = 1", (), first=100)
    assert len(page["edges"]) == 10
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is False

    # walk to the real last page via a small page size instead
    cursor = None
    last_page = None
    for _ in range(20):
        last_page = paginate(conn, "widget", "1 = 1", (), first=3, after=cursor)
        cursor = last_page["page_info"]["end_cursor"]
        if not last_page["page_info"]["has_next_page"]:
            break
    assert labels(last_page) == ["widget-9"]
    assert last_page["page_info"]["has_next_page"] is False
    assert last_page["page_info"]["has_previous_page"] is True


def test_last_before_backward_pagination(conn):
    page = paginate(conn, "widget", "1 = 1", (), last=3)
    assert labels(page) == ["widget-7", "widget-8", "widget-9"]
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is True

    prev_page = paginate(
        conn, "widget", "1 = 1", (), last=3, before=page["page_info"]["start_cursor"]
    )
    assert labels(prev_page) == ["widget-4", "widget-5", "widget-6"]
    assert prev_page["page_info"]["has_next_page"] is True
    assert prev_page["page_info"]["has_previous_page"] is True


def test_where_filter_is_respected_by_paging_too(conn):
    page = paginate(conn, "widget", "kind = ?", ("odd",), first=2)
    assert labels(page) == ["widget-1", "widget-3"]
    assert page["page_info"]["has_next_page"] is True

    page2 = paginate(
        conn, "widget", "kind = ?", ("odd",), first=2, after=page["page_info"]["end_cursor"]
    )
    assert labels(page2) == ["widget-5", "widget-7"]


def test_invalid_cursor_raises():
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor("not-a-real-cursor")


# --- paginate_list (§6.4's nextUp, A.11 — a computed, non-table-backed list) --


def _items() -> list[dict]:
    return [{"label": f"item-{i}"} for i in range(10)]


def _labels(page: dict) -> list[str]:
    return [e["node"]["label"] for e in page["edges"]]


def test_paginate_list_no_args_returns_everything():
    page = paginate_list(_items())
    assert _labels(page) == [f"item-{i}" for i in range(10)]
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is False


def test_paginate_list_first_page_forward():
    page = paginate_list(_items(), first=3)
    assert _labels(page) == ["item-0", "item-1", "item-2"]
    assert page["page_info"]["has_next_page"] is True
    assert page["page_info"]["has_previous_page"] is False


def test_paginate_list_middle_page_forward_has_both_directions():
    first_page = paginate_list(_items(), first=3)
    second_page = paginate_list(_items(), first=3, after=first_page["page_info"]["end_cursor"])
    assert _labels(second_page) == ["item-3", "item-4", "item-5"]
    assert second_page["page_info"]["has_next_page"] is True
    assert second_page["page_info"]["has_previous_page"] is True


def test_paginate_list_walks_to_the_real_last_page():
    cursor = None
    last_page = None
    for _ in range(20):
        last_page = paginate_list(_items(), first=3, after=cursor)
        cursor = last_page["page_info"]["end_cursor"]
        if not last_page["page_info"]["has_next_page"]:
            break
    assert _labels(last_page) == ["item-9"]
    assert last_page["page_info"]["has_next_page"] is False
    assert last_page["page_info"]["has_previous_page"] is True


def test_paginate_list_last_before_backward_pagination():
    page = paginate_list(_items(), last=3)
    assert _labels(page) == ["item-7", "item-8", "item-9"]
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is True

    prev_page = paginate_list(_items(), last=3, before=page["page_info"]["start_cursor"])
    assert _labels(prev_page) == ["item-4", "item-5", "item-6"]
    assert prev_page["page_info"]["has_next_page"] is True
    assert prev_page["page_info"]["has_previous_page"] is True


def test_paginate_list_empty_list():
    page = paginate_list([])
    assert page["edges"] == []
    assert page["page_info"]["has_next_page"] is False
    assert page["page_info"]["has_previous_page"] is False
    assert page["page_info"]["start_cursor"] is None
    assert page["page_info"]["end_cursor"] is None
