/**
 * Global search — triggered by `/` key, renders a search field in the
 * top nav with a dropdown results list.  Uses the LCARS `search` query
 * (full-text across title variants + synopses).
 *
 * Usage: import { initSearch } from './search.js'; initSearch();
 */

import { gql } from './api.js?v=22';

/* ── State ────────────────────────────────────────────────── */

let overlay   = null;   // .search-overlay backdrop
let input     = null;   // <input>
let results   = null;   // .search-results container
let debounce  = null;   // setTimeout handle
let abortCtrl = null;   // AbortController for in-flight query
let open      = false;

/* ── GraphQL ──────────────────────────────────────────────── */

const SEARCH_QUERY = `
  query Search($query: String!, $first: Int) {
    search(query: $query, first: $first) {
      edges { node {
        id displayTitle status score mediaShape trackingSpace
        posterUrl totalEpisodes
      }}
    }
  }
`;

async function runSearch(query) {
  if (abortCtrl) abortCtrl.abort();
  abortCtrl = new AbortController();

  try {
    const data = await gql(SEARCH_QUERY, { query, first: 12 });
    return data.search.edges.map(e => e.node);
  } catch (err) {
    if (err.name === 'AbortError') return null;
    console.error('[search]', err);
    return [];
  }
}

/* ── Status helpers ───────────────────────────────────────── */

const STATUS_COLORS = {
  WATCHING:  'var(--st-watching)',
  COMPLETED: 'var(--st-completed)',
  PLANNED:   'var(--st-planning)',
  PAUSED:    'var(--st-paused)',
  DROPPED:   'var(--st-dropped)',
};

const STATUS_LABELS = {
  WATCHING:  'Watching',
  COMPLETED: 'Completed',
  PLANNED:   'Planning',
  PAUSED:    'Paused',
  DROPPED:   'Dropped',
};

/* ── DOM construction ─────────────────────────────────────── */

function buildOverlay() {
  // Backdrop
  overlay = document.createElement('div');
  overlay.className = 'search-overlay';
  overlay.hidden = true;
  overlay.addEventListener('click', e => {
    if (e.target === overlay) closeSearch();
  });

  // Container
  const box = document.createElement('div');
  box.className = 'search-box';

  // Input row
  const inputRow = document.createElement('div');
  inputRow.className = 'search-input-row';

  const icon = document.createElement('span');
  icon.className = 'search-icon';
  icon.textContent = '⌕';

  input = document.createElement('input');
  input.type = 'text';
  input.className = 'search-input';
  input.placeholder = 'Search shows…';
  input.setAttribute('autocomplete', 'off');
  input.setAttribute('spellcheck', 'false');

  const hint = document.createElement('kbd');
  hint.className = 'search-hint';
  hint.textContent = 'esc';

  inputRow.append(icon, input, hint);

  // Results
  results = document.createElement('div');
  results.className = 'search-results';

  box.append(inputRow, results);
  overlay.appendChild(box);
  document.body.appendChild(overlay);

  // Events
  input.addEventListener('input', onInput);
  input.addEventListener('keydown', onKeydown);
}

/* ── Open / close ─────────────────────────────────────────── */

function openSearch() {
  if (open) return;
  if (!overlay) buildOverlay();
  open = true;
  overlay.hidden = false;
  input.value = '';
  results.innerHTML = '';
  results.hidden = true;
  // Small delay so the focus lands after the overlay paints
  requestAnimationFrame(() => input.focus());
}

function closeSearch() {
  if (!open) return;
  open = false;
  overlay.hidden = true;
  input.value = '';
  results.innerHTML = '';
  if (debounce) clearTimeout(debounce);
  if (abortCtrl) abortCtrl.abort();
}

/* ── Input handling ───────────────────────────────────────── */

let activeIdx = -1;

function onInput() {
  const q = input.value.trim();
  if (debounce) clearTimeout(debounce);

  if (q.length < 2) {
    results.innerHTML = '';
    results.hidden = true;
    activeIdx = -1;
    return;
  }

  debounce = setTimeout(async () => {
    const shows = await runSearch(q);
    if (shows === null) return; // aborted
    renderResults(shows);
  }, 200);
}

function onKeydown(e) {
  if (e.key === 'Escape') {
    e.preventDefault();
    closeSearch();
    return;
  }

  const items = results.querySelectorAll('.search-result');
  if (!items.length) return;

  if (e.key === 'ArrowDown') {
    e.preventDefault();
    activeIdx = Math.min(activeIdx + 1, items.length - 1);
    highlightItem(items);
  } else if (e.key === 'ArrowUp') {
    e.preventDefault();
    activeIdx = Math.max(activeIdx - 1, 0);
    highlightItem(items);
  } else if (e.key === 'Enter') {
    e.preventDefault();
    if (activeIdx >= 0 && items[activeIdx]) {
      items[activeIdx].click();
    }
  }
}

function highlightItem(items) {
  items.forEach((it, i) => {
    it.classList.toggle('active', i === activeIdx);
    if (i === activeIdx) it.scrollIntoView({ block: 'nearest' });
  });
}

/* ── Render results ───────────────────────────────────────── */

function renderResults(shows) {
  results.innerHTML = '';
  activeIdx = -1;

  if (!shows.length) {
    const empty = document.createElement('div');
    empty.className = 'search-empty';
    empty.textContent = 'No results';
    results.appendChild(empty);
    results.hidden = false;
    return;
  }

  for (const show of shows) {
    const item = document.createElement('a');
    item.className = 'search-result';
    item.href = `show.html?id=${show.id}`;

    // Poster thumbnail
    const poster = document.createElement('img');
    poster.className = 'search-poster';
    poster.src = show.posterUrl || '';
    poster.alt = '';
    poster.loading = 'lazy';
    poster.onerror = () => { poster.style.display = 'none'; };
    item.appendChild(poster);

    // Info
    const info = document.createElement('div');
    info.className = 'search-info';

    const title = document.createElement('span');
    title.className = 'search-title';
    title.textContent = show.displayTitle;
    info.appendChild(title);

    const meta = document.createElement('span');
    meta.className = 'search-meta';
    const parts = [];
    if (show.status) {
      const color = STATUS_COLORS[show.status] || 'var(--muted)';
      const label = STATUS_LABELS[show.status] || show.status;
      parts.push(`<span class="search-status" style="color:${color}">${label}</span>`);
    }
    if (show.totalEpisodes) parts.push(`${show.totalEpisodes} ep`);
    if (show.score != null) parts.push(`★ ${show.score}`);
    meta.innerHTML = parts.join(' · ');
    info.appendChild(meta);

    item.appendChild(info);
    results.appendChild(item);
  }

  results.hidden = false;
}

/* ── Keyboard shortcut ────────────────────────────────────── */

function isEditableFocused() {
  const el = document.activeElement;
  if (!el) return false;
  if (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.tagName === 'SELECT') return true;
  return el.isContentEditable;
}

export function initSearch() {
  document.addEventListener('keydown', e => {
    // `/` opens search when not typing in an input
    if (e.key === '/' && !isEditableFocused() && !e.ctrlKey && !e.metaKey) {
      e.preventDefault();
      openSearch();
    }
  });

  // Also wire up any existing nav search button
  const navBtn = document.getElementById('nav-search-btn');
  if (navBtn) {
    navBtn.addEventListener('click', e => {
      e.preventDefault();
      openSearch();
    });
  }

  // Update the Reviews nav badge with unresolved count
  updateReviewBadge();
}

/** Fetch unresolved review count and show a badge on the Reviews nav link. */
async function updateReviewBadge() {
  const link = document.querySelector('a[data-nav="reviews"]');
  if (!link) return;
  try {
    // Paginate to count all open reviews (connection has no totalCount)
    let count = 0;
    let after = null;
    while (true) {
      const data = await gql(
        `query($after: String) { pendingReviews(includeResolved: false, first: 100, after: $after) {
          edges { node { id } } pageInfo { hasNextPage endCursor }
        }}`, { after });
      count += data.pendingReviews.edges.length;
      if (!data.pendingReviews.pageInfo.hasNextPage) break;
      after = data.pendingReviews.pageInfo.endCursor;
    }
    if (count > 0) {
      let badge = link.querySelector('.nav-badge');
      if (!badge) {
        badge = document.createElement('span');
        badge.className = 'nav-badge';
        link.appendChild(badge);
      }
      badge.textContent = count > 99 ? '99+' : count;
    }
  } catch { /* config missing or network error — skip silently */ }
}
