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

import itertools
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

_connection: sqlite3.Connection | None = None
_savepoint_ids = itertools.count(1)


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


@contextmanager
def undo_on_error(conn: sqlite3.Connection) -> Iterator[None]:
    """A best-effort step whose failure is caught by the caller: if the
    body raises, its own uncommitted writes are undone, so a half-done
    step never rides along with whatever commits next (2026-09-26 — the
    `pollMemoryAlpha` transaction leak, and metadata._guarded keeping a
    failed step's partial writes). Writes made before the block are kept.

    Many steps commit part-way (service_health right after the HTTP
    call); that commit also ends the savepoint. Everything still open at
    the failure then belongs to this step alone, so a plain rollback
    undoes exactly the step's own writes after its last commit."""
    began = not conn.in_transaction
    if began:
        # Without an outer transaction, RELEASE would commit — keep the
        # old "pending until the caller commits" behaviour instead.
        conn.execute("BEGIN")
    changes = conn.total_changes
    name = f"undo_{next(_savepoint_ids)}"
    conn.execute(f"SAVEPOINT {name}")
    try:
        yield
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO {name}")
            conn.execute(f"RELEASE {name}")
        except sqlite3.OperationalError:
            conn.rollback()
        else:
            if began and conn.in_transaction:
                conn.rollback()
        raise
    try:
        conn.execute(f"RELEASE {name}")
    except sqlite3.OperationalError:
        return  # the body committed; nothing of ours is left open
    if began and conn.in_transaction and conn.total_changes == changes:
        conn.rollback()  # nothing written: don't leave an empty transaction open
