"""Every external id has a link (user 10-05: Syoboi ids had none).

`show_external_id.url` was filled only where an add path set it (984 of 985 Syoboi rows, 1,055 of
1,061 AniDB and 1,687 of 1,712 TVmaze rows on prod had none), and the show page links an id's badge
only from that column. The link is a pure function of the service and the id, so any row without
one gets it here, every Memory Alpha pass. Sonarr/Radarr links depend on the host and are left to
their own add path.
"""

from __future__ import annotations

TEMPLATES = {
    "anilist": "https://anilist.co/anime/{id}",
    "mal": "https://myanimelist.net/anime/{id}",
    "tvdb": "https://thetvdb.com/dereferrer/series/{id}",
    "tvdb_movie": "https://thetvdb.com/dereferrer/movie/{id}",
    "imdb": "https://www.imdb.com/title/{id}",
    "anidb": "https://anidb.net/anime/{id}",
    "syoboi": "https://cal.syoboi.jp/tid/{id}",
    "tvmaze": "https://www.tvmaze.com/shows/{id}",
}


def fill_missing(conn) -> int:
    """Gives every show_external_id row without a url its service's link. Returns how many.
    (-1 ids, TVmaze's "no such show", are left alone; TMDB depends on the show's shape.)"""
    n = 0
    for service, template in TEMPLATES.items():
        n += conn.execute(
            "UPDATE show_external_id SET url = replace(?, '{id}', external_id)"
            " WHERE service = ? AND (url IS NULL OR url = '') AND external_id != '-1'"
            " AND external_id != ''", (template, service),
        ).rowcount
    n += conn.execute(
        "UPDATE show_external_id SET url = 'https://www.themoviedb.org/' ||"
        " CASE WHEN (SELECT media_shape FROM show WHERE show.id = show_external_id.show_id)"
        " = 'movie' THEN 'movie/' ELSE 'tv/' END || external_id"
        " WHERE service = 'tmdb' AND (url IS NULL OR url = '') AND external_id != '-1'"
    ).rowcount
    return n
