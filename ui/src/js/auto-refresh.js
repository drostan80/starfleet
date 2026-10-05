/**
 * Page auto-refresh (user 2026-10-05).
 *
 * Two triggers reach every page that imports this module:
 *
 *  1. An episode was marked or unmarked watched anywhere (this tab through api.js, another tab or
 *     page on this browser): `starfleet:refresh-after-watch` fires, the event the calendar,
 *     backlog, show, downloads pages already refresh on.
 *  2. Every so often (PERIOD_MS) and when the tab comes back into view: `starfleet:auto-refresh`
 *     fires, for pages that reload their data on it. Skipped while the tab is hidden and while
 *     the viewer is typing in a field or has a dialog open, so an edit is never wiped.
 *
 * A page listens to the events it can act on; this module only decides when.
 */

const PERIOD_MS = 60_000;      // the periodic refresh
const MIN_GAP_MS = 15_000;     // never twice within this, whatever fires
const CHANNEL = 'starfleet-watch';

let lastFired = Date.now();
let channel = null;
try { channel = new BroadcastChannel(CHANNEL); } catch { /* old browser: same-tab only */ }

/** True when a refresh would disturb what the viewer is doing. */
export function viewerIsBusy() {
  const a = document.activeElement;
  if (a && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA' || a.tagName === 'SELECT'
            || a.isContentEditable)) return true;
  return !!document.querySelector('dialog[open], [data-no-autorefresh]');
}

/** Tell every other open page (and this one) that watched state changed. */
export function broadcastWatched() {
  try { channel?.postMessage({ type: 'watched', at: Date.now() }); } catch { /* ignore */ }
}

function fireAutoRefresh(force = false) {
  if (document.hidden) return;
  const now = Date.now();
  if (now - lastFired < MIN_GAP_MS && !force) return;
  if (viewerIsBusy()) return;
  lastFired = now;
  window.dispatchEvent(new CustomEvent('starfleet:auto-refresh'));
}

if (channel) {
  channel.onmessage = (e) => {
    if (e.data && e.data.type === 'watched') {
      // a short wait: the server has just written it, and a burst of marks becomes one refresh
      clearTimeout(channel._t);
      channel._t = setTimeout(() => {
        window.dispatchEvent(new CustomEvent('starfleet:refresh-after-watch'));
      }, 600);
    }
  };
}

setInterval(() => fireAutoRefresh(false), PERIOD_MS);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden && Date.now() - lastFired > MIN_GAP_MS) fireAutoRefresh(true);
});
window.addEventListener('focus', () => {
  if (Date.now() - lastFired > PERIOD_MS / 2) fireAutoRefresh(true);
});
