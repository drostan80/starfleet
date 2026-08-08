"""Short, type-prefixed ids — SCOPE.md §5.0, BUILD_PLAN.md A.3."""

import sqlite3

import pytest

from lcars.ids import ALPHABET, generate_id


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:")
    c.execute("CREATE TABLE show (id TEXT PRIMARY KEY)")
    return c


def test_generated_id_matches_the_shape(conn):
    generated = generate_id(conn, "s")
    assert generated.startswith("s-")
    assert len(generated) == 8
    assert all(ch in ALPHABET for ch in generated[2:])


def test_generated_ids_are_unique_across_many_calls(conn):
    seen = set()
    for _ in range(200):
        new_id = generate_id(conn, "s")
        assert new_id not in seen
        seen.add(new_id)
        conn.execute("INSERT INTO show (id) VALUES (?)", (new_id,))


def test_regenerates_on_collision(conn, monkeypatch):
    """Forces the very first candidate to collide, confirms the retry
    loop actually produces a second, different, unused id rather than
    raising or returning the colliding one."""
    conn.execute("INSERT INTO show (id) VALUES ('s-aaaaaa')")

    calls = {"n": 0}
    real_generate = __import__("nanoid").generate

    def fake_generate(alphabet, size):
        calls["n"] += 1
        return "aaaaaa" if calls["n"] == 1 else real_generate(alphabet=alphabet, size=size)

    monkeypatch.setattr("lcars.ids.nanoid.generate", fake_generate)
    result = generate_id(conn, "s")
    assert result != "s-aaaaaa"
    assert calls["n"] == 2


def test_unknown_prefix_raises(conn):
    with pytest.raises(ValueError, match="unknown id prefix"):
        generate_id(conn, "z")
