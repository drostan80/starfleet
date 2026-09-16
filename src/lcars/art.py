"""Art asset storage and retrieval (2026-08-30).

Manages the ``art_asset`` table: inserting candidates from external
sources (AniList, TVDB, TVmaze), querying by show/season, and selecting/
deselecting art for display.

The "selected" art for a (show, season, kind) slot is what resolvers
surface as ``Show.posterUrl``, ``Season.posterUrl``, etc.  When no
selected asset exists, resolvers fall back to the existing
``show.poster_url`` / ``show.banner_url`` columns (legacy data that
predates this table).
"""

from __future__ import annotations

import logging

from lcars import ids, util

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def upsert_asset(
    conn,
    show_id: str,
    season_id: str | None,
    kind: str,
    source: str,
    url: str,
    *,
    width: int | None = None,
    height: int | None = None,
    language: str | None = None,
    source_score: int | None = None,
) -> str:
    """Insert or update an art asset row.  Returns the asset id.

    Uniqueness is on (show_id, season_id, kind, source, url) — a
    second call with the same key updates width/height/language/score
    but never touches the ``selected`` flag.
    """
    now = util.now_utc_iso()
    existing = conn.execute(
        "SELECT id FROM art_asset"
        " WHERE show_id = ? AND season_id IS ? AND kind = ? AND source = ? AND url = ?",
        (show_id, season_id, kind, source, url),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE art_asset SET width = ?, height = ?, language = ?,"
            "  source_score = ? WHERE id = ?",
            (width, height, language, source_score, existing["id"]),
        )
        return existing["id"]

    asset_id = ids.generate_id(conn, "h")
    conn.execute(
        "INSERT INTO art_asset"
        " (id, show_id, season_id, kind, source, url,"
        "  width, height, language, source_score, selected, created_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)",
        (asset_id, show_id, season_id, kind, source, url,
         width, height, language, source_score, now),
    )
    return asset_id


_KIND_TO_SHOW_COLUMN: dict[str, str] = {
    "banner": "banner_url",
    "background": "banner_url",   # banner slot shows both kinds
    "poster": "poster_url",
}


def select_asset(conn, asset_id: str) -> dict:
    """Mark an asset as the selected art for its (show, season, kind)
    slot, deselecting whatever was previously selected in that slot.

    For show-level assets (season_id IS NULL), also writes the URL back
    to the corresponding ``show`` column (``banner_url`` / ``poster_url``)
    so that fast-path resolvers (calendar, lists) return the correct
    value without an ``art_asset`` lookup.

    Returns the updated asset row.
    """
    asset = conn.execute("SELECT * FROM art_asset WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        raise ValueError(f"Art asset {asset_id} not found")
    asset = dict(asset)

    # Deselect any existing selection in the same slot
    if asset["season_id"] is None:
        conn.execute(
            "UPDATE art_asset SET selected = 0"
            " WHERE show_id = ? AND season_id IS NULL AND kind = ? AND selected = 1",
            (asset["show_id"], asset["kind"]),
        )
    else:
        conn.execute(
            "UPDATE art_asset SET selected = 0"
            " WHERE show_id = ? AND season_id = ? AND kind = ? AND selected = 1",
            (asset["show_id"], asset["season_id"], asset["kind"]),
        )

    conn.execute("UPDATE art_asset SET selected = 1 WHERE id = ?", (asset_id,))

    # Denormalise: write URL back to the show row so calendar/list
    # resolvers (which skip the art_asset table) return the right art.
    if asset["season_id"] is None:
        col = _KIND_TO_SHOW_COLUMN.get(asset["kind"])
        if col:
            conn.execute(
                f"UPDATE show SET {col} = ? WHERE id = ?",
                (asset["url"], asset["show_id"]),
            )

    conn.commit()
    asset["selected"] = 1
    return asset


def deselect_asset(conn, asset_id: str) -> dict:
    """Clear the selected flag on an asset.

    For show-level assets, also clears the corresponding ``show``
    column (``banner_url`` / ``poster_url``) so the fast-path resolvers
    don't keep serving the old art.

    Returns the updated row.
    """
    asset = conn.execute("SELECT * FROM art_asset WHERE id = ?", (asset_id,)).fetchone()
    if not asset:
        raise ValueError(f"Art asset {asset_id} not found")
    conn.execute("UPDATE art_asset SET selected = 0 WHERE id = ?", (asset_id,))

    # Clear the denormalised show column so fast-path resolvers return
    # NULL (which lets the client fall through to its own fallback).
    asset = dict(asset)
    if asset["season_id"] is None:
        col = _KIND_TO_SHOW_COLUMN.get(asset["kind"])
        if col:
            conn.execute(
                f"UPDATE show SET {col} = NULL WHERE id = ?",
                (asset["show_id"],),
            )

    conn.commit()
    asset["selected"] = 0
    return asset


# ---------------------------------------------------------------------------
# Read
# ---------------------------------------------------------------------------

def get_art_assets_for_show(conn, show_id: str) -> list[dict]:
    """All art assets for a show (both show-level and season-level)."""
    rows = conn.execute(
        "SELECT * FROM art_asset WHERE show_id = ? ORDER BY kind, source_score DESC, created_at",
        (show_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_art_assets_for_season(conn, season_id: str) -> list[dict]:
    """Art assets scoped to a specific season."""
    rows = conn.execute(
        "SELECT * FROM art_asset WHERE season_id = ? ORDER BY kind, source_score DESC, created_at",
        (season_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_selected_url(conn, show_id: str, season_id: str | None, kind: str) -> str | None:
    """Return the URL of the selected art asset for a slot, or None."""
    if season_id is None:
        row = conn.execute(
            "SELECT url FROM art_asset"
            " WHERE show_id = ? AND season_id IS NULL AND kind = ? AND selected = 1",
            (show_id, kind),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT url FROM art_asset"
            " WHERE show_id = ? AND season_id = ? AND kind = ? AND selected = 1",
            (show_id, season_id, kind),
        ).fetchone()
    return row["url"] if row else None


# ---------------------------------------------------------------------------
# Populate from sources
# ---------------------------------------------------------------------------

def store_anilist_art(
    conn,
    show_id: str,
    season_id: str | None,
    cover_large: str | None,
    cover_extra_large: str | None,
    banner_image: str | None,
) -> None:
    """Store AniList cover/banner images as art assets.

    Called during metadata fetch — each AniList entry (per-season or
    show-level) provides a cover image and optionally a banner.
    """
    if cover_extra_large:
        upsert_asset(conn, show_id, season_id, "poster", "anilist",
                      cover_extra_large)
    if cover_large and cover_large != cover_extra_large:
        upsert_asset(conn, show_id, season_id, "poster", "anilist",
                      cover_large)
    if banner_image:
        upsert_asset(conn, show_id, season_id, "banner", "anilist",
                      banner_image)


def store_tvdb_art(
    conn,
    show_id: str,
    artworks: list[dict],
    season_map: dict[int, str],
) -> int:
    """Store TVDB artwork as art assets.

    ``artworks`` is the normalized list from ``TvdbClient.series_artworks``
    or ``movie_artworks``.  ``season_map`` maps season_number → LCARS
    season_id (for season-scoped art).

    Returns the number of new assets inserted.
    """
    count = 0
    for art in artworks:
        season_id = None
        sn = art.get("season_number")
        if sn is not None:
            season_id = season_map.get(sn)
            if season_id is None:
                continue  # no matching LCARS season — skip

        upsert_asset(
            conn, show_id, season_id,
            art["kind"], "tvdb", art["url"],
            width=art.get("width"),
            height=art.get("height"),
            language=art.get("language"),
            source_score=art.get("source_score"),
        )
        count += 1
    return count


def store_tvmaze_art(
    conn,
    show_id: str,
    show_data: dict,
) -> None:
    """Store TVmaze poster image from the show lookup payload.

    TVmaze's show lookup returns ``image.medium`` and ``image.original``
    — poster-type only.  Called during ``tvmaze.drip_fetch_episodes``
    which already has ``show_data`` in hand (no extra API call).

    For banner/background, use ``store_tvmaze_images`` with the
    ``/shows/{id}/images`` endpoint response.
    """
    images = show_data.get("image") or {}
    original = images.get("original")
    medium = images.get("medium")

    if original:
        upsert_asset(conn, show_id, None, "poster", "tvmaze", original)
    if medium and medium != original:
        upsert_asset(conn, show_id, None, "poster", "tvmaze", medium)


def store_mal_art(
    conn,
    show_id: str,
    season_id: str | None,
    mal_data: dict,
) -> None:
    """Store MAL poster image as an art asset.

    ``mal_data`` is the response from ``mal_client.fetch_anime_details``.
    MAL provides ``main_picture.large`` and ``main_picture.medium``
    (poster only — no banner equivalent).
    """
    pics = mal_data.get("main_picture") or {}
    large = pics.get("large")
    medium = pics.get("medium")
    if large:
        upsert_asset(conn, show_id, season_id, "poster", "mal", large)
    if medium and medium != large:
        upsert_asset(conn, show_id, season_id, "poster", "mal", medium)


_TVMAZE_TYPE_MAP = {"poster": "poster", "banner": "banner", "background": "background"}


def store_tvmaze_images(
    conn,
    show_id: str,
    images: list[dict],
) -> int:
    """Store TVmaze images from the ``/shows/{id}/images`` endpoint.

    Each image dict has ``type`` (poster/banner/background/typography)
    and ``resolutions.original.url`` / width / height.
    Returns count of assets stored.
    """
    count = 0
    for img in images:
        kind = _TVMAZE_TYPE_MAP.get(img.get("type"))
        if kind is None:
            continue
        resolutions = img.get("resolutions") or {}
        original = resolutions.get("original") or {}
        url = original.get("url")
        if not url:
            continue
        upsert_asset(
            conn, show_id, None, kind, "tvmaze", url,
            width=original.get("width"),
            height=original.get("height"),
        )
        count += 1
    return count


TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/original"


def store_tmdb_art(
    conn,
    show_id: str,
    images_data: dict,
) -> int:
    """Store TMDB images (posters + backdrops) as art assets.

    ``images_data`` is the response from ``TmdbClient.tv_images`` or
    ``movie_images`` — contains ``posters`` and ``backdrops`` lists,
    each entry having ``file_path``, ``width``, ``height``,
    ``iso_639_1`` (language), ``vote_average`` (score).

    TMDB returns file paths (e.g. ``/abc123.jpg``), not full URLs —
    we prefix with the TMDB image base URL.
    """
    count = 0
    for poster in images_data.get("posters") or []:
        path = poster.get("file_path")
        if not path:
            continue
        upsert_asset(
            conn, show_id, None, "poster", "tmdb",
            TMDB_IMAGE_BASE + path,
            width=poster.get("width"),
            height=poster.get("height"),
            language=poster.get("iso_639_1"),
            source_score=int((poster.get("vote_average") or 0) * 10),
        )
        count += 1
    for backdrop in images_data.get("backdrops") or []:
        path = backdrop.get("file_path")
        if not path:
            continue
        upsert_asset(
            conn, show_id, None, "background", "tmdb",
            TMDB_IMAGE_BASE + path,
            width=backdrop.get("width"),
            height=backdrop.get("height"),
            language=backdrop.get("iso_639_1"),
            source_score=int((backdrop.get("vote_average") or 0) * 10),
        )
        count += 1
    return count


def auto_select_best(conn, show_id: str) -> None:
    """For each (show, season, kind) slot that has no selection yet,
    auto-select the highest-scored candidate.  Called after a bulk
    art fetch to seed initial selections without overriding user
    choices.

    Prefers anilist posters (they match what the user already sees),
    then tvdb/tvmaze by source_score descending (tvmaze has no
    source_score, so it ranks below scored TVDB art).
    """
    # Find slots with no selection
    unselected = conn.execute(
        """
        SELECT show_id, season_id, kind
        FROM art_asset
        WHERE show_id = ?
        GROUP BY show_id, season_id, kind
        HAVING SUM(selected) = 0
        """,
        (show_id,),
    ).fetchall()

    for slot in unselected:
        # Pick best candidate: anilist first, then by source_score desc
        best = conn.execute(
            """
            SELECT id, url FROM art_asset
            WHERE show_id = ? AND season_id IS ? AND kind = ?
            ORDER BY
                CASE source WHEN 'anilist' THEN 0 ELSE 1 END,
                COALESCE(source_score, 0) DESC,
                created_at ASC
            LIMIT 1
            """,
            (slot["show_id"], slot["season_id"], slot["kind"]),
        ).fetchone()
        if best:
            conn.execute("UPDATE art_asset SET selected = 1 WHERE id = ?", (best["id"],))
            # Denormalise show-level selections
            if slot["season_id"] is None:
                col = _KIND_TO_SHOW_COLUMN.get(slot["kind"])
                if col:
                    conn.execute(
                        f"UPDATE show SET {col} = ? WHERE id = ?",
                        (best["url"], slot["show_id"]),
                    )

    conn.commit()
