"""Tags LCARS keeps on Sonarr/Radarr for Maintainerr (user, 2026-10-10).

Maintainerr cleans the media server on rules; these tags are how LCARS, which knows what you watch,
steers it. Three tags, all matched to the show by its **TVDB id** (a movie: its TMDB id) — never by
the stored Sonarr/Radarr link, which an old fuzzy title match left wrong on 35 shows:

- `keep`    — yours. Protects a show from every cleanup rule. Set from the show page (`set_keep`)
              or in Sonarr/Radarr; LCARS never adds it, and removes it only when `purge` wins.
- `ongoing` — LCARS's. A show that is planned, watching or paused is protected while you are still
              on it; it comes off when the show is completed (also by automation). Episodic only.
- `purge`   — LCARS's. A dropped show goes to Maintainerr's clearing house. Purge wins over
              everything: it takes `keep` and `ongoing` off. Leaving dropped removes it again.

Best effort, like `sonarr_sync`: a failure is logged and recorded in service health and never
blocks the status change; the hourly `reconcile` heals whatever was missed or edited by hand.
"""

from __future__ import annotations

import contextlib
import logging
from collections import defaultdict

from lcars import config, radarr_client, service_health, sonarr_client

logger = logging.getLogger(__name__)

KEEP, ONGOING, PURGE = "keep", "ongoing", "purge"
MANAGED = (ONGOING, PURGE)
ONGOING_STATUSES = ("planned", "watching", "paused")
_ERRORS = (sonarr_client.SonarrError, radarr_client.RadarrError)


class ArrTagError(ValueError):
    """A keep request that cannot be honoured (the message says why)."""


def wanted(status: str | None, shape: str) -> set[str]:
    """The managed tags a show with this status should carry."""
    if status == "dropped":
        return {PURGE}
    if shape == "episodic" and status in ONGOING_STATUSES:
        return {ONGOING}
    return set()


def plan(current: set[str], status: str | None, shape: str) -> tuple[set[str], set[str]]:
    """(add, remove) turning the tags a series/movie carries into what its status asks for."""
    want = wanted(status, shape)
    add = want - current
    remove = (set(MANAGED) - want) & current
    if PURGE in want:
        remove |= {KEEP, ONGOING} & current  # purge wins over everything
    return add, remove


# ── one service ──────────────────────────────────────────────────────────────


class _Arr:
    def __init__(self, shape: str, client) -> None:
        self.shape, self.client = shape, client
        self._tags: dict[str, int] | None = None

    def label_ids(self) -> dict[str, int]:
        if self._tags is None:
            self._tags = {str(t["label"]).lower(): t["id"] for t in self.client.tags()}
        return self._tags

    def labels_of(self, item: dict) -> set[str]:
        by_id = {i: label for label, i in self.label_ids().items()}
        return {by_id[i] for i in item.get("tags") or [] if i in by_id}

    def tag_id(self, label: str, create: bool) -> int | None:
        found = self.label_ids().get(label)
        if found is not None or not create:
            return found
        made = self.client.create_tag(label)
        new_id = made.get("id") if isinstance(made, dict) else None
        if new_id is not None:
            self._tags[label] = new_id
        return new_id  # None while external writes are captured: the create is pending

    def item_for(self, key: str) -> dict | None:
        if self.shape == "episodic":
            return self.client.series_by_tvdb_id(int(key))
        return self.client.movie_by_tmdb_id(int(key))

    def items(self) -> dict[str, dict]:
        if self.shape == "episodic":
            return {str(s["tvdbId"]): s for s in self.client.all_series() if s.get("tvdbId")}
        return {str(m["tmdbId"]): m for m in self.client.all_movies() if m.get("tmdbId")}

    def edit(self, ids: list[int], tag_id: int, apply: str) -> None:
        self.client.edit_tags(ids, [tag_id], apply)


@contextlib.contextmanager
def _open(shape: str):
    """The service's client wrapped as `_Arr`, or None when it isn't configured."""
    cfg = config.get_current()
    if shape == "episodic":
        if not (cfg.sonarr_url and cfg.sonarr_api_key):
            yield None
            return
        with sonarr_client.SonarrClient(cfg.sonarr_url, cfg.sonarr_api_key) as client:
            yield _Arr(shape, client)
    else:
        if not (cfg.radarr_url and cfg.radarr_api_key):
            yield None
            return
        with radarr_client.RadarrClient(cfg.radarr_url, cfg.radarr_api_key) as client:
            yield _Arr(shape, client)


def _service(shape: str) -> str:
    return "sonarr" if shape == "episodic" else "radarr"


def _apply(arr: _Arr, edits: list[tuple[int, set[str], set[str]]]) -> dict:
    """`edits`: (series/movie id, labels to add, labels to remove). One editor call per label."""
    add_by, remove_by = defaultdict(list), defaultdict(list)
    for item_id, add, remove in edits:
        for label in add:
            add_by[label].append(item_id)
        for label in remove:
            remove_by[label].append(item_id)
    done = {"added": 0, "removed": 0}
    for label, ids in add_by.items():
        tag_id = arr.tag_id(label, create=True)
        if tag_id is not None:
            arr.edit(ids, tag_id, "add")
            done["added"] += len(ids)
    for label, ids in remove_by.items():
        tag_id = arr.tag_id(label, create=False)
        if tag_id is not None:
            arr.edit(ids, tag_id, "remove")
            done["removed"] += len(ids)
    return done


# ── which shows ──────────────────────────────────────────────────────────────


def _targets(conn, show_ids=None) -> dict[str, list[tuple[str, str, str]]]:
    """Tracked shows by shape: (show id, status, TVDB/TMDB key). A key two tracked shows share is
    ambiguous and left alone (R1.14 says it should not happen)."""
    sql = (
        "SELECT s.id, s.status, s.media_shape, x.external_id FROM show s"
        " JOIN show_external_id x ON x.show_id = s.id AND x.service ="
        " CASE s.media_shape WHEN 'movie' THEN 'tmdb' ELSE 'tvdb' END WHERE s.tracked = 1"
    )
    rows = conn.execute(sql).fetchall()
    seen: dict[tuple[str, str], int] = defaultdict(int)
    for r in rows:
        seen[(r[2], str(r[3]))] += 1
    wanted_ids = None if show_ids is None else set(show_ids)
    out: dict[str, list[tuple[str, str, str]]] = {"episodic": [], "movie": []}
    shared = sorted(key for key, n in seen.items() if n > 1)
    if shared:
        logger.warning("arr_tags: %d TVDB/TMDB id(s) shared by several tracked shows — those "
                       "shows are skipped: %s", len(shared),
                       ", ".join(f"{shape} {key}" for shape, key in shared[:20]))
    for r in rows:
        if seen[(r[2], str(r[3]))] > 1:
            continue
        if wanted_ids is None or r[0] in wanted_ids:
            out[r[2] if r[2] in out else "episodic"].append((r[0], r[1], str(r[3])))
    return out


# ── syncing ──────────────────────────────────────────────────────────────────


def sync_shows(conn, show_ids) -> dict:
    """Brings the tags of these shows in line with their status, now. Never raises."""
    stats = {"added": 0, "removed": 0, "not_in_arr": 0, "failed": False}
    show_ids = list(dict.fromkeys(show_ids))
    if not show_ids:
        return stats
    targets = _targets(conn, show_ids)
    for shape, rows in targets.items():
        if not rows:
            continue
        try:
            with _open(shape) as arr:
                if arr is None:
                    continue
                edits = []
                for _show_id, status, key in rows:
                    item = arr.item_for(key)
                    if item is None:
                        stats["not_in_arr"] += 1
                        continue
                    add, remove = plan(arr.labels_of(item), status, shape)
                    if add or remove:
                        edits.append((item["id"], add, remove))
                if edits:
                    done = _apply(arr, edits)
                    stats["added"] += done["added"]
                    stats["removed"] += done["removed"]
            service_health.record_success(conn, _service(shape))
        except _ERRORS as e:
            stats["failed"] = True
            logger.warning("arr_tags: %s tag sync failed: %s", _service(shape), e)
            service_health.record_failure(conn, _service(shape), str(e))
        except Exception:  # a side effect of a status change must never block it
            stats["failed"] = True
            logger.exception("arr_tags: %s tag sync broke", _service(shape))
    return stats


def reconcile(conn) -> dict:
    """Every tracked show against the whole Sonarr/Radarr library, one pass: heals a missed sync
    or a hand edit. Called by the hourly arr reconcile. Never raises."""
    stats = {"episodic": None, "movie": None}
    targets = _targets(conn)
    for shape, rows in targets.items():
        try:
            with _open(shape) as arr:
                if arr is None:
                    continue
                items = arr.items()
                edits = []
                for _show_id, status, key in rows:
                    item = items.get(key)
                    if item is None:
                        continue
                    add, remove = plan(arr.labels_of(item), status, shape)
                    if add or remove:
                        edits.append((item["id"], add, remove))
                stats[shape] = _apply(arr, edits) if edits else {"added": 0, "removed": 0}
                if edits:
                    logger.info("arr_tags: %s %s", _service(shape), stats[shape])
            service_health.record_success(conn, _service(shape))
        except _ERRORS as e:
            logger.warning("arr_tags: %s reconcile failed: %s", _service(shape), e)
            service_health.record_failure(conn, _service(shape), str(e))
        except Exception:  # one service's bug must not cost the other's pass
            logger.exception("arr_tags: %s reconcile broke", _service(shape))
    return stats


# ── keep (yours) ─────────────────────────────────────────────────────────────


def _show_target(conn, show_id: str) -> tuple[str, str, str]:
    row = conn.execute("SELECT status, media_shape, tracked FROM show WHERE id = ?",
                       (show_id,)).fetchone()
    if row is None:
        raise ArrTagError(f"no such show: {show_id}")
    shape = "movie" if row["media_shape"] == "movie" else "episodic"
    key = conn.execute(
        "SELECT external_id FROM show_external_id WHERE show_id = ? AND service = ?",
        (show_id, "tmdb" if shape == "movie" else "tvdb"),
    ).fetchone()
    if key is None:
        raise ArrTagError("the show has no TVDB/TMDB id, so it cannot be matched in "
                          + ("Radarr" if shape == "movie" else "Sonarr"))
    return row["status"], shape, str(key[0])


def keep_state(conn, show_id: str) -> bool | None:
    """Whether the show carries `keep`; None when it is not in Sonarr/Radarr or the service
    cannot be reached (a live read — only the show page asks)."""
    try:
        _status, shape, key = _show_target(conn, show_id)
        with _open(shape) as arr:
            item = None if arr is None else arr.item_for(key)
            return None if item is None else KEEP in arr.labels_of(item)
    except (ArrTagError, *_ERRORS):
        return None


def set_keep(conn, show_id: str, keep: bool) -> bool:
    """Adds or removes `keep` on the show's Sonarr series / Radarr movie. A dropped show is
    purged — purge wins over keep — so it cannot be kept until it is un-dropped."""
    status, shape, key = _show_target(conn, show_id)
    if keep and status == "dropped":
        raise ArrTagError("a dropped show is purged and purge wins over keep — un-drop it first")
    try:
        with _open(shape) as arr:
            if arr is None:
                raise ArrTagError(f"{_service(shape).capitalize()} is not configured")
            item = arr.item_for(key)
            if item is None:
                raise ArrTagError(f"the show is not in {_service(shape).capitalize()}")
            if (KEEP in arr.labels_of(item)) != keep:
                _apply(arr, [(item["id"], {KEEP} if keep else set(), set() if keep else {KEEP})])
        service_health.record_success(conn, _service(shape))
    except _ERRORS as e:
        service_health.record_failure(conn, _service(shape), str(e))
        raise ArrTagError(str(e)) from e
    return keep
