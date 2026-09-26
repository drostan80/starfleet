"""TransactionBoundaryMiddleware (server.py, 2026-09-26): a request that
wrote without committing held SQLite's write lock indefinitely in prod."""

import sqlite3

import httpx
import pytest
from starlette.responses import PlainTextResponse

from lcars import db
from lcars.server import TransactionBoundaryMiddleware


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "tx.db")
    c.execute("CREATE TABLE t (x INTEGER)")
    c.commit()
    yield c
    db.close()


def _app(raise_after_write: bool):
    async def app(scope, receive, send):
        db.get_connection().execute("INSERT INTO t VALUES (1)")  # no commit
        if raise_after_write:
            raise RuntimeError("boom")
        await PlainTextResponse("ok")(scope, receive, send)
    return TransactionBoundaryMiddleware(app)


async def _get(app):
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
        return await client.get("/")



async def test_a_request_that_wrote_without_committing_is_committed(conn, tmp_path):
    await _get(_app(raise_after_write=False))
    assert not conn.in_transaction
    other = sqlite3.connect(tmp_path / "tx.db", timeout=0.1)
    other.execute("BEGIN IMMEDIATE")  # the write lock is free
    other.rollback()
    assert other.execute("SELECT count(*) FROM t").fetchone()[0] == 1



async def test_a_request_that_raised_is_rolled_back(conn):
    await _get(_app(raise_after_write=True))
    assert not conn.in_transaction
    assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0


def test_the_leak_warning_names_the_graphql_operation():
    from lcars.server import _operation_of

    assert _operation_of(b'{"query":"mutation { pollScoreSync { x } }"}') == "pollScoreSync"
    assert _operation_of(b'{"operationName":"Shows","query":"query Shows { a }"}') == "Shows"
    assert _operation_of(b"not json") == "?"
