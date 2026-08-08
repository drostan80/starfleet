"""Relay pagination (src/lcars/pagination.py) — BUILD_PLAN.md A.3.

Exercised against a real in-memory SQLite table, forward and backward,
including the hasNextPage/hasPreviousPage bug caught while writing this
(the first version computed them against a WHERE clause that already
excluded the very rows being checked for, making both always False).
"""

import sqlite3

import pytest

from lcars.pagination import decode_cursor, encode_cursor, paginate


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
    assert page["pageInfo"]["hasNextPage"] is False
    assert page["pageInfo"]["hasPreviousPage"] is False


def test_first_page_forward(conn):
    page = paginate(conn, "widget", "1 = 1", (), first=3)
    assert labels(page) == ["widget-0", "widget-1", "widget-2"]
    assert page["pageInfo"]["hasNextPage"] is True
    assert page["pageInfo"]["hasPreviousPage"] is False


def test_middle_page_forward_has_both_directions(conn):
    first_page = paginate(conn, "widget", "1 = 1", (), first=3)
    second_page = paginate(
        conn, "widget", "1 = 1", (), first=3, after=first_page["pageInfo"]["endCursor"]
    )
    assert labels(second_page) == ["widget-3", "widget-4", "widget-5"]
    assert second_page["pageInfo"]["hasNextPage"] is True
    assert second_page["pageInfo"]["hasPreviousPage"] is True


def test_last_page_forward_has_no_next(conn):
    page = paginate(conn, "widget", "1 = 1", (), first=100)
    assert len(page["edges"]) == 10
    assert page["pageInfo"]["hasNextPage"] is False
    assert page["pageInfo"]["hasPreviousPage"] is False

    # walk to the real last page via a small page size instead
    cursor = None
    last_page = None
    for _ in range(20):
        last_page = paginate(conn, "widget", "1 = 1", (), first=3, after=cursor)
        cursor = last_page["pageInfo"]["endCursor"]
        if not last_page["pageInfo"]["hasNextPage"]:
            break
    assert labels(last_page) == ["widget-9"]
    assert last_page["pageInfo"]["hasNextPage"] is False
    assert last_page["pageInfo"]["hasPreviousPage"] is True


def test_last_before_backward_pagination(conn):
    page = paginate(conn, "widget", "1 = 1", (), last=3)
    assert labels(page) == ["widget-7", "widget-8", "widget-9"]
    assert page["pageInfo"]["hasNextPage"] is False
    assert page["pageInfo"]["hasPreviousPage"] is True

    prev_page = paginate(
        conn, "widget", "1 = 1", (), last=3, before=page["pageInfo"]["startCursor"]
    )
    assert labels(prev_page) == ["widget-4", "widget-5", "widget-6"]
    assert prev_page["pageInfo"]["hasNextPage"] is True
    assert prev_page["pageInfo"]["hasPreviousPage"] is True


def test_where_filter_is_respected_by_paging_too(conn):
    page = paginate(conn, "widget", "kind = ?", ("odd",), first=2)
    assert labels(page) == ["widget-1", "widget-3"]
    assert page["pageInfo"]["hasNextPage"] is True

    page2 = paginate(
        conn, "widget", "kind = ?", ("odd",), first=2, after=page["pageInfo"]["endCursor"]
    )
    assert labels(page2) == ["widget-5", "widget-7"]


def test_invalid_cursor_raises():
    with pytest.raises(ValueError, match="invalid cursor"):
        decode_cursor("not-a-real-cursor")
