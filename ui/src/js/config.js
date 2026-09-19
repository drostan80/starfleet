/**
 * Config management — A0 zero-entry client (DESIGN.md §8).
 *
 * lcars_url and home_server_host no longer live here at all: callers use
 * location.origin / location.hostname directly, which is correct on LAN
 * and Tailscale alike, whichever the browser is currently pointed at.
 * lcars_token and tmdb_api_key are SERVED from LCARS config via
 * /auth/settings — nobody types a token anywhere. Machine-specific
 * settings (mpv_helper_url) stay in localStorage only.
 *
 * Shape (localStorage cache):
 *   { lcars_token, tmdb_api_key, mpv_helper_url }
 */

const CONFIG_KEY = 'starfleet_config';

const SHARED_KEYS = ['lcars_token', 'tmdb_api_key'];

// A0 upgrade hazard: a browser that logged in before A0 shipped has these
// two keys cached in localStorage from syncConfig()'s own pre-A0 behavior.
// If a stale stored value ever won over the derived location.origin/
// location.hostname a caller uses today, that browser would keep pointing
// at its old LAN IP after a Tailscale login — reproducing the exact bug
// A0 exists to fix, as an upgrade artifact. So getConfig() actively
// deletes both keys on every load, not merely stops writing them.
const STALE_STORED_KEYS = ['lcars_url', 'home_server_host'];

/** Read config from localStorage; returns null if missing or malformed. */
export function getConfig() {
  let cfg;
  try {
    cfg = JSON.parse(localStorage.getItem(CONFIG_KEY) || 'null');
  } catch {
    return null;
  }
  if (cfg) {
    let dirty = false;
    for (const k of STALE_STORED_KEYS) {
      if (k in cfg) { delete cfg[k]; dirty = true; }
    }
    if (dirty) saveConfig(cfg);
  }
  return cfg;
}

/** Write config to localStorage. */
export function saveConfig(cfg) {
  localStorage.setItem(CONFIG_KEY, JSON.stringify(cfg));
}

/**
 * Fetch shared settings from the server and merge with localStorage.
 * Server values win for shared keys; localStorage wins for local-only keys.
 * Returns the merged config, or null if not authenticated.
 */
export async function syncConfig() {
  try {
    const res = await fetch('/auth/settings');
    if (!res.ok) return null;  // not authenticated
    const serverSettings = await res.json();
    const local = getConfig() || {};

    // Server wins for shared keys.
    for (const k of SHARED_KEYS) {
      if (serverSettings[k]) {
        local[k] = serverSettings[k];
      }
    }
    saveConfig(local);
    return local;
  } catch {
    return getConfig();  // offline — use cached.
  }
}

/**
 * Read config, redirect to login if essential config values are absent.
 * Returns the config object on success, null after redirect.
 *
 * Redirects to login.html, not settings.html: lcars_token only ever
 * arrives via /auth/settings (A0 — settings.html has no token field to
 * fill it in from), so a missing token means an unauthenticated session,
 * not an unconfigured client.
 */
export function requireConfig() {
  const cfg = getConfig();
  if (!cfg || !cfg.lcars_token) {
    window.location.href = 'login.html';
    return null;
  }
  return cfg;
}

/**
 * Bootstrap config for page init: if localStorage is missing shared keys,
 * pull them from the server first. Then fall through to requireConfig().
 * Call this (with await) instead of requireConfig() at page load.
 */
export async function bootstrapConfig() {
  const cfg = getConfig();
  if (!cfg || !cfg.lcars_token) {
    await syncConfig();
  }
  return requireConfig();
}

/** App name — 'TEST SHUTTLE' on localhost, 'STARFLEET' in production. */
export const APP_NAME = location.hostname === 'localhost' ? 'TEST SHUTTLE' : 'STARFLEET';

/**
 * Apply app name to the page title and nav brand element.
 * Call once at page load from each page's init.
 */
export function applyAppName() {
  const suffix = document.title.includes('—')
    ? document.title.split('—').slice(1).join('—').trim()
    : '';
  document.title = suffix ? `${APP_NAME} — ${suffix}` : APP_NAME;
  const brand = document.querySelector('.nav-brand');
  if (brand) brand.textContent = APP_NAME;
}

/**
 * Rewrite the host in a Sonarr/Radarr URL to the configured
 * home_server_host. Mirrors data's links.py:rewrite_host.
 * Leaves the URL untouched if home_server_host is blank.
 */
export function rewriteHost(url, homeServerHost) {
  if (!homeServerHost || !url) return url;
  try {
    const u = new URL(url);
    u.hostname = homeServerHost;
    return u.toString();
  } catch {
    return url;
  }
}
