"""LCARS → Jellyfin: what you watched is marked played for your Jellyfin user (2026-10-10).

LCARS is the truth (R4.3). Each pass compares LCARS's watched state with the user's played marks in
Jellyfin and sends only the differences — the same "compare and heal" shape as the tag reconcile,
not a hook on each of the ten places an episode becomes watched:

- an episode LCARS holds as watched and Jellyfin holds as not played → mark played (with the time
  you watched it);
- an episode LCARS holds as NOT watched, that LCARS and Jellyfin last agreed was played → mark
  unplayed. A play that exists only in Jellyfin is never undone;
- anything else is left alone.

Matching (never guessed): the show by its TVDB id (a movie: TMDB id), the episode by the TVDB season
and episode numbers Sonarr names the files with (`sonarr_season` / `sonarr_episode`). One Jellyfin
file covering several episodes (S01E01-E02) is played only when all of them are watched, and
unplayed only when none is. What cannot be matched is reported.

`jellyfin_watch_sync` remembers the state both sides last agreed on, per episode or movie.

Phase 2 (user, 2026-10-10: "it matters not where I watch something if it is indeed watched"):
a play that exists only in Jellyfin (LCARS has it unwatched and the two never agreed it was
played) becomes an LCARS watch, at Jellyfin's last-played time, through the same path an mpv watch
takes (`addWatchEvent`: season status, AniList / MAL progress, Sonarr). Only plays at or after
`jellyfin_import_since` are imported; older ones (Jellyfin holds years of them) are counted, never
imported. If both sides changed, LCARS wins (R4.10): an LCARS un-watch of something the two had
agreed on is un-marked in Jellyfin, never re-imported. Jellyfin un-marking something is not
imported.
"""

import logging
from datetime import UTC, datetime

from lcars import config, external_writes, jellyfin_client, util

logger = logging.getLogger(__name__)

DEFAULT_LIMIT = 500  # played / unplayed marks sent per pass, so a catch-up is spread over passes
IMPORT_LIMIT = 50  # Jellyfin plays turned into LCARS watches per pass (each pushes to the lists)
UNMATCHED_SAMPLE = 25


def _provider(item: dict, name: str) -> str | None:
    for key, value in (item.get("ProviderIds") or {}).items():
        if key.lower() == name and value:
            return str(value)
    return None


def _played(item: dict) -> bool:
    return bool((item.get("UserData") or {}).get("Played"))


def _last_played(item: dict) -> str | None:
    """Jellyfin's last-played time as 'YYYY-MM-DDTHH:MM:SSZ' (UTC), or None."""
    raw = (item.get("UserData") or {}).get("LastPlayedDate")
    if not raw:
        return None
    try:
        moment = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _last_watched(conn, show_id: str, season, episode) -> str | None:
    if season is None:
        row = conn.execute("SELECT MAX(watched_at) FROM watch_event WHERE show_id = ?",
                           (show_id,)).fetchone()
    else:
        row = conn.execute(
            "SELECT MAX(watched_at) FROM watch_event WHERE show_id = ? AND season = ?"
            " AND episode = ?", (show_id, season, episode)).fetchone()
    return row[0] if row else None


def _remember(conn, kind: str, entity_id: str, item_id: str, state: str) -> None:
    conn.execute(
        "INSERT INTO jellyfin_watch_sync (entity_kind, entity_id, jellyfin_item_id, state,"
        " synced_at) VALUES (?, ?, ?, ?, ?) ON CONFLICT (entity_kind, entity_id) DO UPDATE SET"
        " jellyfin_item_id = excluded.jellyfin_item_id, state = excluded.state,"
        " synced_at = excluded.synced_at",
        (kind, entity_id, item_id, state, util.now_utc_iso()))


def _agreed_played(conn, kind: str, entity_ids: list[str]) -> bool:
    if not entity_ids:
        return False
    marks = ",".join("?" * len(entity_ids))
    row = conn.execute(
        f"SELECT 1 FROM jellyfin_watch_sync WHERE entity_kind = ? AND state = 'played'"
        f" AND entity_id IN ({marks}) LIMIT 1", (kind, *entity_ids)).fetchone()
    return row is not None


class _Pass:
    def __init__(self, conn, client, user_id, dry_run, limit, import_since=None):
        self.conn, self.client, self.user_id = conn, client, user_id
        self.dry_run, self.limit, self.import_since = dry_run, limit, import_since
        self.record = not dry_run and not external_writes.capturing()
        self.seen_shows: set[str] = set()
        self.stats = {"configured": True, "dry_run": dry_run, "shows_checked": 0,
                      "episodes_matched": 0, "marked_played": 0, "marked_unplayed": 0,
                      "agreed": 0, "movies_marked": 0, "unmatched": [], "capped": False,
                      "failed": False, "imported": 0, "older_plays": 0}

    def writes_left(self) -> bool:
        sent = (self.stats["marked_played"] + self.stats["marked_unplayed"]
                + self.stats["movies_marked"])
        if sent >= self.limit:
            self.stats["capped"] = True
            return False
        return True

    def decide(self, kind: str, entity_ids: list[str], item: dict, watched: list[bool],
               when: str | None, show_id: str | None = None, eps: list | None = None) -> None:
        """One Jellyfin item covering one or more LCARS episodes (or one movie)."""
        have = _played(item)
        item_id = item["Id"]
        agreed = _agreed_played(self.conn, kind, entity_ids)
        if have and not all(watched) and not agreed:
            self.import_play(kind, entity_ids, item, show_id, eps)  # a play only Jellyfin has
        elif all(watched):
            if have:
                self.stats["agreed"] += 1
                if self.record:
                    for entity_id in entity_ids:
                        _remember(self.conn, kind, entity_id, item_id, "played")
            elif self.writes_left():
                if not self.dry_run:
                    self.client.mark_played(item_id, self.user_id, when)
                self.stats["movies_marked" if kind == "movie" else "marked_played"] += 1
                if self.record:
                    for entity_id in entity_ids:
                        _remember(self.conn, kind, entity_id, item_id, "played")
        elif not any(watched):
            if have and agreed:  # LCARS un-watched what both had agreed on
                if self.writes_left():
                    if not self.dry_run:
                        self.client.mark_unplayed(item_id, self.user_id)
                    self.stats["movies_marked" if kind == "movie" else "marked_unplayed"] += 1
                    if self.record:
                        for entity_id in entity_ids:
                            _remember(self.conn, kind, entity_id, item_id, "unplayed")
            elif not have and self.record and agreed:
                for entity_id in entity_ids:
                    _remember(self.conn, kind, entity_id, item_id, "unplayed")

    def import_play(self, kind: str, entity_ids: list[str], item: dict, show_id: str | None,
                    eps: list | None) -> None:
        """Jellyfin has a play LCARS does not: an LCARS watch, if it is recent enough."""
        when = _last_played(item)
        if not (self.import_since and when and when >= self.import_since):
            self.stats["older_plays"] += 1  # counted, never imported
            return
        if self.stats["imported"] >= IMPORT_LIMIT:
            self.stats["capped"] = True
            return
        if kind == "movie":
            row = self.conn.execute("SELECT status FROM show WHERE id = ?", (show_id,)).fetchone()
            if row is None or row["status"] == "skipped":
                return  # a skipped title is not followed; its play is not resurrected
        self.stats["imported"] += 1
        if self.dry_run:
            return
        from lcars import resolvers  # the path an mpv watch takes: status, lists, Sonarr

        try:
            if kind == "movie":
                resolvers.resolve_add_watch_event(None, None, show_id, None, None, when, "jellyfin")
                resolvers._apply_status_change(self.conn, show_id, "completed", "jellyfin",
                                               confirmed=False)
            else:
                for e in eps or []:
                    if e["state"] != "watched":
                        resolvers.resolve_add_watch_event(
                            None, None, show_id, e["season"], e["episode"], when, "jellyfin")
        except Exception:  # one failed import must not stop the pass
            logger.exception("jellyfin_sync: importing a Jellyfin play of %s failed", show_id)
            self.conn.rollback()
            self.stats["imported"] -= 1
            self.stats["failed"] = True
            return
        if self.record:
            for entity_id in entity_ids:
                _remember(self.conn, kind, entity_id, item["Id"], "played")

    def note_item(self, kind: str, entity_id: str, item_id: str) -> None:
        """Remember which Jellyfin item this is, for the Jellyfin links in the UI (local data, not
        an external write: kept in capture mode too, never in a dry run)."""
        if kind == "show":
            self.seen_shows.add(entity_id)
        if self.dry_run:
            return
        self.conn.execute(
            "INSERT INTO jellyfin_item (kind, entity_id, jellyfin_item_id, seen_at)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (kind, entity_id) DO UPDATE SET"
            " jellyfin_item_id = excluded.jellyfin_item_id, seen_at = excluded.seen_at",
            (kind, entity_id, item_id, util.now_utc_iso()))

    def note_unmatched(self, text: str) -> None:
        if len(self.stats["unmatched"]) < UNMATCHED_SAMPLE:
            self.stats["unmatched"].append(text)

    # ── episodes ─────────────────────────────────────────────────────────────

    def episodes(self, show_only: str | None) -> None:
        series_by_tvdb = {
            _provider(s, "tvdb"): s["Id"] for s in self.client.all_series(self.user_id)
            if _provider(s, "tvdb")
        }
        # every tracked show Jellyfin has: a play of a never-watched show must be seen too
        sql = (
            "SELECT s.id, x.external_id AS tvdb, coalesce(s.title_english, s.title_romaji) AS title"
            " FROM show s JOIN show_external_id x ON x.show_id = s.id AND x.service = 'tvdb'"
            " WHERE s.tracked = 1 AND s.media_shape = 'episodic'"
        )
        params: tuple = ()
        if show_only:
            sql += " AND s.id = ?"
            params = (show_only,)
        for show in self.conn.execute(sql, params).fetchall():
            series_id = series_by_tvdb.get(str(show["tvdb"]))
            if series_id is None:
                continue  # not in Jellyfin: no files to mark
            self.stats["shows_checked"] += 1
            self.note_item("show", show["id"], series_id)
            self._show(show, series_id)
            if self.stats["capped"]:
                return

    def _show(self, show, series_id: str) -> None:
        by_number: dict[tuple[int, int], list] = {}
        for e in self.conn.execute(
            "SELECT id, state, season, episode, sonarr_season, sonarr_episode FROM episode"
            " WHERE show_id = ? AND sonarr_season IS NOT NULL AND sonarr_episode IS NOT NULL",
            (show["id"],),
        ):
            by_number.setdefault((e["sonarr_season"], e["sonarr_episode"]), []).append(e)
        covered: set[tuple[int, int]] = set()
        seen_episodes: set[str] = set()
        for item in self.client.series_episodes(series_id, self.user_id):
            first, season = item.get("IndexNumber"), item.get("ParentIndexNumber")
            if first is None or season is None:
                continue
            numbers = [(season, n) for n in range(first, (item.get("IndexNumberEnd") or first) + 1)]
            eps = [e for number in numbers for e in by_number.get(number, [])]
            covered.update(numbers)
            if not eps:
                continue
            for e in eps:
                self.note_item("episode", e["id"], item["Id"])
                seen_episodes.add(e["id"])
            self.stats["episodes_matched"] += len(eps)
            latest = max((_last_watched(self.conn, show["id"], e["season"], e["episode"]) or ""
                          for e in eps), default="") or None
            self.decide("episode", [e["id"] for e in eps], item,
                        [e["state"] == "watched" for e in eps], latest, show["id"], eps)
            if self.stats["capped"]:
                return
        if not self.dry_run:  # an episode that is no longer in Jellyfin has no link any more
            marks = ",".join("?" * len(seen_episodes)) or "NULL"
            self.conn.execute(
                "DELETE FROM jellyfin_item WHERE kind = 'episode' AND entity_id IN"
                " (SELECT id FROM episode WHERE show_id = ?)"
                f" AND entity_id NOT IN ({marks})", (show["id"], *seen_episodes))
        missing = [n for n, eps in by_number.items()
                   if n not in covered and any(e["state"] == "watched" for e in eps)]
        if missing:
            self.note_unmatched(
                f"{show['title']}: {len(missing)} watched episode(s) not in Jellyfin"
                f" (e.g. S{min(missing)[0]:02d}E{min(missing)[1]:02d})")

    # ── movies ───────────────────────────────────────────────────────────────

    def movies(self, show_only: str | None) -> None:
        by_tmdb = {_provider(m, "tmdb"): m for m in self.client.all_movies(self.user_id)
                   if _provider(m, "tmdb")}
        sql = (
            "SELECT s.id, s.status, x.external_id AS tmdb FROM show s JOIN show_external_id x"
            " ON x.show_id = s.id AND x.service = 'tmdb' WHERE s.tracked = 1"
            " AND s.media_shape = 'movie'"
        )
        params: tuple = ()
        if show_only:
            sql += " AND s.id = ?"
            params = (show_only,)
        for show in self.conn.execute(sql, params).fetchall():
            item = by_tmdb.get(str(show["tmdb"]))
            if item is None:
                continue
            when = _last_watched(self.conn, show["id"], None, None)
            self.note_item("show", show["id"], item["Id"])
            self.decide("movie", [show["id"]], item, [show["status"] == "completed"], when,
                        show["id"])
            if self.stats["capped"]:
                return


def _forget_shows_not_seen(conn, seen: set[str]) -> None:
    """After a complete pass: a show (or movie) Jellyfin no longer has loses its links, and so do
    its episodes."""
    marks = ",".join("?" * len(seen)) or "NULL"
    ids = tuple(seen)
    conn.execute(f"DELETE FROM jellyfin_item WHERE kind = 'show' AND entity_id NOT IN ({marks})",
                 ids)
    conn.execute(
        "DELETE FROM jellyfin_item WHERE kind = 'episode' AND entity_id IN"
        f" (SELECT id FROM episode WHERE show_id NOT IN ({marks}))", ids)


def link_for(conn, show_id: str, episode_id: str | None = None) -> str | None:
    """The address a browser opens in Jellyfin: the episode's own page when it has one; for a movie
    (or when no episode is asked about) the show's own page. An episode Jellyfin does not have has
    no link — so the Jellyfin icon appears exactly when the mpv one can play. None when the sync
    has not met it, or Jellyfin is not configured."""
    cfg = config.get_current()
    base = (cfg.jellyfin_public_url or cfg.jellyfin_url or "").rstrip("/")
    if not base:
        return None
    row = None
    if episode_id:
        row = conn.execute("SELECT jellyfin_item_id FROM jellyfin_item WHERE kind = 'episode'"
                           " AND entity_id = ?", (episode_id,)).fetchone()
    show_page = not episode_id  # asked about the show itself
    if episode_id:  # a movie has no episodes of its own: its page is the show's
        shape = conn.execute("SELECT media_shape FROM show WHERE id = ?", (show_id,)).fetchone()
        show_page = shape is not None and shape[0] == "movie"
    if row is None and show_page:
        row = conn.execute("SELECT jellyfin_item_id FROM jellyfin_item WHERE kind = 'show'"
                           " AND entity_id = ?", (show_id,)).fetchone()
    return f"{base}/web/#/details?id={row[0]}" if row else None


def _empty(configured: bool, dry_run: bool, failed: bool = False) -> dict:
    return {"configured": configured, "dry_run": dry_run, "shows_checked": 0,
            "episodes_matched": 0, "marked_played": 0, "marked_unplayed": 0, "agreed": 0,
            "movies_marked": 0, "unmatched": [], "capped": False, "failed": failed,
            "imported": 0, "older_plays": 0}


def run(conn, *, dry_run: bool = False, limit: int = DEFAULT_LIMIT, show_id: str | None = None,
        import_since: str | None = None) -> dict:
    """One pass. Never raises: a Jellyfin failure comes back as `failed`. `import_since` (else
    the `jellyfin_import_since` setting) is the moment from which Jellyfin plays become LCARS
    watches."""
    cfg = config.get_current()
    if not (cfg.jellyfin_url and cfg.jellyfin_api_key and cfg.jellyfin_user):
        return _empty(False, dry_run)
    since = (import_since or cfg.jellyfin_import_since or "").strip() or None
    try:
        with jellyfin_client.JellyfinClient(cfg.jellyfin_url, cfg.jellyfin_api_key) as client:
            one = _Pass(conn, client, client.user_id(cfg.jellyfin_user), dry_run, limit, since)
            try:
                one.episodes(show_id)
                if not one.stats["capped"]:
                    one.movies(show_id)
                if not dry_run and show_id is None and not one.stats["capped"]:
                    _forget_shows_not_seen(conn, one.seen_shows)
            finally:
                if not dry_run:
                    conn.commit()
            return one.stats
    except jellyfin_client.JellyfinError as e:
        logger.warning("jellyfin_sync: %s", e)
        conn.rollback()
        return _empty(True, dry_run, failed=True)
