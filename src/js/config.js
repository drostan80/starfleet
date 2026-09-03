/**
 * Config management — reads/writes starfleet_config in localStorage.
 *
 * Shape:
 *   { lcars_url: string, lcars_token: string, home_server_host: string }
 */

const CONFIG_KEY = 'starfleet_config';

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
 * Read config, redirect to settings.html if lcars_url or lcars_token are
 * absent. Returns the config object on success, null after redirect.
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
