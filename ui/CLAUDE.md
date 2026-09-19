# Starfleet Web — Claude session instructions

HTML client for the LCARS anime/media tracker, living inside the main
starfleet repo as `ui/`. Read `DESIGN.md` (in this directory) first —
it is the source of truth for build plan, design tokens, GraphQL query
shapes, and page specs.

## What this is

A browser-based client for LCARS, connecting via GraphQL over HTTP and
WebSocket. Companion to `~/repos/data` (TUI client).

## Key references

- `DESIGN.md` — full spec; read before writing any feature
- `mockups/calendar.html` — approved calendar mockup (open in browser)
- `../src/lcars/schema.graphql` — live GraphQL schema (same repo)
- `~/repos/data/src/data/lcars_client.py` — reference for query shapes
- `~/repos/data/src/data/links.py` — `rewrite_host` for Sonarr/Radarr URLs

## Config (stored in localStorage)

A0 zero-entry client (DESIGN.md §8): only `lcars_token` and
`tmdb_api_key` are cached here, served from LCARS config via
`GET /auth/settings` — nobody types them. `lcars_url`/`home_server_host`
are never stored; callers use `location.origin`/`location.hostname`
directly.

```json
{
  "lcars_token":     "<bearer token>",
  "tmdb_api_key":    "<tmdb key>",
  "mpv_helper_url":  "http://localhost:19450"
}
```

Key under `starfleet_config`. Every GraphQL call needs
`Authorization: Bearer <token>` header.

## Tech stack decisions

**Vanilla HTML/CSS/ES Modules — no build step.**
- `src/index.html` — calendar page (entry point)
- `src/settings.html` — settings form
- `src/css/main.css` — shared styles
- `src/js/config.js` — localStorage config (getConfig/saveConfig/requireConfig/rewriteHost)
- `src/js/api.js` — GraphQL HTTP client (gql, fetchEpisodesInRange, addWatchEvent, deleteWatchEvent, setStatus)
- `src/js/calendar.js` — calendar page logic (rendering, nav, interactions)
- Served via `./serve.sh [port]` (Python http.server, default port 3000)

**WebSocket subscriptions**: not implemented in Phase 1 — LCARS's
`BearerTokenMiddleware` reads auth from HTTP upgrade headers, which browsers
cannot set. Calendar uses 30s polling instead. Future: add
`connection_init` payload auth on the server side, then subscribe to
`episodeAvailabilityChanged`.

## Design conventions (summary — full detail in DESIGN.md §5)

- Dark-first palette. `--bg: #0d0f14`, `--accent: #5b8cff`
- Fonts: Syne (headers), DM Sans (UI), DM Mono (data/counters)
- Status colours: watching `#22c55e`, completed `#3b82f6`,
  planning `#f59e0b`, paused `#f97316`, dropped `#ef4444`
- Score is LCARS native scale (0–20 quarter-point), not AniList 10-pt
- Service icons: actual logos (Phase 1 uses text abbreviations; replace
  with real SVG icons when available)
- Card max 10/row, portrait ~150×240px, `posterUrl` for cover art

## What NOT to build (Phase 1)

Lists, global search, web player, Android wrapping, Tailscale selector.
Settings UI is lowest priority — functional over pretty.

## Deployment

The web client is baked into both Docker images at build time:

- **`starfleet:<ver>`** (app image) — LCARS serves `ui/src/` at `/ui/`
  via Starlette StaticFiles (`LCARS_WEB_ROOT=/ui`).
- **`starfleet:<ver>-web`** (nginx image) — serves `/ui/` static files,
  `/files/` for media playback, and proxies `/` to LCARS.

Deploy with the normal starfleet release process (tag → CI → SSH →
bump image pins in `starfleet.yml` → pull → up). UI changes require a
new release — there is no separate rsync/deploy.sh step.

The nginx config (`nginx.conf`) is baked into the `-web` image and serves
static files at `/ui/`, raw media at `/files/`, and proxies everything
else to LCARS at `http://lcars:8000`.
