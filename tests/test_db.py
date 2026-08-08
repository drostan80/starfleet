"""The shared connection singleton — SCOPE.md §11.2 addendum, BUILD_PLAN.md A.3."""

import sqlite3

import pytest

from lcars import db


@pytest.fixture(autouse=True)
def _reset_db_module():
    db.close()
    yield
    db.close()


def test_get_connection_before_connect_raises():
    with pytest.raises(RuntimeError, match="connect\\(\\) must be called"):
        db.get_connection()


def test_connect_returns_a_usable_connection(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    conn.execute("CREATE TABLE t (x INTEGER)")
    conn.execute("INSERT INTO t VALUES (1)")
    assert conn.execute("SELECT x FROM t").fetchone()[0] == 1


def test_get_connection_returns_the_same_connection_connect_opened(tmp_path):
    opened = db.connect(tmp_path / "test.db")
    assert db.get_connection() is opened


def test_foreign_keys_pragma_is_on(tmp_path):
    conn = db.connect(tmp_path / "test.db")
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_check_same_thread_stays_default_true(tmp_path):
    """Deliberately not check_same_thread=False (SCOPE.md §11.2 addendum) —
    confirms a connection opened here still rejects cross-thread use,
    which is the actual safety property the decision relies on."""
    import threading

    conn = db.connect(tmp_path / "test.db")
    errors = []

    def use_from_other_thread():
        try:
            conn.execute("SELECT 1")
        except sqlite3.ProgrammingError as exc:
            errors.append(exc)

    thread = threading.Thread(target=use_from_other_thread)
    thread.start()
    thread.join()
    assert len(errors) == 1
    assert "thread" in str(errors[0]).lower()


def test_close_resets_singleton(tmp_path):
    db.connect(tmp_path / "test.db")
    db.close()
    with pytest.raises(RuntimeError):
        db.get_connection()
