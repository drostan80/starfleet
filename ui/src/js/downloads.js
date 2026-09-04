/**
 * Download manager — tracks browser-native downloads in localStorage.
 *
 * Downloads are triggered via the nginx /download/ location which returns
 * Content-Disposition: attachment.  The browser's own download manager
 * handles progress, resume, and errors.  This module keeps a launch log
 * so the user can see what was initiated, re-trigger, or purge entries.
 */

const STORAGE_KEY = 'starfleet_downloads';

/* ── Persistence ─────────────────────────────────────────── */

function loadEntries() {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    return raw ? JSON.parse(raw) : [];
  } catch { return []; }
}

function saveEntries(entries) {
  try { localStorage.setItem(STORAGE_KEY, JSON.stringify(entries)); }
  catch { /* quota or blocked — silent */ }
}

/* ── Public API ──────────────────────────────────────────── */

/**
 * Build the download URL for a container file path.
 * Container paths start with /data (e.g. /data/media/anime/...).
 * Browser-relative URL: /download/media/anime/...
 */
export function downloadUrl(filePath) {
  const mediaPath = filePath.startsWith('/data') ? filePath.slice('/data'.length) : filePath;
  return `/download${mediaPath}`;
}

/**
 * Record a download entry and open the browser-native download.
 * Returns the entry id.
 */
export function startDownload({ showTitle, label, filePath }) {
  const url = downloadUrl(filePath);
  const entry = {
    id: self.crypto?.randomUUID?.()
        ?? Array.from(crypto.getRandomValues(new Uint8Array(16)),
             b => b.toString(16).padStart(2, '0')).join(''),
    showTitle,
    label,          // e.g. "S2 E5 — Episode Title"
    filePath,
    url,
    startedAt: new Date().toISOString(),
  };

  const entries = loadEntries();
  entries.unshift(entry);  // newest first
  // Cap at 100 entries
  if (entries.length > 100) entries.length = 100;
  saveEntries(entries);

  // Trigger browser-native download
  triggerDownload(url);

  return entry.id;
}

/**
 * Re-trigger a previous download by entry id.
 */
export function retriggerDownload(id) {
  const entries = loadEntries();
  const entry = entries.find(e => e.id === id);
  if (!entry) return false;

  // Update timestamp
  entry.startedAt = new Date().toISOString();
  saveEntries(entries);

  triggerDownload(entry.url);
  return true;
}

/**
 * Remove one entry from the log.
 */
export function purgeDownload(id) {
  const entries = loadEntries();
  const idx = entries.findIndex(e => e.id === id);
  if (idx < 0) return false;
  entries.splice(idx, 1);
  saveEntries(entries);
  return true;
}

/**
 * Clear all entries.
 */
export function purgeAll() {
  saveEntries([]);
}

/**
 * Get all entries (newest first).
 */
export function getDownloads() {
  return loadEntries();
}

/**
 * Get count of entries.
 */
export function getDownloadCount() {
  return loadEntries().length;
}

/* ── Internal ────────────────────────────────────────────── */

function triggerDownload(url) {
  // Content-Disposition: attachment means the browser saves instead of
  // navigating, so assigning location is safe — the page stays put.
  // This avoids both the hidden-<a>.click() suppression on Android
  // Firefox and the popup-blocker that kills window.open().
  window.location.assign(url);
}
