/**
 * Calendar page — view state, data fetching, and rendering.
 *
 * View modes: Today | 1D | 3D | Week
 * - Today: forces anchor = current day; prev/next disabled
 * - 1D:    prev/next step by 1 day
 * - 3D:    prev/next step by 3 days
 * - Week:  prev/next step by 7 days; range is Mon–Sun of anchor week
 */

import { getConfig, requireConfig, bootstrapConfig, rewriteHost } from './config.js?v=3';
import { fetchEpisodesInRange, addWatchEvent, deleteWatchEvent, setStatus } from './api.js?v=11';
import { arrIcon, _anilistSvg, _malSvg, _mpvSvg, _tvdbSvg, _imdbMarkSvg, _tmdbMarkSvg, _downloadSvg, SVC_ICONS } from './icons.js?v=8';
import { startDownload } from './downloads.js?v=2';

/* ── Constants ────────────────────────────────────────────── */

const DAY_NAMES  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const MON_NAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
const STATUSES   = ['WATCHING','COMPLETED','PLANNED','PAUSED','DROPPED'];
const STATUS_LABELS     = { WATCHING:'Watching', COMPLETED:'Completed', PLANNED:'Planned', PAUSED:'Paused', DROPPED:'Dropped' };
const STATUS_CLASS      = { WATCHING:'st-watching', COMPLETED:'st-completed', PLANNED:'st-planning', PAUSED:'st-paused', DROPPED:'st-dropped' };
const STATUS_COLOR      = { WATCHING:'var(--st-watching)', COMPLETED:'var(--st-completed)', PLANNED:'var(--st-planning)', PAUSED:'var(--st-paused)', DROPPED:'var(--st-dropped)' };
/** Unicode glyphs used on the status icon button and radial options. */
const STATUS_ICONS      = { WATCHING:'👁', COMPLETED:'✓', PLANNED:'◷', PAUSED:'⏸', DROPPED:'✕' };
/** CSS colour class for the filled icon button background. */
const STATUS_ICON_CLASS = { WATCHING:'si-watching', COMPLETED:'si-completed', PLANNED:'si-planning', PAUSED:'si-paused', DROPPED:'si-dropped' };
/** Angles (degrees) for the 5 radial picker petals: 130° arc, left-biased above the button. */
const STATUS_RADIAL_ANGLES = [-85, -53, -20, 13, 45];

/** Refresh interval when WebSocket subscriptions are unavailable (ms). */
const POLL_INTERVAL_MS = 10_000;

const STATUS_FILTER_KEY = 'starfleet_status_filter';
const TMDB_IMAGE_BASE   = 'https://image.tmdb.org/t/p/w300';

/* ── TMDB poster cache ────────────────────────────────────── */

/** showId → poster URL string (or null if fetch failed / no result) */
const posterCache = new Map();

/**
 * Sequential TMDB request queue — prevents 429 rate-limit errors when many
 * shows need posters simultaneously. Each call waits 220ms before firing,
 * keeping throughput well under TMDB's 40 req/10s limit.
 */
let _tmdbChain = Promise.resolve();
const TMDB_INTERVAL_MS = 220;

/**
 * Fetch a TMDB poster URL for a show that has no posterUrl.
 * Updates posterCache and patches any rendered card art elements in the DOM.
 * Requests are queued and spaced to avoid rate-limiting.
 */
export function fetchTmdbPoster(show, apiKey) {
  if (posterCache.has(show.id)) return;
  posterCache.set(show.id, null); // mark as in-flight / prevent duplicate fetches

  if (!apiKey) return;

  // Chain onto the sequential queue with a fixed delay between requests
  _tmdbChain = _tmdbChain
    .then(() => new Promise(r => setTimeout(r, TMDB_INTERVAL_MS)))
    .then(() => _doTmdbFetch(show, apiKey));
}

async function _doTmdbFetch(show, apiKey) {
  // Skip if the result is already known (e.g. from a cache hit on a re-render)
  if (posterCache.get(show.id) !== null) return;

  const extIds = (show.externalIds?.edges ?? []).map(e => e.node);
  const tmdbExt = extIds.find(n => n.service === 'tmdb');
  const tvdbExt = extIds.find(n => n.service === 'tvdb');

  // TMDB supports two auth formats:
  //   v3 API key (short alphanumeric) → ?api_key=...
  //   Read Access Token (long JWT eyJ...) → Authorization: Bearer ...
  const isJwt = apiKey.startsWith('eyJ');
  const tmdbHeaders = isJwt ? { Authorization: `Bearer ${apiKey}` } : {};

  const tmdbFetch = (path, extraParams = {}) => {
    const params = new URLSearchParams(extraParams);
    if (!isJwt) params.set('api_key', apiKey);
    return fetch(`https://api.themoviedb.org/3${path}?${params}`, { headers: tmdbHeaders });
  };

  try {
    let posterPath = null;

    if (tmdbExt) {
      // Direct TMDB lookup — movies use /movie/, series use /tv/
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/${type}/${tmdbExt.externalId}`, {});
      if (res.ok) {
        const data = await res.json();
        posterPath = data.poster_path ?? null;
      }
    }

    if (!posterPath && tvdbExt) {
      // Sonarr shows have TVDB IDs but no TMDB IDs in LCARS.
      // TMDB's /find endpoint cross-references external IDs.
      const res = await tmdbFetch(`/find/${tvdbExt.externalId}`, { external_source: 'tvdb_id' });
      if (res.ok) {
        const data = await res.json();
        const results = show.mediaShape === 'MOVIE'
          ? (data.movie_results ?? [])
          : (data.tv_results ?? []);
        posterPath = results[0]?.poster_path ?? null;
      }
    }

    if (!posterPath) {
      // ID lookups found nothing — fall back to a TMDB title search.
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/search/${type}`, { query: show.displayTitle, page: 1 });
      if (res.ok) {
        const data = await res.json();
        posterPath = data.results?.[0]?.poster_path ?? null;
      }
    }

    if (!posterPath) return;

    const url = TMDB_IMAGE_BASE + posterPath;
    posterCache.set(show.id, url);

    // Patch all rendered art divs for this show
    document.querySelectorAll(`.card-art[data-show-id="${show.id}"]`).forEach(art => {
      if (!art.querySelector('img')) {
        const img = document.createElement('img');
        img.src = url;
        img.alt = show.displayTitle;
        art.prepend(img);
      }
    });
  } catch {
    // Network error — leave null in cache, no retry this session
  }
}

/* ── View state ───────────────────────────────────────────── */

let state = {
  mode: '3d',       // 'today' | '1d' | '3d' | 'week'
  anchor: new Date(), // local-time reference day (time portion ignored)
};

/* ── Status filter state ─────────────────────────────────── */

/** Set of status values currently visible. Persisted in localStorage. */
let activeStatuses = loadStatusFilter();

function loadStatusFilter() {
  try {
    const stored = localStorage.getItem(STATUS_FILTER_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (Array.isArray(parsed)) return new Set(parsed);
    }
  } catch {}
  return new Set(STATUSES); // default: all on
}

function saveStatusFilter() {
  try {
    localStorage.setItem(STATUS_FILTER_KEY, JSON.stringify([...activeStatuses]));
  } catch {}
}

function toggleStatus(status) {
  if (activeStatuses.has(status)) {
    activeStatuses.delete(status);
  } else {
    activeStatuses.add(status);
  }
  saveStatusFilter();

  // Update pill appearance
  document.querySelectorAll('.sf-pill').forEach(pill => {
    pill.classList.toggle('active', activeStatuses.has(pill.dataset.status));
  });

  // Re-render without refetching (filter is client-side)
  renderEpisodes(lastFetchedEpisodes, getConfig());
}

/** Episodes from the last successful fetch — kept so status filter can
 *  re-render without hitting the network. */
let lastFetchedEpisodes = [];

/** Normalize anchor to midnight local time. */
function dayOf(d) {
  const r = new Date(d);
  r.setHours(0, 0, 0, 0);
  return r;
}

function addDays(d, n) {
  const r = new Date(d);
  r.setDate(r.getDate() + n);
  return r;
}

/** Monday of the ISO week containing d. */
function isoWeekMon(d) {
  const r = dayOf(d);
  r.setDate(r.getDate() - (r.getDay() + 6) % 7);
  return r;
}

/**
 * Compute UTC ISO start and end strings for the current view state.
 * Returns a half-open interval [start, end) in UTC ISO format.
 */
function computeRange() {
  const anchor = dayOf(state.anchor);

  let startLocal, endLocal;
  switch (state.mode) {
    case 'today':
    case '1d':
      startLocal = anchor;
      endLocal   = addDays(anchor, 1);
      break;
    case '3d':
      startLocal = anchor;
      endLocal   = addDays(anchor, 3);
      break;
    case 'week': {
      startLocal = isoWeekMon(anchor);
      endLocal   = addDays(startLocal, 7);
      break;
    }
  }

  return {
    start: startLocal.toISOString(),
    end:   endLocal.toISOString(),
    startLocal,
    endLocal,
  };
}

/* ── Date formatting ─────────────────────────────────────── */

function fmtShortDate(d) {
  return `${d.getDate()} ${MON_NAMES[d.getMonth()]} ${d.getFullYear()}`;
}

function fmtDayName(d) {
  return DAY_NAMES[d.getDay()];
}

/** Format for the date-range label in the controls bar. */
function fmtRangeLabel(startLocal, endLocal) {
  // endLocal is exclusive — the last visible day is endLocal - 1 day
  const lastDay = addDays(endLocal, -1);
  if (startLocal.toDateString() === lastDay.toDateString()) {
    // single day
    const isToday = startLocal.toDateString() === dayOf(new Date()).toDateString();
    if (isToday && state.mode === 'today') {
      return `Today · ${fmtShortDate(startLocal)}`;
    }
    return `${fmtDayName(startLocal)} ${fmtShortDate(startLocal)}`;
  }
  // multi-day — show abbreviated if same month+year
  if (startLocal.getMonth() === lastDay.getMonth() && startLocal.getFullYear() === lastDay.getFullYear()) {
    return `${fmtDayName(startLocal)} ${startLocal.getDate()} — ${fmtDayName(lastDay)} ${lastDay.getDate()} ${MON_NAMES[lastDay.getMonth()]} ${lastDay.getFullYear()}`;
  }
  return `${fmtDayName(startLocal)} ${fmtShortDate(startLocal)} — ${fmtDayName(lastDay)} ${fmtShortDate(lastDay)}`;
}

/** Format air time for display (e.g. "17:00 JST"). */
export function fmtAirTime(isoUtc) {
  if (!isoUtc) return '—';
  const d = new Date(isoUtc);
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
    + ' ' + (Intl.DateTimeFormat().resolvedOptions().timeZone?.split('/').pop()?.replace('_', ' ') || '');
}

/** Episode badge string: S01E08 or absolute ep number fallback. */
export function fmtEpBadge(ep) {
  if (ep.season != null && ep.episode != null) {
    const s = String(ep.season).padStart(2, '0');
    const e = String(ep.episode).padStart(2, '0');
    return `S${s}E${e}`;
  }
  if (ep.absoluteNumber != null) return `#${ep.absoluteNumber}`;
  return '—';
}

/* ── Availability classification ─────────────────────────── */

/**
 * Returns 'ready' | 'downloading' | 'missing' | 'future' based on availability.
 * - ready:       file imported and available
 * - downloading: Sonarr/Radarr has it queued or actively downloading
 * - missing:     aired but UNAVAILABLE and not downloading (nothing in the queue)
 * - future:      has not aired yet
 */
export function availState(ep) {
  const isMovie = ep.show.mediaShape === 'MOVIE';
  const status  = isMovie ? ep.availableViaRadarr : ep.availableViaSonarr;

  if (status === 'AVAILABLE')   return 'ready';
  if (status === 'DOWNLOADING') return 'downloading';

  // UNAVAILABLE — distinguish aired (missing) vs future
  if (!ep.airDateUtc) return 'future';
  return new Date(ep.airDateUtc) <= new Date() ? 'missing' : 'future';
}

/* ── Service strip helpers ───────────────────────────────── */


const SVC_DEFS = [
  { key: 'anilist', cls: 'svc-al',     title: 'AniList', rewrite: false, svg: _anilistSvg,         always: true  },
  { key: 'mal',     cls: 'svc-mal',    title: 'MAL',     rewrite: false, svg: _malSvg,              always: true  },
  { key: 'tvdb',    cls: 'svc-tvdb',   title: 'TVDB',    rewrite: false, svg: _tvdbSvg,             always: false },
  { key: 'imdb',    cls: 'svc-imdb',   title: 'IMDb',    rewrite: false, svg: _imdbMarkSvg,         always: false },
  { key: 'tmdb',    cls: 'svc-tmdb',   title: 'TMDB',    rewrite: false, svg: _tmdbMarkSvg,         always: false },
  { key: 'sonarr',  cls: 'svc-sonarr', title: 'Sonarr',  rewrite: true,  svg: arrIcon('#00C8FF'), always: true  },
  { key: 'radarr',  cls: 'svc-radarr', title: 'Radarr',  rewrite: true,  svg: arrIcon('#FFC230'), always: true  },
];

/* ── mpv launcher ────────────────────────────────────────── */

/**
 * Launch a file in mpv via the local helper daemon.
 * filePath: server-side path (/data/media/…)
 * cfg: config object
 */
export async function launchMpv(filePath, cfg) {
  if (!filePath) {
    showBanner('No file available for this episode', 'error');
    return;
  }

  const helperUrl = (cfg.mpv_helper_url || 'http://localhost:19450').replace(/\/$/, '');

  // filePathSonarr/Radarr is the path inside the server container (/data/…).
  // nginx serves /data/ at /files/, so strip the /data prefix to form the URL.
  const mediaPath = filePath.startsWith('/data') ? filePath.slice('/data'.length) : filePath;
  const mediaUrl  = `${cfg.lcars_url}/files${mediaPath}`;

  try {
    // Timeout: browsers on non-secure contexts may silently block
    // Private Network Access requests (LAN page → localhost) without
    // ever resolving or rejecting the fetch promise.
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 4000);
    const res = await fetch(`${helperUrl}/play`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ url: mediaUrl }),
      signal:  controller.signal,
    });
    clearTimeout(timer);
    if (!res.ok) throw new Error(`helper returned ${res.status}`);
    const body = await res.text();
    if (body === 'FAILED') {
      showBanner('No mpv output — try Jellyfin', 'error');
      return;
    }
    showBanner('▶ Launching mpv…', 'info');
    setTimeout(hideBanner, 2500);
  } catch (err) {
    showBanner('No mpv output — try Jellyfin', 'error');
  }
}

/**
 * Build the left service strip element for a card.
 * externalIds: array of { service, url } objects
 * malId: season-level MAL id (integer) — fallback when show has no mal externalId row
 * filePath: server-side file path (filePathSonarr or filePathRadarr), or null
 * availableLocally: bool
 */
export function buildSvcStrip(externalIds, malId, filePath, availableLocally, cfg) {
  const byService = {};
  for (const ei of externalIds) {
    // prefer the real link over the :add synthetic link
    if (!byService[ei.service] || ei.service.endsWith(':add') === false) {
      byService[ei.service] = ei;
    }
  }

  // MAL IDs live on the season, not the show — synthesise the link from
  // seasonEntity.malId when the show has no show-level 'mal' externalId row.
  if (!byService['mal'] && malId) {
    byService['mal'] = {
      service:    'mal',
      externalId: String(malId),
      url:        `https://myanimelist.net/anime/${malId}`,
    };
  }

  const strip = document.createElement('div');
  strip.className = 'svc-strip';

  for (const def of SVC_DEFS) {
    const realLink = byService[def.key];          // actual followed link
    const addLink  = byService[`${def.key}:add`]; // synthetic add-new link
    const linked   = realLink || addLink;

    // Conditional icons (tvdb/imdb/tmdb) only appear when a link exists
    if (!def.always && !realLink) continue;

    const a = document.createElement('a');
    // Full colour (.on) only for real links, not :add synthetics
    a.className = `svc ${def.cls}${realLink ? ' on' : ''}`;
    a.title = def.title;
    a.innerHTML = def.svg;

    if (linked) {
      const url = def.rewrite
        ? rewriteHost(linked.url, cfg.home_server_host)
        : linked.url;
      a.href = url;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    } else {
      a.href = '#';
      a.addEventListener('click', e => e.preventDefault());
    }
    strip.appendChild(a);
  }

  // Player icon
  const playerA = document.createElement('a');
  playerA.className = `svc svc-player${availableLocally ? ' on' : ''}`;
  playerA.title = availableLocally ? 'Play in mpv' : 'No local file';
  playerA.innerHTML = _mpvSvg;
  playerA.href = '#';
  playerA.addEventListener('click', async e => {
    e.preventDefault();
    if (!availableLocally) return;
    playerA.textContent = '…';
    await launchMpv(filePath, cfg);
    playerA.textContent = '▶';
  });
  strip.appendChild(playerA);

  return strip;
}

/* ── Per-show episode stats ───────────────────────────────── */

/**
 * Show-level episode total — the one accurate count we have from the show node.
 * (A per-show watched count is not exposed by the API at query time.)
 */
export function showTotal(show) {
  return show.totalEpisodes ?? '?';
}

/* ── Card rendering ──────────────────────────────────────── */

/**
 * Build a single episode card DOM element.
 *
 * @param {object} ep        - Episode node from GraphQL
 * @param {object} showStats - { available, aired, total } for this show
 * @param {object} cfg       - Config object
 * @returns {HTMLElement}
 */
export function buildCard(ep, cfg) {
  const avail  = availState(ep);
  const aired  = ep.airDateUtc && new Date(ep.airDateUtc) <= new Date();
  const status = ep.seasonEntity?.status || ep.show.status || 'WATCHING';
  const externalIds = ep.show.externalIds.edges.map(e => e.node);

  const card = document.createElement('article');
  card.className = 'show-card';
  card.dataset.episodeId = ep.id;
  card.dataset.showId    = ep.show.id;

  /* ── Meta bar ── */
  const meta = document.createElement('div');
  meta.className = 'card-meta';

  const badge = document.createElement('span');
  badge.className = 'ep-badge';
  badge.textContent = fmtEpBadge(ep);
  meta.appendChild(badge);

  const airtime = document.createElement('span');
  airtime.className = 'airtime';
  airtime.textContent = fmtAirTime(ep.airDateUtc);
  meta.appendChild(airtime);

  const availIcon = document.createElement('span');
  availIcon.className = `avail-icon avail-${avail}`;
  availIcon.title = {
    ready:       'Available',
    downloading: 'Downloading',
    missing:     'Aired — not available and not downloading',
    future:      'Not yet aired',
  }[avail];
  availIcon.textContent = avail === 'ready' ? '▶' : avail === 'future' ? '◷' : '⬇';
  meta.appendChild(availIcon);

  // Watch button — only for aired episodes
  if (aired) {
    const watchBtn = document.createElement('button');
    watchBtn.className = `watch-btn${ep.state === 'WATCHED' ? ' watched' : ''}`;
    watchBtn.title = ep.state === 'WATCHED' ? 'Watched — click to unmark' : 'Mark as watched';
    watchBtn.textContent = '✓';
    watchBtn.dataset.state = ep.state;
    // Pre-populate watchEventId from the query so unwatch works without refetch
    const existingWid = ep.watchEvents?.edges?.[0]?.node?.id;
    if (existingWid) watchBtn.dataset.watchEventId = existingWid;
    watchBtn.addEventListener('click', () => onWatchToggle(watchBtn, ep));
    meta.appendChild(watchBtn);
  }

  card.appendChild(meta);

  /* ── Card body ── */
  const body = document.createElement('div');
  body.className = 'card-body';

  // Service strip
  const filePath = ep.show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;
  const malId    = ep.seasonEntity?.malId ?? null;
  body.appendChild(buildSvcStrip(externalIds, malId, filePath, ep.availableLocally, cfg));

  // Cover art — use posterUrl if available; otherwise use TMDB cache or trigger a fetch
  const art = document.createElement('div');
  art.className = 'card-art';
  art.dataset.showId = ep.show.id;

  // Click on art launches mpv for available episodes
  if (ep.availableLocally) {
    art.classList.add('art-playable');
    art.addEventListener('click', () => launchMpv(filePath, cfg));
  }

  const posterUrl = ep.show.posterUrl || posterCache.get(ep.show.id) || null;
  if (posterUrl) {
    const img = document.createElement('img');
    img.src = posterUrl;
    img.alt = ep.show.displayTitle;
    // No loading="lazy" — cards render in a wrapped flex row, not off-screen
    // below a scroll boundary, so lazy-loading only delays the request.
    // If the LCARS-provided URL fails (Sonarr auth, broken path, etc.), fall back to TMDB
    if (cfg.tmdb_api_key) {
      img.onerror = () => {
        img.remove();
        posterCache.delete(ep.show.id); // allow fresh fetch
        fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
      };
    }
    art.appendChild(img);
  } else if (cfg.tmdb_api_key) {
    // Kick off TMDB fetch; it will patch the DOM when it resolves
    fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
  }
  if (ep.show.score != null) {
    const score = document.createElement('span');
    score.className = 'art-score';
    score.textContent = `★ ${ep.show.score.toFixed(1)}`;
    art.appendChild(score);
  }
  body.appendChild(art);

  card.appendChild(body);

  /* ── Footer ── */
  const footer = document.createElement('div');
  footer.className = 'card-footer';

  const title = document.createElement('a');
  title.className = 'show-title';
  title.href = `show.html?id=${encodeURIComponent(ep.show.id)}`;
  title.textContent = ep.show.displayTitle;
  title.title = ep.show.displayTitle;
  footer.appendChild(title);

  // Episode count — sits in the footer's normal flow
  const total = showTotal(ep.show);
  if (total !== '?') {
    const counts = document.createElement('div');
    counts.className = 'ep-counts';
    counts.innerHTML = `<span class="c-total">${total} ep</span>`;
    footer.appendChild(counts);
  }

  card.appendChild(footer);

  // Download button — absolutely pinned to bottom-left of the card
  if (ep.availableLocally && filePath) {
    const dlBtn = document.createElement('button');
    dlBtn.className = 'card-dl-btn';
    dlBtn.title = 'Download';
    dlBtn.innerHTML = _downloadSvg;
    dlBtn.addEventListener('click', e => {
      e.stopPropagation();
      startDownload({
        showTitle: ep.show.displayTitle,
        label: fmtEpBadge(ep) + (ep.title ? ` — ${ep.title}` : ''),
        filePath,
      });
      dlBtn.classList.add('triggered');
      setTimeout(() => dlBtn.classList.remove('triggered'), 1200);
    });
    card.appendChild(dlBtn);
  }

  // Status button — absolutely pinned to bottom-right of the card itself
  card.appendChild(buildStatusBtn(ep.show.id, status));

  return card;
}

/* ── Status icon button + radial picker ──────────────────── */

/**
 * Build a compact status icon button with a semicircle hover-picker.
 * Replaces the old slide-out status-tab overlay on the card art.
 *
 * @param {string} showId
 * @param {string} currentStatus  - one of STATUSES
 * @returns {HTMLElement}  .status-btn-wrap
 */
export function buildStatusBtn(showId, currentStatus) {
  const wrap = document.createElement('div');
  wrap.className = 'status-btn-wrap';
  wrap.dataset.showId = showId;
  wrap.dataset.status = currentStatus;

  // Main icon button
  const mainBtn = document.createElement('button');
  mainBtn.className = `status-icon-btn ${STATUS_ICON_CLASS[currentStatus] || 'si-watching'}`;
  mainBtn.title = STATUS_LABELS[currentStatus] || currentStatus;
  mainBtn.textContent = STATUS_ICONS[currentStatus] || '●';
  wrap.appendChild(mainBtn);

  // Open/close logic: close only when the cursor is >15 px from every
  // element in the wrap (button + all petals), measured against their
  // actual rendered bounding rects (which honour CSS transforms).
  let _moveHandler = null;

  const closeRadial = () => {
    wrap.classList.remove('radial-open');
    if (_moveHandler) {
      document.removeEventListener('mousemove', _moveHandler);
      _moveHandler = null;
    }
  };

  const openRadial = () => {
    wrap.classList.add('radial-open');
    if (_moveHandler) return; // already watching
    _moveHandler = e => {
      // Check distance from cursor to the button centre.
      // Threshold = fan radius (62) + petal half-size (13) + comfort margin (20) = 95 px.
      // Using a circle avoids racing the CSS transition: we test the static
      // geometry of the full fan zone rather than each petal's mid-animation rect.
      const r  = wrap.getBoundingClientRect();
      const cx = r.left + r.width  / 2;
      const cy = r.top  + r.height / 2;
      if (Math.hypot(e.clientX - cx, e.clientY - cy) > 95) closeRadial();
    };
    document.addEventListener('mousemove', _moveHandler);
  };

  wrap.addEventListener('mouseenter', openRadial);

  // Radial picker petals — one per status
  for (let i = 0; i < STATUSES.length; i++) {
    const s = STATUSES[i];
    const opt = document.createElement('button');
    opt.className = `sr-opt ${STATUS_ICON_CLASS[s]}${s === currentStatus ? ' cur' : ''}`;
    opt.style.setProperty('--a', `${STATUS_RADIAL_ANGLES[i]}deg`);
    opt.title = STATUS_LABELS[s];
    opt.textContent = STATUS_ICONS[s];
    opt.dataset.status = s;
    opt.addEventListener('click', e => {
      e.stopPropagation();
      onStatusChange(wrap, showId, s);
    });
    wrap.appendChild(opt);
  }

  return wrap;
}

/* ── Interactions ────────────────────────────────────────── */

export async function onWatchToggle(btn, ep) {
  if (btn.classList.contains('loading')) return;
  btn.classList.add('loading');

  try {
    if (btn.dataset.state === 'WATCHED') {
      // watchEventId is pre-populated from the query (watchEvents(first:1))
      if (btn.dataset.watchEventId) {
        await deleteWatchEvent(btn.dataset.watchEventId);
        btn.classList.remove('watched');
        btn.dataset.state = 'UNWATCHED';
        btn.title = 'Mark as watched';
        btn.dataset.watchEventId = '';
      } else {
        // Fallback: no id stored — show error (should not normally happen)
        throw new Error('No watch event id — try refreshing the page');
      }
    } else {
      const wid = await addWatchEvent(ep.show.id, ep.season, ep.episode);
      btn.classList.add('watched');
      btn.dataset.state = 'WATCHED';
      btn.title = 'Watched — click to unmark';
      btn.dataset.watchEventId = wid;
    }
  } catch (err) {
    console.error('Watch toggle failed:', err);
    showBanner(`Watch error: ${err.message}`, 'error');
  } finally {
    btn.classList.remove('loading');
  }
}

export async function onStatusChange(wrap, showId, newStatus) {
  if (newStatus === wrap.dataset.status) return;
  try {
    try {
      await setStatus(showId, newStatus);
    } catch (err) {
      // Completion guard — prompt for confirmation
      if (newStatus === 'COMPLETED' && err.message?.includes('confirmed: true')) {
        if (!confirm('This show still has unaired or undated episodes. Mark completed anyway?')) {
          return;
        }
        await setStatus(showId, newStatus, true);
      } else {
        throw err;
      }
    }

    // Update every status button for this show (multiple episodes may share it)
    document.querySelectorAll(`.status-btn-wrap[data-show-id="${showId}"]`).forEach(w => {
      w.dataset.status = newStatus;

      // Refresh the main icon button
      const mainBtn = w.querySelector('.status-icon-btn');
      if (mainBtn) {
        for (const s of STATUSES) mainBtn.classList.remove(STATUS_ICON_CLASS[s]);
        mainBtn.classList.add(STATUS_ICON_CLASS[newStatus]);
        mainBtn.textContent = STATUS_ICONS[newStatus];
        mainBtn.title = STATUS_LABELS[newStatus];
      }

      // Refresh the active ring on each petal
      w.querySelectorAll('.sr-opt').forEach(opt => {
        opt.classList.toggle('cur', opt.dataset.status === newStatus);
      });
    });

    // Dispatch custom event so show page can react (e.g. bulk-mark watched)
    wrap.dispatchEvent(new CustomEvent('status-confirmed', {
      bubbles: true, detail: { showId, status: newStatus }
    }));
  } catch (err) {
    console.error('Status change failed:', err);
    showBanner(`Status error: ${err.message}`, 'error');
  }
}

/* ── Banner ──────────────────────────────────────────────── */

export function showBanner(msg, type = 'info') {
  const banner = document.getElementById('status-banner');
  if (!banner) return;
  banner.className = `status-banner ${type}`;
  banner.innerHTML = type === 'loading'
    ? `<span class="spinner"></span>${msg}`
    : msg;
}

export function hideBanner() {
  const banner = document.getElementById('status-banner');
  if (banner) banner.className = 'status-banner hidden';
}

/* ── Calendar rendering ──────────────────────────────────── */

/**
 * Group episodes by local air date, applying tracked and status filters.
 * Returns an array of { dateStr, localDate, episodes[] } sorted by date.
 */
function groupByDay(episodes) {
  const groups = {};
  for (const ep of episodes) {
    if (!ep.show.tracked) continue;                      // skip merged-away duplicates
    const epStatus = ep.seasonEntity?.status || ep.show.status;
    if (!activeStatuses.has(epStatus)) continue;  // status filter

    const localDate = ep.airDateUtc ? new Date(ep.airDateUtc) : null;
    const dateStr   = localDate
      ? `${localDate.getFullYear()}-${String(localDate.getMonth()+1).padStart(2,'0')}-${String(localDate.getDate()).padStart(2,'0')}`
      : 'unknown';

    if (!groups[dateStr]) groups[dateStr] = { dateStr, localDate, episodes: [] };
    groups[dateStr].episodes.push(ep);
  }

  return Object.values(groups).sort((a, b) => {
    if (!a.localDate) return 1;
    if (!b.localDate) return -1;
    return a.localDate - b.localDate;
  });
}

/** Render episodes into the calendar DOM (no network call). */
function renderEpisodes(episodes, cfg) {
  if (!cfg) return;

  const calendar = document.getElementById('calendar');
  if (!calendar) return;
  calendar.innerHTML = '';

  const dayGroups = groupByDay(episodes);

  if (dayGroups.length === 0) {
    const empty = document.createElement('div');
    empty.className = 'calendar-empty';
    empty.textContent = activeStatuses.size === 0
      ? 'All statuses hidden — enable some in the Show filter.'
      : 'No episodes in this range.';
    calendar.appendChild(empty);
    return;
  }

  const todayStr  = (() => {
    const n = new Date();
    return `${n.getFullYear()}-${String(n.getMonth()+1).padStart(2,'0')}-${String(n.getDate()).padStart(2,'0')}`;
  })();

  for (const group of dayGroups) {
    const section = document.createElement('section');
    section.className = `day-group${group.dateStr === todayStr ? ' day-today' : ''}`;

    const header = document.createElement('h2');
    header.className = 'day-header';
    header.innerHTML = group.localDate
      ? `<span class="day-name">${fmtDayName(group.localDate)}</span>
         <span class="day-date">${fmtShortDate(group.localDate)}</span>`
      : `<span class="day-name">Unknown</span>`;
    section.appendChild(header);

    const row = document.createElement('div');
    row.className = 'card-row';

    group.episodes.sort((a, b) => {
      if (!a.airDateUtc) return 1;
      if (!b.airDateUtc) return -1;
      return new Date(a.airDateUtc) - new Date(b.airDateUtc);
    });

    for (const ep of group.episodes) {
      row.appendChild(buildCard(ep, cfg));
    }

    section.appendChild(row);
    calendar.appendChild(section);
  }
}

/**
 * Fetch episodes for the current range, then render.
 * @param {boolean} [silent=false] - If true, suppress the loading banner
 *   (used for background polls so the page doesn't visually shift).
 *   Errors are always shown regardless.
 */
async function render(silent = false) {
  const cfg = requireConfig();
  if (!cfg) return;

  const { start, end, startLocal, endLocal } = computeRange();

  const label = document.getElementById('date-range-label');
  if (label) label.textContent = fmtRangeLabel(startLocal, endLocal);

  ['prev-btn', 'next-btn'].forEach(id => {
    const btn = document.getElementById(id);
    if (btn) btn.disabled = state.mode === 'today';
  });

  if (!silent) showBanner('Loading…', 'loading');

  try {
    lastFetchedEpisodes = await fetchEpisodesInRange(start, end);
  } catch (err) {
    showBanner(`Failed to load: ${err.message}`, 'error');
    return;
  }

  hideBanner();
  renderEpisodes(lastFetchedEpisodes, cfg);
}

/* ── Navigation ──────────────────────────────────────────── */

function navigate(delta) {
  const steps = { today: 0, '1d': 1, '3d': 2, week: 7 };
  state.anchor = addDays(dayOf(state.anchor), delta * steps[state.mode]);
  render();
}

function setMode(mode) {
  state.mode = mode;
  if (mode === 'today') state.anchor = new Date();

  document.querySelectorAll('.view-mode').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });

  render();
}

/* ── Keyboard navigation ─────────────────────────────────── */

const KEYBIND_HELP = [
  ['←  h',  'Previous period'],
  ['→  l',  'Next period'],
  ['0',     'Jump to today'],
  ['1',     '1-day view'],
  ['3',     '3-day view'],
  ['7',     'Week view'],
  ['r',     'Refresh now'],
  ['?',     'Show this help'],
].map(([k, d]) => `<b>${k}</b> — ${d}`).join('  ·  ');

/** Returns true when a key event should be ignored (focus inside editable). */
function isEditableFocused() {
  const el = document.activeElement;
  if (!el) return false;
  return el.tagName === 'INPUT' || el.tagName === 'TEXTAREA' || el.isContentEditable;
}

function initKeyboardNav() {
  console.debug('[starfleet] keyboard nav armed');
  document.addEventListener('keydown', e => {
    // Never fire when the user is typing in an input/textarea
    if (isEditableFocused()) return;
    // Never fire for modifier combos (ctrl/meta shortcuts are browser's)
    if (e.ctrlKey || e.metaKey || e.altKey) return;

    console.debug('[starfleet] key:', e.key, 'mode:', state.mode);
    switch (e.key) {
      case 'ArrowLeft':
      case 'h':
        if (state.mode !== 'today') { e.preventDefault(); navigate(-1); }
        break;

      case 'ArrowRight':
      case 'l':
        if (state.mode !== 'today') { e.preventDefault(); navigate(+1); }
        break;

      case '0':
        e.preventDefault();
        state.anchor = new Date();
        render();
        break;

      case '1':
        e.preventDefault();
        setMode('1d');
        break;

      case '3':
        e.preventDefault();
        setMode('3d');
        break;

      case '7':
        e.preventDefault();
        setMode('week');
        break;

      case 'r':
        e.preventDefault();
        render();
        break;

      case '?':
        e.preventDefault();
        showBanner(KEYBIND_HELP, 'info');
        // Auto-dismiss after 6 s; clicking banner also hides it
        clearTimeout(initKeyboardNav._helpTimer);
        initKeyboardNav._helpTimer = setTimeout(hideBanner, 6_000);
        break;
    }
  });

  // Clicking the info banner dismisses it
  document.getElementById('status-banner')?.addEventListener('click', () => {
    hideBanner();
    clearTimeout(initKeyboardNav._helpTimer);
  });
}

/* ── Init ────────────────────────────────────────────────── */

/** Wire up controls and kick off first render. */
export async function init() {
  const cfg = await bootstrapConfig();
  if (!cfg) return;

  // View mode buttons
  document.querySelectorAll('.view-mode').forEach(btn => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });

  // Prev / Next
  const prevBtn = document.getElementById('prev-btn');
  const nextBtn = document.getElementById('next-btn');
  if (prevBtn) prevBtn.addEventListener('click', () => navigate(-1));
  if (nextBtn) nextBtn.addEventListener('click', () => navigate(+1));

  // Today button
  const todayBtn = document.getElementById('today-btn');
  if (todayBtn) {
    todayBtn.addEventListener('click', () => {
      state.anchor = new Date();
      render();
    });
  }

  // Set initial active mode button
  document.querySelectorAll('.view-mode').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === state.mode);
  });

  // Status filter pills — sync initial state from localStorage, wire clicks
  document.querySelectorAll('.sf-pill').forEach(pill => {
    const status = pill.dataset.status;
    pill.classList.toggle('active', activeStatuses.has(status));
    pill.addEventListener('click', () => toggleStatus(status));
  });

  // Keyboard navigation
  initKeyboardNav();

  // Initial render
  render();

  // Polling fallback — re-render silently in background
  // (replaces WebSocket subscriptions, which require server-side changes for
  //  browser auth — see api.js for the full explanation)
  setInterval(() => render(true), POLL_INTERVAL_MS);
}
