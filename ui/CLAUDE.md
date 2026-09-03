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

```json
{
  "lcars_url":         "http://192.168.0.152:8888",
  "lcars_token":       "<bearer token>",
  "home_server_host":  "192.168.0.152"
}
```

Key under `starfleet_config`. Settings page reads/writes this. Every
GraphQL call needs `Authorization: Bearer <token>` header.

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

The web client is **not baked into the LCARS Docker image** — it is served
by the nginx reverse-proxy container from a host bind mount
(`/home/tiny/repos/web/src:/ui`). Deploy with `./deploy.sh` from this
directory, which rsyncs `src/` to the remote host over SSH. No container
restart needed.

```
cd ui && ./deploy.sh
# → deployed → http://192.168.0.152:8888/ui/index.html
```

The nginx config (`nginx.conf`) serves static files at `/ui/` and proxies
everything else to LCARS at `http://lcars:8000`.
