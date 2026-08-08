"""Generic Relay-style cursor pagination — one implementation shared by
every connection field, per the decision recorded in BUILD_PLAN.md A.2
(every list field uses Connection/edges/pageInfo, no exceptions).

Cursors encode a row's SQLite `rowid` (every table here is an ordinary
rowid table, monotonically increasing on insert — so paging by rowid
also means insertion-order paging, a reasonable default for lists that
don't have a more meaningful natural order). Opaque to callers, per the
Relay spec — base64-encoded, not a raw integer, so nothing outside this
module depends on the encoding.

Supports the two primary Relay modes: forward (`first`/`after`) and
backward (`last`/`before`). Mixing both isn't specially handled beyond
`first` taking priority if both are somehow given — not a real use case
for any of this project's clients.
"""

import base64
import sqlite3

_CURSOR_PREFIX = "lcars-rowid:"


def encode_cursor(rowid: int) -> str:
    return base64.b64encode(f"{_CURSOR_PREFIX}{rowid}".encode()).decode()


def decode_cursor(cursor: str) -> int:
    try:
        decoded = base64.b64decode(cursor.encode()).decode()
        if not decoded.startswith(_CURSOR_PREFIX):
            raise ValueError
        return int(decoded[len(_CURSOR_PREFIX) :])
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid cursor: {cursor!r}") from exc


def paginate(
    conn: sqlite3.Connection,
    table: str,
    where: str,
    params: tuple,
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
) -> dict:
    """Runs a paginated `SELECT rowid, * FROM {table} WHERE {where}` (plus
    caller's `params`) and returns a Connection-shaped dict: `{"edges":
    [...], "pageInfo": {...}}`, each edge `{"node": <dict-row>, "cursor":
    str}`.

    `table`/`where` are always caller-supplied constants from this
    codebase (never raw user input) — same non-issue as ids.py's
    equivalent f-string use.
    """
    base_where = where if where else "1 = 1"
    base_args = list(params)

    clauses = [base_where]
    args = list(base_args)
    if after is not None:
        clauses.append("rowid > ?")
        args.append(decode_cursor(after))
    if before is not None:
        clauses.append("rowid < ?")
        args.append(decode_cursor(before))
    where_sql = " AND ".join(clauses)

    backward = last is not None and first is None
    limit = last if backward else first
    order = "DESC" if backward else "ASC"

    sql = f"SELECT rowid, * FROM {table} WHERE {where_sql} ORDER BY rowid {order}"  # noqa: S608
    if limit is not None:
        sql += " LIMIT ?"
        args.append(limit)

    rows = [dict(r) for r in conn.execute(sql, args).fetchall()]
    if backward:
        rows.reverse()

    # hasNextPage/hasPreviousPage answer "is there anything beyond what
    # THIS PAGE actually returned" — computed against the *base* filter
    # only (not the cursor clauses above, which would make either check
    # circularly always-false), anchored on the page's own first/last row.
    if rows:
        has_next_page = _exists(
            conn, table, base_where, base_args, "rowid > ?", rows[-1]["rowid"]
        )
        has_previous_page = _exists(
            conn, table, base_where, base_args, "rowid < ?", rows[0]["rowid"]
        )
    else:
        has_next_page = False
        has_previous_page = False

    edges = [{"node": row, "cursor": encode_cursor(row["rowid"])} for row in rows]
    return {
        "edges": edges,
        "pageInfo": {
            "hasNextPage": has_next_page,
            "hasPreviousPage": has_previous_page,
            "startCursor": edges[0]["cursor"] if edges else None,
            "endCursor": edges[-1]["cursor"] if edges else None,
        },
    }


def _exists(
    conn: sqlite3.Connection,
    table: str,
    base_where: str,
    base_args: list,
    extra_clause: str,
    boundary_rowid: int,
) -> bool:
    sql = f"SELECT 1 FROM {table} WHERE ({base_where}) AND {extra_clause} LIMIT 1"  # noqa: S608
    row = conn.execute(sql, [*base_args, boundary_rowid]).fetchone()
    return row is not None
