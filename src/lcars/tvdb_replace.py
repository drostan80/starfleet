"""PLAN-CODE phase 10 — a tracked show whose TVDB entry TheTVDB deleted and re-created under a new
id (user, 2026-10-13: four shows at once; Sonarr kept the old series with status `deleted` and logs
"was not found, it may have been removed from TheTVDB", so nothing about the show refreshes again).

`scan` (hourly, with the TVDB backfill) finds Sonarr series with status `deleted` that a tracked
show links to, looks for the same show under a new id, and opens a review `tvdb_replace` that
carries the evidence side by side (old vs proposed, what the ids say, what the swap would touch in
Sonarr). With `tvdb_replace = auto` in lcars.ini a replacement that several things agree on — the
lookup's own IMDb / TMDB id equals the show's, and the title and year fit — is applied at once and
kept as a resolved review; the default is `review`: nothing is changed until you choose.

Every tracked show is covered, finished or dropped too (user, 2026-10-13: a finished show may get a
new season, a dropped one may be un-dropped, and a stale TVDB id is never acceptable). The LCARS
write goes through `tvdb_guard.replace` (R1.14, R3.2a, recorded source).

`apply` is the whole swap, or nothing: the Sonarr series keeps its id, path, files, tags and
monitoring (its `tvdbId` is edited in place and refreshed — checked live 2026-10-13); then LCARS's
`show_external_id` follows, in the same step. Any check that fails puts the Sonarr series back as it
was. Episode tvdb ids are `season:episode` and the Sonarr link is a slug, so nothing else in LCARS
moves.
"""

from __future__ import annotations

import json
import logging
import re

from lcars import (
    add_check,
    config,
    external_writes,
    pending_review,
    reviews,
    sonarr_client,
    tvdb_guard,
    tvdb_vetting,
    util,
)

logger = logging.getLogger(__name__)

FIELD = "tvdb_replace"
REPLACE, DESPITE, OTHER, NOID = (
    "replace_tvdb", "replace_tvdb_despite", "replace_other_tvdb", "log_no_tvdb")
SOURCE_PREFIX = "recreated:"  # show_external_id.source = recreated:<the id it replaced>
URL = "https://thetvdb.com/dereferrer/series/{}"
MAX_TITLE_TERMS = 3


# ── finding the replacement ──────────────────────────────────────────────


def _ext(conn, show_id: str, service: str) -> str | None:
    row = conn.execute("SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
                       (show_id, service)).fetchone()
    return row[0] if row else None


def _show_titles(show) -> list[str]:
    return add_check._show_titles(show)


def _lookup(client, term: str) -> list[dict]:
    try:
        return client.lookup_series(term)[:8]
    except sonarr_client.SonarrError:
        return []


def _judge(show, imdb: str | None, tmdb: str | None, found: dict, series_year) -> dict | None:
    """What the ids and the names say about one lookup result; None when it can't be this show
    (an id that is present on both sides and differs)."""
    f_imdb, f_tmdb = found.get("imdbId") or None, found.get("tmdbId") or None
    if f_imdb and imdb and f_imdb != imdb:
        return None
    if f_tmdb and tmdb and str(f_tmdb) != str(tmdb):
        return None
    checks = []
    ids = 0
    if f_imdb and imdb:
        checks.append(f"IMDb {imdb} matches")
        ids += 1
    if f_tmdb and tmdb:
        checks.append(f"TMDB {tmdb} matches")
        ids += 1
    titles = [t for t in [found.get("title"), found.get("originalTitle"),
                          *[a.get("title") for a in found.get("alternateTitles") or []]] if t]
    title_ok = add_check.titles_match(_show_titles(show), titles)
    year = found.get("year") or None
    year_ok = bool(year and series_year and abs(int(year) - int(series_year)) <= 1)
    if title_ok:
        checks.append("title fits")
    if year_ok:
        checks.append(f"first aired {year}, as before")
    if not ids and not (title_ok and year_ok):
        return None
    return {"ids": ids, "title_ok": title_ok, "year_ok": year_ok, "checks": checks}


def find_replacement(conn, client, show, series: dict) -> dict:
    """{"best": (lookup result, judgement) | None, "others": [...]} — the lookup is by the show's
    IMDb / TMDB ids first (exact), then its titles; every result is judged by `_judge`."""
    old = int(series["tvdbId"])
    imdb, tmdb = _ext(conn, show["id"], "imdb"), _ext(conn, show["id"], "tmdb")
    terms = [t for t in (f"imdb:{imdb}" if imdb else None, f"tmdb:{tmdb}" if tmdb else None) if t]
    terms += _show_titles(show)[:MAX_TITLE_TERMS]
    if series.get("title"):
        terms.append(series["title"])
    seen: dict[int, dict] = {}
    for term in dict.fromkeys(terms):
        for found in _lookup(client, term):
            tid = found.get("tvdbId")
            if tid and int(tid) != old and int(tid) not in seen:
                seen[int(tid)] = found
    judged = []
    for found in seen.values():
        j = _judge(show, imdb, tmdb, found, series.get("year"))
        if j:
            judged.append((found, j))
    judged.sort(key=lambda fj: (fj[1]["ids"], fj[1]["title_ok"], fj[1]["year_ok"]), reverse=True)
    return {"best": judged[0] if judged else None, "others": judged[1:3]}


# ── the evidence ─────────────────────────────────────────────────────────


def _sonarr_block(client, series: dict) -> dict:
    labels = {}
    try:
        labels = {t["id"]: t["label"] for t in client.tags()}
    except sonarr_client.SonarrError:
        pass
    stats = series.get("statistics") or {}
    return {
        "series_id": series["id"], "title": series.get("title"), "path": series.get("path"),
        "status": series.get("status"), "monitored": series.get("monitored"),
        "files": stats.get("episodeFileCount"), "episodes": stats.get("episodeCount"),
        "tags": [labels.get(t, str(t)) for t in series.get("tags") or []],
        "seasons": [{"n": s["seasonNumber"], "monitored": s.get("monitored"),
                     "files": (s.get("statistics") or {}).get("episodeFileCount"),
                     "total": (s.get("statistics") or {}).get("totalEpisodeCount")}
                    for s in series.get("seasons") or []],
    }


def _blocked_by(conn, new_id: int, show_id: str | None = None) -> str | None:
    other = conn.execute("SELECT show_id FROM show_external_id WHERE service = 'tvdb' AND"
                         " external_id = ? AND show_id IS NOT ?", (str(new_id), show_id)).fetchone()
    return f"TVDB {new_id} already belongs to show {other[0]} (R1.14)" if other else None


def _lcars_block(conn, show) -> dict:
    """The LCARS record as it is now: what the review shows under "LCARS has"."""
    ids = {r["service"]: r["external_id"] for r in conn.execute(
        "SELECT service, external_id FROM show_external_id WHERE show_id = ?", (show["id"],))}
    tvdb_row = conn.execute("SELECT source FROM show_external_id WHERE show_id = ? AND service ="
                            " 'tvdb'", (show["id"],)).fetchone()
    seasons = [{"season": r["season_number"], "part": r["part_number"], "status": r["status"]}
               for r in conn.execute(
                   "SELECT season_number, part_number, status FROM season WHERE show_id = ?"
                   " ORDER BY season_number, part_number", (show["id"],))]
    return {
        "title": show["title_english"] or show["title_romaji"] or show["title_native"],
        "status": show["status"], "tvdb_id": ids.get("tvdb"),
        "tvdb_source": tvdb_row["source"] if tvdb_row else None,
        "anilist": ids.get("anilist"), "mal": ids.get("mal"), "imdb": ids.get("imdb"),
        "tmdb": ids.get("tmdb"), "sonarr_slug": ids.get("sonarr"), "seasons": seasons,
    }


def _evidence(conn, client, show, series: dict, replacement: dict) -> tuple[dict, list[str]]:
    """The payload of the review, and the hard stops against the proposal."""
    old = int(series["tvdbId"])
    entry = tvdb_vetting.show_entry_facts(conn, show["id"])
    payload = {
        "show_id": show["id"], "tvdb_id": old, "entry": entry, "proposed": None,
        "lcars": _lcars_block(conn, show),
        "mismatches": [], "checks": [], "others": [],
        "sonarr": _sonarr_block(client, series),
        "columns": [{"label": f"TVDB {old} (deleted — as Sonarr last knew it)", "tvdb_id": old,
                     "facts": {"title": series.get("title"), "year": series.get("year"),
                               "genres": series.get("genres") or [],
                               "network": series.get("network"),
                               "language": (series.get("originalLanguage") or {}).get("name"),
                               "seasons": sorted(s["seasonNumber"] for s in
                                                 series.get("seasons") or []
                                                 if s.get("seasonNumber")),
                               "poster": None}}],
    }
    stops: list[str] = []
    best = replacement["best"]
    if best:
        found, judged = best
        new = int(found["tvdbId"])
        facts = tvdb_vetting.facts_from_lookup(found, new)
        stops = tvdb_vetting.hard_stops(entry, facts, anime=show["tracking_space"] == "anime",
                                        new_show=False)
        blocked = _blocked_by(conn, new, show["id"])
        if found.get("id"):
            blocked = (blocked or "") + f" Sonarr already has it as series {found['id']}."
        payload["proposed"] = {"tvdb_id": new, "title": found.get("title"),
                               "year": found.get("year"), "imdb": found.get("imdbId"),
                               "tmdb": found.get("tmdbId"), "in_library": found.get("id"),
                               "blocked": blocked.strip() if blocked else None}
        payload["checks"] = judged["checks"]
        payload["mismatches"] = stops
        payload["columns"].append({"label": f"TVDB {new} (proposed)", "tvdb_id": new,
                                   "facts": facts})
    payload["others"] = [{"tvdb_id": int(f["tvdbId"]), "title": f.get("title"),
                          "year": f.get("year"), "checks": j["checks"]}
                         for f, j in replacement["others"]]
    return payload, stops


def _text(old: int, best: tuple | None) -> str:
    if not best:
        return f"Sonarr flagged TVDB {old} as deleted; no replacement TVDB id is available"
    found = best[0]
    return (f"Sonarr flagged TVDB {old} as deleted; proposed: TVDB {found['tvdbId']} "
            f"({found.get('title')}, {found.get('year')})")


def _choices(payload: dict) -> list[str]:
    """With a proposal: yes / no, use mine / neither for now. Without: log without an id for now /
    use the one I give."""
    p = payload["proposed"]
    if p and not p["blocked"]:
        return [DESPITE if payload["mismatches"] else REPLACE, OTHER, NOID]
    return [NOID, OTHER]


# ── scan ─────────────────────────────────────────────────────────────────


def _mode() -> str:
    return (getattr(config.get_current(), "tvdb_replace", None) or "review").strip().lower()


def scan(conn, client=None) -> int:
    """Opens (or, in `auto` mode, applies) one review per tracked show whose Sonarr series is
    `deleted`. Idempotent: an open review, or one answered with the same finding, is left alone.
    Returns how many reviews were opened. Never raises: Sonarr trouble just means no findings."""
    mode = _mode()
    if mode == "off":
        return 0
    own = None
    try:
        if client is None:
            cfg = config.get_current()
            if not (cfg.sonarr_url and cfg.sonarr_api_key):
                return 0
            own = client = sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key)
        deleted = [s for s in client.all_series() if s.get("status") == "deleted"]
        opened = 0
        for series in deleted:
            row = _show_for(conn, series)
            if row is None:
                continue
            opened += _consider(conn, client, row, series, mode)
        conn.commit()
        return opened
    except sonarr_client.SonarrError as e:
        logger.warning("tvdb replace scan: %s", e)
        return 0
    except Exception:
        logger.exception("tvdb replace scan failed")
        conn.rollback()
        return 0
    finally:
        if own is not None:
            own.close()


def _show_for(conn, series: dict):
    """The tracked show a deleted Sonarr series belongs to: the one that holds its TVDB id or —
    when that was removed (logged without an id) or already moved on — the one linked to its
    Sonarr slug (the id match is how that link was made)."""
    row = conn.execute(
        "SELECT s.* FROM show s JOIN show_external_id x ON x.show_id = s.id AND x.service ="
        " 'tvdb' WHERE x.external_id = ? AND s.tracked = 1", (str(series["tvdbId"]),)).fetchone()
    if row is None and series.get("titleSlug"):
        row = conn.execute(
            "SELECT s.* FROM show s JOIN show_external_id x ON x.show_id = s.id AND x.service ="
            " 'sonarr' WHERE x.external_id = ? AND s.tracked = 1 AND s.media_shape = 'episodic'",
            (series["titleSlug"],)).fetchone()
    return row


def _consider(conn, client, show, series: dict, mode: str) -> int:
    if conn.execute("SELECT 1 FROM pending_review WHERE entity_type = 'show' AND entity_id = ? AND"
                    " field = ? AND resolved_at IS NULL", (show["id"], FIELD)).fetchone():
        return 0
    replacement = find_replacement(conn, client, show, series)
    old = int(series["tvdbId"])
    text = _text(old, replacement["best"])
    if pending_review.already_resolved_with(conn, "show", show["id"], FIELD, text):
        return 0  # answered already: no AniList / further Sonarr calls on every tick
    payload, stops = _evidence(conn, client, show, series, replacement)
    reviews.open_review(conn, "show", show["id"], FIELD, "sonarr", text, _choices(payload),
                        payload, show_id=show["id"])
    conn.commit()  # the review stays whatever the automatic step below does
    logger.info("tvdb replace: %s", text)
    p = payload["proposed"]
    if mode == "auto" and p and not p["blocked"] and not stops and _agreed(payload):
        review_id = conn.execute(
            "SELECT id FROM pending_review WHERE entity_type = 'show' AND entity_id = ? AND field"
            " = ? AND resolved_at IS NULL", (show["id"], FIELD)).fetchone()[0]
        try:
            reviews.resolve_choice(conn, review_id, REPLACE, None, None)  # no client: LCARS itself
        except reviews.ReviewError as e:  # stays open for you, with the reason
            conn.rollback()
            logger.warning("tvdb replace: not applied automatically (%s)", e)
    return 1


def _agreed(payload: dict) -> bool:
    """Auto-apply needs the lookup's own IMDb/TMDB id to equal the show's AND the title and year to
    fit, and nobody else to be a candidate."""
    checks = payload["checks"]
    has_id = any(c.startswith(("IMDb", "TMDB")) for c in checks)
    return (has_id and "title fits" in checks and any(c.startswith("first aired") for c in checks)
            and not payload["others"])


# ── the swap ─────────────────────────────────────────────────────────────


def _same_tags_and_monitoring(before: dict, after: dict) -> list[str]:
    problems = []
    if sorted(before.get("tags") or []) != sorted(after.get("tags") or []):
        problems.append(f"tags changed ({before.get('tags')} → {after.get('tags')})")
    if before.get("path") != after.get("path"):
        problems.append(f"path changed ({before.get('path')} → {after.get('path')})")
    if bool(before.get("monitored")) != bool(after.get("monitored")):
        problems.append("monitored changed")
    was = {s["seasonNumber"]: s.get("monitored") for s in before.get("seasons") or []}
    now = {s["seasonNumber"]: s.get("monitored") for s in after.get("seasons") or []}
    for n, monitored in was.items():
        if n in now and bool(now[n]) != bool(monitored):
            problems.append(f"season {n} monitoring changed")
    return problems


def _files(series: dict) -> int:
    return int((series.get("statistics") or {}).get("episodeFileCount") or 0)


def apply(conn, show_id: str, old_id: int, new_id: int, client=None, who: str = "you",
          source: str = "you") -> str:
    """The swap. Raises `reviews.ReviewError` (nothing changed, or Sonarr put back) with the
    reason; returns the sentence the resolved review keeps."""
    if external_writes.capturing():
        raise reviews.ReviewError(
            "external writes are in capture mode on this LCARS: the Sonarr change would only be "
            "recorded, not made — nothing was changed")
    held = _blocked_by(conn, new_id, show_id)
    if held:
        raise reviews.ReviewError(held)
    current = _ext(conn, show_id, "tvdb")
    if current not in (None, str(old_id), str(new_id)):
        raise reviews.ReviewError(f"the show's TVDB id is {current} now, not {old_id} — look at "
                                  "the show page")
    own = None
    if client is None:
        cfg = config.get_current()
        if not (cfg.sonarr_url and cfg.sonarr_api_key):
            raise reviews.ReviewError("Sonarr isn't configured")
        own = client = sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key)
    try:
        return _swap(conn, client, show_id, old_id, new_id, who, source)
    except sonarr_client.SonarrError as e:
        raise reviews.ReviewError(f"Sonarr: {e}") from e
    finally:
        if own is not None:
            own.close()


def _swap(conn, client, show_id: str, old_id: int, new_id: int, who: str, source: str) -> str:
    series = client.series_by_tvdb_id(old_id)
    if series is None:
        raise reviews.ReviewError(f"Sonarr has no series under TVDB {old_id} (changed already?)")
    if client.series_by_tvdb_id(new_id) is not None:
        raise reviews.ReviewError(f"Sonarr already has a series under TVDB {new_id}")
    found = _lookup(client, f"tvdb:{new_id}")
    if not found or int(found[0].get("tvdbId") or 0) != new_id:
        raise reviews.ReviewError(f"Sonarr can't find TVDB {new_id}")
    new_seasons = {s["seasonNumber"] for s in found[0].get("seasons") or []}
    with_files = {s["seasonNumber"] for s in series.get("seasons") or []
                  if (s.get("statistics") or {}).get("episodeFileCount")}
    if with_files - new_seasons:
        raise reviews.ReviewError(
            f"TVDB {new_id} has no season {sorted(with_files - new_seasons)} but the files on disk "
            "are in it — it numbers the show differently; nothing was changed")

    before = _sonarr_block(client, series)
    snapshot = json.loads(json.dumps(series))
    logger.info("tvdb replace: Sonarr series %s snapshot before TVDB %s -> %s: %s", series["id"],
                old_id, new_id, json.dumps(snapshot))
    review = conn.execute("SELECT id, payload FROM pending_review WHERE entity_type = 'show' AND"
                          " entity_id = ? AND field = ? AND resolved_at IS NULL",
                          (show_id, FIELD)).fetchone()
    if review is not None:  # the way back, kept with the review
        kept = json.loads(review["payload"] or "{}")
        kept["backup"] = snapshot
        conn.execute("UPDATE pending_review SET payload = ? WHERE id = ?",
                     (json.dumps(kept), review["id"]))
        conn.commit()

    def put_back(reason: str) -> str:
        try:
            client.update_series(snapshot)
            return f"{reason}; Sonarr put back as it was"
        except sonarr_client.SonarrError as e:
            logger.error("tvdb replace: ROLLBACK FAILED for series %s (%s); snapshot: %s",
                         series["id"], e, json.dumps(snapshot))
            return (f"{reason}; putting Sonarr back FAILED ({e}) — series {series['id']} needs "
                    "its TVDB id set back by hand (the snapshot is in this review)")

    changed = dict(snapshot, tvdbId=new_id)
    try:
        client.update_series(changed)
        client.refresh_series(series["id"])
        after_series = client.series(series["id"])
    except sonarr_client.SonarrError as e:
        raise reviews.ReviewError(put_back(f"Sonarr failed during the swap ({e})")) from e
    after = _sonarr_block(client, after_series)
    problems = []
    if int(after_series.get("tvdbId") or 0) != new_id:
        problems.append(f"Sonarr kept TVDB {after_series.get('tvdbId')}")
    if after_series.get("status") == "deleted":
        problems.append("Sonarr still says the series is deleted")
    if _files(after_series) < _files(series):
        problems.append(f"files fell from {_files(series)} to {_files(after_series)}")
    problems += _same_tags_and_monitoring(snapshot, after_series)
    if problems:
        raise reviews.ReviewError(put_back("checks after the swap failed: " + "; ".join(problems)))

    try:
        tvdb_guard.replace(conn, show_id, old_id, new_id, source)
        conn.execute("UPDATE show SET updated_at = ? WHERE id = ?", (util.now_utc_iso(), show_id))
        conn.commit()
    except Exception as e:  # the LCARS side failed: Sonarr goes back too
        conn.rollback()
        raise reviews.ReviewError(put_back(f"LCARS couldn't record the new id ({e})")) from e
    return (f"TVDB {old_id} → {new_id} ({who}): Sonarr series {series['id']} "
            f"'{after_series.get('title')}' {before['status']} → {after['status']}, "
            f"files {before['files']} → {after['files']}, tags and monitoring unchanged")


# ── the review's choices ─────────────────────────────────────────────────


def _source(who: str | None, old: int) -> str:
    """Provenance of the new link (R3.7f): `you` when you chose it (never rechecked); LCARS's own
    automatic swap is `recreated:<old id>`, which the Fribb recheck tolerates while Fribb still
    gives the deleted id."""
    return "you" if who else f"{SOURCE_PREFIX}{old}"


def resolve(conn, row, payload: dict, choice: str, note: str | None, who: str) -> str | None:
    """A `tvdb_replace` review answered. Returns the sentence to keep with the resolution;
    raises `reviews.ReviewError` — the review stays open — otherwise."""
    show_id, old = row["entity_id"], int(payload["tvdb_id"])
    if choice == NOID:
        return tvdb_guard.clear(conn, show_id, old)
    if choice in (REPLACE, DESPITE):
        p = payload.get("proposed")
        if not p:
            raise reviews.ReviewError("there is no proposed TVDB show — type the TVDB id in the "
                                      "field")
        if p.get("blocked"):
            raise reviews.ReviewError(p["blocked"])
        if choice == REPLACE and payload.get("mismatches"):
            raise reviews.ReviewError("this replacement has mismatches — use the 'despite' choice")
        return apply(conn, show_id, old, int(p["tvdb_id"]), who=who or "LCARS, automatically",
                     source=_source(who, old))
    m = re.search(r"\d+", note or "")  # OTHER: the id in your note, checked like any other
    if not m:
        raise reviews.ReviewError("type the TVDB id in the field")
    new = int(m.group())
    facts = tvdb_vetting.tvdb_facts(conn, new)
    if facts is None:
        raise reviews.ReviewError(f"Sonarr can't read TVDB {new} — nothing was changed")
    show = conn.execute("SELECT tracking_space FROM show WHERE id = ?", (show_id,)).fetchone()
    stops = tvdb_vetting.hard_stops(payload.get("entry") or {}, facts,
                                    anime=show["tracking_space"] == "anime", new_show=False)
    if stops:
        raise reviews.ReviewError(
            f"TVDB {new} doesn't fit: " + "; ".join(stops) + " — it's now this review's candidate;"
            " use the 'despite' choice if it's right",
            reopen={"replace_review": row["id"], "tvdb_id": new, "facts": facts, "stops": stops})
    return apply(conn, show_id, old, new, who=who or "LCARS", source=_source(who, old))


def reopen(conn, info: dict) -> None:
    """Your id didn't fit: it becomes the review's proposal (with its mismatches), and the choice
    that replaces despite them is offered."""
    row = conn.execute("SELECT * FROM pending_review WHERE id = ? AND resolved_at IS NULL",
                       (info["replace_review"],)).fetchone()
    if row is None:
        return
    payload = json.loads(row["payload"] or "{}")
    facts = info["facts"]
    payload["proposed"] = {"tvdb_id": info["tvdb_id"], "title": facts.get("title"),
                           "year": facts.get("year"), "imdb": None, "tmdb": None,
                           "in_library": None,
                           "blocked": _blocked_by(conn, info["tvdb_id"], row["entity_id"])}
    payload["mismatches"] = info["stops"]
    payload["checks"] = ["the TVDB id you named"]
    payload["columns"] = payload["columns"][:1] + [
        {"label": f"TVDB {info['tvdb_id']} (your id)", "tvdb_id": info["tvdb_id"],
         "facts": facts}]
    conn.execute("UPDATE pending_review SET payload = ?, choices = ? WHERE id = ?",
                 (json.dumps(payload),
                  json.dumps([{"id": c, "label": reviews.LABELS[c]} for c in _choices(payload)]),
                  row["id"]))
