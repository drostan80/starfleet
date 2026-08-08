"""The shared SQLite connection — SCOPE.md §11.2 addendum (A.3, resolved
2026-08-08): sync resolvers, one connection opened at startup and reused
for the app's lifetime, no threading/locking.

Deliberately does *not* pass `check_same_thread=False`. That flag exists
to let a connection be used from multiple threads — but this project's
whole execution-model decision is that nothing ever does that (uvicorn's
default single worker, one event loop, one thread). Leaving the default
(`check_same_thread=True`) turns that architectural decision into a real
runtime guard: if some future change accidentally introduces threading
(e.g. `asyncio.to_thread`) around a DB call, sqlite3 raises immediately
instead of silently allowing unsynchronized concurrent access.
"""

import sqlite3
from pathlib import Path

_connection: sqlite3.Connection | None = None


def connect(db_path: Path) -> sqlite3.Connection:
    """Opens the one shared connection. Call once, at app startup."""
    global _connection
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    # SQLite defaults FK checking to OFF per connection — required per the
    # A.1 migration's own note (migrations/versions/7196ca889757_*.py).
    conn.execute("PRAGMA foreign_keys = ON")
    _connection = conn
    return conn


def get_connection() -> sqlite3.Connection:
    """The shared connection. Raises if connect() hasn't run yet — a
    programming error (missing app startup), not a recoverable condition."""
    if _connection is None:
        raise RuntimeError("lcars.db.connect() must be called before get_connection()")
    return _connection


def close() -> None:
    """For tests and clean shutdown — resets the module-level singleton too,
    so a fresh connect() in the next test doesn't reuse a closed connection."""
    global _connection
    if _connection is not None:
        _connection.close()
        _connection = None
