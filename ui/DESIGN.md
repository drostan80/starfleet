# Starfleet Web — Design & Build Plan

Personal anime/media tracker HTML client. Connects to the LCARS GraphQL server
(`~/repos/starfleet`) via bearer-token auth. Companion to the TUI client
(`~/repos/data`), eventually wrappable as a desktop/Android app.

---

## 1. Architecture

### LCARS connection

```
Browser  ──HTTP──►  LCARS GraphQL  POST /graphql
         ──WS───►   LCARS GraphQL  ws://.../graphql  (graphql-transport-ws)
```

- **HTTP endpoint**: `POST http://<lcars_host>/graphql`
  - Header: `Authorization: Bearer <token>`
  - Content-Type: `application/json`
- **WS endpoint**: `ws://<lcars_host>/graphql`
  - Same bearer token, passed as `Authorization` header during the WebSocket
    handshake — LCARS's `BearerTokenMiddleware` covers both `http` and
    `websocket` scope types (verified live, `server.py`)
  - Protocol: `graphql-transport-ws` (NOT the older `graphql-ws`)
  - Subscriptions: `episodeAvailabilityChanged` and `showCreated`

### Config (Settings page)
The web client needs, at minimum:
- `lcars_url` — base URL, e.g. `http://192.168.0.152:8888`
  (same field `data`'s `Config.lcars_url` uses)
- `lcars_token` — the bearer token (same static token LCARS validates)
- `home_server_host` — IP/host for rewriting Sonarr/Radarr deep links to
  reach them from the browser (`data`'s `Config.home_server_host`,
  default `192.168.0.152`)
- **Future**: Tailscale IP as alternative to LAN IP (user selects in Settings)
- **Future**: player type (local mpv / web player / android)
- **Future**: file path routing (local mirror / SMB / remote IP)

Store in `localStorage` under a single `starfleet_config` key (JSON).
Phase 1: simple form in Settings, no validation theatre.

### Video playback (Phase 1 only)
Local mpv. Pass `filePathSonarr` / `filePathRadarr` from the episode to mpv
via a custom URI scheme (`mpv://`) or a small local helper daemon.
Decision deferred until building the play action — document approach here
when settled.

---

## 2. Pages & build order

### Phase 1 — ship together as the first working thing

#### Settings (skeleton)
- Functional form: `lcars_url`, `lcars_token`, `home_server_host`
- Saves to `localStorage`; page reloads use stored values
- UI is lowest priority — a plain `<form>` is fine for Phase 1
- Must exist before anything else works (auth requirement)

#### Calendar
Full design: see `mockups/calendar.html` and §4 below.
Key GraphQL query: `episodesInRange(start, end)` — returns episodes in a
date window with their show, season, availability, file paths.

### Phase 2 — after calendar is solid

#### Grabs
Port of TUI's G screen. Two columns (Sonarr / Radarr), 20 events per page,
paging with `[` / `]`, click row to launch player.
GraphQL: `recentGrabs(service, page, pageSize)` — already exists, returns
`GrabEvent { showTitle episodeTitle season episode airDate filePath }`.

#### Reviews
Port of TUI's review screen. Shows `pendingReviews` queue.
GraphQL: `pendingReviews(includeResolved: false)` + `resolvePendingReview`
mutation + `setSeasonScore` for score-type reviews.

#### Backlog
"Available to watch" — episodes ready (`availableViaSonarr = AVAILABLE` or
`availableViaRadarr = AVAILABLE`) but unwatched, on watching-status shows.
GraphQL: `backlog` — exact query for this already exists in LCARS (§8).

### Phase 3 — own design session before touching code

#### Lists (Shows)
Big work. Library browser over ~2000+ shows. Needs filtering, sorting,
virtual/paginated scrolling. Filterable search within the page.
GraphQL: `shows` (paginated), `showsByStatus`, `filterPresets`,
`filterPreset`. See §3 for key Show fields.

---

## 3. Key GraphQL fields reference

### Calendar card — `episodesInRange`
```graphql
query CalendarRange($start: DateTime!, $end: DateTime!) {
  episodesInRange(start: $start, end: $end, first: 500) {
    edges { node {
      id
      season episode absoluteNumber
      airDateUtc
      availableViaSonarr availableViaRadarr availableLocally
      filePathSonarr filePathRadarr
      show {
        id displayTitle status score mediaShape trackingSpace
        posterUrl
        externalIds(first: 20) { edges { node { service url } } }
        seasons(first: 1) {          # current season only, for status tab
          edges { node { id seasonNumber score } }
        }
      }
    }}
  }
}
```

### Show card / detail — `show(id)`
Key fields on `Show`:
- `id`, `displayTitle`, `displayTitleOverride`
- `titleRomaji`, `titleEnglish`, `titleNative`, `synonyms`
- `status` — `ShowStatus` enum: `WATCHING | COMPLETED | PLANNING | PAUSED | DROPPED`
- `score` — Float, 0–20 quarter-point scale (LCARS native, **not** AniList's 10-pt)
- `mediaShape` — `EPISODIC | MOVIE`
- `trackingSpace` — `ANIME | NON_ANIME`
- `posterUrl` — AniList CDN URL for cover art
- `bannerUrl`
- `externalIds` — service deep links (AniList/MAL/TVDB/TMDB/Sonarr/Radarr)
  - `service` values: `"anilist"`, `"mal"`, `"sonarr"`, `"radarr"`, `"tvdb"`, `"tmdb"`
  - `service: "sonarr:add"` / `"radarr:add"` — synthetic add-new links (not yet followed)
- `seasons` — each has `seasonNumber`, `score`, `startedAt`, `completedAt`,
  `absStart`, `absEnd` (nullable Int — set for Bookworm-style ranged seasons)

### Backlog — `backlog`
Returns `EpisodeConnection` — same `Episode` shape as `episodesInRange`.
Definition: unwatched-but-available episodes on watching-status shows (§8 LCARS).

### Service icon routing
From `externalIds.service` → icon + URL:
| service       | icon     | colour         | opens                     |
|---------------|----------|----------------|---------------------------|
| `anilist`     | AL logo  | `#e85d04`      | AniList show page         |
| `mal`         | MAL logo | `#2e51a2`      | MAL anime page            |
| `sonarr`      | S logo   | `#35c5f4`      | Sonarr series page        |
| `radarr`      | R logo   | `#ffc230`      | Radarr movie page         |
| `sonarr:add`  | S logo   | dim            | Sonarr add-new search     |
| `radarr:add`  | R logo   | dim            | Radarr add-new search     |
Player icon (▶): coloured when `availableLocally = true`, dim otherwise.

Deep link host rewriting: swap the host in Sonarr/Radarr URLs for
`home_server_host` (same logic as `data`'s `links.py:rewrite_host`).

### Key mutations
```graphql
# Mark episode watched
mutation { addWatchEvent(showId, season, episode, watchedAt) { id } }
# Remove watch
mutation { deleteWatchEvent(watchEventId) { id } }
# Change show status
mutation { setStatus(showId, status, confirmed) { id status } }
# Score
mutation { setScore(showId, score) { id score } }
mutation { setSeasonScore(seasonId, score) { id score } }
# Resolve review
mutation { resolvePendingReview(id, resolvedWith) { id } }
```

### Subscriptions (live updates)
```graphql
subscription { episodeAvailabilityChanged { id availableViaSonarr availableViaRadarr show { id } } }
subscription { showCreated { id displayTitle } }
```
Connect once on app load; on any event, re-fetch the current view's data
(same approach as `data`'s `_trigger_lcars_window_refresh`).

---

## 4. Calendar screen — design spec

Reference: `mockups/calendar.html` (approved 2026-08-28).
Live mockup: https://claude.ai/code/artifact/403f6611-6811-4eee-a2ac-9cf2c22b6542

### View modes
Today | 1D | 3D | Week — controls the `start`/`end` passed to `episodesInRange`.
`Today` = current day 00:00–23:59 local. `Week` = Mon–Sun.
Previous / Next buttons shift the window by the current view duration.

### Day grouping
Episodes grouped by `airDateUtc` converted to local time.
Day header: large `Syne` day name (Mon/Tue…) + `DM Mono` date (25 Aug 2026).
Today's header: accent blue (`--accent: #5b8cff`).

### Card anatomy (per episode)
```
┌──────────────────────────────┐
│ S02E08  ·  17:00 JST  ·  ▶  │  ← meta bar (24px)
├───┬──────────────────────┬───┤
│AL │                      │ ▌ │  ← left: service icons (20px strip)
│M  │   [cover art]        │   │     right: status tab (6px → expands)
│S  │   posterUrl image    │   │
│R  │                      │   │
│▶  │   ★ 19.5 (score)     │   │
├───┴──────────────────────┴───┤
│ Show Title                   │  ← footer
│ 8 / 8 / 28                   │    avail / aired / total
└──────────────────────────────┘
```

**Meta bar icons** (availability state of this episode):
- `▶` green  — `availableViaSonarr = AVAILABLE` (or Radarr for movies)
- `⬇` amber  — `DOWNLOADING` (grabbed, not yet imported)
- `◷` gray   — `UNAVAILABLE` / not yet aired

**Mark-as-watched button** — rightmost element in the meta bar, shown only for
episodes that have already aired (`airDateUtc` in the past, i.e. avail-ready or
avail-wait states — not for avail-future):
- Unwatched: dim circle-check outline (`○` or `✓` outline, `--muted` colour)
- Watched: filled green check (✓, `--av-ready` / `--st-watching` colour)
- Click → `addWatchEvent(showId, season, episode, watchedAt: now)` mutation
- On success: toggle to watched state; optimistic UI is fine
- Watched state is not returned by `episodesInRange` directly — track locally
  after a successful mutation, or add `watchEvents { id }` to the query to
  pre-populate state on load
- Button is 18×18px, `border-radius: 50%`, sits between `avail-icon` and the
  right edge of the meta bar

**Left service strip** — from `show.externalIds`:
- Full colour = linked and reachable; dim (opacity 0.18) = not linked
- Only show services relevant to `mediaShape` (no Sonarr for movies, no Radarr for series typically)
- Player icon (▶) full colour when `availableLocally = true`
- Click → open URL in new tab (after `rewrite_host` for Sonarr/Radarr)

**Status tab** — right edge:
- Collapsed: 6px coloured strip (current status colour)
- Expanded on hover: dropdown listing all statuses; current highlighted
- **Future refinement**: make it a small bookmark-style tab (~40px tall,
  centred vertically) that expands on click, not hover
- Status colours:
  - Watching   `#22c55e`
  - Completed  `#3b82f6`
  - Planning   `#f59e0b`
  - Paused     `#f97316`
  - Dropped    `#ef4444`

**Cover art**: `show.posterUrl` (AniList CDN). Gradient placeholder while loading.
**Score**: `show.score` in LCARS scale (0–20), shown bottom-left of art.
**Episode counter**: `available / aired / total` in `DM Mono`.
  - `available` = count of episodes where `availableViaSonarr = AVAILABLE`
    (or Radarr for movies) in this show's current season
  - `aired` = episodes with `airDateUtc` in the past
  - `total` = `show.totalEpisodes` (or season episode count if available)

**Card max per row**: 10. Card width ≈ 150px. Grid: `flex-wrap`.
**Card size**: maximize art height — make card height a Settings option
eventually. Phase 1: fixed at ~240px total (200px art + bars).

---

## 5. Design tokens

```css
/* Palette — dark-first (this is a night-use media app) */
--bg:        #0d0f14;   /* cool near-black page ground */
--surface:   #141720;   /* card ground */
--surface-2: #1b2030;   /* nav, raised panels, service strip */
--surface-3: #222840;   /* hover, selected states */
--border:    #202540;   /* hairline separators */
--accent:    #5b8cff;   /* nav active, links, today header */
--accent-d:  #2a3f80;   /* accent button fill */
--text:      #dde2f0;   /* cool off-white */
--muted:     #6b7494;   /* secondary labels */
--faint:     #363d5a;   /* very quiet elements */

/* Status (semantic — separate from accent) */
--st-watching:  #22c55e;
--st-completed: #3b82f6;
--st-planning:  #f59e0b;
--st-paused:    #f97316;
--st-dropped:   #ef4444;

/* Services */
--svc-al:     #e85d04;
--svc-mal:    #2e51a2;
--svc-sonarr: #35c5f4;
--svc-radarr: #ffc230;
--svc-player: #22c55e;

/* Availability */
--av-ready:  #22c55e;
--av-wait:   #f59e0b;
--av-future: #6b7494;
```

```css
/* Typography */
/* Display / day headers */  font-family: 'Syne', sans-serif; font-weight: 800;
/* UI / card text / body */  font-family: 'DM Sans', system-ui, sans-serif;
/* Data / counters / times */font-family: 'DM Mono', monospace;

/* Google Fonts load */
/* Syne:wght@600;700;800  +  DM+Sans:ital,wght@0,300;0,400;0,500;0,600;1,400  +  DM+Mono:wght@400;500 */
```

Light theme tokens (defined, secondary — swap only the neutrals):
```css
--bg: #eef0f8; --surface: #ffffff; --surface-2: #e2e5f2;
--surface-3: #d4d8ec; --border: #c8cde4;
--text: #1a1e30; --muted: #5a6080; --faint: #b0b8d0;
```

---

## 6. Global search — `/` shortcut

Independent of the Lists page search/filters. Triggered by `/` on any page.
Opens a modal or inline top-nav input; user types a show name; results come
from `search(query: String!)` → `ShowConnection`.
Selecting a result navigates to the show's detail page.
Build when show pages exist (Phase 2+).

---

## 7. What NOT to build in Phase 1

- Lists page (own design session first; big work)
- Global `/` search (needs show pages)
- Web player (Phase 1 is local mpv only)
- Android wrapping
- Multi-player Settings UI (build settings incrementally as players are added)
- Tailscale IP selector in Settings

---

## 8. Related repos

| repo                        | what it is                              |
|-----------------------------|-----------------------------------------|
| `~/repos/starfleet`         | LCARS GraphQL server (source of truth)  |
| `~/repos/data`              | TUI client (Python/Textual)             |
| `~/repos/starfleet-archive` | Full build history, old plans           |

Key files in `~/repos/starfleet`:
- `src/lcars/schema.graphql` — full GraphQL schema
- `src/lcars/server.py` — ASGI app, auth middleware, WS config
- `src/lcars/resolvers.py` — all resolver implementations
- `src/lcars/config.py` — `LcarsConfig` fields (server-side config)

Key files in `~/repos/data`:
- `src/data/lcars_client.py` — every GraphQL call the TUI makes (reference for query shapes)
- `src/data/config.py` — `Config` fields (client-side config, incl. `lcars_url`, `home_server_host`)
- `src/data/links.py` — `rewrite_host` (Sonarr/Radarr URL host swap logic)
- `src/data/show_detail_screen.py` — show detail, season/episode display, mark-watched
- `src/data/grabs_screen.py` — grabs screen (direct port reference)
- `src/data/review_screen.py` — review screen (direct port reference)
