#!/usr/bin/env python3
"""Maintainerr: the two-step season clean-up and the tag-driven clearing house (user OK
2026-10-10).

Runs on tiny against Maintainerr's own API (localhost:6246, no auth). Dry run by default: it only
reads, and prints exactly what each stage would create or change.

  stage 1  CREATE  "Housekeeping" (anime library) and "Housekeeping (series)": a season that is
                   fully aired, whose last episode aired more than 10 days ago, with files, whose
                   show carries none of keep / ongoing / purge, and whose season is still
                   MONITORED -> the SEASON (not the show) is unmonitored, files kept. Acts at the
                   next 00:00 / 12:00 run.
  stage 2  UPDATE  the two "spring cleaning" groups become "Checkout" / "Checkout (series)": the
                   same conditions but the season is UNMONITORED -> 15 days later its files are
                   deleted (Sonarr season delete + the torrent). The 115 seasons queued on 10-07
                   keep their date (15 days -> about 10-22); a still-monitored one leaves the
                   queue and goes through Housekeeping first.
  stage 3  UPDATE  the three "clearing house" collections (anime, series, movies) stop being
                   manual: a show / movie tagged `purge` joins and, 15 days later, is unmonitored
                   with all its files deleted (the show stays in Sonarr / Radarr).

Back up first: sqlite3 /opt/appdata/maintainerr/maintainerr.sqlite ".backup <file>" (done
2026-10-10 12:53: maintainerr.sqlite.bak-20261010-pre-housekeeping).

    python3 - [--apply] [--stage 1|2|3] [--execute] < maintainerr_housekeeping_20261010.py

--execute runs each touched rule group once right after it is written, so its membership can be
read before anything is acted on (membership is only a list; actions wait for the 00:00 / 12:00
run).
"""

import json
import sys
import urllib.error
import urllib.request

B = "http://localhost:6246/api"
APPLY = "--apply" in sys.argv
EXECUTE = "--execute" in sys.argv
STAGE = int(sys.argv[sys.argv.index("--stage") + 1]) if "--stage" in sys.argv else None


def call(method, path, body=None):
    req = urllib.request.Request(
        B + path, data=None if body is None else json.dumps(body).encode(), method=method,
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            text = r.read().decode()
            return r.status, (json.loads(text) if text else None)
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:800]


# rule = (operator, action, (application, property), value type, value)
# application 2 = Sonarr, 1 = Radarr, 6 = Jellyfin; action 0 bigger, 2 equals, 4 contains,
# 5 before, 9 not contains; value type 0 number, 2 text, 3 boolean
def rules(*items):
    return [{"operator": o, "action": a, "firstVal": list(f),
             "customVal": {"ruleTypeId": t, "value": v}, "section": 0} for o, a, f, t, v in items]


def season_rules(monitored):
    return rules(
        (None, 2, (2, 12), 3, "0"),              # Sonarr: the season has no unaired episodes
        (0, 5, (6, 29), 0, "864000"),            # Jellyfin: its last episode aired > 10 days ago
        (0, 0, (6, 14), 0, "0"),                 # Jellyfin: it has episodes on the server
        (0, 9, (2, 2), 2, "keep"),               # Sonarr: the show has no keep tag
        (0, 9, (2, 2), 2, "ongoing"),            # ... no ongoing tag
        (0, 9, (2, 2), 2, "purge"),              # ... no purge tag (clearing house handles those)
        (0, 2, (2, 9), 3, "1" if monitored else "0"),  # Sonarr: the SEASON is monitored / not
    )


def describe(rule_list):
    names = {(2, 12): "Sonarr: season has unaired episodes",
             (6, 29): "Jellyfin: season's last ep aired", (6, 14): "Jellyfin: episodes on server",
             (2, 2): "Sonarr: show tags", (2, 9): "Sonarr: season monitored",
             (1, 2): "Radarr: movie tags"}
    acts = {0: ">", 2: "=", 4: "contains", 5: "before", 9: "does not contain"}
    return [
        f"{'AND ' if r['operator'] == 0 else ''}{names[tuple(r['firstVal'])]} "
        f"{acts[r['action']]} {r['customVal']['value']}"
        for r in rule_list
    ]


def show(title, arr_action, days, rule_list):
    print(f"   {title}: action {arr_action}, {days} day(s) in the collection")
    for line in describe(rule_list):
        print("      ", line)


def new_group(src, title, description):
    c = src["collection"]
    return {
        "name": title, "description": description, "libraryId": src["libraryId"], "isActive": True,
        "useRules": True, "dataType": "season", "arrAction": 3,
        "sonarrSettingsId": c["sonarrSettingsId"],
        "listExclusions": False, "cleanupLeftoverFolders": False, "forceSeerr": False,
        "forceOmbi": False, "keepInMaintainerrOnly": False, "manualCollection": False,
        "manualCollectionName": "", "tagInArr": False,
        "collection": {"visibleOnRecommended": c["visibleOnRecommended"],
                       "visibleOnHome": c["visibleOnHome"], "deleteAfterDays": 0,
                       "manualCollection": False, "manualCollectionName": "",
                       "keepLogsForMonths": c["keepLogsForMonths"]},
        "rules": season_rules(True),
    }


def update_body(group, **changes):
    """The group as the API returned it, with the given collection / rule changes."""
    body = {k: group[k] for k in ("id", "libraryId", "name", "description", "isActive", "useRules",
                                  "dataType")}
    c = dict(group["collection"])
    body["collection"] = c
    for key in ("sonarrSettingsId", "radarrSettingsId", "listExclusions", "cleanupLeftoverFolders",
                "forceSeerr", "forceOmbi", "keepInMaintainerrOnly", "manualCollection",
                "manualCollectionName", "tagInArr", "arrAction"):
        if key in c:
            body[key] = c[key]
    body["rules"] = [json.loads(r["ruleJson"]) for r in group["rules"]]
    title = changes.pop("title", None)
    if title:
        body["name"] = c["title"] = title
    if "rules" in changes:
        body["rules"] = changes.pop("rules")
    if "arrAction" in changes:
        body["arrAction"] = c["arrAction"] = changes.pop("arrAction")
    if "days" in changes:
        c["deleteAfterDays"] = changes.pop("days")
    body["useRules"] = True
    body["manualCollection"] = c["manualCollection"] = False
    body["manualCollectionName"] = c["manualCollectionName"] = ""
    return body


def write(method, path, body, label):
    if not APPLY:
        return None
    status, result = call(method, path, body)
    print(f"   -> {label}: HTTP {status}" + ("" if status < 300 else f" {result}"))
    return status < 300


def run_group(gid):
    if APPLY and EXECUTE:
        status, _ = call("POST", f"/rules/{gid}/execute")
        print(f"   executed rule group {gid}: HTTP {status}")


def main():
    print("APPLY" if APPLY else "DRY RUN", "| stage:", STAGE or "all", "| execute:", EXECUTE)
    status, groups = call("GET", "/rules")
    assert status == 200, groups
    by_id = {g["id"]: g for g in groups}
    expect = {1: "clearing house", 2: "spring cleaning", 3: "clearing house (series)",
              4: "spring cleaning (series)", 5: "clearing house (movies)"}
    for gid, name in expect.items():
        ok_names = (name, name.replace("spring cleaning", "Checkout"))
        if gid not in by_id or by_id[gid]["name"] not in ok_names:
            raise SystemExit(
                f"REFUSED: group {gid} is not '{name}' any more — Maintainerr has changed")

    if STAGE in (None, 1):
        print("\nstage 1: create the Housekeeping groups")
        for src, title in ((2, "Housekeeping"), (4, "Housekeeping (series)")):
            if any(g["name"] == title for g in groups):
                print(f"   {title} exists already — skipped")
                continue
            show(title, 3, 0, season_rules(True))
            note = ("Unmonitors a fully aired season (the show stays monitored); "
                    "Checkout deletes it 15 days later")
            write("POST", "/rules", new_group(by_id[src], title, note), title)
    if STAGE in (None, 2):
        print("\nstage 2: spring cleaning -> Checkout")
        for gid, title in ((2, "Checkout"), (4, "Checkout (series)")):
            show(title, 0, 15, season_rules(False))
            body = update_body(by_id[gid], title=title, arrAction=0, days=15,
                               rules=season_rules(False))
            if write("PUT", "/rules", body, title):
                run_group(gid)
    if STAGE in (None, 3):
        print("\nstage 3: clearing house -> tag-driven (`purge`), 15 days")
        for gid, app, label in ((1, 2, "clearing house"), (3, 2, "clearing house (series)"),
                                (5, 1, "clearing house (movies)")):
            purge = rules((None, 4, (app, 2), 2, "purge"))
            show(label, by_id[gid]["collection"]["arrAction"], 15, purge)
            body = update_body(by_id[gid], rules=purge, days=15)
            if write("PUT", "/rules", body, label):
                run_group(gid)
    if not APPLY:
        print("\nnothing written; run with --apply (and --stage N, --execute) to do it")


main()
