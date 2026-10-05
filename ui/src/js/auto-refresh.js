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
 * A third, manual trigger exists for touch devices and the Android app (user 10-05): pull the page
 * down from the top, or press the round refresh button in the corner. Both fire
 * `starfleet:refresh-after-watch`, the event every page already reloads on.
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

/* ── Manual refresh: pull down, or the corner button (touch devices and the Android app) ── */

/** Refresh the page now, whatever the timers say. */
export function refreshNow() {
  lastFired = Date.now();
  window.dispatchEvent(new CustomEvent('starfleet:refresh-after-watch'));
}

// The packaged app resumes without always flipping `hidden`: Capacitor sends `resume`.
document.addEventListener('resume', () => fireAutoRefresh(true));

const isTouch = !!(window.Capacitor?.isNativePlatform?.()
  || (window.matchMedia && window.matchMedia('(pointer: coarse)').matches));

function installManualRefresh() {
  if (!refreshable()) return;
  const style = document.createElement('style');
  style.textContent = `
    html { overscroll-behavior-y: none; } /* the pull gesture below replaces the browser's own */
    .sf-pull { position: fixed; left: 50%; top: env(safe-area-inset-top, 0px); z-index: 2147483000;
      transform: translate(-50%, -64px); transition: transform .18s ease; pointer-events: none;
      background: rgba(24, 22, 34, .92); color: #fff; font: 600 13px system-ui, sans-serif;
      padding: 8px 14px; border-radius: 999px; box-shadow: 0 2px 10px rgba(0,0,0,.35); }
    .sf-pull.show { transform: translate(-50%, 12px); }
    .sf-refresh { position: fixed; right: 16px; z-index: 2147482000;
      bottom: calc(16px + env(safe-area-inset-bottom, 0px)); width: 46px; height: 46px;
      border-radius: 50%; border: 0; background: rgba(24, 22, 34, .88); color: #fff;
      font: 22px/1 system-ui, sans-serif; box-shadow: 0 2px 10px rgba(0,0,0,.35); cursor: pointer; }
    .sf-refresh:focus-visible { outline: 3px solid #f0a15c; outline-offset: 2px; }
    .sf-refresh.spin { animation: sf-spin .8s linear infinite; }
    @keyframes sf-spin { to { transform: rotate(360deg); } }
    @media (prefers-reduced-motion: reduce) { .sf-pull, .sf-refresh.spin { transition: none; animation: none; } }
  `;
  document.head.appendChild(style);

  const pill = document.createElement('div');
  pill.className = 'sf-pull';
  pill.setAttribute('role', 'status');
  document.body.appendChild(pill);

  const button = document.createElement('button');
  button.className = 'sf-refresh';
  button.type = 'button';
  button.title = 'Refresh this page';
  button.setAttribute('aria-label', 'Refresh this page');
  button.textContent = '\u21BB';
  document.body.appendChild(button);

  let hideTimer = null;
  function announce(text, ms = 0) {
    pill.textContent = text;
    pill.classList.add('show');
    clearTimeout(hideTimer);
    if (ms) hideTimer = setTimeout(() => pill.classList.remove('show'), ms);
  }
  function run() {
    button.classList.add('spin');
    announce('Refreshing\u2026', 1200);
    refreshNow();
    setTimeout(() => button.classList.remove('spin'), 1200);
  }
  button.addEventListener('click', run);

  // Pull down from the top of the page (only when nothing inside is scrolled, nothing is being edited).
  let startY = null, armed = false;
  const THRESHOLD = 80;
  const scrolledInside = (el) => {
    for (let n = el; n && n !== document.body; n = n.parentElement) {
      if (n.scrollTop > 0) return true;
    }
    return false;
  };
  document.addEventListener('touchstart', (e) => {
    startY = null; armed = false;
    if (e.touches.length !== 1 || window.scrollY > 0 || viewerIsBusy() || scrolledInside(e.target)) return;
    startY = e.touches[0].clientY;
  }, { passive: true });
  document.addEventListener('touchmove', (e) => {
    if (startY === null) return;
    const dy = e.touches[0].clientY - startY;
    if (dy <= 10) { if (!armed) pill.classList.remove('show'); return; }
    armed = dy >= THRESHOLD;
    announce(armed ? 'Release to refresh' : 'Pull to refresh');
  }, { passive: true });
  document.addEventListener('touchend', () => {
    if (startY !== null && armed) run(); else pill.classList.remove('show');
    startY = null; armed = false;
  }, { passive: true });
}

// Only pages that reload on the event (<html data-refreshable>) get the button and the gesture.
const refreshable = () => document.documentElement.hasAttribute('data-refreshable');
if (isTouch) {
  if (document.body) installManualRefresh();
  else document.addEventListener('DOMContentLoaded', installManualRefresh, { once: true });
}
