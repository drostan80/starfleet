# Next up

Current version: **v0.2.40** (deployed 2026-09-20).
Full build history archived to `~/repos/starfleet-archive`.

---

## Production freeze incident — fixed in v0.2.38-v0.2.40 (2026-09-20)

Real outages, not theoretical. Three fixes shipped same-night:

- [x] **v0.2.38**: `anilist_reconcile`/`mal_reconcile` were writing
      `show.status` directly instead of `season.status`, with no guard
      against real unwatched aired episodes — caused a live multi-week
      status oscillation for two shows. Fixed to write season-level and
      defer to the existing single-authority derivation.
- [x] **v0.2.39**: the v0.2.38 fix itself had a bug — `_recompute_show_
      status` did its own AniList+MAL push on top of the reconcile's
      already-correct one-directional onward push, doubling/tripling
      throttled AniList calls (2.1s/call, synchronous, process-wide) on
      the first run after the fix landed, when a backlog of shows
      corrected all at once. Froze the whole server for minutes; fixed by
      skipping the redundant push from that one call site.
- [x] **v0.2.40**: separately, `show.js`'s automatic art/synopsis
      auto-fetch on every page load (no memory of "already tried, found
      nothing") froze the server again — a messy stub show with many
      AniList-linked seasons and missing synopses paid the full external-
      fetch cost on every view. Disabled outright as a stopgap; the real
      fix (negative caching, staged throttle, manual trigger) is a
      separate, deliberately deferred item below.

Recovered via `docker restart lcars` twice during the incident — safe
both times, no uncommitted transaction to lose.

## Data cleanup: 3 "Season 2" shows linked to the wrong Sonarr series (2026-09-20)

Found while diagnosing the freeze above, then confirmed as a real pattern,
not a one-off — see `season-2-linking-bug-pattern` in Claude memory for
full detail:

- [x] "Tantei wa mou, Shindeiru. Season 2" (`s-qp3t5j`) — fully traced:
      linked to Sonarr's "Detective Opera Milky Holmes" (confirmed via
      episode titles/air-dates, completely unrelated show). Soft-deleted;
      hard-delete requested (elapses 2026-09-21T19:51:49Z, needs
      `confirmHardDelete(retypedTitle: "Tantei wa mou, Shindeiru. Season 2")`
      once the 24h delay passes).
- [x] "Boys Over Flowers 2" (`s-vce5mc`) and "Bones Collector" (`s-vr1pcf`)
      — user found and deleted the wrong entries in Sonarr directly; LCARS
      side soft-deleted + hard-delete requested (both elapse
      2026-09-20T19:57:14Z).
- [ ] **Not yet done**: find and fix the actual matching logic that lets
      a sequel/"Season 2" title link to an unrelated Sonarr/TVDB series in
      the first place, and add a guard rail (low-confidence match should
      never auto-accept, same lesson `show_merge.py`'s own auto-merge
      retirement already learned). Worth checking other tracked shows for
      the same "no AniList match for tvdb id" pending_review symptom this
      bug pattern leaves behind, even without the root cause pinned yet.

---

## Android app auto-download controls — shipped in v0.2.31 (2026-09-20)

User feedback after using the app for real: wanted control over what
auto-download grabs and how it manages storage, rather than it being an
unconfigurable black box. Four pieces, all confirmed live on-device
against the deployed server (not just built/unit-tested):

- [x] **`backlog(includePlanned: true)`** — a planning-status show whose
      first episode has already aired is now included, matching "started"
      shows the user wants grabbed without a manual promotion to
      watching. Paused shows remain excluded (already were). Default
      `false` server-side, so the web Backlog page is unaffected.
      Confirmed live: previously-invisible episodes of several
      already-started planned shows appeared in the very next real
      backlog fetch after deploying.
- [x] **Auto-download on/off toggle** — previously no way to disable it
      short of never configuring a server. `AutoDownloadWorker.schedule()`
      now cancels the periodic work when disabled rather than leaving a
      no-op tick every 30 minutes. Confirmed live via the settings UI:
      toggling off produced a real `WM-GreedyScheduler: Cancelling work
      ID ...` in Logcat; toggling back on re-scheduled and (since
      periodic work runs immediately when constraints are already met)
      triggered an actual run within seconds.
- [x] **Time-based auto-delete** — new `watched_at` column
      (`DownloadDatabase` v3) plus `evictWatchedPastHours()`: deletes a
      watched episode a configurable number of hours after it was marked
      watched, independent of (and checked before) the existing
      size-based eviction. Disabled by default (0 = never).
- [x] **In-app Settings page access** — new `AppSettingsPlugin` bridges
      all four settings (the three above plus the existing Wi-Fi-only
      toggle) to `settings.html`, so they're reachable without leaving
      the app for the native "Change server" screen. That screen keeps
      its own Wi-Fi-only/storage-limit fields too — same SharedPreferences
      keys, so both stay in sync automatically; not a replacement.

One side effect worth knowing about: `DownloadDatabase`'s v2→v3 upgrade
drops and recreates the table (existing, intentional policy — a
re-downloadable cache, not data of record), so any app update that bumps
`DB_VERSION` will make the worker's next tick treat every already-
downloaded file as "new" again and re-enqueue it. Confirmed this doesn't
create `-1` duplicate files (`episode_id` `PRIMARY KEY` still holds), but
it does mean real re-download bandwidth/storage churn on every such
upgrade — acceptable for a hobby app, but a good reason not to bump
`DB_VERSION` casually.

Still open: the app icon is a placeholder — user is designing a real one
(needs a square PNG, 1024×1024 ideally, safe detail within the center
~66%, for Android Studio's Image Asset tool to generate the adaptive-icon
set from).

---

## Android app A0 (server + web client prep) — shipped in v0.2.27 (2026-09-19)

Build steps 5–12 of `ui/DESIGN.md` §8 done, tested, and verified live
against the deployed stack (1292 tests green pre-deploy; every A0-specific
behavior below re-checked against `tiny` post-deploy, not just assumed):

- [x] `auth.py`: `/auth/check` also accepts `Authorization: Bearer <token>`;
      `/auth/settings` GET serves `lcars_token`/`tmdb_api_key` from LCARS
      config (never from `web_setting`); `SHARED_SETTING_KEYS` emptied
      (table kept for any future genuinely-shared setting) — PUT and
      `/auth/setup`'s old `settings` import are now no-ops for those two
      keys, the dead setup-import code path was removed outright.
- [x] `nginx.conf`: `/_auth_check` forwards `Authorization`; `/` gains
      `proxy_http_version 1.1` + `Upgrade`/`Connection $connection_upgrade`
      (a `map $http_upgrade $connection_upgrade` block, not the hard-coded
      `"upgrade"` DESIGN.md sketched — that form puts `Connection: upgrade`
      on every plain GraphQL POST too, an asymmetric header pair a plain
      HTTP/1.1 backend can choke on; caught in review, not by any test
      here — no local nginx/docker to run this config against) +
      `proxy_read_timeout 3600s` for future WS passthrough (A3).
- [x] `api.js` → `fetch('/')`; `calendar.js`/`grabs.html` mpv URLs →
      `location.origin`; both `rewriteHost` call sites (`calendar.js`,
      `show.js`) → `location.hostname` directly.
- [x] `config.js`: `SHARED_KEYS` reduced to `['lcars_token','tmdb_api_key']`;
      `requireConfig`/`bootstrapConfig` need only `lcars_token` and now
      redirect to `login.html` (not `settings.html`, which has no token
      field to fill it from); `getConfig` actively deletes stale
      `lcars_url`/`home_server_host` from localStorage on every load.
      Dead `saveConfigWithSync` (no callers left once settings.html
      stopped writing these fields) removed.
      **Verified** by running the real `config.js` module (unmodified,
      dynamic import) against a shimmed `localStorage`/`location` in
      Node: a simulated pre-A0 config with stale `lcars_url`/
      `home_server_host` and a still-valid token has both stripped on
      the first `getConfig()` call and is accepted by `requireConfig`;
      a token-less config redirects to `login.html`.
- [x] **Step 9** (real-browser desktop check, run by hand 2026-09-19
      against `tiny` v0.2.27 in Firefox) — fresh-profile login →
      calendar/Sonarr link/TMDB poster fallback/mpv/download all worked,
      `starfleet_config` held only `lcars_token`/`tmdb_api_key`. Stale-profile
      regression also confirmed: a `starfleet_config` seeded by hand with
      old-style `lcars_url`/`home_server_host` (simulating a pre-A0
      browser) had both stripped and never reappeared after a real login.
      Found a real (if narrow) gap along the way: `bootstrapConfig()`
      only re-syncs from `/auth/settings` when a key is *missing*, not
      when it's present-but-wrong — a corrupted cached `lcars_token`/
      `tmdb_api_key` (e.g. from a bad manual edit, not a normal user
      path) stays stuck through any number of refreshes; only clearing
      `starfleet_config` outright forces a re-pull. Not a shipped-code
      regression since nothing writes a bad-but-present token in normal
      use, but worth knowing if a token is ever rotated server-side.
- [x] `settings.html` → only `mpv_helper_url` remains; `login.html` →
      "import existing settings" block removed (backend path it fed is
      gone too).
- [x] `dev.sh` → stopped writing `lcars_url`/`lcars_token`/
      `home_server_host`/`tmdb_api_key` into `web_setting` (now inert);
      added `LCARS_TMDB_API_KEY` env var so the dev server still serves
      the TMDB key via config the way A0 requires.
- [x] `tmdb_api_key` confirmed present in `/opt/appdata/lcars/config/lcars.ini`
      on `tiny` (user confirmed the value itself; presence also confirmed
      here via a non-destructive `grep -c`) — TMDB poster fallback keeps
      working under the new "served from config only" model.
- [x] **Step 10** (curl bearer test against deployed nginx) — both
      `GET /auth/check` and `POST /` with `Authorization: Bearer` and no
      cookie return 200 through the live nginx on `tiny`.
- [x] **Step 11** (release + deploy) — tagged/pushed `v0.2.27`, CI built
      and published both `starfleet:0.2.27` and `-web` images, DB
      snapshotted (`lcars.db.bak-20260919-android-a0-v0.2.27`), pins
      bumped on `tiny`, `pull`+`up -d`, all three containers healthy on
      the new tag, `ops`'s expected post-recreate `LcarsError` noise
      confirmed to self-clear within 20s.
- [x] **Step 12** (WS `graphql-transport-ws` end-to-end check) — a real
      Python client connected to `ws://<tiny>:8888/` with a bearer
      header, negotiated the `graphql-transport-ws` subprotocol through
      nginx, completed `connection_init`/`connection_ack`, and had a
      `subscribe` accepted with no error frame. Confirms the whole chain
      (nginx upgrade headers → LCARS WS → subprotocol negotiation) works
      live — not just that `BearerTokenMiddleware` accepts the scope.
      Did not force an actual event through (would mean mutating
      production state just to see a payload) — a real `episodeAvailabilityChanged`/
      `showCreated` payload landing client-side is still worth eyeballing
      once A3 actually subscribes to one.
---

## login.html couldn't log in when genuinely logged out — shipped in v0.2.28 (2026-09-19)

- [x] Found while testing Android against production (below): `login.html`
      statically imported `js/config.js` (for `applyAppName()`'s dev/prod
      tab-title swap), but nginx's `/ui/` location auth-gates everything
      except `login.html`/`css/`/`favicon.svg` themselves. A client with
      no session cookie at all got a 302-to-self for that import, failing
      the whole ES module graph — including the login form's own submit
      handler. Pre-existing (confirmed via git history, predates this
      session's A0 work), never hit because real sessions are long-lived.
      Fixed by inlining the few lines `login.html` needs instead of
      widening what nginx serves unauthenticated. Verified live + via
      screenshot on the Android WebView.

---

## Android app A1 (shell + VLC) — shipped in v0.2.29 (2026-09-19)

Test machine (Android Studio, JDK 21 for Gradle, `android-tools` for
`adb`) set up this session; a real device (Asus Zenfone 9, Android 14)
connected throughout. All of DESIGN.md §8's steps 13–22 built and
verified live on that device — see DESIGN.md itself for the full
per-step detail (Capacitor version actually used, the `SuperNotCalledException`
gotcha, the `<queries>` package-visibility fix, the release keystore).
Login/setup/VLC streaming/signed-release-build all confirmed working
end to end, including a real episode file streamed and played from the
live production server. Tailscale half of the LAN/Tailscale test (step
21) is blocked on the test phone's own Tailscale login, not app code —
LAN half thoroughly proven.

Steps 19-20 (TokenPlugin wiring, platform-gated play button) are now
exercised through the real deployed app UI, not just CDP — found (and
fixed) exactly the gap this note flagged: the production `ui/` bundle
predated all of A1-A4's native-Capacitor branching, so the app's play
button was silently falling through to the desktop-only mpv-helper
endpoint (`localhost:19450`) and failing. Confirmed the deployed
`calendar.js` now has `isNativePlatform`/`Vlc.play` (fetched from inside
the live authenticated WebView session, not just curl — nginx auth-gates
`/ui/js/*`), reloaded the page, and confirmed live: VLC launches natively
on the phone, desktop mpv-helper path still works unaffected in a
browser.

---

## Android app A2 (download) — shipped in v0.2.29 (2026-09-19)

DESIGN.md §8 steps 23–27 built and verified live on the same device:

- [x] Local download index — plain `SQLiteOpenHelper`, not
      `@capacitor-community/sqlite` as the plan originally named (deemed
      unnecessary third-party native surface for what's actually a small,
      fixed set of operations).
- [x] `DownloadPlugin.kt` — real bug found and fixed by testing:
      the completion broadcast receiver needed `RECEIVER_EXPORTED`, not
      `RECEIVER_NOT_EXPORTED` (DownloadManager is a different-UID system
      service; the "not exported" flag was blocking its own broadcast to
      us). See DESIGN.md for the full story.
- [x] Offline playback — `VlcPlugin` takes a local `content://` URI now,
      confirmed by actually disabling WiFi on the device and watching a
      downloaded episode play from local disk.
- [x] Downloaded a real ~190MB file, confirmed byte-exact size match,
      confirmed the download survives backgrounding the app (the actual
      reason to use `DownloadManager` over Capacitor's own `Filesystem`
      API) and the index still updates correctly on foreground return.
- [x] Web half (platform-gated download button, `downloads.html` native
      index) now deployed and exercised through the real UI — same fix as
      A1's play-button gap above (stale `ui/` bundle), same v0.2.29 deploy.

---

## Android app A3 (live updates via WebSocket) — shipped in v0.2.29 (2026-09-19)

- [x] `LcarsWsPlugin.kt`: connect, `connection_init`, subscribe,
      `notifyListeners`, bearer on the WS handshake header (OkHttp 4.12.0 —
      5.5.0 needs compileSdk 37, one more than this project's 36).
- [x] Reconnect on connectivity change (`ConnectivityManager
      .registerDefaultNetworkCallback`, exponential backoff 2s→60s cap) —
      confirmed live by disabling WiFi mid-session (`onFailure` + doubling
      backoff observed) and re-enabling it (real second `connection_ack`).
- [x] `calendar.js`'s 10s poll now only runs on non-Android platforms;
      Android registers `LcarsWs` listeners instead.
- [x] Tested live against the real deployed server: `graphql-transport-ws`
      subprotocol negotiated, both subscribes accepted with no error frame.

---

## Android app A4 (auto-download + storage management) — shipped in v0.2.30 (2026-09-20)

`AutoDownloadWorker` (WorkManager `CoroutineWorker`, unique periodic,
30min, UNMETERED-only by default) diffs the LCARS backlog against the
local download index and enqueues anything new, then evicts
oldest-watched-first once `storage_limit_gb` is exceeded. See
`ui/DESIGN.md` §8 steps 33-38 for full per-step detail. Found by live
device testing, not designed in:

- [x] **Duplicate-download bug** — an ad-hoc one-time `WorkRequest` added
      purely to force a test run raced the periodic work's own first
      execution and double-enqueued every episode (every file downloaded
      twice, `-1` suffix). Root-caused to that one-time trigger, not the
      periodic mechanism itself; removed, since `enqueueUniquePeriodicWork`
      alone already gives WorkManager's own serialization guarantee.
- [x] **Stale "pending" rows** — the completion broadcast only fires while
      the app process is alive, so a process death mid-download left a
      permanently-stale DB row; the worker now reconciles against
      `DownloadManager` directly on every tick.
- [x] **Evict/redownload infinite loop** — eviction must soft-delete
      (`markEvicted`, tombstone row) rather than hard-delete: a hard
      delete let the same tick's own backlog diff see the episode as new
      again and immediately re-enqueue it.
- [x] **`DownloadManager.remove()` doesn't delete the file** under
      `setDestinationInExternalFilesDir` — confirmed live; fixed by also
      deleting the file directly.
- [x] Wi-Fi-only downloads toggle and storage limit (GB) field, both in
      `ServerSetupActivity`, same immediate-save-on-toggle pattern.
- [x] `VlcPlugin` reports watched status natively off its own
      `onActivityResult` (SQLite write always succeeds immediately; the
      network `addWatchEvent` call is independently retried via
      `WatchEventRetryQueue` on failure, flushed every worker tick).
- [x] **Native `lcars_token` sync bug, found and fixed** — the token
      didn't reliably reach native `SharedPreferences` even though the
      WebView's own `localStorage` copy was correct, silently auth-failing
      every `AutoDownloadWorker` GraphQL call for a stretch of the previous
      evening's testing (fast ~20-40ms "SUCCESS" results were auth
      failures returning zero episodes, not real backlog fetches).
      Root cause was never pinned to a single line — two contributing
      issues were found and fixed together instead: (1) `bootstrapConfig()`
      only pushed the native token when `localStorage` itself was missing
      it, so a silent native failure had no retry path once the web side
      looked fine — `pushNativeToken()` is now its own function, called
      unconditionally on every native page load; (2) both the JS
      (`syncConfig()`'s catch) and native (`TokenPlugin.setToken()`'s
      `apply()`, fire-and-forget) sides could fail with zero observable
      trace — JS now logs `console.warn`/`console.debug`, and native uses
      `commit()` (a real success boolean) plus an immediate readback,
      rejecting the call if the write didn't verifiably land. Verified
      reliable over 5 consecutive fresh app launches the next morning
      (each showing `commit()=true readbackMatches=true` in Logcat and the
      token actually present in the prefs file afterward) — confidence
      the *observable* bug is gone even without a single root-cause line,
      since the fix closes every path that could produce the symptom.
- [x] **Full end-to-end verification, real data, zero duplicates**:
      digging through the DB/file state the next morning turned up a
      complete real run from the previous evening (20:24:46-47, moments
      before a forced second run got correctly refused by WorkManager) —
      5 real backlog episodes downloaded with 5 unique `episode_id` rows
      (no duplicates), then eviction correctly ran and evicted the 4
      oldest-by-`downloaded_at` (all unwatched, so pure oldest-first),
      leaving exactly the newest under `storage_limit_gb`. Cross-checked
      against the actual files on disk: exactly one file present, matching
      the one `complete` row; the 4 evicted rows' files were genuinely
      deleted, not orphaned. That state held unchanged for ~10 hours
      overnight with zero re-download thrashing of the evicted episodes —
      the evict/redownload loop fix (above) is confirmed solid under real
      elapsed time, not just a single forced test tick.

Also fixed along the way: the deployed `ui/` bundle predated all of
A1-A4's native-Capacitor branching (`isNativePlatform()` checks added
across earlier phases were sitting in the repo, undeployed) — the app's
play/download buttons were silently falling through to desktop-only code
paths. This is what v0.2.29 actually shipped along with A3/A4's own code,
with the token-sync fix following in v0.2.30. This same "code committed,
never deployed" class of bug is also step 33's headline duplicate-download
fix's twin — worth remembering `v*` tag + redeploy is a discrete step,
not implied by `git commit`.

All four Android app phases (A1-A4) are now shipped and verified end to
end against the real deployed server, not just native-only or CDP-driven
testing.

---

## Android app: mark-as-watched, resume, auto-refresh — shipped in v0.2.37 (2026-09-20)

Found through live use after A1-A4 shipped, not part of the original build
plan. This closes out the Android app build — the whole app (server prep,
shell, VLC, download, live updates, auto-download, and this round of
watch-tracking fixes) is now shipped and confirmed working live, end to end:

- [x] **VLC resume-last-position blocked re-watching an already-watched
      episode** — VLC's own resume feature opened straight to the last few
      seconds and closed almost instantly, no time to seek back. Fixed with
      `from_start: true` on `Vlc.play()` whenever the episode being launched
      is already watched (forces playback from 0, overriding VLC's resume
      memory); left alone for anything not yet watched. `episodeCtx()`
      gained a `watched` field; `show.js`'s three `launchMpv()` call sites
      (which bypassed `episodeCtx()` entirely) were fixed to compute it
      inline — the actual primary usage path, found only by auditing every
      call site rather than trusting the first one fixed.
- [x] **Mark-as-watched never fired on natural end-of-file completion** —
      root-caused via live device testing: VLC's own `extra_duration`
      result extra reliably returns 0 specifically on natural completion
      (fine on a manual back-press exit), silently breaking the
      position/duration threshold check. Fixed with `MediaMetadataRetriever`
      pulling a real duration up front (offline `content://` URI or
      streaming URL), used in place of VLC's own value whenever available.
      Verified end-to-end (logs → local DB → network call → server GraphQL
      state) for both offline and streaming playback, on both the test
      phone and the user's tablet.
- [x] **No auto-refresh after a watch** — calendar/backlog/show/downloads
      pages required a manual reload to show a new watched status or drop a
      just-watched episode from the backlog. `launchMpv()`'s native branch
      and `downloads.js`'s `playOffline()` now dispatch a
      `starfleet:refresh-after-watch` event ~2s after VLC returns control to
      the app (once the background `addWatchEvent` report has had time to
      land); each page listens and does a silent re-fetch. The desktop
      mpv-helper path deliberately does not dispatch this — that `fetch()`
      resolves when mpv launches, not when it exits, so there's no
      equivalent signal to hang a timer on. `grabs.html`'s separate
      `launchMpv()` also doesn't wire it: `GrabEvent` has no watched-state
      field for that page to redraw (folded into the future unified list
      page instead).
- [x] User-confirmed live end-to-end post-deploy: worked as expected.

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

## Small fixes — shipped in v0.2.25 (2026-09-19)

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

## ID correction + Sonarr/Radarr ↔ LCARS sync — shipped in v0.2.25 (2026-09-19)

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

## mpv → LCARS watched status — shipped in v0.2.25 (2026-09-19)

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

## On-air indicator (calendar/backlog availability icon) — shipped in v0.2.25 (2026-09-19)

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

## Faster list-page cover art — shipped in v0.2.26 (2026-09-19)

Root cause was an eager/no-fallback asymmetry: anime shows get `poster_url`
reliably at add-time (AniList link mandatory, MAL fallback if AniList is
down); TV/movie shows only got one from Sonarr/Radarr's own bundled image
list (optional, no fallback) — the real art fetch (TVDB+TMDB+TVmaze+MAL)
existed but was manual-only. So most TV shows arrived at the list page with
`posterUrl = NULL`, and the client-side TMDB fallback queued hundreds of
shows through one shared 220ms-spaced request chain — the real "timeout."

- [x] **Eager fallback going forward** (`_fetch_tmdb_duration`,
      `metadata.py`) — now also pulls `poster_path` from the same TMDB
      response it already fetches for `duration_minutes` (no extra API
      call), writing `poster_url` via the same COALESCE-write pattern
      Sonarr/Radarr's own poster writes already use, so Sonarr/Radarr
      (running after this in `fetch_and_populate`) still wins when they
      have their own poster — TMDB only fills the gap.
- [x] **One-time backfill for the existing library** — new
      `backfillShowPosters` mutation (`metadata.backfill_show_posters`)
      runs the full art fetch for every tracked show still missing a
      poster, paced between shows, per-show failure isolated (never
      aborts the batch). Manual trigger only (`ops backfill-posters`),
      same reasoning as `audit-local-files`/`reconcile-arr-state` — real
      outbound calls per show, too much cost for the automatic loop.
      Safe to re-run.
- [x] **Backfill run** (2026-09-20) — by the time this was triggered, only
      4 of the original 369 tracked shows still had `poster_url IS NULL`;
      the eager TMDB fallback added alongside this feature had already
      caught the rest through normal use over the following day. 0/4
      filled: one real show (a Demon Slayer movie entry) genuinely has no
      art available from any source (TVDB/TVmaze/TMDB/MAL); the other 3
      are stub rows with no title at all in the DB, a data-completeness
      gap unrelated to this feature. Safe to re-run if either gets fixed.
- Client-side queue/timeout hardening (option 3 from the original
  diagnosis) deliberately skipped — once posters are populated
  server-side, the client fallback path barely gets exercised.

---

## Ideas / future

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

### Art-fetch negative cache + throttle (found 2026-09-20, deferred)

Found while diagnosing the v0.2.38/v0.2.39 freeze incident: `autoFetchArt()`
only skips its AniList/TVDB/TMDB/TVmaze/MAL fetch cascade when a show
already has *any* stored art asset — a show whose art is genuinely
unfindable (a real confirmed case: a Demon Slayer entry from that night's
poster backfill) re-triggers the full cascade on *every* page view,
forever. User's own design, deliberately deferred:

- [ ] **Negative caching**: remember "tried, found nothing" so automatic
      per-page-load fetch stops retrying; a manual re-fetch button must
      still bypass it. Art dialog gains: (a) add art manually via a pasted
      image URL from any source, (b) delete a stored art address (for a
      stale/404'd URL — expected to be rediscovered on the next manual
      re-fetch if it's still findable).
- [ ] **Staged/throttled auto-fetch, not a hard cap**: banner + highest
      season's art first (stands in for the show meanwhile) → after a
      beat, main show art → only then search other seasons, and only
      when that season has no art yet AND is actually unhidden/visible.
- [ ] **Manual pulls take priority** over background/automatic fetches
      (both the page's own auto-fetch queue and ops's scheduled jobs).

Full detail in Claude memory: `art-fetch-negative-cache-and-throttle-plan`.

### Smaller ideas

- [ ] IMDB datasets for cross-referencing and fallback ID bridging.
- [ ] Browse / filter by studio (`show.studio` gets an id-prefix).
- [ ] Direct TVDB search (use TVDB API instead of through Sonarr).
- [ ] livechart.me headlines for delay/reschedule news on tracked shows.
- [ ] Grabs screen: show only title+episode/film title, add mpv launch key.
