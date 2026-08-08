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
    [...], "page_info": {...}}`, each edge `{"node": <dict-row>, "cursor":
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
        # snake_case keys, not "pageInfo"/"hasNextPage" — convert_names_case=True
        # (server.py) makes Ariadne's default resolver look up a GraphQL
        # `pageInfo` field as this dict's `page_info` key, same as every plain
        # scalar field elsewhere in this codebase (resolvers.py's own module
        # docstring). A real bug caught while building A.11 (nextUp): this had
        # been camelCase since A.3, silently returning null for `pageInfo` on
        # every connection field ever queried through GraphQL — invisible until
        # now because test_pagination.py only ever calls this function
        # directly as plain Python (bypassing GraphQL entirely), and no
        # end-to-end test had ever queried a `pageInfo` sub-field before.
        "page_info": {
            "has_next_page": has_next_page,
            "has_previous_page": has_previous_page,
            "start_cursor": edges[0]["cursor"] if edges else None,
            "end_cursor": edges[-1]["cursor"] if edges else None,
        },
    }


def paginate_list(
    items: list[dict],
    first: int | None = None,
    after: str | None = None,
    last: int | None = None,
    before: str | None = None,
) -> dict:
    """Same Connection/edges/pageInfo shape as paginate() above, for a
    computed/composite list with no single physical table to page
    against — §6.4's `nextUp` (A.11) is the first: one row per show,
    each paired with its own best-candidate episode, ordered by a
    mix of manual overrides and computed availability, not a plain
    table scan. Cursors encode a position in *this* list rather than
    a `rowid` — reuses encode_cursor/decode_cursor regardless, since
    both are just opaque integers to callers either way.
    """
    start = 0
    end = len(items)
    if after is not None:
        start = decode_cursor(after) + 1
    if before is not None:
        end = decode_cursor(before)
    window = items[start:end]

    backward = last is not None and first is None
    if backward:
        page = window[-last:] if last is not None else window
        page_start = start + len(window) - len(page)
    else:
        page = window[:first] if first is not None else window
        page_start = start

    edges = [
        {"node": item, "cursor": encode_cursor(page_start + i)} for i, item in enumerate(page)
    ]
    return {
        "edges": edges,
        # snake_case — see paginate()'s own comment on this exact key shape.
        "page_info": {
            "has_next_page": (page_start + len(page)) < len(items),
            "has_previous_page": page_start > 0,
            "start_cursor": edges[0]["cursor"] if edges else None,
            "end_cursor": edges[-1]["cursor"] if edges else None,
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
