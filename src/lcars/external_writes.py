"""External writes: sent, or captured for review (PLAN-CODE 9.0).

`external_writes = capture` (the default) records every AniList/MAL list write
and Sonarr/Radarr write in `captured_write` instead of sending it, and blocks
the MAL token refresh (a refresh rotates the refresh token, which would break
the copy of it anywhere else). Only `send` — in `lcars.ini`, or
`LCARS_EXTERNAL_WRITES` — lets them out. The checks sit in the client
functions themselves, so no caller can go around them.

A captured write is *not* agreed: `list_baseline` records nothing for it, so
the next reconcile still sees LCARS and the list apart and captures it again
(merged into the same pending line). `lcars captured send` sends the pending
lines in order, in capped batches, through the same functions.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os

from lcars import config, db, util

_force_send: contextvars.ContextVar[bool] = contextvars.ContextVar("force_send", default=False)


def capturing() -> bool:
    if _force_send.get():
        return False
    mode = os.environ.get("LCARS_EXTERNAL_WRITES") or config.get_current().external_writes
    return (mode or "capture").strip().lower() != "send"


@contextlib.contextmanager
def sending():
    """For `lcars captured send`: these writes were approved — send them."""
    token = _force_send.set(True)
    try:
        yield
    finally:
        _force_send.reset(token)


def capture(service: str, op: str, dedupe_key, args: dict) -> None:
    """Records one write (never a token). Joins the caller's transaction:
    rolled back with it, committed with it."""
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO captured_write (service, op, dedupe_key, args, captured_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT (service, op, dedupe_key)"
        "   WHERE sent_at IS NULL AND dedupe_key IS NOT NULL"
        " DO UPDATE SET args = json_patch(captured_write.args, excluded.args),"
        "   captured_at = excluded.captured_at",
        (service, op, None if dedupe_key is None else str(dedupe_key),
         json.dumps(args, default=str), util.now_utc_iso()),
    )


def pending(conn) -> list:
    return conn.execute(
        "SELECT * FROM captured_write WHERE sent_at IS NULL ORDER BY id"
    ).fetchall()


def arr_write(service: str, method: str, path: str, body=None, params=None):
    """Sonarr/Radarr `_post`/`_put`/`_delete` while capturing: recorded,
    with a stand-in answer. An added series/movie comes back with no id and
    no titleSlug, so nothing links to an entry that doesn't exist yet
    (`lcars captured send` writes the link once it does)."""
    if method == "POST":
        key = (body or {}).get("tvdbId") or (body or {}).get("tmdbId")
    elif path == "episode/monitor":
        key = None  # each call is its own change
    else:
        key = path
    capture(service, f"{method} {path}", key,
            {"path": path, "body": body, "params": params})
    if method == "POST":
        return {**(body or {}), "id": None, "titleSlug": None}
    if method == "PUT":
        return body
    return None


# ── sending the approved writes (`lcars captured send`) ──────────────────


def send_one(conn, row) -> None:
    """Sends one captured write through the same functions, for real; what
    those functions record on success (list_baseline, the Sonarr link) is
    recorded now."""
    from lcars import (
        anilist_client,
        list_baseline,
        mal_client,
        radarr_client,
        shows,
        sonarr_client,
    )

    cfg = config.get_current()
    args = json.loads(row["args"])
    service, op = row["service"], row["op"]
    with sending():
        if service == "anilist" and op == "save":
            fields = {k: v for k, v in args.items() if k != "anilist_id"}
            list_baseline.anilist_save(conn, cfg.anilist_access_token, args["anilist_id"],
                                       **fields)
        elif service == "anilist" and op == "delete":
            anilist_client.delete_media_list_entry(cfg.anilist_access_token, args["entry_id"])
            if args.get("anilist_id") is not None:
                conn.execute("DELETE FROM list_baseline WHERE service = 'anilist'"
                             " AND external_id = ?", (args["anilist_id"],))
        elif service == "mal" and op == "save":
            fields = {k: v for k, v in args.items() if k != "mal_id"}
            list_baseline.mal_save(conn, cfg.mal_access_token, args["mal_id"], **fields)
        elif service == "mal" and op == "delete":
            mal_client.delete_my_list_status(cfg.mal_access_token, args["mal_id"])
            conn.execute("DELETE FROM list_baseline WHERE service = 'mal'"
                         " AND external_id = ?", (args["mal_id"],))
        elif service in ("sonarr", "radarr"):
            method = op.split(" ", 1)[0]
            if service == "sonarr":
                client = sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key)
            else:
                client = radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key)
            with client:
                if method == "POST":
                    created = client._post(args["path"], json=args["body"]) or {}
                elif method == "PUT":
                    client._put(args["path"], json=args["body"])
                else:
                    client._delete(args["path"], params=args["params"])
            if method == "POST" and created.get("titleSlug"):
                # The link a captured add couldn't write (no entry existed yet).
                svc_id, shape, value = (("tvdb", "episodic", created.get("tvdbId"))
                                        if service == "sonarr"
                                        else ("tmdb", "movie", created.get("tmdbId")))
                show = conn.execute(
                    "SELECT show_id FROM show_external_id WHERE service = ? AND external_id = ?",
                    (svc_id, str(value)),
                ).fetchone()
                if show is not None:
                    shows.write_arr_external_id(conn, show[0], shape, created["titleSlug"])
        else:
            raise ValueError(f"unknown captured write {service} {op}")


def send_pending(conn, limit: int) -> dict:
    """Sends up to `limit` pending writes in capture order; each is marked
    sent (or its error kept) and committed on its own."""
    sent = failed = 0
    for row in pending(conn)[:limit]:
        try:
            send_one(conn, row)
            conn.execute("UPDATE captured_write SET sent_at = ?, send_error = NULL WHERE id = ?",
                         (util.now_utc_iso(), row["id"]))
            sent += 1
        except Exception as e:
            conn.rollback()
            conn.execute("UPDATE captured_write SET send_error = ? WHERE id = ?",
                         (str(e), row["id"]))
            failed += 1
        conn.commit()
    return {"sent": sent, "failed": failed, "left": len(pending(conn))}


def main(argv=None) -> int:
    import argparse

    parser = argparse.ArgumentParser(
        prog="lcars captured", description="external writes captured instead of sent (9.0)")
    parser.add_argument("database")
    sub = parser.add_subparsers(dest="action", required=True)
    list_cmd = sub.add_parser("list", help="the pending writes")
    list_cmd.add_argument("--json", action="store_true")
    send_cmd = sub.add_parser("send", help="send pending writes for real, in order")
    send_cmd.add_argument("--limit", type=int, required=True, help="at most this many")
    args = parser.parse_args(argv)
    conn = db.connect(args.database)
    if args.action == "list":
        rows = [dict(r) for r in pending(conn)]
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            for r in rows:
                print(f"{r['id']:>6}  {r['service']:<7} {r['op']:<22} {r['args']}")
            print(f"{len(rows)} pending")
        return 0
    config.set_current(config.load_config())
    print(json.dumps(send_pending(conn, args.limit)))
    return 0
