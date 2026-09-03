/**
 * Config management — hybrid localStorage + server-side settings.
 *
 * Shared settings (lcars_url, lcars_token, home_server_host, tmdb_api_key)
 * are stored server-side in web_setting, so they survive across browsers.
 * Machine-specific settings (mpv_helper_url) stay in localStorage only.
 *
 * Shape (localStorage cache):
 *   { lcars_url, lcars_token, home_server_host, tmdb_api_key, mpv_helper_url }
 */

const CONFIG_KEY = 'starfleet_config';

const SHARED_KEYS = ['lcars_url', 'lcars_token', 'home_server_host', 'tmdb_api_key'];

/** Read config from localStorage; returns null if missing or malformed. */
export function getConfig() {
  try {
    return JSON.parse(localStorage.getItem(CONFIG_KEY) || 'null');
  } catch {
    return null;
  }
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
 * Save config: write to localStorage and push shared keys to the server.
 */
export async function saveConfigWithSync(cfg) {
  saveConfig(cfg);
  // Push shared keys to server.
  const shared = {};
  for (const k of SHARED_KEYS) {
    if (cfg[k] !== undefined) shared[k] = cfg[k];
  }
  try {
    await fetch('/auth/settings', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(shared),
    });
  } catch { /* best-effort */ }
}

/**
 * Read config, redirect to settings if essential config values are absent.
 * Returns the config object on success, null after redirect.
 */
export function requireConfig() {
  const cfg = getConfig();
  if (!cfg || !cfg.lcars_url || !cfg.lcars_token) {
    window.location.href = 'settings.html';
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
  if (!cfg || !cfg.lcars_url || !cfg.lcars_token) {
    await syncConfig();
  }
  return requireConfig();
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
