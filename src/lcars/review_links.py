"""The links a review carries (user 10-06): the AniList, MAL, TVDB and Sonarr/Radarr pages of the
show or season the review is about, so the evidence can be opened from the review itself.

Resolved on the server from the review's entity (show, season or episode), its payload (a review
opened for a candidate that is not tracked yet carries its AniList/MAL/TVDB ids there) and the
show's external ids; the URL templates are `external_urls`'s, nothing is hardcoded in the page.
"""

from __future__ import annotations

import json

from lcars import external_urls

_LABELS = {"anilist": "AniList", "mal": "MyAnimeList", "tvdb": "TVDB", "sonarr": "Sonarr",
           "radarr": "Radarr"}
_ORDER = ("anilist", "mal", "tvdb", "sonarr", "radarr")


def for_review(conn, review: dict) -> list[dict]:
    """[{service, label, id, url}] in a fixed order, one per service (empty when none is known)."""
    payload = json.loads(review.get("payload") or "{}")
    show_id = review.get("show_id") or payload.get("show_id")
    season = None
    season_id = payload.get("season_id")
    if review["entity_type"] == "season":
        season_id = review["entity_id"]
    if season_id:
        season = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
        if season is not None and not show_id:
            show_id = season["show_id"]
    if (review["entity_type"] == "show" and not show_id
            and str(review["entity_id"]).startswith("s-")):
        show_id = review["entity_id"]
    if review["entity_type"] == "episode" and not show_id:
        row = conn.execute("SELECT show_id FROM episode WHERE id = ?",
                           (review["entity_id"],)).fetchone()
        show_id = row["show_id"] if row else None

    ids: dict[str, tuple[str, str | None]] = {}  # service -> (id, url)
    if show_id:
        for row in conn.execute(
            "SELECT service, external_id, url FROM show_external_id WHERE show_id = ?"
            " AND service IN ('tvdb', 'sonarr', 'radarr')", (show_id,)
        ):
            ids[row["service"]] = (row["external_id"], row["url"])
    for service, key in (("anilist", "anilist_id"), ("mal", "mal_id"), ("tvdb", "tvdb_id")):
        value = payload.get(key)
        if value is None and season is not None and service in ("anilist", "mal"):
            value = season[key]
            if value is None:
                row = conn.execute(
                    "SELECT external_id FROM season_external_id WHERE season_id = ?"
                    " AND service = ?", (season["id"], service)).fetchone()
                value = row["external_id"] if row else None
        if value is not None:
            ids[service] = (str(value), None)  # the review's own id wins over the show's

    if show_id and season is None and "anilist" not in ids and "mal" not in ids:
        # a show-level review: the show's first entry
        first = conn.execute(
            "SELECT anilist_id, mal_id FROM season WHERE show_id = ? AND (anilist_id IS NOT NULL"
            " OR mal_id IS NOT NULL) ORDER BY COALESCE(season_number, 9999), part_number LIMIT 1",
            (show_id,)).fetchone()
        if first is not None:
            for service, value in (("anilist", first["anilist_id"]), ("mal", first["mal_id"])):
                if value is not None:
                    ids[service] = (str(value), None)

    links = []
    for service in _ORDER:
        if service not in ids:
            continue
        ext_id, url = ids[service]
        template = external_urls.TEMPLATES.get(service)
        if template is not None:
            url = template.replace("{id}", ext_id)
        if not url:
            continue
        links.append({"service": service, "label": _LABELS[service], "id": ext_id, "url": url})
    # a TVDB id Fribb gives that is not the stored one (the TVDB site has no search by id: the
    # dereferrer link opens the show) — user 10-06
    stored = ids.get("tvdb", (None,))[0]
    for other in payload.get("fribb_ids") or []:
        if str(other) != str(stored):
            links.append({"service": "tvdb", "label": "TVDB (Fribb's)", "id": str(other),
                          "url": external_urls.TEMPLATES["tvdb"].replace("{id}", str(other))})
    return links
