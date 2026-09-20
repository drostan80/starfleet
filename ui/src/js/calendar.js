/**
 * Calendar page — view state, data fetching, and rendering.
 *
 * View modes: Today | 1D | 3D | Week
 * - Today: forces anchor = current day; prev/next disabled
 * - 1D:    prev/next step by 1 day
 * - 3D:    prev/next step by 3 days
 * - Week:  prev/next step by 7 days; range is Mon–Sun of anchor week
 */

import { getConfig, requireConfig, bootstrapConfig, rewriteHost, applyAppName } from './config.js?v=5';
import { fetchEpisodesInRange, addWatchEvent, deleteWatchEvent, setStatus, getShowArtAssets } from './api.js?v=19';
import { openArtPicker } from './art-picker.js?v=1';
import {
  buildStatusBtn, refreshStatusBtn,
  STATUSES_5 as STATUSES, STATUS_LABELS, STATUS_CLASS, STATUS_COLOR,
  STATUS_ICONS, STATUS_ICON_CLASS,
} from './status-picker.js?v=1';
import { arrIcon, _anilistSvg, _malSvg, _mpvSvg, _tvdbSvg, _imdbMarkSvg, _tmdbMarkSvg, _downloadSvg, _tvmazeMarkSvg, _anidbMarkSvg, _syoboiSvg, SVC_ICONS } from './icons.js?v=16';
import { startDownload } from './downloads.js?v=2';
import { buildWatchedToggle, loadShowWatched } from './watched-toggle.js?v=1';

/* ── Constants ────────────────────────────────────────────── */

const DAY_NAMES  = ['Sun','Mon','Tue','Wed','Thu','Fri','Sat'];
const DAY_NAMES_FULL = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday'];
const MON_NAMES  = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
/* Status constants imported from status-picker.js */

/** Refresh interval when WebSocket subscriptions are unavailable (ms). */
const POLL_INTERVAL_MS = 10_000;

const STATUS_FILTER_KEY = 'starfleet_status_filter';
const TMDB_IMAGE_BASE   = 'https://image.tmdb.org/t/p/w300';
const TMDB_BACKDROP_BASE = 'https://image.tmdb.org/t/p/w780';

/* ── TMDB poster cache ────────────────────────────────────── */

/** showId → poster URL string (or null if fetch failed / no result) */
const posterCache = new Map();
/** showId → backdrop/banner URL string (or null if unavailable) */
const backdropCache = new Map();

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
    let backdropPath = null;

    if (tmdbExt) {
      // Direct TMDB lookup — movies use /movie/, series use /tv/
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/${type}/${tmdbExt.externalId}`, {});
      if (res.ok) {
        const data = await res.json();
        posterPath = data.poster_path ?? null;
        backdropPath = data.backdrop_path ?? null;
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
        backdropPath = results[0]?.backdrop_path ?? null;
      }
    }

    if (!posterPath) {
      // ID lookups found nothing — fall back to a TMDB title search.
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/search/${type}`, { query: show.displayTitle, page: 1 });
      if (res.ok) {
        const data = await res.json();
        posterPath = data.results?.[0]?.poster_path ?? null;
        if (!backdropPath) backdropPath = data.results?.[0]?.backdrop_path ?? null;
      }
    }

    if (posterPath) {
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
      // Also patch planner-cover elements (img-based)
      document.querySelectorAll(`.planner-cover[data-show-id="${show.id}"]`).forEach(c => {
        if (!c.querySelector('img')) {
          const img = document.createElement('img');
          img.src = url;
          img.alt = show.displayTitle;
          c.appendChild(img);
        }
      });
    }

    // Cache backdrop and patch planner bodies missing a banner
    if (backdropPath) {
      const bdUrl = TMDB_BACKDROP_BASE + backdropPath;
      backdropCache.set(show.id, bdUrl);
      _patchPlannerBanners(show.id, bdUrl);
    }
  } catch {
    // Network error — leave null in cache, no retry this session
  }
}

/** Patch planner body elements that have no banner with a TMDB backdrop. */
function _patchPlannerBanners(showId, bdUrl) {
  document.querySelectorAll(`.planner-body[data-show-id="${showId}"]:not(.has-banner)`).forEach(body => {
    body.classList.add('has-banner');
    body.style.setProperty('--banner-url', `url(${bdUrl})`);
  });
}

/**
 * Fetch a TMDB backdrop for a specific show (triggered by the art fetch button).
 * If already cached, patches immediately; otherwise queues behind the TMDB chain.
 */
function fetchTmdbBackdrop(show, apiKey) {
  // Already have a backdrop? Patch immediately.
  const cached = backdropCache.get(show.id);
  if (cached) { _patchPlannerBanners(show.id, cached); return; }

  // If a poster fetch is already queued it will capture the backdrop too.
  // But if the show already has a poster (from LCARS), no poster fetch was ever triggered,
  // so we need a dedicated backdrop-only fetch.
  if (posterCache.has(show.id) && posterCache.get(show.id) !== null) {
    // Poster is known — we need a backdrop-only TMDB call
    if (backdropCache.has(show.id)) return; // already tried
    backdropCache.set(show.id, null); // mark in-flight
    _tmdbChain = _tmdbChain
      .then(() => new Promise(r => setTimeout(r, TMDB_INTERVAL_MS)))
      .then(() => _doTmdbBackdropFetch(show, apiKey));
  } else {
    // No poster yet either — a full fetch will capture both
    fetchTmdbPoster(show, apiKey);
  }
}

/** Backdrop-only TMDB fetch for shows that already have a poster. */
async function _doTmdbBackdropFetch(show, apiKey) {
  if (backdropCache.get(show.id) !== null) return;

  const extIds = (show.externalIds?.edges ?? []).map(e => e.node);
  const tmdbExt = extIds.find(n => n.service === 'tmdb');
  const tvdbExt = extIds.find(n => n.service === 'tvdb');

  const isJwt = apiKey.startsWith('eyJ');
  const tmdbHeaders = isJwt ? { Authorization: `Bearer ${apiKey}` } : {};
  const tmdbFetch = (path, extraParams = {}) => {
    const params = new URLSearchParams(extraParams);
    if (!isJwt) params.set('api_key', apiKey);
    return fetch(`https://api.themoviedb.org/3${path}?${params}`, { headers: tmdbHeaders });
  };

  try {
    let backdropPath = null;

    if (tmdbExt) {
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/${type}/${tmdbExt.externalId}`, {});
      if (res.ok) backdropPath = (await res.json()).backdrop_path ?? null;
    }

    if (!backdropPath && tvdbExt) {
      const res = await tmdbFetch(`/find/${tvdbExt.externalId}`, { external_source: 'tvdb_id' });
      if (res.ok) {
        const data = await res.json();
        const results = show.mediaShape === 'MOVIE' ? (data.movie_results ?? []) : (data.tv_results ?? []);
        backdropPath = results[0]?.backdrop_path ?? null;
      }
    }

    if (!backdropPath) {
      const type = show.mediaShape === 'MOVIE' ? 'movie' : 'tv';
      const res = await tmdbFetch(`/search/${type}`, { query: show.displayTitle, page: 1 });
      if (res.ok) backdropPath = (await res.json()).results?.[0]?.backdrop_path ?? null;
    }

    if (!backdropPath) return;

    const bdUrl = TMDB_BACKDROP_BASE + backdropPath;
    backdropCache.set(show.id, bdUrl);
    _patchPlannerBanners(show.id, bdUrl);
  } catch {
    // Network error — no retry
  }
}

/* ── View state ───────────────────────────────────────────── */

let state = {
  mode: '3d',       // 'today' | '1d' | '3d' | 'week'
  layout: 'rows',   // 'rows' | 'planner'
  anchor: new Date(), // local-time reference day (time portion ignored)
};

const LAYOUT_KEY = 'starfleet_calendar_layout';

/* ── Status filter state ─────────────────────────────────── */

/** Set of status values currently visible. Persisted in localStorage. */
let activeStatuses = loadStatusFilter();

/** Whether watched episodes are visible. Persisted in localStorage. */
let _showWatched = loadShowWatched();

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
    case 'week':
      startLocal = anchor;
      endLocal   = addDays(anchor, 7);
      break;
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
 * Returns 'ready' | 'downloading' | 'airing' | 'missing' | 'future' based
 * on availability.
 * - ready:       file imported and available
 * - downloading: Sonarr/Radarr has it queued or actively downloading
 * - airing:      broadcast window is open right now (client-only overlay —
 *                not a server-side status; see episodeAiringWindow below)
 * - missing:     aired but UNAVAILABLE and not downloading (nothing in the queue)
 * - future:      has not aired yet
 */
export function availState(ep) {
  const isMovie = ep.show.mediaShape === 'MOVIE';
  const status  = isMovie ? ep.availableViaRadarr : ep.availableViaSonarr;

  if (status === 'AVAILABLE')   return 'ready';
  if (status === 'DOWNLOADING') return 'downloading';

  // UNAVAILABLE — distinguish airing-now vs aired (missing) vs future
  if (!ep.airDateUtc) return 'future';
  if (isEpisodeAiringNow(ep)) return 'airing';
  return new Date(ep.airDateUtc) <= new Date() ? 'missing' : 'future';
}

/**
 * True when `now` falls inside the episode's broadcast window: airDateUtc
 * through airDateUtc + runtime. Computed client-side (not a server field)
 * so the window is evaluated against the viewer's own clock at render
 * time, not baked into a cached response as a moving target. Runtime
 * falls back show.durationMinutes when the episode has no override, and
 * to a conservative default when neither is known — an unknown runtime
 * shouldn't silently suppress the indicator for the whole broadcast day.
 */
const DEFAULT_RUNTIME_MINUTES = 24;

export function isEpisodeAiringNow(ep) {
  if (!ep.airDateUtc) return false;
  const start = new Date(ep.airDateUtc);
  const runtime = ep.runtimeMinutes ?? ep.show?.durationMinutes ?? DEFAULT_RUNTIME_MINUTES;
  const end = new Date(start.getTime() + runtime * 60000);
  const now = new Date();
  return now >= start && now < end;
}

/* ── Service strip helpers ───────────────────────────────── */


const SVC_DEFS = [
  { key: 'anilist', cls: 'svc-al',     title: 'AniList', rewrite: false, svg: _anilistSvg,         always: true  },
  { key: 'mal',     cls: 'svc-mal',    title: 'MAL',     rewrite: false, svg: _malSvg,              always: true  },
  { key: 'tvdb',    cls: 'svc-tvdb',   title: 'TVDB',    rewrite: false, svg: _tvdbSvg,             always: false },
  { key: 'imdb',    cls: 'svc-imdb',   title: 'IMDb',    rewrite: false, svg: _imdbMarkSvg,         always: false },
  { key: 'tmdb',    cls: 'svc-tmdb',   title: 'TMDB',    rewrite: false, svg: _tmdbMarkSvg,         always: false },
  { key: 'anidb',   cls: 'svc-anidb',  title: 'AniDB',   rewrite: false, svg: _anidbMarkSvg,      always: false },
  { key: 'syoboi',  cls: 'svc-syoboi', title: 'Syoboi',  rewrite: false, svg: _syoboiSvg,         always: false },
  { key: 'tvmaze',  cls: 'svc-tvmaze', title: 'TVmaze',  rewrite: false, svg: _tvmazeMarkSvg,     always: false },
  { key: 'sonarr',  cls: 'svc-sonarr', title: 'Sonarr',  rewrite: true,  svg: arrIcon('#00C8FF'), always: true  },
  { key: 'radarr',  cls: 'svc-radarr', title: 'Radarr',  rewrite: true,  svg: arrIcon('#FFC230'), always: true  },
];

/* ── mpv launcher ────────────────────────────────────────── */

/**
 * Build the {showId, season, episode} context launchMpv needs to report
 * watched status — null when the episode has no show id (shouldn't
 * happen for a calendar/planner ep, but keeps this call-site-safe).
 * Movies carry season/episode as null; addWatchEvent already treats
 * both as optional.
 */
export function episodeCtx(ep) {
  const showId = ep?.show?.id;
  if (!showId) return null;
  return { showId, season: ep.season ?? null, episode: ep.episode ?? null, episodeId: ep.id };
}

/**
 * Launch a file in mpv via the local helper daemon.
 * filePath: server-side path (/data/media/…)
 * cfg: config object
 * ctx: optional {showId, season, episode} (episodeCtx(ep)) — when
 * present, the helper watches playback and reports watched status
 * (>=90% viewed, mirrors the Android VLC client's own threshold) back
 * to LCARS on its own, no client-side polling needed.
 */
export async function launchMpv(filePath, cfg, ctx = null) {
  if (!filePath) {
    showBanner('No file available for this episode', 'error');
    return;
  }

  // A1 step 20 — on Android, VlcPlugin streams straight from /files/ via a
  // native Intent; there's no mpv-helper daemon on a phone. Title is just
  // the release-group filename — cosmetic (VLC's title bar), not worth
  // threading a real title through every launchMpv() call site for.
  //
  // A4 step 37 — addWatchEvent is reported natively (VlcPlugin's own
  // onActivityResult), not from here: passing episodeId/showId/season/
  // episode through lets it do that without a second round trip back into
  // JS. Do NOT also call addWatchEvent here — that would double-report
  // the same viewing.
  if (window.Capacitor?.isNativePlatform?.()) {
    try {
      await window.Capacitor.Plugins.Vlc.play({
        path: filePath,
        title: filePath.split('/').pop(),
        episodeId: ctx?.episodeId,
        showId: ctx?.showId,
        season: ctx?.season,
        episode: ctx?.episode,
      });
    } catch (err) {
      showBanner(`VLC error: ${err.message || err}`, 'error');
    }
    return;
  }

  const helperUrl = (cfg.mpv_helper_url || 'http://localhost:19450').replace(/\/$/, '');

  // filePathSonarr/Radarr is the path inside the server container (/data/…).
  // nginx serves /data/ at /files/, so strip the /data prefix to form the URL.
  const mediaPath = filePath.startsWith('/data') ? filePath.slice('/data'.length) : filePath;
  // location.origin, not cfg.lcars_url (removed A0) — the mpv helper runs on
  // a different machine and needs an absolute URL.
  const mediaUrl  = `${location.origin}/files${mediaPath}`;

  const playBody = { url: mediaUrl };
  if (ctx?.showId) {
    playBody.showId   = ctx.showId;
    playBody.season   = ctx.season;
    playBody.episode  = ctx.episode;
    playBody.token    = cfg.lcars_token;
    playBody.lcarsBase = location.origin;
  }

  try {
    const res = await fetch(`${helperUrl}/play`, {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(playBody),
    });
    if (!res.ok) {
      // Helper returns 502 + JSON when mpv exits immediately (bad URL, auth, …)
      const body = await res.json().catch(() => ({}));
      if (body.error === 'mpv_failed') {
        showBanner('No mpv output — try Jellyfin', 'error');
        return;
      }
      throw new Error(`helper returned ${res.status}`);
    }
    showBanner('▶ Launching mpv…', 'info');
    setTimeout(hideBanner, 2500);
  } catch (err) {
    const isNetErr = err instanceof TypeError;
    showBanner(
      isNetErr
        ? 'mpv helper not running — start mpv-helper.py on this machine'
        : `mpv error: ${err.message}`,
      'error',
    );
  }
}

/**
 * Build the left service strip element for a card.
 * externalIds: array of { service, url } objects
 * malId: season-level MAL id (integer) — fallback when show has no mal externalId row
 * filePath: server-side file path (filePathSonarr or filePathRadarr), or null
 * availableLocally: bool
 * ctx: optional {showId, season, episode} (episodeCtx(ep)) for watched-status reporting
 */
export function buildSvcStrip(externalIds, malId, filePath, availableLocally, cfg, ctx = null) {
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
    if (def.svg) {
      a.innerHTML = def.svg;
    } else {
      a.textContent = def.title.slice(0, 3).toUpperCase();
      a.style.fontSize = '8px';
      a.style.fontWeight = '700';
      a.style.lineHeight = '1';
    }

    if (linked) {
      const url = def.rewrite
        ? rewriteHost(linked.url, location.hostname)
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
    playerA.style.opacity = '0.5';
    await launchMpv(filePath, cfg, ctx);
    playerA.style.opacity = '';
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
    airing:      'Airing now',
    missing:     'Aired — not available and not downloading',
    future:      'Not yet aired',
  }[avail];
  availIcon.textContent = avail === 'ready' ? '▶' : avail === 'future' ? '◷' : avail === 'airing' ? '●' : '⬇';
  meta.appendChild(availIcon);

  const watchBtn = document.createElement('button');
  watchBtn.className = `watch-btn${ep.state === 'WATCHED' ? ' watched' : ''}`;
  watchBtn.title = ep.state === 'WATCHED' ? 'Watched — click to unmark' : 'Mark as watched';
  watchBtn.textContent = '✓';
  watchBtn.dataset.state = ep.state;
  const existingWid = ep.watchEvents?.edges?.[0]?.node?.id;
  if (existingWid) watchBtn.dataset.watchEventId = existingWid;
  watchBtn.addEventListener('click', () => onWatchToggle(watchBtn, ep));
  meta.appendChild(watchBtn);

  card.appendChild(meta);

  /* ── Card body ── */
  const body = document.createElement('div');
  body.className = 'card-body';

  // Service strip
  const filePath = ep.show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;
  const malId    = ep.seasonEntity?.malId ?? null;
  body.appendChild(buildSvcStrip(externalIds, malId, filePath, ep.availableLocally, cfg, episodeCtx(ep)));

  // Cover art — use posterUrl if available; otherwise use TMDB cache or trigger a fetch
  const art = document.createElement('div');
  art.className = 'card-art';
  art.dataset.showId = ep.show.id;

  // Click on art launches mpv for available episodes
  if (ep.availableLocally) {
    art.classList.add('art-playable');
    art.addEventListener('click', () => launchMpv(filePath, cfg, episodeCtx(ep)));
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

  // Episode counts — watched / available / total, centered
  const w = ep.show.watchedEpisodeCount;
  const a = ep.show.availableEpisodeCount;
  const total = showTotal(ep.show);
  if (w != null || a != null || total !== '?') {
    const counts = document.createElement('div');
    counts.className = 'ep-counts';
    const parts = [];
    if (w != null) parts.push(`<span class="c-watched">W-${w}</span>`);
    if (a != null) { if (parts.length) parts.push(`<span class="c-sep">/</span>`); parts.push(`<span class="c-avail">A-${a}</span>`); }
    if (total !== '?') { if (parts.length) parts.push(`<span class="c-sep">/</span>`); parts.push(`<span class="c-total">T-${total}</span>`); }
    counts.innerHTML = parts.join('');
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
        episodeId: ep.id,
        showId: ep.show.id,
        season: ep.season ?? null,
        episode: ep.episode ?? null,
      });
      dlBtn.classList.add('triggered');
      setTimeout(() => dlBtn.classList.remove('triggered'), 1200);
    });
    card.appendChild(dlBtn);
  }

  // Status button — absolutely pinned to bottom-right of the card itself
  card.appendChild(buildStatusBtn({ id: ep.show.id, currentStatus: status, onPick: onStatusChange }));

  return card;
}

/* ── Status icon button + radial picker ──────────────────── */

/* buildStatusBtn imported from status-picker.js — callers pass { id, currentStatus, onPick }. */

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
      refreshStatusBtn(w, newStatus);
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
    if (!_showWatched && ep.state === 'WATCHED') continue;

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
  _lastEpisodes = episodes;

  const calendar = document.getElementById('calendar');
  if (!calendar) return;
  calendar.innerHTML = '';
  calendar.classList.toggle('planner', state.layout === 'planner');

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

  if (state.layout === 'planner') {
    renderPlanner(dayGroups, todayStr, cfg);
  } else {
    renderRows(dayGroups, todayStr, cfg);
  }
}

/* ── Show grouping (collapse 4+ episodes of the same show) ── */

const GROUP_THRESHOLD = 4;
const _expandedShows = new Set();

function groupByShow(episodes) {
  const byShow = new Map();
  for (const ep of episodes) {
    const sid = ep.show.id;
    if (!byShow.has(sid)) byShow.set(sid, { show: ep.show, episodes: [] });
    byShow.get(sid).episodes.push(ep);
  }
  return byShow;
}

function buildGroupCard(showGroup, cfg, cardBuilder) {
  const card = document.createElement('div');
  card.className = 'group-card';

  const artDiv = document.createElement('div');
  artDiv.className = 'group-art';
  const posterUrl = showGroup.show.posterUrl;
  if (posterUrl) {
    const img = document.createElement('img');
    img.src = posterUrl;
    img.alt = showGroup.show.displayTitle;
    if (cfg.tmdb_api_key) {
      img.onerror = () => { img.remove(); fetchTmdbPoster(showGroup.show, cfg.tmdb_api_key); };
    }
    artDiv.appendChild(img);
  }

  const overlay = document.createElement('div');
  overlay.className = 'group-overlay';
  const count = document.createElement('div');
  count.className = 'group-count';
  count.textContent = showGroup.episodes.length;
  overlay.appendChild(count);
  const label = document.createElement('div');
  label.className = 'group-label';
  label.textContent = 'episodes';
  overlay.appendChild(label);
  artDiv.appendChild(overlay);
  card.appendChild(artDiv);

  const footer = document.createElement('div');
  footer.className = 'group-footer';
  const title = document.createElement('div');
  title.className = 'group-title';
  title.textContent = showGroup.show.displayTitle;
  title.title = showGroup.show.displayTitle;
  footer.appendChild(title);

  const meta = document.createElement('div');
  meta.className = 'group-meta';
  const firstEp = showGroup.episodes[0];
  const lastEp = showGroup.episodes[showGroup.episodes.length - 1];
  const range = `S${String(firstEp.season ?? 0).padStart(2, '0')}E${String(firstEp.episode ?? 0).padStart(2, '0')}–E${String(lastEp.episode ?? 0).padStart(2, '0')}`;
  meta.textContent = `${range} · click to expand`;
  footer.appendChild(meta);
  card.appendChild(footer);

  card.addEventListener('click', () => {
    _expandedShows.add(showGroup.show.id);
    renderEpisodes(_lastEpisodes, cfg);
  });

  return card;
}

function buildExpandedGroup(showGroup, cfg, cardBuilder) {
  const wrapper = document.createElement('div');
  wrapper.className = 'group-expanded';

  const header = document.createElement('div');
  header.className = 'group-expanded-header';

  if (showGroup.show.posterUrl) {
    const img = document.createElement('img');
    img.src = showGroup.show.posterUrl;
    img.alt = '';
    img.onerror = () => { img.style.display = 'none'; };
    header.appendChild(img);
  }

  const info = document.createElement('div');
  info.className = 'group-expanded-info';
  const title = document.createElement('div');
  title.className = 'group-expanded-title';
  title.textContent = showGroup.show.displayTitle;
  info.appendChild(title);
  const meta = document.createElement('div');
  meta.className = 'group-expanded-meta';
  meta.textContent = `${showGroup.episodes.length} episodes`;
  info.appendChild(meta);
  header.appendChild(info);

  const collapseBtn = document.createElement('button');
  collapseBtn.className = 'group-expanded-collapse';
  collapseBtn.textContent = '▴ Collapse';
  header.appendChild(collapseBtn);

  header.addEventListener('click', () => {
    _expandedShows.delete(showGroup.show.id);
    renderEpisodes(_lastEpisodes, cfg);
  });
  wrapper.appendChild(header);

  const cardsDiv = document.createElement('div');
  cardsDiv.className = 'group-expanded-cards';
  for (const ep of showGroup.episodes) {
    cardsDiv.appendChild(cardBuilder(ep, cfg));
  }
  wrapper.appendChild(cardsDiv);

  return wrapper;
}

function appendGroupedEpisodes(container, episodes, cfg, cardBuilder) {
  const byShow = groupByShow(episodes);
  for (const [, showGroup] of byShow) {
    const isExpanded = _expandedShows.has(showGroup.show.id);
    if (showGroup.episodes.length >= GROUP_THRESHOLD && !isExpanded) {
      container.appendChild(buildGroupCard(showGroup, cfg, cardBuilder));
    } else if (showGroup.episodes.length >= GROUP_THRESHOLD && isExpanded) {
      container.appendChild(buildExpandedGroup(showGroup, cfg, cardBuilder));
    } else {
      for (const ep of showGroup.episodes) {
        container.appendChild(cardBuilder(ep, cfg));
      }
    }
  }
}

let _lastEpisodes = [];

/** Rows layout — day header as left column, cards in right area. */
function renderRows(dayGroups, todayStr, cfg) {
  const calendar = document.getElementById('calendar');
  for (const group of dayGroups) {
    const section = document.createElement('section');
    section.className = `day-group${group.dateStr === todayStr ? ' day-today' : ''}`;

    const header = document.createElement('div');
    header.className = 'day-label';
    header.innerHTML = group.localDate
      ? `<span class="day-date">${group.localDate.getDate()}</span>
         <span class="day-month">${MON_NAMES[group.localDate.getMonth()]}</span>
         <span class="day-name">${DAY_NAMES_FULL[group.localDate.getDay()]}</span>`
      : `<span class="day-name">?</span>`;
    section.appendChild(header);

    const row = document.createElement('div');
    row.className = 'card-row';

    group.episodes.sort((a, b) => {
      if (!a.airDateUtc) return 1;
      if (!b.airDateUtc) return -1;
      return new Date(a.airDateUtc) - new Date(b.airDateUtc);
    });

    appendGroupedEpisodes(row, group.episodes, cfg, buildCard);

    section.appendChild(row);
    calendar.appendChild(section);
  }
}

/** Planner layout — days as side-by-side columns. */
function renderPlanner(dayGroups, todayStr, cfg) {
  const calendar = document.getElementById('calendar');
  const container = document.createElement('div');
  container.className = 'planner-grid';
  const wideMode = state.mode === '1d' || state.mode === 'today' || state.mode === '3d';
  container.dataset.mode = state.mode;

  for (const group of dayGroups) {
    const col = document.createElement('div');
    col.className = `day-col${group.dateStr === todayStr ? ' day-today' : ''}`;

    const header = document.createElement('div');
    header.className = 'day-col-header';
    header.innerHTML = group.localDate
      ? `${DAY_NAMES_FULL[group.localDate.getDay()].toUpperCase()} ${group.localDate.getDate()} ${MON_NAMES[group.localDate.getMonth()]}`
      : `?`;
    col.appendChild(header);

    group.episodes.sort((a, b) => {
      if (!a.airDateUtc) return 1;
      if (!b.airDateUtc) return -1;
      return new Date(a.airDateUtc) - new Date(b.airDateUtc);
    });

    const cardFn = wideMode ? buildPlannerCard : buildCard;
    appendGroupedEpisodes(col, group.episodes, cfg, cardFn);

    container.appendChild(col);
  }

  calendar.appendChild(container);
}

/**
 * Format air time only (no date) for planner badge row.
 * Returns e.g. "17:00 London" or "—" if no airdate.
 */
function fmtTimeOnly(isoUtc) {
  if (!isoUtc) return '';
  const d = new Date(isoUtc);
  const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone?.split('/').pop()?.replace('_', ' ') || '';
  return `${time} ${tz}`.trim();
}

/**
 * Build a horizontal browse-style card for planner columns (1–3 cols).
 * Layout: [svc-strip] [poster img, scaled] [detail pane with optional banner bg]
 */
function buildPlannerCard(ep, cfg) {
  const avail  = availState(ep);
  const aired  = ep.airDateUtc && new Date(ep.airDateUtc) <= new Date();
  const status = ep.seasonEntity?.status || ep.show.status || 'WATCHING';

  const card = document.createElement('article');
  card.className = 'planner-card';
  card.dataset.episodeId = ep.id;
  card.dataset.showId    = ep.show.id;

  const filePath = ep.show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;

  // Service strip — same vertical icon bar as regular calendar cards
  const externalIds = ep.show.externalIds?.edges?.map(e => e.node) || [];
  const malId = ep.seasonEntity?.malId ?? null;
  card.appendChild(buildSvcStrip(externalIds, malId, filePath, ep.availableLocally, cfg, episodeCtx(ep)));

  // Cover image — full poster, scaled (not cropped)
  const cover = document.createElement('div');
  cover.className = 'planner-cover';
  cover.dataset.showId = ep.show.id;
  const posterUrl = ep.show.posterUrl || posterCache.get(ep.show.id) || null;
  if (posterUrl) {
    const img = document.createElement('img');
    img.src = posterUrl;
    img.alt = ep.show.displayTitle;
    if (cfg.tmdb_api_key) {
      img.onerror = () => {
        img.remove();
        posterCache.delete(ep.show.id);
        fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
      };
    }
    cover.appendChild(img);
  } else if (cfg.tmdb_api_key) {
    fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
  }

  if (ep.availableLocally) {
    cover.classList.add('art-playable');
    cover.addEventListener('click', () => launchMpv(filePath, cfg, episodeCtx(ep)));
  }

  card.appendChild(cover);

  // Detail body — banner art background when available
  const body = document.createElement('div');
  body.className = 'planner-body';
  body.dataset.showId = ep.show.id;
  const bannerSrc = ep.show.bannerUrl || backdropCache.get(ep.show.id) || null;
  if (bannerSrc) {
    body.classList.add('has-banner');
    body.style.setProperty('--banner-url', `url(${bannerSrc})`);
  } else if (cfg.tmdb_api_key) {
    fetchTmdbBackdrop(ep.show, cfg.tmdb_api_key);
  }

  // Title
  const title = document.createElement('a');
  title.className = 'planner-title';
  title.href = `show.html?id=${encodeURIComponent(ep.show.id)}`;
  title.textContent = ep.show.displayTitle;
  title.title = ep.show.displayTitle;
  body.appendChild(title);

  // Badge row: S01E02 + air time (time only, no date)
  const badgeRow = document.createElement('div');
  badgeRow.className = 'planner-badge-row';
  const badge = document.createElement('span');
  badge.className = 'planner-badge';
  badge.textContent = fmtEpBadge(ep);
  badgeRow.appendChild(badge);
  const timeStr = fmtTimeOnly(ep.airDateUtc);
  if (timeStr) {
    const airtime = document.createElement('span');
    airtime.className = 'planner-airtime';
    airtime.textContent = timeStr;
    badgeRow.appendChild(airtime);
  }
  body.appendChild(badgeRow);

  // Action row: avail/play button + watch button
  const actions = document.createElement('div');
  actions.className = 'planner-actions';

  const availBtn = document.createElement('button');
  availBtn.className = `planner-avail-btn avail-${avail}`;
  availBtn.title = {
    ready:       'Play in mpv',
    downloading: 'Downloading',
    airing:      'Airing now',
    missing:     'Aired — not available',
    future:      'Not yet aired',
  }[avail];
  availBtn.textContent = avail === 'ready' ? '▶' : avail === 'future' ? '◷' : avail === 'airing' ? '●' : '⬇';
  if (avail === 'ready' && ep.availableLocally) {
    availBtn.addEventListener('click', () => launchMpv(filePath, cfg, episodeCtx(ep)));
  }
  actions.appendChild(availBtn);

  const watchBtn = document.createElement('button');
  watchBtn.className = `watch-btn${ep.state === 'WATCHED' ? ' watched' : ''}`;
  watchBtn.title = ep.state === 'WATCHED' ? 'Watched — click to unmark' : 'Mark as watched';
  watchBtn.textContent = '✓';
  watchBtn.dataset.state = ep.state;
  const existingWid = ep.watchEvents?.edges?.[0]?.node?.id;
  if (existingWid) watchBtn.dataset.watchEventId = existingWid;
  watchBtn.addEventListener('click', () => onWatchToggle(watchBtn, ep));
  actions.appendChild(watchBtn);

  // Episode counts
  const w = ep.show.watchedEpisodeCount;
  const a = ep.show.availableEpisodeCount;
  const total = showTotal(ep.show);
  if (w != null || a != null || total !== '?') {
    const counts = document.createElement('div');
    counts.className = 'ep-counts planner-counts';
    const parts = [];
    if (w != null) parts.push(`<span class="c-watched">W-${w}</span>`);
    if (a != null) { if (parts.length) parts.push(`<span class="c-sep">/</span>`); parts.push(`<span class="c-avail">A-${a}</span>`); }
    if (total !== '?') { if (parts.length) parts.push(`<span class="c-sep">/</span>`); parts.push(`<span class="c-total">T-${total}</span>`); }
    counts.innerHTML = parts.join('');
    body.appendChild(counts);
  }

  // Episode title if available
  if (ep.title) {
    const epTitle = document.createElement('div');
    epTitle.className = 'planner-ep-title';
    epTitle.textContent = ep.title;
    body.appendChild(epTitle);
  }

  // Action buttons (absolutely positioned right side — appended last)
  body.appendChild(actions);

  card.appendChild(body);

  // Art picker button — opens full art picker modal
  const fetchBtn = document.createElement('button');
  fetchBtn.className = 'planner-fetch-art';
  fetchBtn.title = 'Choose banner art';
  fetchBtn.textContent = '🖼';
  fetchBtn.addEventListener('click', async () => {
    fetchBtn.textContent = '⏳';
    fetchBtn.disabled = true;
    try {
      const result = await getShowArtAssets(ep.show.id);
      const showObj = { id: ep.show.id, artAssets: result.artAssets || [] };
      openArtPicker(showObj, null, 'banner', null, (url) => {
        ep.show.bannerUrl = url;
        backdropCache.set(ep.show.id, url);
        body.classList.add('has-banner');
        body.style.setProperty('--banner-url', `url(${url})`);
        document.querySelectorAll(`.planner-card .planner-body[data-show-id="${ep.show.id}"]`).forEach(b => {
          if (b !== body) {
            b.classList.add('has-banner');
            b.style.setProperty('--banner-url', `url(${url})`);
          }
        });
      }, (msg) => showBanner(msg, 'error'));
    } catch {
      fetchBtn.textContent = '✗';
      setTimeout(() => { fetchBtn.textContent = '🖼'; fetchBtn.disabled = false; }, 2000);
      return;
    }
    fetchBtn.textContent = '🖼';
    fetchBtn.disabled = false;
  });
  card.appendChild(fetchBtn);

  // Status flower picker
  card.appendChild(buildStatusBtn({ id: ep.show.id, currentStatus: status, onPick: onStatusChange }));

  return card;
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

  ['prev-period-btn', 'prev-day-btn', 'next-day-btn', 'next-period-btn'].forEach(id => {
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

/** Navigate by one day (inner ‹/› buttons). */
function navigateDay(delta) {
  if (state.mode === 'today') return;
  state.anchor = addDays(dayOf(state.anchor), delta);
  render();
}

/** Navigate by one full period (outer «/» buttons). */
function navigatePeriod(delta) {
  if (state.mode === 'today') return;
  const steps = { today: 0, '1d': 1, '3d': 3, week: 7 };
  state.anchor = addDays(dayOf(state.anchor), delta * steps[state.mode]);
  render();
}

function setMode(mode) {
  state.mode = mode;
  if (mode === 'today') state.anchor = new Date();
  if (mode === 'week') state.anchor = isoWeekMon(new Date());

  document.querySelectorAll('.view-mode[data-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === mode);
  });

  render();
}

/* ── Keyboard navigation ─────────────────────────────────── */

const KEYBIND_HELP = [
  ['←  h',    'Previous day'],
  ['→  l',    'Next day'],
  ['⇧←  H',  'Previous period'],
  ['⇧→  L',  'Next period'],
  ['0',       'Jump to today'],
  ['1',       '1-day view'],
  ['3',       '3-day view'],
  ['7',       'Week view'],
  ['p',       'Toggle planner view'],
  ['r',       'Refresh now'],
  ['?',       'Show this help'],
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

    console.debug('[starfleet] key:', e.key, 'shift:', e.shiftKey, 'mode:', state.mode);
    switch (e.key) {
      case 'ArrowLeft':
        if (state.mode !== 'today') { e.preventDefault(); (e.shiftKey ? navigatePeriod : navigateDay)(-1); }
        break;
      case 'h':
        if (state.mode !== 'today') { e.preventDefault(); navigateDay(-1); }
        break;

      case 'ArrowRight':
        if (state.mode !== 'today') { e.preventDefault(); (e.shiftKey ? navigatePeriod : navigateDay)(+1); }
        break;
      case 'l':
        if (state.mode !== 'today') { e.preventDefault(); navigateDay(+1); }
        break;

      case 'H':
        if (state.mode !== 'today') { e.preventDefault(); navigatePeriod(-1); }
        break;

      case 'L':
        if (state.mode !== 'today') { e.preventDefault(); navigatePeriod(+1); }
        break;

      case 'p':
        e.preventDefault();
        toggleLayout();
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

/* ── Layout toggle ──────────────────────────────────────── */

function toggleLayout() {
  state.layout = state.layout === 'rows' ? 'planner' : 'rows';
  try { localStorage.setItem(LAYOUT_KEY, state.layout); } catch {}
  syncLayoutBtn();
  renderEpisodes(lastFetchedEpisodes, getConfig());
}

function syncLayoutBtn() {
  const btn = document.getElementById('planner-btn');
  if (btn) btn.classList.toggle('active', state.layout === 'planner');
}

/* ── Init ────────────────────────────────────────────────── */

/** Wire up controls and kick off first render. */
export async function init() {
  applyAppName();
  const cfg = await bootstrapConfig();
  if (!cfg) return;

  // View mode buttons (only those with data-mode, not the planner toggle)
  document.querySelectorAll('.view-mode[data-mode]').forEach(btn => {
    btn.addEventListener('click', () => setMode(btn.dataset.mode));
  });

  // Navigation buttons: << < period > >>
  const prevPeriod = document.getElementById('prev-period-btn');
  const prevDay    = document.getElementById('prev-day-btn');
  const nextDay    = document.getElementById('next-day-btn');
  const nextPeriod = document.getElementById('next-period-btn');
  if (prevPeriod) prevPeriod.addEventListener('click', () => navigatePeriod(-1));
  if (prevDay)    prevDay.addEventListener('click',    () => navigateDay(-1));
  if (nextDay)    nextDay.addEventListener('click',    () => navigateDay(+1));
  if (nextPeriod) nextPeriod.addEventListener('click', () => navigatePeriod(+1));

  // Planner layout toggle
  const plannerBtn = document.getElementById('planner-btn');
  if (plannerBtn) plannerBtn.addEventListener('click', () => toggleLayout());
  // Restore persisted layout
  try {
    const saved = localStorage.getItem(LAYOUT_KEY);
    if (saved === 'planner') state.layout = 'planner';
  } catch {}
  syncLayoutBtn();

  // Today button
  const todayBtn = document.getElementById('today-btn');
  if (todayBtn) {
    todayBtn.addEventListener('click', () => {
      state.anchor = new Date();
      render();
    });
  }

  // Set initial active mode button
  document.querySelectorAll('.view-mode[data-mode]').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.mode === state.mode);
  });

  // Status filter pills — sync initial state from localStorage, wire clicks
  document.querySelectorAll('.sf-pill').forEach(pill => {
    const status = pill.dataset.status;
    pill.classList.toggle('active', activeStatuses.has(status));
    pill.addEventListener('click', () => toggleStatus(status));
  });

  // Watched toggle
  const wtMount = document.getElementById('watched-toggle-mount');
  if (wtMount) {
    wtMount.appendChild(buildWatchedToggle({
      onChange(on) {
        _showWatched = on;
        renderEpisodes(lastFetchedEpisodes, getConfig());
      },
    }));
  }

  // Keyboard navigation
  initKeyboardNav();

  // Initial render
  render();

  // A3 (DESIGN.md §8) — Android has a real WS subscription via LcarsWsPlugin
  // (native, since browsers can't set the Authorization header a WS upgrade
  // needs — see api.js for the desktop-side explanation); the poll interval
  // exists for every OTHER platform, this app included when run in a
  // desktop/mobile browser rather than as the packaged Android app.
  if (window.Capacitor?.isNativePlatform?.()) {
    window.Capacitor.Plugins.LcarsWs.addListener('episodeAvailabilityChanged', () => render(true));
    window.Capacitor.Plugins.LcarsWs.addListener('showCreated', () => render(true));
  } else {
    setInterval(() => render(true), POLL_INTERVAL_MS);
  }
}
