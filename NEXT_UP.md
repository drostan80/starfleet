# Next up

Current version: **v0.2.24** (deployed 2026-09-17).
List-page fix committed on main (`14fe145`), not yet released.
Full build history archived to `~/repos/starfleet-archive`.

---

## Ongoing (background)

- [ ] **Rotate keys** at cutover: Sonarr/Radarr API keys, MAL `client_id`, LCARS's own
      AniList `client_secret`. All intentionally live until Data is 100% to the user's
      liking — no fixed date.
- [ ] **Move secrets** out of plaintext `config.ini`. Deferred, reminder only.
- [ ] **Score sync paused**: AniList/MAL per-entry vs LCARS show-level scoring mismatch
      needs redesign before re-enabling. Code intact in `score_sync.py`.
- [ ] **AniList metadata fallback is still scalar-only**: `_fetch_mal_fallback` fills
      poster/synopsis/duration but skips relations, studios, characters, genres. Sequel
      detection (W1) now works without AniList, but other metadata paths still degrade.
- [ ] **Franchise function deferred**: SEQUEL/PREQUEL edges are show-level season chains,
      not true cross-media franchises. Needs broader definition covering TV+movies+anime.

---

## Small fixes — shipped 2026-09-19, not yet released

- [x] **Browse sequel dialog: "It isn't — add as new show"** — third button in
      `confirmSequelAttach` (`browse.js`), calls `addShow` directly (already
      AniList+MAL only on this path, no field stripping needed).
- [x] **`fetchShowArt`: show-level AniList fallback when no season rows exist**
      (`metadata.py`) — mirrors the existing MAL show-level block, stores with
      `seasonId = NULL`.
- [x] **Fuzzy guard on Sonarr/Radarr title search** — both sites guarded:
      backend `_enrich_tvdb_via_sonarr`/`_best_matching_result` (`browse.py`,
      title + alternate titles via `difflib`) and frontend `bestMatchingCandidate`
      (`browse.js`, title-only Sørensen–Dice — GraphQL's `ArrCandidate` doesn't
      expose alternate titles). A non-matching top result now behaves like "no
      results" instead of silently attaching a wrong ID.

---

## ID correction + Sonarr/Radarr ↔ LCARS sync — shipped 2026-09-19, not yet released

- [x] **Edit every external ID on the show page** — AniList/MAL/TVDB/TMDB/IMDB
      were already editable (right-click badge → `openExtEditor`, easy to miss
      but present). Added: AniDB (dedicated form → `linkAniDb`, since it also
      seeds the TVDB-season/episode-offset `anime_list_entry` mapping the generic
      editor can't), Syoboi, TVmaze (generic editor, removed from `READONLY_SVCS`).
- [x] **Edit IDs from a review** — `franchise_season_collision` reviews (the
      actual gap; `anilist_id`/`_conflict`/`cross_service_merge` already had
      inline editors in `reviews.html`) now get a parent/child comparison panel
      with Confirm (season #) / Reject (+ optional corrected TVDB/AniList ID),
      wired to `resolveFranchiseMerge` (added to `api.js`, wasn't called from
      the frontend at all before).
- [x] **Sonarr/Radarr → LCARS**:
      a) Webhooks: `SeriesAdd`/`MovieAdded` create a tracked, PLANNED show (same
         shape as the reconcile auto-create below); `SeriesDelete`/`MovieDelete`
         unlink the `sonarr`/`radarr` deep-link row and open a review — never
         raises on an unrecognized/malformed payload (Sonarr/Radarr's webhook-
         connection Test requirement).
      b) `reconcileArrState` mutation (new, `local_audit.py`) folded into Ops's
         existing availability loop (no filesystem walk, unlike `auditLocalFiles`,
         so — unlike that one — this rides the automatic loop): untracked-show
         auto-create, availability correction (existing logic, `walk_orphans=False`),
         and `monitored` ↔ status reconcile both directions. A service that fails
         to connect contributes nothing that tick — a blip must never read as
         "pause the whole library." Resume target is remembered
         (`show.status_before_pause`, migration `038b4fbb1ec7`) rather than
         hardcoded to WATCHING, and only ever fires when `status_before_pause`
         is actually set — a show paused/dropped before this feature existed
         (NULL on every pre-migration row) is left alone even if Sonarr still
         reports `monitored=true`, rather than getting silently reactivated
         (with a real AniList/MAL push) on the very first tick. Closed a real
         pre-existing gap along the way: `_apply_status_change` (extracted from
         `resolve_set_status`) now re-monitors in Sonarr/Radarr on leaving
         paused/dropped (`_remonitor_in_arr_on_resume` — update-only, no
         add-if-missing; deliberately not `shows.ensure_arr_monitored`, whose
         add branch would re-add and full-search a show missing from Sonarr for
         an unrelated reason) — without this, resuming in LCARS left Sonarr/
         Radarr still unmonitored and the next reconcile tick would've silently
         paused it right back. Manual trigger: `ops reconcile-arr-state` (same
         mutation the automatic loop calls — useful for a first look before
         turning the loop loose on a real library).
      c) Fixing a wrong link *in Sonarr* (delete wrong series, add right one) is
         now genuinely enough — LCARS follows via (a)/(b).

---

## mpv → LCARS watched status — shipped 2026-09-19, not yet released

- [x] Every `launchMpv()` call site (`calendar.js` x5 including the service-strip
      player icon, `show.js` x3 — one of which, `buildAnidbEpRow`'s mpv button, had
      a real pre-existing arg-order bug, `launchMpv(cfg, epFilePath)`, fixed in
      passing — and `grabs.html`, the one site with no `showId` in scope until
      `GrabEvent.showId` shipped alongside it, see below) now passes
      `{showId, season, episode}` through.
- [x] `launchMpv(filePath, cfg, ctx)`: `/play` POST body gains `showId`, `season`,
      `episode`, `token: cfg.lcars_token`, `lcarsBase: cfg.lcars_url` (not
      `location.origin` as originally sketched — `cfg.lcars_url` is what the
      function already uses to build the media URL itself, and is correct even
      when the web client and LCARS aren't same-origin).
- [x] `mpv-helper.py`: `--input-ipc-server=<per-launch-socket>` on the mpv launch;
      a background thread observes `percent-pos`, tracks the session peak, and on
      normal exit with peak >= 90% (same threshold as the Android VLC client)
      POSTs `addWatchEvent(showId, season, episode, platform: "mpv-helper")`
      straight to LCARS with the bearer token. Best-effort throughout — IPC
      connect/POST failure just logs, playback is never affected. A launch
      superseded by a newer one (single-instance policy, already existed) is
      matched by an incrementing launch id, not just process-exit — so a killed
      episode never reports watched even if it had already crossed 90%.
      Smoke-tested standalone (fake IPC socket + fake LCARS endpoint), not part
      of the pytest suite — mpv-helper.py runs on the user's own client machine,
      not inside LCARS.
- [x] `GrabEvent.showId` (schema + resolver) — `_grab_file_paths_{sonarr,radarr}`
      already resolved `show_id` internally for the filePath lookup, just never
      returned it; needed so the Grabs page's launch site could report watched
      status too. Backend piece already committed.

---

## On-air indicator (calendar/backlog availability icon) — shipped 2026-09-19, not yet released

- [x] Fifth `availState()` value, **airing**: broadcast window open right now
      (`airDateUtc` through `airDateUtc + runtime`), client-only overlay — no
      schema change, no new `AvailabilityStatus` enum value. `isEpisodeAiringNow()`
      computes the window client-side each render (never a cached server field),
      falling back to a conservative default runtime when neither
      `episode.runtimeMinutes` nor `show.durationMinutes` is known, so an unknown
      runtime doesn't silently suppress the indicator for the whole broadcast day.
- [x] `runtimeMinutes`/`durationMinutes` added to `episodesInRange`'s query (not to
      `backlog`'s — backlog is `available_locally = 1` by definition, so `airing`
      can never apply there; narrower than the original sketch, deliberately).
      `show.js`'s episode rows thread `show.durationMinutes` through too.
- [x] Icon: filled circle (●), blue (`--av-airing`), 2s opacity pulse — distinct
      from ready (▶ green)/downloading (⬇ amber)/missing (⬇ red)/future (◷ gray).
      Wired into both calendar-card and planner-card icon rendering.

---

## Ideas / future

### Android app (Capacitor wrapper)

Full plan in `ui/DESIGN.md` §8. Remote WebView (loads UI from nginx, not bundled).
Native Kotlin plugins for VLC playback, file download, WebSocket subscriptions,
and background auto-download. Four phases: A1 (shell + VLC), A2 (download),
A3 (live updates), A4 (auto-download + storage management).

### Unified list page

Fold Lists, Backlog, Grabs, Browse, and Add into one page with preset filters/views.
List page works now (fix committed, pending release) but the unified redesign is the long-term direction.

### Discover page (extension of Add page)

Passive import: ops loop pre-creating untracked stubs from upcoming AniList/TMDB seasons.
User browses in "Upcoming", promotes to PLANNING or dismisses as SKIP. Nice-to-have.

### Statistics page

Episode/show counts by status, watch history over time, score distribution, genre
breakdown, total runtime. Needs date-bucketed watch-event aggregates from LCARS.

### External ID season-level display

Show season-specific AniList/MAL IDs in the external ID bar (not just show-level).
Wire during season table rework. Currently clicking "AL" on S2 goes to the S1 page.

### Memory Alpha — browse prefill + add-confirmation UI

`propagate_cross_ids` runs every tick (built). Future:
- Browse prefill: use Memory Alpha IDs to prefill service links on cards before adding.
- Add-confirmation popup: show each discovered ID for validation before committing.

### Smaller ideas

- [ ] Check if list page cover art could load faster from TMDB or TVDB.
- [ ] IMDB datasets for cross-referencing and fallback ID bridging.
- [ ] Browse / filter by studio (`show.studio` gets an id-prefix).
- [ ] Direct TVDB search (use TVDB API instead of through Sonarr).
- [ ] livechart.me headlines for delay/reschedule news on tracked shows.
- [ ] Grabs screen: show only title+episode/film title, add mpv launch key.
