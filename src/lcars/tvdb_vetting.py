"""TVDB links you didn't really check (PLAN-CODE 8.8; RULEBOOK R3.7, R3.2, R3.6d).

A TVDB id is only taken when something independent stands behind it:
- the ID crosswalk (Fribb/anime-lists from AniList/MAL, Wikidata from TMDB/IMDb);
- for a title-search *guess* (browse's Sonarr hit): the series already in your
  Sonarr library under a matching title;
- otherwise you, looking at the evidence (an id you picked, or a review).

Hard stops run on every link the crosswalk didn't confirm — yours included,
because a wrong click is as easy as a wrong guess: an anime against a TVDB show
that isn't animation; a first-air year more than a year off (a new show only —
a later season's TVDB show starts years earlier). Episode counts aren't compared:
Sonarr's lookup gives none per season. A mismatch needs the separate "link
despite" choice (or `tvdb_mismatch_acknowledged`), checked here, not in a page.

Unconfirmed with an AniList/MAL id → an individual season plus a `tvdb_link`
review (link / another TVDB id / keep individual). Without one → the add is
refused as `tvdb_check:{json}` carrying the evidence, for the page to show.
Linking later joins the individual season to the show (`join`).
"""

from __future__ import annotations

import logging
import re

from lcars import add_check, anilist_client, config, sonarr_client

logger = logging.getLogger(__name__)

ANIME_LANGUAGES = {"Japanese", "Chinese", "Mandarin", "Cantonese", "Korean"}
ANIMATION_GENRES = {"Animation", "Anime"}
LINK = "link_tvdb"
LINK_DESPITE = "link_tvdb_despite"
LINK_OTHER = "link_other_tvdb"
KEEP = "keep_individual"


# ── facts ────────────────────────────────────────────────────────────────


def tvdb_facts(conn, tvdb_id: int) -> dict | None:
    """Sonarr's lookup of the TVDB show (the fields checked live 2026-09-28)."""
    from lcars import shows

    try:
        results = shows._lookup_arr(conn, "episodic", f"tvdb:{tvdb_id}")
    except shows.ShowInputError:
        return None  # Sonarr unreachable: the facts are unknown
    if not results:
        return None
    e = results[0]
    language = e.get("originalLanguage") or {}
    return {
        "tvdb_id": tvdb_id,
        "title": e.get("title"),
        "titles": [t for t in [e.get("title"), e.get("originalTitle"),
                               *[a.get("title") for a in e.get("alternateTitles") or []]] if t],
        "year": e.get("year") or None,
        "language": language.get("name") if isinstance(language, dict) else None,
        "genres": e.get("genres") or [],
        "network": e.get("network"),
        "seasons": sorted(s["seasonNumber"] for s in e.get("seasons") or []
                          if s.get("seasonNumber")),
        "poster": e.get("remotePoster"),
    }


def entry_facts(anilist_id: int | None, titles: list[str]) -> dict:
    facts = {"anilist_id": anilist_id, "titles": list(titles), "year": None,
             "country": None, "format": None, "episodes": None, "poster": None}
    if anilist_id is None:
        return facts
    try:
        m = anilist_client.fetch_media_facts(anilist_id)
    except anilist_client.AniListError:
        m = None
    if m:
        t = m.get("title") or {}
        facts.update({
            "titles": [x for x in [t.get("english"), t.get("romaji"), t.get("native"),
                                   *(m.get("synonyms") or []), *titles] if x],
            "year": (m.get("startDate") or {}).get("year"),
            "country": m.get("countryOfOrigin"),
            "format": m.get("format"),
            "episodes": m.get("episodes"),
            "poster": (m.get("coverImage") or {}).get("large"),
        })
    return facts


def evidence(conn, entry: dict, candidates: list[tuple[str, int]]) -> dict:
    """PLAN-CODE 8.8.3 — what a review shows side by side: the entry's own facts (`entry_facts`)
    and, for each TVDB id in question, the TVDB show's (Sonarr's lookup): title, first-aired year,
    language / country, genres / format, network / episode count, TVDB seasons, poster.
    {"entry": facts, "columns": [{"label", "facts"}]}; fetched once, when the review opens, and
    stored in its payload (never in a resolver: the page loads fifty reviews at a time). Optional
    evidence — any failure gives {} and the review opens without it."""
    try:
        return {"entry": entry,
                "columns": [{"label": label, "tvdb_id": tvdb_id,
                             "facts": tvdb_facts(conn, tvdb_id)}
                            for label, tvdb_id in candidates]}
    except Exception:
        logger.exception("review evidence for %s failed", candidates)
        return {}


def show_entry_facts(conn, show_id: str) -> dict:
    """`entry_facts` for a tracked show: its first AniList entry and its titles."""
    show = conn.execute("SELECT * FROM show WHERE id = ?", (show_id,)).fetchone()
    titles = [t for t in (show["title_english"], show["title_romaji"], show["title_native"])
              if t] if show else []
    row = conn.execute("SELECT anilist_id FROM season WHERE show_id = ? AND anilist_id IS NOT"
                       " NULL ORDER BY COALESCE(season_number, 9999), part_number LIMIT 1",
                       (show_id,)).fetchone()
    return entry_facts(row[0] if row else None, titles)


def hard_stops(entry: dict, tvdb: dict | None, *, anime: bool, new_show: bool) -> list[str]:
    """The mismatches a link must be confirmed *despite* (unknown facts: none)."""
    if not tvdb:
        return []
    stops = []
    genres = set(tvdb.get("genres") or [])
    if anime and tvdb.get("language") and tvdb["language"] not in ANIME_LANGUAGES \
            and not genres & ANIMATION_GENRES:
        stops.append(f"TVDB show isn't animation ({tvdb['language']};"
                     f" {', '.join(sorted(genres)) or 'no genres'})")
    ey, ty = entry.get("year"), tvdb.get("year")
    if ey and ty:
        if new_show and abs(ey - ty) > 1:
            stops.append(f"first aired {ey}, the TVDB show {ty}")
        elif not new_show and ey < ty - 1:
            stops.append(f"aired {ey}, before the TVDB show started ({ty})")
    return stops


def _in_sonarr_library(tvdb_id: int) -> bool:
    cfg = config.get_current()
    if not (cfg.sonarr_url and cfg.sonarr_api_key):
        return False
    try:
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            return client.series_by_tvdb_id(tvdb_id) is not None
    except sonarr_client.SonarrError:
        return False


# ── before an add ────────────────────────────────────────────────────────


def vet(conn, input: dict) -> dict:
    """Runs before an episodic add writes anything (Sonarr included).
    Returns the input with a TVDB id LCARS stands behind (or none); raises
    `shows.IndividualSeasonAdded` / `shows.TvdbCheckNeeded` when you have to look."""
    from lcars import shows

    if input.get("media_shape") == "movie" or input.get("tvdb_vetted"):
        return input
    input = {**input, "tvdb_vetted": True}  # once per add (both add paths call this)
    guess = input.get("tvdb_candidate_id")
    given = input.get("tvdb_id")
    input = {k: v for k, v in input.items() if k != "tvdb_candidate_id"}
    crosswalk = shows._resolve_tvdb_for_sequel(
        None, input.get("anilist_id"), input.get("mal_id"),
        input.get("tmdb_id"), input.get("imdb_id"),
    )
    if crosswalk:
        return input if given else {**input, "tvdb_id": int(crosswalk)}  # Fribb beats a guess
    chosen = given or guess
    if not chosen:
        return input
    chosen = int(chosen)
    titles = [input[k] for k in ("title_english", "title_romaji", "title_native") if input.get(k)]
    entry = entry_facts(input.get("anilist_id"), titles)
    tvdb = tvdb_facts(conn, chosen)
    new_show = add_check._tracked_show_for_tvdb(conn, chosen) is None
    stops = hard_stops(entry, tvdb, anime=input.get("tracking_space") == "anime",
                       new_show=new_show)
    reason = None
    if not given:
        confirmed = bool(tvdb) and _in_sonarr_library(chosen) and add_check.titles_match(
            tvdb["titles"], entry["titles"])
        if not confirmed:
            reason = "a title-search match no source confirms (R3.7)"
    if reason is None and stops and not input.get("tvdb_mismatch_acknowledged"):
        reason = "the TVDB show doesn't fit"
    if reason is None:
        return {**input, "tvdb_id": chosen}
    evidence = {"tvdbId": chosen, "reason": reason, "mismatches": stops,
                "tvdb": tvdb, "entry": entry, "newShow": new_show}
    if input.get("anilist_id") is None and input.get("mal_id") is None:
        raise shows.TvdbCheckNeeded(evidence)
    candidate = add_check.Candidate(
        add_check.USER, anilist_id=input.get("anilist_id"), mal_id=input.get("mal_id"),
        titles=titles,
    )
    existing = add_check._season_by_list_id(conn, candidate.anilist_id, candidate.mal_id)
    if existing is not None and existing["show_id"] is None:
        raise shows.IndividualSeasonAdded(existing["id"], None, existing=True)
    if existing is not None:
        raise shows.ShowInputError(
            f"this season is already tracked (show {existing['show_id']})"
            " — refusing to add it twice"
        )
    season_id = add_check.create_individual_season(conn, candidate)
    open_link_review(conn, season_id, evidence)
    conn.commit()
    raise shows.IndividualSeasonAdded(season_id, titles[0] if titles else None)


def open_link_review(conn, season_id: str, evidence: dict) -> None:
    from lcars import reviews

    link = LINK_DESPITE if evidence["mismatches"] else LINK
    text = f"TVDB {evidence['tvdbId']}: {evidence['reason']}"
    if evidence["mismatches"]:
        text += " — " + "; ".join(evidence["mismatches"])
    reviews.open_review(conn, "season", season_id, "tvdb_link", "add_check", text,
                        [link, LINK_OTHER, KEEP], {"season_id": season_id, **evidence})


# ── the review's choices ─────────────────────────────────────────────────


def resolve(conn, payload: dict, choice: str, note: str | None) -> None:
    """Applies a `tvdb_link` choice. Raises `reviews.ReviewError` (the review
    stays open) when the choice can't be applied as it stands."""
    from lcars import reviews

    season_id = payload["season_id"]
    if choice == KEEP:
        return
    if choice == LINK and payload.get("mismatches"):
        raise reviews.ReviewError("this link has mismatches — use the 'despite' choice")
    if choice in (LINK, LINK_DESPITE):
        join(conn, season_id, int(payload["tvdbId"]), _season_from_note(note))
        return
    # Another TVDB id, from your note: checked like any link, never linked blind.
    m = re.search(r"\d+", note or "")
    if not m:
        raise reviews.ReviewError("put the TVDB id in the note")
    tvdb_id = int(m.group())
    season = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    entry = payload.get("entry") or entry_facts(season["anilist_id"], [season["label"] or ""])
    tvdb = tvdb_facts(conn, tvdb_id)
    new_show = add_check._tracked_show_for_tvdb(conn, tvdb_id) is None
    stops = hard_stops(entry, tvdb, anime=True, new_show=new_show)
    if stops:
        raise reviews.ReviewError(
            "TVDB " + str(tvdb_id) + " doesn't fit: " + "; ".join(stops)
            + " — it's now this review's candidate; use the 'despite' choice if it's right",
            reopen={"season_id": season_id, "tvdbId": tvdb_id, "reason": "your TVDB id",
                    "mismatches": stops, "tvdb": tvdb, "entry": entry, "newShow": new_show},
        )
    join(conn, season_id, tvdb_id, _season_from_note(note))


def _season_from_note(note: str | None) -> int | None:
    m = re.search(r"season\s*(\d+)", note or "", re.I)
    return int(m.group(1)) if m else None


# ── joining an individual season to its show (R3.6d) ─────────────────────


def join(conn, season_id: str, tvdb_id: int, season_number: int | None = None) -> str:
    """The individual season becomes a level of the TVDB show: the show is
    created (and sent to Sonarr, R5.5) if you don't track it; the season
    lands on TVDB season `season_number` (your note), or the only one there
    is; its status, history and art move with it. Returns the level's id."""
    from lcars import reviews, shows, status_rules

    indiv = conn.execute("SELECT * FROM season WHERE id = ?", (season_id,)).fetchone()
    if indiv is None or indiv["kind"] != "individual_season":
        raise reviews.ReviewError("this isn't an individual season any more")
    c = add_check.Candidate(add_check.USER, anilist_id=indiv["anilist_id"],
                            mal_id=indiv["mal_id"], tvdb_id=tvdb_id,
                            titles=[indiv["label"]] if indiv["label"] else [])
    facts = entry_facts(indiv["anilist_id"], c.titles)
    is_piece = (facts.get("format") or "").upper() in ("MOVIE", "SPECIAL", "OVA")
    show = add_check._tracked_show_for_tvdb(conn, tvdb_id)
    if show is None:
        tvdb = tvdb_facts(conn, tvdb_id) or {}
        numbers = tvdb.get("seasons") or [1]
        if season_number is None and not is_piece:
            if len(numbers) != 1:
                raise reviews.ReviewError(
                    f"TVDB {tvdb_id} has seasons {numbers} — say which in the note"
                    " (e.g. 'season 2')")
            season_number = numbers[0]
        show_input = {
            "media_shape": "episodic", "tracking_space": "anime", "tvdb_id": tvdb_id,
            "title_english": tvdb.get("title"), "title_romaji": indiv["label"],
            "primary_title": "romaji" if indiv["label"] else "english",
            "skip_sequel_check": True,
        }
        show_id = shows.create_show(conn, show_input)
        shows._add_new_show_to_sonarr(conn, show_id, show_input)
    else:
        show_id = show["id"]
    rows = {r["season_number"]: r for r in conn.execute(
        "SELECT * FROM season WHERE show_id = ? AND kind = 'tvdb_season' AND season_number > 0",
        (show_id,))}
    if is_piece:
        d = add_check.Decision("special", show_id, tvdb_id=tvdb_id, season_number=0)
    else:
        if season_number is None:
            free = [n for n, r in rows.items() if r["anilist_id"] is None and r["mal_id"] is None]
            if len(free) != 1:
                raise reviews.ReviewError(
                    f"which season of the show? (TVDB seasons {sorted(rows) or 'none yet'})"
                    " — say it in the note, e.g. 'season 2'")
            season_number = free[0]
        row = rows.get(season_number)
        if row is None:
            d = add_check.Decision("new_season", show_id, tvdb_id=tvdb_id,
                                   season_number=season_number)
        elif row["anilist_id"] is None and row["mal_id"] is None:
            d = add_check.Decision("link_season", show_id, row["id"], tvdb_id=tvdb_id,
                                   season_number=season_number)
        else:  # the season already holds a cour: this is another one (R1.10)
            d = add_check.Decision("part", show_id, row["id"], tvdb_id=tvdb_id,
                                   season_number=season_number)
    from lcars import sonarr_sync

    target = add_check.apply_decision(conn, d, c)
    _merge_into(conn, season_id, target)
    status_rules.recompute_show(conn, show_id, status_rules.AUTO)
    sonarr_sync.apply(conn, [(target, None, indiv["status"])])  # R5.6/R5.8 on your status
    return target


def _merge_into(conn, source_id: str, target_id: str) -> None:
    """Moves everything that points at `source_id` onto `target_id` (every
    table with a foreign key to season, found by SQLite itself), keeps your
    status, then deletes the source row."""
    src = conn.execute("SELECT * FROM season WHERE id = ?", (source_id,)).fetchone()
    conn.execute(
        "UPDATE season SET status = ?, status_set_manually = ?, score = COALESCE(score, ?),"
        " started_at = COALESCE(started_at, ?) WHERE id = ?",
        (src["status"], src["status_set_manually"], src["score"], src["started_at"], target_id),
    )
    conn.execute("DELETE FROM season_external_id WHERE season_id = ?", (source_id,))
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")]
    for table in tables:
        for fk in conn.execute(f"PRAGMA foreign_key_list('{table}')").fetchall():
            if fk[2] == "season" and fk[4] == "id":
                conn.execute(f"UPDATE {table} SET {fk[3]} = ? WHERE {fk[3]} = ?",
                             (target_id, source_id))
    conn.execute(
        "UPDATE pending_review SET entity_id = ? WHERE entity_type = 'season' AND entity_id = ?",
        (target_id, source_id),
    )
    conn.execute(  # history logged while it had no show (migration f0a1b2c3d4e5)
        "UPDATE season_status_change SET show_id = (SELECT show_id FROM season WHERE id = ?)"
        " WHERE season_id = ? AND show_id IS NULL",
        (target_id, target_id),
    )
    conn.execute("DELETE FROM season WHERE id = ?", (source_id,))
