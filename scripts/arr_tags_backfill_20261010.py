#!/usr/bin/env python3
"""One-off tag backfill for Maintainerr (user OK 2026-10-10). Dry run by default.

Brings every tracked show's Sonarr/Radarr tags in line with its LCARS status, once, with the same
rules the live sync uses (`arr_tags.plan`): `ongoing` on planned / watching / paused shows, `purge`
on dropped ones (purge takes `keep` and `ongoing` off), `ongoing`/`purge` off everything else.
Matched by TVDB id (TMDB for movies) — never by the stored Sonarr link, which 35 shows hold wrong.

On top of that, two things the user asked for:
  - Under the Banner of Heaven loses its `keep` tag once it is protected by `ongoing` (planned).
  - A dry run lists every show that would get `purge`, with its size, for the last look.

Nothing is changed unless `--apply` is given AND external writes are in `send` mode; every change
is logged to a JSON file (`--log`) with the tags the item carried before, so it can be undone.

    docker exec -i lcars python - [--apply] [--log /db/arr_tags_backfill_20261010.json] < this
"""

import json
import os
import sqlite3
import sys

from lcars import arr_tags, config, external_writes

DB = os.environ.get("LCARS_FIX_DB", "/db/lcars.db")
APPLY = "--apply" in sys.argv
LOG = (sys.argv[sys.argv.index("--log") + 1] if "--log" in sys.argv
       else "/db/arr_tags_backfill_20261010.json")
UNKEEP_TITLES = ("Under the Banner of Heaven",)  # keep comes off once `ongoing` protects it


def title_of(item):
    return item.get("title") or "?"


def gb(item):
    stats = item.get("statistics") or {}
    size = stats.get("sizeOnDisk")
    if size is None:
        size = sum((z.get("statistics") or {}).get("sizeOnDisk", 0)
                   for z in item.get("seasons", []))
    return round((size or item.get("sizeOnDisk") or 0) / 1e9, 1)


def main():
    config.set_current(config.load_config())
    conn = sqlite3.connect(DB, timeout=60)
    conn.row_factory = sqlite3.Row
    mode = "send" if not external_writes.capturing() else "capture"
    print(("APPLY" if APPLY else "DRY RUN"), "| external writes:", mode, "| db:", DB)
    if APPLY and mode != "send":
        raise SystemExit("REFUSED: external writes are in capture mode — nothing would be sent")
    changes = []  # for the undo log
    totals = {}
    for shape, rows in arr_tags._targets(conn).items():
        with arr_tags._open(shape) as arr:
            if arr is None:
                print(f"{shape}: service not configured — skipped")
                continue
            items = arr.items()
            edits, purge_list, ongoing_n, unkeep = [], [], 0, []
            by_status = {}
            for _show_id, status, key in rows:
                item = items.get(key)
                if item is None:
                    continue
                by_status[status] = by_status.get(status, 0) + 1
                current = arr.labels_of(item)
                add, remove = arr_tags.plan(current, status, shape)
                if title_of(item) in UNKEEP_TITLES and arr_tags.KEEP in current \
                        and status in arr_tags.ONGOING_STATUSES:
                    remove = remove | {arr_tags.KEEP}
                    unkeep.append(title_of(item))
                if add or remove:
                    edits.append((item["id"], add, remove))
                    changes.append({"service": shape, "id": item["id"], "title": title_of(item),
                                    "status": status, "before": sorted(current),
                                    "add": sorted(add), "remove": sorted(remove)})
                    if arr_tags.PURGE in add:
                        purge_list.append((gb(item), title_of(item), sorted(current)))
                    if arr_tags.ONGOING in add:
                        ongoing_n += 1
            print(f"\n== {shape}: {len(items)} in the service, {sum(by_status.values())} tracked"
                  f" shows matched {by_status}")
            print(f"   {len(edits)} item(s) to change: ongoing +{ongoing_n}, purge"
                  f" +{len(purge_list)}, keep removed from {unkeep or 'none'}")
            if purge_list:
                total_gb = sum(p[0] for p in purge_list)
                print(f"   purge ({len(purge_list)} shows, {total_gb:.0f} GB):")
                for size, title, before in sorted(purge_list, reverse=True):
                    print(f"     {size:7.1f} GB  {title}  (had: {', '.join(before) or '-'})")
            totals[shape] = len(edits)
            if APPLY and edits:
                done = arr_tags._apply(arr, edits)
                print("   applied:", done)
    if APPLY:
        with open(LOG, "w") as fh:
            json.dump(changes, fh, indent=1)
        print(f"\nlogged {len(changes)} change(s) to {LOG}")
    else:
        print(f"\n{sum(totals.values())} item(s) would change; run with --apply to do it.")


main()
