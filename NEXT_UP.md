# Next up

Current version: **v0.2.5** (deployed 2026-09-08, under testing).
Full build history archived to `~/repos/starfleet-archive`.

---

## Recently shipped — still under testing (Sep 2–8)

### v0.2.6 (2026-09-08, not yet deployed)

- **Retire AniList air_date_utc review**: `_reconcile_air_dates` no longer opens
  `pending_review` for routine AniList air-date changes — silently writes for
  unprotected sources, skips for protected ones (manual/syoboi/animeschedule).
  The Sonarr+available+delay guard ("Draw This, Then Die!" bug) is kept.
- **TVmaze poster art**: `store_tvmaze_art()` in `art.py` extracts `image.medium`
  and `image.original` from the TVmaze show lookup during `drip_fetch_episodes`
  (no extra API call). Poster only — TVmaze has no banner. IMDB has no free
  image API, skipped.

### v0.2.5 (2026-09-08, not yet deployed)

- **Airdate source priority chain** (`airdate_priority.py`): single source of truth
  for which automatic source wins. Order: manual > syoboi > animeschedule > anilist >
  sonarr > anidb > tvmaze. All automatic writers now consult this.
- **animeschedule demoted to backup**: `animeschedule.py` now skips episodes whose
  `air_date_source` outranks animeschedule (manual, syoboi). Reverses B.5's earlier
  decision that animeschedule overrides manual.
- **Syoboi change-driven sync**: replaces new-shows-only incremental fetch with
  `proginfo.xml` RSS pulse + `ProgLookup` with `LastUpdate` range. Catches
  reschedules on existing shows, not just new TIDs. Sync cursor derived from
  `MAX(last_update)` in `syoboi_program`, backed off 5 minutes.
- **Rewire air_date_change rows**: `rewire_airdates` now records `air_date_change`
  rows before bulk UPDATE (preserves schedule-change signal for future calendar
  "*new schedule time" annotations).

### v0.2.4 (2026-09-07)

- **Art denormalisation**: `select_asset`/`deselect_asset`/`auto_select_best` write
  `banner_url`/`poster_url` back to the `show` row so calendar/list resolvers get
  correct art without an `art_asset` JOIN.
- **Auto-monitor on season map**: `setSeasonMapping` calls `ensure_arr_monitored` so
  new seasons are picked up by Sonarr/Radarr automatically.
- **Sequel confirmation dialog**: CSS for the confirm overlay (`.sequel-confirm-*`).
- **Add page opens with "not in LCARS" selected** by default.
- **Tests**: 8 new tests for art denormalisation (`tests/test_art.py`).

### v0.2.3 (2026-09-06) — browse enrichment pipeline

- **Sonarr title search** as final TVDB enrichment pass (when Fribb + relation walk
  fail, search Sonarr by title to resolve TVDB ID).
- **TVDB recovery for sequels** via LCARS `show_relation` walk — one-hop sequel/prequel
  edges yield the related show's TVDB ID.
- **Fribb MAL index** now includes entries without `tvdb_id` (broader coverage).
- **Fribb MAL index for browse fallback** + iframe preview pane on browse cards.
- **Browse cards enriched** with cross-database IDs (TVDB, IMDB, TMDB) from
  `show_external_id`.
- **Browse card service links** + sequel attach UI (link a sequel to its parent from
  the browse card).
- **Sequel detection** works regardless of stub state + AniList fallback when relation
  data is missing locally.

### v0.2.2 (2026-09-05) — source failover + UI polish

- **AniList→MAL source failover**: when AniList API is down, `_fetch_mal_fallback`
  fills poster/synopsis/duration from MAL's public API. NULL-only writes, never
  overwrites existing AniList data.
- **Browse + search fallback**: seasonal browse falls back to MAL's
  `GET /v2/anime/season/{year}/{season}`; `searchAniList` falls back to
  `mal_client.search_anime`. Source field (`ANILIST`/`MAL`) lets the web client
  show a degraded-service banner.
- **AniList search** in season mapping editor — search button prefilled with show
  title, results carry `idMal` so both AL + MAL IDs fill from one pick.
- **Art picker extraction** to shared `art-picker.js` module; planner 🖼 button opens
  full picker; images at natural aspect ratio.
- **Calendar UI**: day column layout, planner view toggle, 1-day step navigation in
  week/3-day views, planner service links + episode titles + play overlay.
- **App name**: runtime-derived from hostname (`STARFLEET` in production, `TEST SHUTTLE`
  on localhost).
- `watchedEpisodeCount` / `availableEpisodeCount` on `Show` type for web client
  progress display.

### v0.2.0–v0.2.1 (2026-09-05)

- **Amend external IDs**: `amendShowArrLink` mutation + show page ⇄ button to correct
  wrong TVDB/TMDB IDs (deletes old Sonarr/Radarr entry, adds correct one, updates
  LCARS link).
- **Status flower picker** extracted to `status-picker.js`; used on calendar, show page,
  and browse (6 petals with SKIP).

### v0.1.99–v0.1.100 (2026-09-04) — browse + SKIP

- **TV & Movies browse** via TMDB Discover API (Add → Browse → Anime / TV & Movies /
  TV / Movies tabs). Month navigation, status filters, "Load more" pagination. TMDB
  ID backfill: 1207 shows resolved (577 → 1784 tmdb rows).
- **SKIP status**: lightweight tombstone for dismissed shows. `skipShow` mutation
  (`tracked=0`, no metadata fetch, no Sonarr/Radarr, no AniList/MAL push). Browse
  cross-reference shows SKIPPED, not NOT_IN_LCARS.
- **Download episode locally**: browser-native download via nginx `/download/` location
  with Content-Disposition + Accept-Ranges. Download button on episode rows.
  Downloads page with localStorage history (100 entries, retry/purge).
- **mpv playback fixes**: un-gated `/files/` auth, failure detection.

### v0.1.79–v0.1.82 (2026-09-03) — web client in Docker + auth

- **Web client baked into Docker images**: `starfleet:<ver>` serves via LCARS at `/ui/`,
  `starfleet:<ver>-web` serves via nginx. No separate deploy step.
- **Login/password gate**: cookie-based session auth (nginx `auth_request` → LCARS
  `/auth/check`), bcrypt passwords, server-side shared settings (`web_setting` table),
  CLI user management.
- **splitSeason mutation + web UI** (S6): generic season subdivision tool, ✂ button on
  season headers.
- **Subdivision conflict suppression**: shared-TVDB shows no longer generate false
  positive ID-conflict reviews on reconcile.

### v0.1.77 (2026-09-02) — season validation + sequel reviews

- **Sequel-season review queue**: auto-detects new AniList seasons via SEQUEL relation
  edges, opens `pending_review` suggesting the user add the mapping.
- `showByExternalId` query for tracked-show lookup by service ID.
- `relation_type` on `show_relation` (SEQUEL, PREQUEL, etc.).
- Season-number gap validation + `setSeasonStatus` completion guard.
- `setStatus` fanout fix: only stomps highest season; `_try_complete_season` respects
  PAUSED/DROPPED.
- New seasons default to `planned` status instead of NULL.
- `addShowWithArr` unmonitored flag for PAUSED adds.

---

## Outstanding

- [ ] **B.5.3a — Scoped reconcile**: target a single show instead of sweeping the full
      AniList list every time. Good-to-have, not urgent.

---

## Ongoing (background)

- [ ] **Rotate keys** at cutover: Sonarr/Radarr API keys, MAL `client_id`, LCARS's own
      AniList `client_secret`. All intentionally live until Data is 100% to the user's
      liking — no fixed date.
- [ ] **Move secrets** out of plaintext `config.ini`. Deferred, reminder only.

---

## Ideas / future

### Memory Alpha (v0.3 / v1 scope)

Authoritative cross-source episode mapping layer with AniDB as the ordering authority.

**Data sources (researched 2026-09-07):**

| Source | What it gives | Cost |
|--------|---------------|------|
| Fribb JSON (have it) | AniList → AniDB ID for ~1,036/1,151 tracked anime (90%) | free, cached weekly |
| Anime-Lists XML (ScudLee) | AniDB → TVDB season + episode offset + per-episode overrides, 10,736 entries, 1,427 with episode-level mappings | free GitHub download |
| AniDB titles dump | AniDB ID → titles in all languages (romaji, English, Japanese, etc.) | free, daily |
| AniList / MAL (have it) | Episode titles (romaji + English) | already fetched |
| AniDB HTTP API | Full episode data (titles in all langs, airdates, lengths) — needs registered client, 1 req/2s | only for new shows going forward |
| Wikidata SPARQL | TVDB↔TMDB↔IMDB bridge for TV series — 46,842 entries, 94% have all 3 IDs. Backbone for TV/movie side | free, cached weekly, ~12 MB |
| TVmaze API | TV episode airdates + titles, lookup by TVDB/IMDB, carries cross-IDs | free, no API key, CC BY-SA 4.0, 20 req/10s |
| ARM (kawaiioverflow) | AniList↔MAL↔Annict↔Syoboi bridge, 36,819 entries, 6,821 with Syoboi TIDs | free GitHub download, ~2 MB, cached weekly |
| Syoboi Calendar (しょぼいカレンダー) | Anime broadcast events with JST minute-accurate times, 288 channels, per-episode subtitles | free XML API, ~106k events for 946 TIDs |

**Build steps:**

1. ✅ **Seed AniDB IDs from Fribb** → 1,037 `show_external_id` rows.
2. ✅ **Download + cache datasets** — Anime-Lists XML (10,736 entries) + AniDB titles
   dump (~17k anime, ~100k title rows). Weekly refresh.
3. ✅ **Episode offset derivation** — `_SeasonResolver` applies three priority levels
   (individual episode maps > range-based mapping overrides > default entry offset) to
   compute AniDB absolute episode numbers from TVDB data. Handles multi-entry TVDB
   shows (different seasons, offset-partitioned ranges), skips ambiguous cases.
   Carries `anidb_season` (0=special, 1=regular) to distinguish episode namespaces.
   First-write-wins collision detection on episode maps.
4. ✅ **`episode_anidb_mapping` table** — 3,640 regular + 11 special episodes mapped
   across 970 shows (87.1% of tracked anime regular episodes). 456 unmapped (mostly
   unlinked later seasons or NULL-default-season entries like Urusei Yatsura).
   1,051 mismatches between Sonarr absolute numbers and AniDB-per-entry numbers are
   expected, not bugs — Sonarr counts show-wide while AniDB counts per-anime-entry.
   Mismatch flagging deliberately not built: the signal is dominated by this numbering
   difference and would produce ~1,000 junk reviews as-is.
5. ✅ **Title search + suggest + manual link** — GraphQL API for finding and linking
   AniDB IDs when Fribb/Anime-Lists miss. Source='manual' entries.
6. ✅ **Wire into ops loop** — `pollMemoryAlpha` mutation: refreshes datasets if stale
   (weekly), re-derives mappings on data change, drip-fetches AniDB episode data
   (5 shows/tick, ~10s), fills episode.title gaps from AniDB English titles.
7. ✅ **Episode titles from AniDB API** — `anidb_episode` table stores per-episode
   JP/romaji/English titles, airdates, lengths. Keyed AniDB-side
   `(anidb_anime_id, anidb_season, anidb_epno)` so data is fetched once per anime.
   Drip backfill via `pollMemoryAlpha` (5 shows/tick at 1 req/2s). 269 shows
   remaining at build time. `fill_title_gaps` patches NULL `episode.title` from
   AniDB English titles through the mapping join (same NULL-only guard as Sonarr).
8. ✅ **Register AniDB client** — `client=memalpha&clientver=1` (HTTP API,
   registered 2026-09-07). 1 req/2s rate limit.
9. ✅ **UI: AniDB ordering view** — show page toggle between broadcast order and
   AniDB absolute order. Separate render path sorted by `(anidb_season, anidb_epno)`,
   regulars block → specials block → unmapped (greyed). JA/romaji subtitles shown
   beneath English titles. Broadcast reference (S×E×) shown as secondary badge.
   `AnidbEpisodeMapping` GraphQL type on `Episode` carries all AniDB data.
10. ✅ **Cross-database ID propagation (full graph)** — `propagate_cross_ids` fills
    missing `show_external_id` rows via a 4-phase pipeline:
    - Phase 1: TVDB→AniDB (anime_list_entry reverse, never-guess on ambiguity)
    - Phase 2a: AniList→MAL/TMDB/IMDB (Fribb, anilist-keyed)
    - Phase 2b: AniDB→AniList/MAL/TMDB/IMDB (Fribb anidb_index, alongside 2a)
    - Phase 3: anime-lists→TVDB/TMDB/IMDB
    - Phase 4: TMDB/IMDB→TVDB (Wikidata bridge, non-anime)
    Sonarr-add scenario works end-to-end: TVDB → AniDB → all other DBs (same tick).
    TVmaze closes the loop via drip-fetch on next tick.
    Strictly insert-only (never overwrites manual corrections).
    Coverage: AniList 100%, MAL 99%, AniDB 91%, TMDB 89%, IMDB 88%, TVDB 85%.
    Heals missing IDs; does NOT correct disagreements (needs review lifecycle rework).
11. ✅ **Wikidata TV bridge** — `wikidata.py` downloads SPARQL dump of 46,842 TV
    series with TVDB↔TMDB↔IMDB IDs. Cached weekly like Fribb. `propagate_cross_ids`
    extended for TV/movies: TMDB→TVDB and IMDB→TVDB lookups via Wikidata indexes.
    First run: +49 TVDB IDs for non-anime shows (199→150 gap remaining).
12. ✅ **TVmaze drip-fetch** — `tvmaze.py` looks up shows by IMDB/TVDB, fetches all
    episodes, stores in `tvmaze_episode` table. 5 shows/tick at 0.5s pacing.
    Auto-discovers TVmaze show IDs, backfills missing TVDB/IMDB from TVmaze.
    `tvmaze_episode` table: PK (tvmaze_show_id, season, episode), stores title,
    airdate, airtime, runtime. Tombstone pattern prevents re-fetching failed lookups.
13. ✅ **Airdate gap fill** — `fill_airdate_gaps_anidb()` and `tvmaze.fill_airdate_gaps()`
    fill NULL `episode.air_date_utc` from AniDB (anime) and TVmaze (TV/movies).
    NULL-only writes, stamps `air_date_source = 'anidb'/'tvmaze'`. Sonarr/AniList/
    animeschedule airdates are never overwritten (lower priority).
    Target: ~10,300 TV episodes + ~15 anime episodes missing airdates.
    First manual test: NCIS:LA → 289 airdates filled from TVmaze.
14. ✅ **Wired into ops loop** — `pollMemoryAlpha` now runs 8 steps per tick:
    (1) refresh datasets if stale (Anime-Lists + AniDB titles + Wikidata + ARM),
    (2) propagate cross-IDs (Fribb + Wikidata), (2b) seed Syoboi TIDs from ARM,
    (3) AniDB drip-fetch, (4) fill title gaps, (5) TVmaze drip-fetch,
    (6) Syoboi incremental fetch (new shows), (7) fill airdate gaps
    (AniDB + TVmaze + Syoboi).
15. ✅ **ARM dataset + Syoboi Calendar integration** — `arm.py` caches ARM JSON
    (weekly, 36,819 entries). `syoboi.py` fetches broadcast events from Syoboi
    Calendar XML API (batch multi-TID, 50/request, 2s pacing, 429 retry).
    `syoboi_program` table: 106,061 broadcast events across 946 TIDs.
    `syoboi_title` table: 971 titles with Japanese episode subtitles.
    974 Syoboi TIDs seeded via ARM (84.6% of tracked anime).
    Airdate fill: NULL-only, restricted to episodes whose `anidb_anime_id`
    matches the show's primary AniDB external_id (prevents multi-entry
    numbering collisions where S2 E1's `anidb_epno=1` would match S1's
    broadcast). 0 episodes filled (all previous fills were collisions,
    reverted). Parallel store — does NOT overwrite existing airdates yet.
    **Divergence measured (corrected)**: 2,336 episodes joinable (primary
    entry only), 437 (18.7%) diverge by >30 minutes from existing dates.
    By source: Sonarr 75.4% exact / 20.0% diverge, AniList 59.4% exact /
    16.9% diverge. ✅ **Rewiring shipped** (v0.2.4 step 8 in ops loop).
    animeschedule demoted to backup (v0.2.5, `airdate_priority.py`).
16. ✅ **Change-driven sync** (v0.2.5) — `proginfo.xml` RSS pulse as
    lightweight "something changed" trigger, then `ProgLookup` with
    `LastUpdate` range fetches only changed rows. Replaces new-shows-only
    incremental. Catches reschedules on shows already in `syoboi_program`.

**Budget:** Steps 1–5 required **zero AniDB API calls**. The free dumps + Anime-Lists
cover ordering for the full backlog. The API drip-fetches episode titles/dates for
linked shows at 5/tick (~10s per tick), covering the 269-show backlog over ~54 ticks.

**Migration strategy (settled 2026-09-06):** bottom-up. Episode rows as first-class
entities, linked alongside existing schema. Flip direction of truth once proven.

### Discover page (extension of Add page)

- **Passive import**: Ops loop pre-creating untracked stubs from upcoming AniList/TMDB
  seasons. User browses in "Upcoming", promotes to PLANNING or dismisses as SKIP.
  Nice-to-have, not blocking anything.

### Statistics page (web client)

Episode/show counts by status, watch history over time, score distribution, genre
breakdown, total runtime. Needs date-bucketed watch-event aggregates from LCARS.

### Browse / filter by studio

`show.studio` gets an id-prefix like other entities for proper browse-by-studio.

### ~~Better calendar schedule / news sync~~ (superseded)

✅ Syoboi Calendar is now the authoritative anime schedule source (v0.2.5,
`airdate_priority.py`). animeschedule demoted to backup. The v3 API is no
longer needed — Syoboi provides minute-accurate JST broadcast times directly.
- livechart.me headlines for delay/reschedule news on tracked shows — still a
  possible nice-to-have, parked.

### Android app (Capacitor wrapper)

Wrap the existing web client; native Kotlin plugins only for what the web can't do.
- **VLC intent plugin** for local playback.
- **Local download** via Capacitor Filesystem from nginx `/download/` endpoint.
- **Auto-download new episodes** via WorkManager (Wi-Fi constraint, local notification).
- **Storage management**: max storage limit, auto-delete watched episodes.

### Smaller ideas

- [ ] Check if list page cover art could load faster from TMDB or TVDB (or split the
      work between both).
- [ ] **IMDB datasets**: cross-referencing and fallback ID bridging (AniDB ordering
      research done — see Memory Alpha above).

### Notes as we are building
- ✅ TVmaze poster art added (v0.2.6, `store_tvmaze_art`). IMDB has no free image API — skipped.
- make sure we include correct timezone with the source airdate 
- make sure to add the link to all the db as we have ids, new icons to be sourced and checked
- some ui improvement to make on planner view, including (not limited to) show / season poster not cropped, better organisation location font colour and size / visibility of data in the detail pane as well as rework on how the banner is displayed within
- ✅ Anime airdate sources settled (v0.2.5): Syoboi Calendar (JST minute-accurate) as
  source of truth, animeschedule demoted to backup, full priority chain in
  `airdate_priority.py`. Change-driven sync via `proginfo.xml` + `LastUpdate` built.
  TV airdates from TVmaze already integrated (v0.2.4).
  Reference doc: Downloads/japanese_anime_airdate_syoboi_integration.md
- ✅ AniList air_date_utc pending_review retired (v0.2.6). Sonarr+available+delay guard kept (the "Draw This, Then Die!" bug). Future: calendar annotation for schedule changes (*new schedule time, *no episode this week) — to be defined.
### Episode-first cross-database identity (schema completion)

Building toward: franchise → show → season → episode, where episode is the atomic
source of truth. Each episode carries per-source coordinates (AniList S2E14 vs TVDB
S2pt2E2); seasons are grouping conveniences with per-source labels.

1. ✅ **Schema migration** (2026-09-12): `season.part_number` + `season.label`,
   `season_external_id` with `name`/`url`/open-ended `service`/TEXT `external_id`,
   new `episode_external_id` table (PK `episode_id, service`). GraphQL types +
   resolvers wired. All 1,165 tests pass.
2. ✅ **TV season rows + episode.season_id backfill** (2026-09-12): `ensure_all_season_rows`
   + `backfill_episode_season_id` in `season_ranges.py`, wired into ops loop step 2c.
   +861 season rows (TV direct, anime via reconcile_season), +10,211 episodes linked.
   Season 0 excluded by design. Double-insert guard on anime fallback. 11 tests.
3. ⏳ **Seed episode_external_id**: from `episode_anidb_mapping` (3,640 rows), TVDB
   (sonarr_season/episode), AniList/MAL (from season external IDs).
4. ⏳ **Backfill season_external_id.name**: pull names from Fribb/AniDB/MAL/AniList.
5. ⏳ **abs_start/abs_end ranges**: from AniDB mappings (anime) + episode counts (TV).
6. ⏳ **Franchise auto-seed**: walk show_relation SEQUEL/PREQUEL → 115 groups.
7. ⏳ **GraphQL resolvers + UI**: wire the new data into the show page.

