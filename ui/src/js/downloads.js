/**
 * Download manager.
 *
 * Desktop: downloads are triggered via the nginx /download/ location which
 * returns Content-Disposition: attachment.  The browser's own download
 * manager handles progress, resume, and errors.  This module keeps a
 * launch log in localStorage so the user can see what was initiated,
 * re-trigger, or purge entries — a log, not a real index: the desktop
 * side has no way to know if a browser download actually succeeded, or
 * to delete the resulting file.
 *
 * Android (A2, DESIGN.md §8): DownloadPlugin manages a real native index
 * (SQLite) of actually-downloaded files, keyed by GraphQL episode id —
 * status genuinely reflects DownloadManager's own state, and purging
 * really deletes the file, not just a log line. Every exported function
 * here branches on window.Capacitor?.isNativePlatform?.() so callers
 * (downloads.html, the per-episode download buttons) don't need their own
 * platform checks.
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
 * Start a download. Android: enqueues a real DownloadManager request via
 * DownloadPlugin, indexed by episodeId. Desktop: logs the entry and opens
 * the browser-native download. Always async now (Android's plugin call is
 * inherently a Promise) — existing call sites don't use the return value,
 * so this is safe.
 */
export async function startDownload({ showTitle, label, filePath, episodeId, showId, season, episode }) {
  if (window.Capacitor?.isNativePlatform?.()) {
    // Already stored on this device: the (green) button does nothing (user 2026-11-11).
    if (isOnDevice(episodeId)) return { alreadyOnDevice: true };
    // showId/season/episode: stored alongside the download (DownloadDatabase,
    // A4) so VlcPlugin can report addWatchEvent for offline playback without
    // JS needing to pass them again at play time — episodeId alone is
    // enough to look the rest up from the download row.
    return window.Capacitor.Plugins.Download.download({
      path: filePath, episodeId, showId, season, episode, showTitle, label,
    });
  }

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
 * Re-trigger a previous download by entry id. Desktop only — a completed
 * or failed native Android download should be deleted and re-added, not
 * "retried" (DownloadPlugin has no such concept); downloads.html hides
 * this action for Android entries instead of calling it.
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
 * Remove one entry. Android: really deletes the downloaded file too, not
 * just the index row (DownloadPlugin.deleteDownload).
 */
export async function purgeDownload(id) {
  if (window.Capacitor?.isNativePlatform?.()) {
    await window.Capacitor.Plugins.Download.deleteDownload({ episodeId: id });
    return true;
  }
  const entries = loadEntries();
  const idx = entries.findIndex(e => e.id === id);
  if (idx < 0) return false;
  entries.splice(idx, 1);
  saveEntries(entries);
  return true;
}

/**
 * Clear all entries. Android: deletes every downloaded file too.
 */
export async function purgeAll() {
  if (window.Capacitor?.isNativePlatform?.()) {
    await window.Capacitor.Plugins.Download.deleteAllDownloads();
    return;
  }
  saveEntries([]);
}

/**
 * Play an Android download offline via VLC. Desktop has no equivalent —
 * a browser download isn't tracked well enough to know it's still there.
 */
export async function playOffline(episodeId, title = '') {
  const { localUri } = await window.Capacitor.Plugins.Download.getLocalUri({ episodeId });
  if (!localUri) return false;
  // episodeId, not showId/season/episode: VlcPlugin looks those up from the
  // download row itself (DownloadDatabase, A4) for its own native
  // addWatchEvent report — see VlcPlugin.kt's reportWatched().
  await window.Capacitor.Plugins.Vlc.play({ uri: localUri, title, episodeId });
  // Same async watched-report lag as calendar.js's launchMpv() — give
  // VlcPlugin.kt's reportWatched() time to land before pages refresh.
  setTimeout(() => {
    window.dispatchEvent(new CustomEvent('starfleet:refresh-after-watch'));
  }, 2000);
  return true;
}

/**
 * Get all entries (newest first). Android: the real native index, mapped
 * to the same shape desktop entries use so downloads.html doesn't need
 * its own platform branch, plus `status`/`sizeBytes` fields desktop
 * entries don't have (desktop has no way to know either of those).
 */
export async function getDownloads() {
  if (window.Capacitor?.isNativePlatform?.()) {
    const { downloads } = await window.Capacitor.Plugins.Download.listDownloads();
    return downloads.map(d => ({
      id: d.episodeId,
      showTitle: d.showTitle,
      label: d.label,
      filePath: d.filePath,
      startedAt: d.downloadedAt,
      status: d.status,
      sizeBytes: d.sizeBytes,
      watched: d.watched,
    }));
  }
  return loadEntries();
}

/**
 * Android: run the auto-download / auto-delete check now (Wi-Fi only; waits for Wi-Fi).
 * Resolves false when it can't run (desktop, or auto-download and auto-delete both off).
 */
export async function checkNow() {
  if (!window.Capacitor?.isNativePlatform?.()) return false;
  const { started } = await window.Capacitor.Plugins.Download.checkNow();
  return !!started;
}

/**
 * Get count of entries.
 */
export function getDownloadCount() {
  return loadEntries().length;
}

/* ── Episodes stored on this device (Android only) ───────── */

/*
 * 2026-11-11 (user): in the app, an episode already downloaded to this device shows its download
 * button green (tapping it does nothing) and its play button blue (it plays the local copy, offline).
 * The set below mirrors DownloadPlugin's index (status 'complete' only); every element carrying an
 * episode id (`data-episode-id`, or `data-dl-ep` on the show page's episode rows) gets the class
 * `on-device` while that episode is stored here; the colours live in main.css.
 */
let _onDevice = new Set();

function isNative() { return !!window.Capacitor?.isNativePlatform?.(); }

/** True when this episode's file is downloaded on this device (always false off Android). */
export function isOnDevice(episodeId) {
  return episodeId != null && _onDevice.has(String(episodeId));
}

/** Toggle `on-device` on every element in `root` that names an episode. */
export function markOnDevice(root = document) {
  root.querySelectorAll('[data-episode-id], [data-dl-ep]').forEach((n) => {
    const id = n.dataset.dlEp || n.dataset.episodeId;
    n.classList.toggle('on-device', _onDevice.has(id));
  });
}

/** Re-read the native index, then re-mark the page. */
export async function refreshOnDevice() {
  if (!isNative()) return;
  try {
    const { downloads } = await window.Capacitor.Plugins.Download.listDownloads();
    _onDevice = new Set(downloads.filter((d) => d.status === 'complete').map((d) => String(d.episodeId)));
  } catch { return; }  // keep the last known set
  markOnDevice();
}

if (isNative()) {
  refreshOnDevice();
  // Pages rebuild their cards on every refresh: mark new ones as they appear (once per frame).
  let queued = false;
  new MutationObserver(() => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; markOnDevice(); });
  }).observe(document.documentElement, { childList: true, subtree: true });
  // A download finishing, the auto-delete, or a watched episode: re-read now and then.
  setInterval(refreshOnDevice, 60_000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshOnDevice(); });
  window.addEventListener('starfleet:refresh-after-watch', () => refreshOnDevice());
}

/* ── Internal ────────────────────────────────────────────── */

function triggerDownload(url) {
  // Content-Disposition: attachment means the browser saves instead of
  // navigating, so assigning location is safe — the page stays put.
  // This avoids both the hidden-<a>.click() suppression on Android
  // Firefox and the popup-blocker that kills window.open().
  window.location.assign(url);
}
