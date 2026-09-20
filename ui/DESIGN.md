# Starfleet Web — Design & Build Plan

Personal anime/media tracker HTML client. Connects to the LCARS GraphQL server
(`~/repos/starfleet`) via bearer-token auth. Companion to the TUI client
(`~/repos/data`), eventually wrappable as a desktop/Android app.

---

## 1. Architecture

### LCARS connection

```
Browser  ──HTTP──►  nginx :8888  ──►  LCARS GraphQL  POST /
         ──WS───►   nginx :8888  ──►  LCARS GraphQL  ws://.../  (graphql-transport-ws)
```

- **HTTP endpoint**: `POST http://<nginx_host>:8888/` (Ariadne app is mounted
  at `/`, `server.py:211` — there is no `/graphql` path)
  - Header: `Authorization: Bearer <token>`
  - Content-Type: `application/json`
- **WS endpoint**: `ws://<nginx_host>:8888/` (same mount; needs the nginx WS
  passthrough from §8 before it works through the proxy)
  - Same bearer token, passed as `Authorization` header during the WebSocket
    handshake — LCARS's `BearerTokenMiddleware` covers both `http` and
    `websocket` scope types (verified live, `server.py`)
  - Protocol: `graphql-transport-ws` (NOT the older `graphql-ws`)
  - Subscriptions: `episodeAvailabilityChanged` and `showCreated`

### Config (Settings page)
**Target (A0, §8): zero entry.** Open the server address, log in — that is
the whole setup on any browser or phone. `lcars_url` and `home_server_host`
are derived from `location`; `lcars_token` and `tmdb_api_key` are served by
`/auth/settings` from LCARS config after login and never displayed. Only
`mpv_helper_url` remains a (defaulted, desktop-only) local setting.

Until A0 ships, the four values are typed once into Settings and shared via
`web_setting`. localStorage key: `starfleet_config`.

### Video playback (Phase 1 only)
**Settled and built**: a small local helper daemon (`ui/mpv-helper.py`),
not a custom URI scheme. The client strips the `/data` prefix from
`filePathSonarr`/`filePathRadarr`, builds a `/files/...` URL, and POSTs it
to the helper's `/play` endpoint; the helper launches mpv against that URL
(streamed, no local mount needed). Since 2026-09-19 the POST also carries
`{showId, season, episode, token, lcarsBase}` when known — the helper
watches playback via mpv's own JSON IPC socket and reports `addWatchEvent`
back to LCARS at ≥90% watched, the same threshold §8's Android plan uses
for VLC. See `launchMpv()` in `calendar.js` and `ui/mpv-helper.py` itself.

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
- Multi-player Settings UI (build settings incrementally as players are added)
- Tailscale IP selector in Settings

---

## 8. Android app (Capacitor)

### Architecture: remote WebView

The Capacitor app loads the UI remotely from the nginx host (`server.url`),
not bundled into the APK. This preserves:

- All root-relative URLs (`/download/...`, `/auth/...`, `/ui/...`) work unchanged
- Cookie-based session auth works unchanged (WebView cookie jar)
- No build step for UI — deploy a new LCARS version, the app picks it up
- LAN vs Tailscale: just a different IP, everything else identical

Native Kotlin plugins handle only what the WebView can't do.

### Build ordering — stop after A0

A0 (server + web client) is fully testable today, on desktop, without a
phone or the Android toolchain (build steps 9–12 below, plus the WS/VLC
checks added to "Facts verified" below). A1's own step 14 spike (dynamic
`server.url` from SharedPreferences) can only be answered on a device with
Android Studio installed — not available yet ("deployment will be when I
have access to a machine for test").

Everything A2–A4 builds native plugins on top of the shell A1 produces. If
the step-14 spike fails and falls back to the bundled-launcher-page
approach (`server.allowNavigation` — see Android hurdles below), the origin
every later plugin assumes could shift. Writing A2–A4 Kotlin against an
unproven A1 shell risks throwing that work away.

**Rule: build A0, ship it, verify it (including the WS/VLC checks below) —
then stop. Do not start A1's Capacitor scaffolding until the test machine
is available and step 14 has actually run.**

### Zero-config principle

**The only app setting is the server address** (LAN IP or Tailscale IP, port
8888 = nginx). Everything else is automatic:

1. App opens → "Enter server address" screen
2. Address stored in SharedPreferences
3. WebView loads `http://<address>:8888/ui/` → login page
4. User logs in → session cookie set → `/auth/settings` syncs `lcars_token`
   and `tmdb_api_key` into localStorage
5. A Capacitor bridge call copies `lcars_token` into SharedPreferences for
   native code (A3/A4)

No settings page in the app. A static app shortcut (long-press icon →
"Change server") re-shows the address screen. **Two addresses = two origins =
two logins** (separate cookie jars and localStorage per origin) — accepted.

### Facts verified against the code (2026-09-18), re-confirmed live (2026-09-19)

Confirmed against the real deployment (`192.168.0.152:8888`), not just read
from source:

- Port **8888 is nginx**, not LCARS. `lcars_url` in `web_setting` is the nginx
  origin. LCARS direct is `localhost:8123` on tiny and is never needed.
- `/` unauthenticated → `302` to `/ui/login.html`. `/` with a bogus
  `Authorization: Bearer` header and no cookie → still `302` — confirms
  bearer is genuinely not accepted yet, not just unread/stale code.
  `/auth/check` bare → `401`. `/ui/` unauthenticated → `302`. `/files/` →
  `403` (no index, autoindex off) but reachable with no login redirect —
  confirms it really is ungated, matching `nginx.conf`.
- nginx `/` proxies to LCARS but gates on `auth_request` → LCARS
  `/auth/check`, which reads **only the session cookie** (`auth.py:151`).
- GraphQL HTTP **and** WebSocket are both mounted at `/` (`server.py:211`),
  not `/graphql`. §1 above is stale on this.
- `BearerTokenMiddleware` covers the `websocket` scope, so a native WS client
  that sets `Authorization: Bearer` on the handshake is accepted by LCARS —
  confirmed in code; the rest of the WS path is not (see below).
- Session cookie: `sf_session`, 30 days, sliding renewal, `httponly`,
  `samesite=strict`, no `secure` (plain HTTP).
- `/download/` is cookie-gated with hard 401.

**Read from code, not yet run — verify before building on them:**

- nginx `/` has no `Upgrade`/`Connection` headers and a 60s default read
  timeout — WS through nginx does not work today. Once A0 adds the headers,
  the *whole* path (nginx upgrade → LCARS WS → `graphql-transport-ws`
  subprotocol negotiation → an actual subscription payload) still needs a
  real end-to-end test — `BearerTokenMiddleware` accepting the scope doesn't
  prove the rest of the chain holds. Testable today from desktop with
  `websocat`/a Python client against the deployed nginx the moment A0
  ships — **do this before writing `LcarsWsPlugin.kt` (A3)**, not after.
- VLC's seek behavior over HTTP with encoded non-ASCII/space path segments
  against `/files/` (`Accept-Ranges` is set, but the specific
  Range-request-plus-encoding combination is the plan's own flagged 404
  risk — see Android hurdles below). **Partially confirmed live 2026-09-19**:
  path resolution + URL-decoding through the nginx `alias` was verified
  against two real show directories on the live server (percent-encoded
  spaces, apostrophe, parens, curly braces, tildes, and fullwidth
  `＜＜`/`＞＞` all resolved correctly — each directory returned `403`
  forbidden-listing rather than `404`, confirming nginx found the real
  path on disk). Could not reach a specific *file* to test an actual
  `Range:` request against — one candidate directory was empty, the other's
  episode filename couldn't be guessed from Sonarr-convention patterns and
  wasn't otherwise available. **Still open: an actual `Range: bytes=...`
  request against a real file**, ungated and testable any time `/files/`
  is live (no A0 dependency) — **do this before `VlcPlugin.kt` (A1 step
  18)** if a sample file path turns up before then; otherwise this is now
  the fallback plan: ship A1 on the strength of the path-resolution
  evidence above, and revisit this specific check first if real-device VLC
  playback fails (user's own call, 2026-09-19).

### Server-side changes (all small)

| Change | Where | Why |
|--------|-------|-----|
| `/auth/check` also accepts `Authorization: Bearer <token>` (compare_digest against the LCARS bearer) | `auth.py` | native code authenticates with bearer only, no cookie plumbing |
| `/auth/settings` GET returns `lcars_token` and `tmdb_api_key` **from LCARS config** (`config.py`), not from `web_setting`; PUT no longer accepts them | `auth.py` | nobody types a token anywhere; LCARS already owns both |
| `SHARED_SETTING_KEYS` emptied of `lcars_url`, `lcars_token`, `home_server_host`, `tmdb_api_key` (table stays for future shared settings) | `auth.py` | all four are derived or served now |
| `/_auth_check` forwards `Authorization` header | `nginx.conf` | so the subrequest sees the bearer |
| `/` location: `proxy_http_version 1.1`, `Upgrade`/`Connection "upgrade"` headers, `proxy_read_timeout 3600s` | `nginx.conf` | WebSocket passthrough |

**Decision (user, 2026-09-19):** `/auth/check` is one shared `auth_request`
target for `/`, `/download/`, and `/ui/` — accepting bearer there widens all
three, not just the GraphQL endpoint native code actually needs. Accepted as
one shared gate rather than splitting into a bearer-only variant scoped to
`/` — simpler, and acceptable on the current LAN-only plain-HTTP deployment
with no external exposure.

Direction (user, 2026-09-18): every API token lives in LCARS config/env and is
used seamlessly where needed, the way `ops` already does — never entered or
displayed in a client. A0 does this for the four keys the web client needs;
the rest of the web app follows in a later rework.

### Web client changes

**A0 — zero-entry client.** Setup on any browser or phone becomes: open the
server address, log in. Nothing else.

| Setting | Today | After A0 |
|---------|-------|----------|
| `lcars_url` | typed once | **derived**: `location.origin` |
| `lcars_token` | typed once, shared via `web_setting` | **served**: `/auth/settings` from LCARS config |
| `home_server_host` | typed once | **derived**: `location.hostname` (Sonarr/Radarr are on the nginx host, so this is right on LAN *and* Tailscale — today it's wrong on Tailscale) |
| `tmdb_api_key` | typed once | **served**: `/auth/settings` from LCARS config |
| `mpv_helper_url` | localStorage, defaults `localhost:19450` | unchanged — desktop-only default |

Why derived and not stored: after a Tailscale login, `syncConfig()` makes the
server-stored LAN value win, so every `/files/` URL and Sonarr link points at
the wrong network.

| File | Change |
|------|--------|
| `api.js:28` | `fetch('/', …)` instead of `${cfg.lcars_url}/` |
| `calendar.js:458`, `grabs.html:455` | `${location.origin}/files${mediaPath}` (mpv helper needs an absolute URL) |
| `calendar.js:540` (+ every `rewriteHost` caller) | `rewriteHost(url, location.hostname)` |
| `config.js` | `SHARED_KEYS` → `['lcars_token','tmdb_api_key']`; `requireConfig` needs only `lcars_token`; `syncConfig` stays (it is how the token arrives); `getConfig` must also **actively delete** any stored `lcars_url`/`home_server_host` on load, not just stop writing them — see upgrade hazard below |
| `settings.html` | only `mpv_helper_url` remains; token and key are never shown |
| `login.html` | remove the "import existing settings" block |

**Upgrade hazard:** an already-logged-in browser has `lcars_url`/
`home_server_host` in localStorage from before A0 (`syncConfig()`'s own
pre-A0 behavior). If `getConfig` still prefers a stored value over the
derived one after A0 ships, that browser keeps pointing at the old LAN IP
post-upgrade — reproducing the exact Tailscale bug A0 exists to fix, as an
upgrade artifact. `getConfig` must delete both keys from localStorage on
load, not merely stop writing them going forward.

Bearer header stays on `api.js` — nginx passes it through, LCARS validates it.

**Platform detection**: `window.Capacitor?.isNativePlatform?.()` — must be
optional-chained, `window.Capacitor` is undefined on desktop.

### Android hurdles (known up front)

- **Cleartext HTTP**: `android:usesCleartextTraffic="true"` in the manifest
  is the mechanism that actually matters — checked against real
  @capacitor/android 8.5.2 source (2026-09-19): `server.cleartext` in
  `capacitor.config.json` has no code path that reads it at all in this
  version (only a separate, irrelevant Cordova-compat manifest generator
  does). Set the manifest flag directly; the config key is a no-op here.
- **Dynamic `server.url`**: Capacitor reads it from static JSON by default.
  **Spike passed (2026-09-19)**: building a `CapConfig` in `MainActivity`
  and setting it on the `config` field before calling `super.onCreate()`
  works — confirmed against real source (`BridgeActivity.onCreate()` calls
  `load()`, which reads `config`, at the very end of its own
  `super.onCreate()` chain) and live on a real device against the real
  deployed server. The bundled-launcher-page fallback was not needed.
  One real gotcha found the same way: overriding `onCreate()` to redirect
  before ever calling `super.onCreate()` crashes immediately with
  `SuperNotCalledException` — override `load()` instead (see
  `MainActivity.kt`) so the framework's own `onCreate()` chain still runs,
  and the bridge/WebView still never spins up when redirecting away.
- **URL encoding**: media paths contain spaces and Japanese; `Uri.encode()`
  each path segment (not the whole path in one call) or VLC 404s — verified
  against a real deployed file with spaces/parens/braces/brackets: a
  `Range:` request through the live nginx came back `206` with a correct
  `Content-Range`, and VLC actually streamed and played it end to end.
- **Package visibility (Android 11+)**: `Intent.resolveActivity()` can't see
  VLC even when it's genuinely installed unless the manifest declares
  `<queries><package android:name="org.videolan.vlc" /></queries>` —
  found by testing `VlcPlugin.play()` for real, not by reading the code
  back: it rejected with "VLC not installed" despite `adb shell pm list
  packages` showing it present, until this was added.
- **File visibility**: on Android 11+ other apps can't read
  `/Android/data/<pkg>/`. Hand files to VLC as `content://` via `FileProvider`
  with `FLAG_GRANT_READ_URI_PERMISSION`. No storage permissions needed.
- **Long downloads**: use the system `DownloadManager`, not Capacitor
  `Filesystem.downloadFile` (dies when the WebView is backgrounded).
  DownloadManager gives background, resume, Wi-Fi-only, and notifications for
  free, and A4 enqueues the same requests.
- **Watched on return**: VLC returns `extra_position`/`extra_duration` only
  when launched with `startActivityForResult`. ≥90% → watched.

### Native plugins

#### A1 — VLC intent (playback)

URL = `http://<server>:8888/files` + `filePath` with `/data` stripped and
segments encoded. Intent: `org.videolan.vlc`, `ACTION_VIEW`, type `video/*`,
extra `title`, launched with `startActivityForResult`. ~30 lines of Kotlin.
Web side: play button calls `VlcPlugin.play({path, title})` on Android,
`launchMpv` on desktop.

#### A2 — Manual download (DownloadManager)

`DownloadPlugin.download({path, title})` → `DownloadManager.Request` on the
`/files/` URL (ungated) → `setDestinationInExternalFilesDir`. Local index in
SQLite (`@capacitor-community/sqlite`): `(episode_id, file_path, size_bytes,
downloaded_at, watched, dm_id)`. Broadcast receiver on `ACTION_DOWNLOAD_COMPLETE`
updates the index. Offline playback: VlcPlugin takes a `content://` URI when
the episode is in the index.

#### A3 — WebSocket subscriptions

`LcarsWsPlugin` wraps OkHttp WebSocket → `ws://<server>:8888/` with
`Sec-WebSocket-Protocol: graphql-transport-ws` and `Authorization: Bearer`.
Subscribes to `episodeAvailabilityChanged` + `showCreated`, forwards via
`notifyListeners`. JS replaces the 30s poll with a listener. Reconnect on
network change (`ConnectivityManager` callback) with backoff.

#### A4 — Auto-download + storage management

`AutoDownloadWorker` (WorkManager, periodic 30 min, `NetworkType.UNMETERED`):
`backlog` query via OkHttp POST to `http://<server>:8888/` with bearer → diff
against SQLite index → enqueue DownloadManager requests → notification per
completion. Storage limit (second and last app setting): delete watched
first (oldest), then oldest unwatched until under limit.

**Eviction source of truth (user, 2026-09-19): local SQLite wins.** VLC's
result (A1's `onActivityResult`) does two independent things the moment
playback crosses ≥90%: writes `watched=true` to the local SQLite index
(always succeeds, immediate — this is what eviction reads) and fires
`addWatchEvent` to LCARS (a network POST, may fail). Eviction never waits on
the POST succeeding — an offline or failed `addWatchEvent` must not cause a
just-watched episode to re-download on the next `AutoDownloadWorker` tick. A
retry queue re-sends failed `addWatchEvent` calls separately so the server
eventually converges with the local state, decoupled from eviction timing.

`LcarsClient.kt` (shared A3/A4): OkHttp, server address + bearer from
SharedPreferences, raw GraphQL strings, `backlog` + `addWatchEvent` only.

### Build steps

**Step 0 — toolchain (nothing installed yet)**
1. Install Android Studio (bundles SDK + JDK 17); accept SDK licences
2. Install Node LTS (for Capacitor CLI)
3. Phone: enable Developer options + USB debugging; install VLC
4. Verify `adb devices` sees the phone

**Release sequencing:** A0's `settings.html`/`login.html` reduction (step 8
below) ships in its own release, not bundled with any Capacitor/Kotlin
scaffolding — if the derived-config change misbehaves on an existing
browser (see the upgrade hazard above), the revert needs to be one small
commit, not entangled with unrelated native-shell work that hasn't even
been spiked yet.

**A0 — server + web client prep** (ship as a normal LCARS release)
5. `auth.py`: `/auth/check` accepts bearer; `/auth/settings` serves
   `lcars_token` + `tmdb_api_key` from LCARS config; `SHARED_SETTING_KEYS`
   emptied of the four keys
6. `nginx.conf`: forward `Authorization` in `/_auth_check`; WS headers +
   timeout on `/`
7. `api.js` → `fetch('/')`; `calendar.js` + `grabs.html` → `location.origin`;
   all `rewriteHost` callers → `location.hostname`
8. `config.js`: `SHARED_KEYS` + `requireConfig` reduced; `settings.html` →
   mpv helper only; `login.html` → drop settings import
9. Test on desktop:
   - Fresh browser profile → login → calendar, Sonarr link, TMDB poster
     fallback, mpv play, download — with nothing typed in Settings
   - **Existing browser profile with pre-A0 stored settings** (old
     `lcars_url`/`home_server_host` sitting in localStorage) → login →
     confirm `getConfig` uses the derived values, not the stale stored
     ones (see upgrade hazard above)
10. Test curl: `-H "Authorization: Bearer …"` with no cookie against `/` returns
    GraphQL, not a 302
11. Release + deploy
12. **WS end-to-end verification** — before writing any A3 code: test with
    `websocat`/a Python `graphql-transport-ws` client against the deployed
    nginx (bearer + subscribe to `episodeAvailabilityChanged`). Confirms the
    whole chain (nginx upgrade headers → LCARS WS → subprotocol negotiation
    → a real payload), not just that `BearerTokenMiddleware` accepts the
    scope.

---
**STOP HERE until the test machine is available.** The steps below assume
A1's own step-14 spike has been run — see "Build ordering" above.

---

**A1 — shell + VLC**
13. ✅ `npm init` in `ui/`, `npx cap init`, `npx cap add android`,
    Capacitor 8.5.2 (current stable at build time — this doc's original
    "6" was stale). Kotlin wired into Gradle by hand (the template
    defaults to Java).
14. ✅ **Spike passed** (2026-09-19) — see "Android hurdles" above.
15. ✅ `ServerSetupActivity.kt`: one text field, saves address, launches
    `MainActivity`; shown when no address stored
16. ✅ Static app shortcut "Change server" → `ServerSetupActivity`
    (non-exported target activity — `LauncherApps` resolves app-declared
    shortcuts without needing `exported=true`; user-confirmed live
    long-press → shortcut → pre-filled screen).
17. ✅ **VLC Range+encoding test** (2026-09-19) — found a real file via
    GraphQL (`backlog` query), built its per-segment-encoded `/files/` URL,
    confirmed `206 Partial Content` + correct `Content-Range` through the
    live nginx.
18. ✅ `VlcPlugin.kt` with per-segment path encoding + `startActivityForResult`
    (Capacitor's `@ActivityCallback` mechanism, not the deprecated raw
    Activity API). Needed a `<queries>` manifest entry for Android 11+
    package visibility — see "Android hurdles" above. Verified live: called
    from the real WebView bridge over Chrome DevTools Protocol, VLC
    actually streamed and played a real episode from the deployed server,
    returned position/duration matched the on-screen player exactly.
19. ✅ `TokenPlugin.kt`: JS calls `setToken(lcars_token)` after `syncConfig` →
    SharedPreferences. Wired centrally inside `syncConfig()` itself
    (`config.js`), not duplicated at each of the six pages that call it.
20. ✅ Web: platform-gated play button — `launchMpv()` (`calendar.js`, and
    `grabs.html`'s own copy) branches on
    `window.Capacitor?.isNativePlatform?.()`. Not yet exercised through a
    real deployed web client (native half verified directly via CDP,
    bypassing the UI) — needs its own release to close the loop.
21. Test LAN: enter IP, login, stream to VLC. Test Tailscale: change server,
    login again, stream
22. Generate the release keystore (`keytool -genkey`), wire `signingConfigs`
    in `build.gradle`; keystore + password in `.gitignore`; `assembleRelease`
    → APK

**A2 — download**
23. ✅ Index schema — plain `SQLiteOpenHelper` (`DownloadDatabase.kt`), not
    `@capacitor-community/sqlite` as originally sketched: the actual
    surface needed (list/check-one/mark-watched/delete-one) is small and
    fixed, all exposed as ordinary `@PluginMethod`s, so a third-party
    native dependency (bundling SQLCipher/requery) for one hobby app's
    download list wasn't worth it.
24. ✅ `DownloadPlugin.kt`: `DownloadManager` request (via `MediaUrl.build()`,
    factored out and shared with `VlcPlugin`) + completion receiver +
    index update, keyed by GraphQL episode id (not `DownloadManager`'s own
    transient `dm_id`).
    **Real bug found by testing**: the completion receiver was registered
    `RECEIVER_NOT_EXPORTED`, which silently blocked delivery —
    `DownloadManager` is a system service under a different UID, so this
    broadcast is inherently cross-UID and needs `RECEIVER_EXPORTED`.
    Confirmed both the failure and the fix live (a manually-fired
    `adb shell am broadcast` targeted at the exact package never arrived
    either, under `NOT_EXPORTED`); safe to widen since `onReceive`
    re-queries `DownloadManager` by id rather than trusting the broadcast.
25. ✅ `FileProvider` — needed a new `<external-files-path>` entry in
    `file_paths.xml` (the Capacitor template's own `external-path` entry
    maps to a different directory than `setDestinationInExternalFilesDir`
    actually writes to). `VlcPlugin.play()` gained a `uri` param for local
    (offline) playback alongside the existing `path` (remote stream).
26. ✅ Web: platform-gated download button (`downloads.js` branches every
    export on `isNativePlatform()`) + `downloads.html` reads the native
    index, with a "Play" action replacing "Retry" for a completed Android
    download. Syntax-checked; not yet exercised through a real deployed
    web client (native half verified directly via CDP) — batched with
    steps 19-20's same open item for the next release.
27. ✅ Tested on a real device: downloaded a real ~190MB file over LAN
    (byte-exact size match to the source's `Content-Length`), backgrounded
    the app mid-download via the Home button and confirmed both that
    `DownloadManager` kept going and that the index still updated
    correctly on foreground return, played the result back fully offline
    (WiFi disabled) via `VlcPlugin`'s new `uri` path, and confirmed
    `deleteAllDownloads()` removes both the real files and the index rows.

**A3 — live updates**
28. ✅ `LcarsClient.kt` — shared `OkHttpClient` + server address/bearer
    reader only; `backlog()`/`addWatchEvent()` deferred to A4, whenever
    that phase actually needs them.
29. ✅ `LcarsWsPlugin.kt`: connect, `connection_init`, subscribe,
    `notifyListeners`. Bearer goes on the WS handshake's Authorization
    header, not inside `connection_init`'s payload — same mechanism A0
    step 12 already proved from Python, now from Kotlin. OkHttp pinned to
    4.12.0 (latest 5.5.0 needs compileSdk 37, one more than this project's
    36).
30. ✅ Reconnect on connectivity change — `ConnectivityManager
    .registerDefaultNetworkCallback`, exponential backoff (2s→60s cap).
31. ✅ Web: `calendar.js`'s 10s poll now only runs on non-Android
    platforms; Android registers `LcarsWs` listeners instead.
32. ✅ Tested live against the real deployed server (temporary logging,
    added and removed for the check): `onOpen` negotiated the
    `graphql-transport-ws` subprotocol, `connection_ack` arrived, both
    subscribes accepted with no error frame. Reconnect genuinely
    confirmed too — disabling WiFi produced `onFailure` + doubling
    backoff (2s/4s/8s/16s observed), re-enabling led to a real second
    `connection_ack`. Did not force an actual Sonarr grab through
    production just to see a subscription payload — same call as A0 step
    12's own WS check; the mechanism is what's being verified, not a
    live event.

**A4 — auto-download + storage**
33. ✅ `AutoDownloadWorker.kt`: `CoroutineWorker`, `enqueueUniquePeriodicWork`
    + `KEEP` (30min, `NetworkType.UNMETERED` by default — see A2's Wi-Fi-only
    setting), diffs `LcarsClient.fetchBacklog()` against the local index and
    enqueues anything new via `DownloadPlugin`'s shared `enqueue()`.
    Found by testing, not designed in: an ad-hoc one-time
    `OneTimeWorkRequestBuilder` trigger added alongside the periodic work
    (purely to force a run for testing) raced the periodic work's own first
    execution and double-enqueued every episode — confirmed live, every
    file downloaded twice with DownloadManager auto-renaming the second
    copy `-1`. Removed the one-time trigger entirely; the periodic work
    alone doesn't have this problem, since WorkManager itself serializes
    a single uniquely-named periodic work. Re-verified after the fix with
    the app's own SQLite index (PRIMARY KEY on `episode_id`, so a real
    duplicate row is structurally impossible) showing exactly one row per
    episode after a forced first run; leftover `-1` files seen once more
    after that were traced to orphaned `DownloadManager` jobs from the
    *earlier* buggy test run, not the current code — `pm clear` on our own
    app doesn't cancel jobs already enqueued in the separate
    `com.android.providers.downloads` system service.
34. Notification channel — skipped as unnecessary: `DownloadManager.Request`
    already sets `VISIBILITY_VISIBLE_NOTIFY_COMPLETED` (A2), which covers
    per-download notifications regardless of what enqueues the request, so
    a second/duplicate notification channel would add nothing.
35. ✅ Storage limit (GB) field added to `ServerSetupActivity`, same
    immediate-save-on-edit pattern as A2's Wi-Fi-only checkbox. Blank/0 =
    unlimited.
36. ✅ Eviction: `compareByDescending { watched }.thenBy { downloadedAt }`
    ordering (watched-oldest-first, then unwatched-oldest), reading only
    the local SQLite `watched` flag. Found by testing: eviction must
    soft-delete (`markEvicted` — sets `status='evicted'`, nulls
    `local_uri`/`size_bytes`, keeps the row) rather than hard-delete —
    a hard `DELETE` let the very same tick's backlog-diff step see the
    episode as "new" again and immediately re-enqueue it, an infinite
    evict/redownload loop confirmed live before the fix. Also found:
    `DownloadManager.remove()` doesn't reliably delete the underlying file
    under `setDestinationInExternalFilesDir` (confirmed — file remained on
    disk after `remove()` on a completed download), so
    `cancelAndDeleteFile()` now also deletes the file directly.
37. ✅ `VlcPlugin`'s `onActivityResult` now reports watched status natively:
    writes `watched=1` to SQLite immediately (always succeeds, what step
    36's eviction reads), resolves it to `showId`/`season`/`episode` via
    the download row if not already passed, then fires
    `LcarsClient.addWatchEvent()` on a background thread — a network POST,
    independently retried via `WatchEventRetryQueue` (SharedPreferences-
    backed, flushed at the start of every worker tick) if it fails.
38. ✅ Duplicate-download bug found, root-caused, and fixed (see step 33),
    verified via the local SQLite index (structurally duplicate-proof —
    `PRIMARY KEY` on `episode_id`) after a forced first run. WorkManager
    correctly refused a second forced early run — the periodic-work
    serialization guarantee itself is confirmed solid.

    A second, separate bug blocked the rest of this step overnight: native
    `lcars_token` (read by `LcarsClient.kt`, distinct from the WebView's
    own `localStorage` copy) didn't reliably persist via
    `TokenPlugin.setToken()`, so real `AutoDownloadWorker` ticks
    auth-failed and fetched zero episodes. No single root-cause line was
    ever pinned down — the original failure had a clean explanation
    (`config.js`'s `setToken()` call, `e1c8ef6`/A1 steps 19-20, was
    undeployed at login time), but the same failure also reproduced later
    against confirmed-current code via direct calls with no page-init
    timing involved, success reported with no thrown exception both times
    it worked and both times it silently didn't. Fixed defensively instead
    of diagnostically (v0.2.30): `pushNativeToken()` extracted as its own
    function and called unconditionally on every native page load, not
    just when `syncConfig()` needed a server round trip (closes the
    "silent failure + no retry" gap regardless of cause); `syncConfig()`'s
    catch now logs (`console.warn`/`console.debug`) instead of swallowing;
    `TokenPlugin.setToken()` uses `commit()` (a real success boolean) plus
    an immediate readback, rejecting the call if the write didn't
    verifiably land. Verified reliable over 5 consecutive fresh app
    launches the next morning — every one showing `commit()=true
    readbackMatches=true` in Logcat and the token actually present in the
    prefs file afterward.

    Full end-to-end verification followed, using real data recovered from
    the DB rather than a fresh forced test: a genuine successful run from
    the previous evening (20:24:46-47, moments before a forced second run
    was correctly refused by WorkManager) had downloaded 5 real backlog
    episodes with 5 unique `episode_id` rows — no duplicates — then
    eviction correctly evicted the 4 oldest-by-`downloaded_at` (all
    unwatched, so pure oldest-first order), leaving exactly the newest
    under `storage_limit_gb`. Cross-checked against the actual files on
    disk: exactly one file present, matching the one `complete` row; the
    4 evicted rows' files were genuinely deleted, not orphaned. That state
    held unchanged for ~10 hours overnight with zero re-download
    thrashing of the evicted episodes — real elapsed-time confirmation of
    the evict/redownload loop fix, not just a single forced tick.

### Project structure

```
ui/android/                     ← Capacitor Android project
  app/src/main/java/.../
    MainActivity.kt             ← A1: builds CapConfig from SharedPreferences
    ServerSetupActivity.kt      ← A1: server address (+ A4 storage limit)
    VlcPlugin.kt                ← A1
    TokenPlugin.kt              ← A1: bearer → SharedPreferences
    DownloadPlugin.kt           ← A2
    LcarsClient.kt              ← A3/A4
    LcarsWsPlugin.kt            ← A3
    AutoDownloadWorker.kt       ← A4
ui/capacitor.config.ts          ← cleartext: true; server.url overridden at runtime
ui/android/release.keystore     ← gitignored, back it up — lost key = can't update installed apps
```

### Distribution

Only the dev phone (USB, developer mode) ever touches the toolchain. Everyone
else gets a signed release APK: serve it from nginx (e.g. `/ui/starfleet.apk`),
open the URL on the device, allow install from browser, done. Updates install
over the top as long as the signing key is the same. Because the UI is remote,
a new APK is only needed when Kotlin changes — web releases reach every
installed phone immediately.

---

## 9. Related repos

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
