/**
 * browse.js — Seasonal anime + TV/Movies browse module for the Add page.
 *
 * Two browse modes:
 *   - Anime: seasonal (AniList) — season+year picker
 *   - TV/Movies: monthly (TMDB) — month+year picker
 *
 * Manages navigation, status filtering, card rendering, pagination,
 * and add-to-LCARS flows. Wired from add.html's mode toggle.
 */

import {
  browseSeasonalAnime, browseTmdb,
  searchArrCandidates, addShowWithArr, addShow, skipShow,
  setStatus, setSeasonStatus, setSeasonMapping,
} from './api.js?v=19';
import { showBanner } from './calendar.js?v=23';
import {
  buildStatusBtn, refreshStatusBtn,
  STATUSES_6, STATUS_LABELS as PICKER_LABELS, STATUS_ICON_CLASS,
} from './status-picker.js?v=1';
import { _anilistSvg, _malSvg, _tvdbSvg, _imdbSvg, _tmdbMarkSvg } from './icons.js?v=8';

// ── Constants ────────────────────────────────────────────

const SEASONS = ['WINTER', 'SPRING', 'SUMMER', 'FALL'];
const SEASON_LABELS = { WINTER: 'Winter', SPRING: 'Spring', SUMMER: 'Summer', FALL: 'Fall' };

const MONTH_LABELS = [
  '', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];
const MONTH_FULL = [
  '', 'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

const STATUSES = ['PLANNED', 'WATCHING', 'PAUSED', 'COMPLETED', 'DROPPED', 'SKIPPED'];
const STATUS_LABELS = { PLANNED: 'Plan', WATCHING: 'Watch', PAUSED: 'Pause', COMPLETED: 'Done', DROPPED: 'Drop', SKIPPED: 'Skip' };

// ── Browse card service link definitions ────────────────
const BROWSE_SVC_DEFS = [
  { key: 'anilist', cls: 'svc-al',   svg: _anilistSvg, label: 'AniList', urlTpl: 'https://anilist.co/anime/{id}' },
  { key: 'mal',     cls: 'svc-mal',  svg: _malSvg,     label: 'MAL',     urlTpl: 'https://myanimelist.net/anime/{id}' },
  { key: 'tvdb',    cls: 'svc-tvdb', svg: _tvdbSvg,    label: 'TheTVDB', urlTpl: 'https://thetvdb.com/dereferrer/series/{id}' },
  { key: 'imdb',    cls: 'svc-imdb', svg: _imdbSvg,    label: 'IMDb',    urlTpl: 'https://www.imdb.com/title/{id}/' },
  { key: 'tmdb',    cls: 'svc-tmdb', svg: _tmdbMarkSvg, label: 'TMDB',   urlTpl: 'https://www.themoviedb.org/tv/{id}' },
];

// ── State ────────────────────────────────────────────────

let resultsEl = null;
let controlsEl = null;
// Browse type: 'anime' | 'all' | 'tv' | 'movie'
let browseType = 'anime';

// Anime state (season-based)
let currentSeason = '';
let currentYear = 0;

// TMDB state (month-based)
let currentMonth = 0;
let tmdbYear = 0;

let currentPage = 1;
let items = [];          // all loaded items (accumulates across pages)
let hasNextPage = false;
let activeFilters = new Set();  // active status filters (empty = show all)
let loading = false;

// ── Sequel / title helpers ──────────────────────────────

/**
 * Strip common season suffixes from a title for a broader Sonarr search.
 * "Sasaki and Peeps Season 2"  → "Sasaki and Peeps"
 * "Solo Leveling: Season 2"    → "Solo Leveling"
 * "Bookworm Part 3"            → "Bookworm"
 * "Medalist 2nd Season"        → "Medalist"
 */
function stripSeasonSuffix(title) {
  return title
    .replace(/[:\s]*\b(?:Season|Part)\s+\d+\s*$/i, '')
    .replace(/\s+\d+(?:st|nd|rd|th)\s+Season\s*$/i, '')
    .replace(/\s+S\d+\s*$/i, '')
    .trim();
}

/**
 * Parse a "sequel_of:{json}" error from the server into structured data.
 * Returns null if the error isn't a sequel detection.
 */
function parseSequelError(msg) {
  if (!msg.startsWith('sequel_of:')) return null;
  try {
    return JSON.parse(msg.slice('sequel_of:'.length));
  } catch {
    return null;
  }
}

/**
 * Show a modal confirmation for attaching a sequel as a new season.
 * Returns a Promise<boolean>.
 */
function confirmSequelAttach(sequel) {
  return new Promise(resolve => {
    const overlay = document.createElement('div');
    overlay.className = 'sequel-confirm-overlay';
    overlay.innerHTML = `
      <div class="sequel-confirm-dialog">
        <p>This looks like <strong>Season ${sequel.nextSeason}</strong>
           of <em>${sequel.parentTitle}</em>.</p>
        <p>Attach as a new season?</p>
        <div class="sequel-confirm-btns">
          <button class="btn-confirm">Attach as Season ${sequel.nextSeason}</button>
          <button class="btn-cancel">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(overlay);
    overlay.querySelector('.btn-confirm').onclick = () => {
      overlay.remove();
      resolve(true);
    };
    overlay.querySelector('.btn-cancel').onclick = () => {
      overlay.remove();
      resolve(false);
    };
  });
}

/**
 * Handle a confirmed sequel attach: call setSeasonMapping, then refresh.
 */
async function attachSequel(sequel) {
  await setSeasonMapping(
    sequel.parentShowId,
    sequel.nextSeason,
    sequel.sequelAnilistId,
    sequel.sequelMalId,
  );
}

// ── Season math (anime) ─────────────────────────────────

function currentSeasonYear() {
  const now = new Date();
  const m = now.getMonth() + 1;
  let season, year = now.getFullYear();
  if (m <= 3)      season = 'WINTER';
  else if (m <= 6) season = 'SPRING';
  else if (m <= 9) season = 'SUMMER';
  else             season = 'FALL';
  return { season, year };
}

/** Default to the upcoming season (one ahead of current). */
function defaultSeasonYear() {
  const { season, year } = currentSeasonYear();
  const idx = SEASONS.indexOf(season);
  if (idx === 3) return { season: 'WINTER', year: year + 1 };
  return { season: SEASONS[idx + 1], year };
}

function prevSeason() {
  const idx = SEASONS.indexOf(currentSeason);
  if (idx === 0) {
    currentSeason = 'FALL';
    currentYear -= 1;
  } else {
    currentSeason = SEASONS[idx - 1];
  }
}

function nextSeason() {
  const idx = SEASONS.indexOf(currentSeason);
  if (idx === 3) {
    currentSeason = 'WINTER';
    currentYear += 1;
  } else {
    currentSeason = SEASONS[idx + 1];
  }
}

function canGoNextSeason() {
  const maxYear = new Date().getFullYear() + 2;
  if (currentYear > maxYear) return false;
  if (currentYear === maxYear && currentSeason === 'FALL') return false;
  return true;
}

// ── Month math (TMDB) ───────────────────────────────────

function currentMonthYear() {
  const now = new Date();
  return { month: now.getMonth() + 1, year: now.getFullYear() };
}

function prevMonth() {
  if (currentMonth === 1) {
    currentMonth = 12;
    tmdbYear -= 1;
  } else {
    currentMonth -= 1;
  }
}

function nextMonth() {
  if (currentMonth === 12) {
    currentMonth = 1;
    tmdbYear += 1;
  } else {
    currentMonth += 1;
  }
}

function canGoNextMonth() {
  const maxYear = new Date().getFullYear() + 2;
  if (tmdbYear > maxYear) return false;
  if (tmdbYear === maxYear && currentMonth === 12) return false;
  return true;
}

// ── Effective status ─────────────────────────────────────

/** Compute the single display status for filtering/highlighting. */
function effectiveStatus(item) {
  // Anime items have season-level match; TMDB items are show-level only
  return item.lcarsSeasonStatus ?? item.lcarsStatus ?? 'NOT_IN_LCARS';
}

// ── Data fetching ────────────────────────────────────────

async function fetchPage(page) {
  loading = true;
  updateLoadingState();
  try {
    let result;
    if (browseType === 'anime') {
      result = await browseSeasonalAnime(currentSeason, currentYear, page);
    } else {
      const mediaType = browseType === 'tv' ? 'TV' : browseType === 'movie' ? 'MOVIE' : 'ALL';
      result = await browseTmdb(tmdbYear, currentMonth, mediaType, page);
    }
    if (page === 1) {
      items = result.items;
    } else {
      items = items.concat(result.items);
    }
    currentPage = result.currentPage;
    hasNextPage = result.hasNextPage;
    // Show/hide degraded-service banner for MAL fallback
    _showSourceBanner(result.source);
    renderCards();
  } catch (err) {
    resultsEl.innerHTML = `<div class="add-empty">Browse failed: ${err.message}</div>`;
  } finally {
    loading = false;
    updateLoadingState();
  }
}

function _showSourceBanner(source) {
  let banner = document.getElementById('source-fallback-banner');
  if (source === 'MAL') {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'source-fallback-banner';
      banner.className = 'source-fallback-banner';
      banner.textContent = 'AniList is unavailable — showing MyAnimeList data (some fields may differ)';
      resultsEl.parentElement.insertBefore(banner, resultsEl);
    }
    banner.hidden = false;
  } else if (banner) {
    banner.hidden = true;
  }
}

function updateLoadingState() {
  const bar = document.getElementById('loading-bar');
  if (bar) bar.classList.toggle('active', loading);
}

// ── Controls UI ─────────────────────────────────────────

function renderControls() {
  controlsEl.innerHTML = '';

  if (browseType === 'anime') {
    renderSeasonNav();
  } else {  // 'all', 'tv', 'movie'
    renderMonthNav();
  }

  renderFilterBar();
}

function renderSeasonNav() {
  const nav = document.createElement('div');
  nav.className = 'browse-season-nav';

  const prevBtn = document.createElement('button');
  prevBtn.className = 'browse-nav-btn';
  prevBtn.textContent = '◀';
  prevBtn.addEventListener('click', () => {
    prevSeason();
    updateSeasonSelect();
    loadFresh();
  });

  const select = document.createElement('select');
  select.className = 'browse-season-select';
  select.id = 'browse-season-select';
  populateSeasonOptions(select);
  select.addEventListener('change', () => {
    const [s, y] = select.value.split(':');
    currentSeason = s;
    currentYear = parseInt(y);
    loadFresh();
  });

  const nextBtn = document.createElement('button');
  nextBtn.className = 'browse-nav-btn';
  nextBtn.id = 'browse-next-btn';
  nextBtn.disabled = !canGoNextSeason();
  nextBtn.textContent = '▶';
  nextBtn.addEventListener('click', () => {
    if (!canGoNextSeason()) return;
    nextSeason();
    updateSeasonSelect();
    loadFresh();
  });

  nav.append(prevBtn, select, nextBtn);
  controlsEl.appendChild(nav);
}

function renderMonthNav() {
  const nav = document.createElement('div');
  nav.className = 'browse-season-nav';

  const prevBtn = document.createElement('button');
  prevBtn.className = 'browse-nav-btn';
  prevBtn.textContent = '◀';
  prevBtn.addEventListener('click', () => {
    prevMonth();
    updateMonthSelect();
    loadFresh();
  });

  const select = document.createElement('select');
  select.className = 'browse-season-select';
  select.id = 'browse-month-select';
  populateMonthOptions(select);
  select.addEventListener('change', () => {
    const [m, y] = select.value.split(':');
    currentMonth = parseInt(m);
    tmdbYear = parseInt(y);
    loadFresh();
  });

  const nextBtn = document.createElement('button');
  nextBtn.className = 'browse-nav-btn';
  nextBtn.id = 'browse-next-btn';
  nextBtn.disabled = !canGoNextMonth();
  nextBtn.textContent = '▶';
  nextBtn.addEventListener('click', () => {
    if (!canGoNextMonth()) return;
    nextMonth();
    updateMonthSelect();
    loadFresh();
  });

  nav.append(prevBtn, select, nextBtn);
  controlsEl.appendChild(nav);
}

function renderFilterBar() {
  const filterBar = document.createElement('div');
  filterBar.className = 'browse-filter-bar';

  // "Not in LCARS" pill
  const nilPill = createFilterPill('NOT_IN_LCARS', 'Not in LCARS', 'var(--faint)');
  filterBar.appendChild(nilPill);

  // Status pills
  const statusColors = {
    WATCHING: 'var(--st-watching)',
    PLANNED: 'var(--st-planning)',
    PAUSED: 'var(--st-paused)',
    COMPLETED: 'var(--st-completed)',
    DROPPED: 'var(--st-dropped)',
    SKIPPED: 'var(--faint)',
  };
  for (const st of STATUSES) {
    const pill = createFilterPill(st, STATUS_LABELS[st] || st, statusColors[st]);
    filterBar.appendChild(pill);
  }

  controlsEl.appendChild(filterBar);
}

function createFilterPill(value, label, color) {
  const pill = document.createElement('button');
  pill.className = 'bf-pill';
  pill.dataset.status = value;
  if (activeFilters.has(value)) pill.classList.add('active');
  pill.style.setProperty('--pill-color', color);

  const dot = document.createElement('span');
  dot.className = 'bf-dot';
  dot.style.background = color;
  pill.append(dot, label);

  pill.addEventListener('click', () => {
    if (activeFilters.has(value)) {
      activeFilters.delete(value);
    } else {
      activeFilters.add(value);
    }
    pill.classList.toggle('active', activeFilters.has(value));
    applyFilters();
  });

  return pill;
}

function populateSeasonOptions(select) {
  const maxYear = new Date().getFullYear() + 2;
  const minYear = 2020;
  const opts = [];
  for (let y = maxYear; y >= minYear; y--) {
    for (let s = SEASONS.length - 1; s >= 0; s--) {
      const season = SEASONS[s];
      const val = `${season}:${y}`;
      const label = `${SEASON_LABELS[season]} ${y}`;
      opts.push({ val, label });
    }
  }
  for (const o of opts) {
    const opt = document.createElement('option');
    opt.value = o.val;
    opt.textContent = o.label;
    select.appendChild(opt);
  }
  select.value = `${currentSeason}:${currentYear}`;
}

function populateMonthOptions(select) {
  const maxYear = new Date().getFullYear() + 2;
  const minYear = 2020;
  const opts = [];
  for (let y = maxYear; y >= minYear; y--) {
    for (let m = 12; m >= 1; m--) {
      const val = `${m}:${y}`;
      const label = `${MONTH_FULL[m]} ${y}`;
      opts.push({ val, label });
    }
  }
  for (const o of opts) {
    const opt = document.createElement('option');
    opt.value = o.val;
    opt.textContent = o.label;
    select.appendChild(opt);
  }
  select.value = `${currentMonth}:${tmdbYear}`;
}

function updateSeasonSelect() {
  const select = document.getElementById('browse-season-select');
  if (select) select.value = `${currentSeason}:${currentYear}`;
  const nextBtn = document.getElementById('browse-next-btn');
  if (nextBtn) nextBtn.disabled = !canGoNextSeason();
}

function updateMonthSelect() {
  const select = document.getElementById('browse-month-select');
  if (select) select.value = `${currentMonth}:${tmdbYear}`;
  const nextBtn = document.getElementById('browse-next-btn');
  if (nextBtn) nextBtn.disabled = !canGoNextMonth();
}

function loadFresh() {
  currentPage = 1;
  items = [];
  hasNextPage = false;
  resultsEl.innerHTML = '';
  fetchPage(1);
}

// ── Card rendering ───────────────────────────────────────

function renderCards() {
  resultsEl.innerHTML = '';

  if (!items.length) {
    const emptyLabel = browseType === 'anime'
      ? 'No anime found for this season'
      : `No ${browseType === 'tv' ? 'TV shows' : browseType === 'movie' ? 'movies' : 'TV shows or movies'} found for this month`;
    resultsEl.innerHTML = `<div class="add-empty">${emptyLabel}</div>`;
    return;
  }

  for (const item of items) {
    const card = browseType === 'anime'
      ? createAnimeCard(item)
      : createTmdbCard(item);  // 'all', 'tv', 'movie' all use TMDB cards
    resultsEl.appendChild(card);
  }

  // Load more button
  if (hasNextPage) {
    const moreBtn = document.createElement('button');
    moreBtn.className = 'browse-load-more';
    moreBtn.textContent = 'Load more';
    moreBtn.addEventListener('click', () => {
      if (!loading) fetchPage(currentPage + 1);
    });
    resultsEl.appendChild(moreBtn);
  }

  applyFilters();
}

// ── Service link strip for browse cards ──────────────────

/**
 * Build a horizontal row of service-logo links for a browse card.
 * ids: { anilist, mal, tvdb, tmdb } — values are string/number IDs or falsy.
 * Returns a DOM element.
 */
function buildBrowseLinks(ids) {
  const strip = document.createElement('div');
  strip.className = 'browse-links';

  for (const def of BROWSE_SVC_DEFS) {
    const id = ids[def.key];
    if (!id) continue;
    const url = def.urlTpl.replace('{id}', id);

    const a = document.createElement('a');
    a.className = `svc ${def.cls} on`;
    a.href = url;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.innerHTML = def.svg;

    // Hover tooltip
    a.dataset.svcLabel = def.label;
    a.dataset.svcId = String(id);
    a.dataset.svcUrl = url;
    a.addEventListener('mouseenter', showLinkTooltip);
    a.addEventListener('mouseleave', hideLinkTooltip);

    strip.appendChild(a);
  }
  return strip;
}

// Shared tooltip element — created once, repositioned on hover.
let _linkTip = null;

function showLinkTooltip(e) {
  const anchor = e.currentTarget;
  if (!_linkTip) {
    _linkTip = document.createElement('div');
    _linkTip.className = 'browse-link-tip';
    document.body.appendChild(_linkTip);
  }
  _linkTip.innerHTML = `
    <strong>${anchor.dataset.svcLabel}</strong>
    <span class="tip-id">${anchor.dataset.svcId}</span>
    <span class="tip-url">${anchor.dataset.svcUrl}</span>
  `;
  _linkTip.hidden = false;

  // Position below the icon
  const rect = anchor.getBoundingClientRect();
  _linkTip.style.left = `${rect.left + rect.width / 2}px`;
  _linkTip.style.top = `${rect.bottom + 6}px`;
}

function hideLinkTooltip() {
  if (_linkTip) _linkTip.hidden = true;
}

// ── Anime card (AniList data) ────────────────────────────

function createAnimeCard(item) {
  const card = document.createElement('div');
  card.className = 'browse-card';
  card.dataset.anilistId = item.anilistId || '';
  card.dataset.malId = item.malId || '';

  const eff = effectiveStatus(item);
  card.dataset.effectiveStatus = eff;
  if (eff !== 'NOT_IN_LCARS') {
    card.classList.add('tracked');
    card.style.setProperty('--tracked-color', statusColor(eff));
  }

  // Cover art
  const cover = document.createElement('div');
  cover.className = 'browse-cover';
  if (item.coverImageUrl) {
    cover.style.backgroundImage = `url(${item.coverImageUrl})`;
  }
  card.appendChild(cover);

  // Body
  const body = document.createElement('div');
  body.className = 'browse-body';

  // Title
  const title = document.createElement('div');
  title.className = 'browse-title';
  title.textContent = item.titleEnglish || item.titleRomaji || item.titleNative || '(Untitled)';
  if (item.titleRomaji && item.titleEnglish && item.titleRomaji !== item.titleEnglish) {
    title.title = item.titleRomaji;
  }
  body.appendChild(title);

  // Meta line: format · episodes · studio · start date
  const meta = document.createElement('div');
  meta.className = 'browse-meta';
  const metaParts = [];
  if (item.format) metaParts.push(formatLabel(item.format));
  if (item.episodes) metaParts.push(`${item.episodes} ep`);
  if (item.duration) metaParts.push(`${item.duration}m`);
  if (item.studioNames?.length) metaParts.push(item.studioNames[0]);
  if (item.startDate) metaParts.push(item.startDate);
  meta.textContent = metaParts.join(' · ');
  body.appendChild(meta);

  // Genres
  if (item.genres?.length) {
    const genres = document.createElement('div');
    genres.className = 'browse-genres';
    genres.textContent = item.genres.join(', ');
    body.appendChild(genres);
  }

  // Synopsis (scrollable)
  if (item.description) {
    const synopsis = document.createElement('div');
    synopsis.className = 'browse-synopsis';
    synopsis.textContent = item.description;
    body.appendChild(synopsis);
  }

  // Service links (AniList, MAL, TVDB, IMDB, TMDB)
  const links = buildBrowseLinks({
    anilist: item.anilistId,
    mal: item.malId,
    tvdb: item.tvdbId,
    imdb: item.imdbId,
    tmdb: item.tmdbId,
  });
  body.appendChild(links);

  // Radial status picker on cover
  const picker = buildStatusBtn({
    id: item.anilistId || `mal-${item.malId}`,
    currentStatus: eff === 'NOT_IN_LCARS' ? 'UNTRACKED' : eff,
    statuses: STATUSES_6,
    onPick: (_wrap, _id, st) => onAnimeChipClick(card, item, st),
  });
  card.style.position = 'relative';
  card.appendChild(picker);

  // Spinner
  const spinner = document.createElement('div');
  spinner.className = 'cand-spinner';
  card.appendChild(spinner);

  card.appendChild(body);
  return card;
}

// ── TMDB card (TV/Movie data) ────────────────────────────

function createTmdbCard(item) {
  const card = document.createElement('div');
  card.className = 'browse-card';
  card.dataset.tmdbId = item.tmdbId;

  const eff = effectiveStatus(item);
  card.dataset.effectiveStatus = eff;
  if (eff !== 'NOT_IN_LCARS') {
    card.classList.add('tracked');
    card.style.setProperty('--tracked-color', statusColor(eff));
  }

  // Poster
  const cover = document.createElement('div');
  cover.className = 'browse-cover';
  if (item.posterUrl) {
    cover.style.backgroundImage = `url(${item.posterUrl})`;
  }
  card.appendChild(cover);

  // Body
  const body = document.createElement('div');
  body.className = 'browse-body';

  // Title
  const title = document.createElement('div');
  title.className = 'browse-title';
  title.textContent = item.title;
  if (item.originalTitle && item.originalTitle !== item.title) {
    title.title = item.originalTitle;
  }
  body.appendChild(title);

  // Meta line
  const meta = document.createElement('div');
  meta.className = 'browse-meta';
  const metaParts = [];
  metaParts.push(item.mediaType === 'MOVIE' ? 'Movie' : 'TV');
  if (item.releaseDate) metaParts.push(item.releaseDate);
  if (item.firstAirDate) metaParts.push(`Since ${item.firstAirDate}`);
  if (item.voteAverage) metaParts.push(`★ ${item.voteAverage.toFixed(1)}`);
  meta.textContent = metaParts.join(' · ');
  body.appendChild(meta);

  // Overview
  if (item.overview) {
    const synopsis = document.createElement('div');
    synopsis.className = 'browse-synopsis';
    synopsis.textContent = item.overview;
    body.appendChild(synopsis);
  }

  // Service links (TMDB)
  const tmdbUrlBase = item.mediaType === 'MOVIE'
    ? 'https://www.themoviedb.org/movie/'
    : 'https://www.themoviedb.org/tv/';
  const links = buildBrowseLinks({
    tmdb: item.tmdbId,
  });
  // Fix TMDB URL for movies (the default template uses /tv/)
  if (item.mediaType === 'MOVIE') {
    const tmdbLink = links.querySelector('.svc-tmdb');
    if (tmdbLink) {
      const movieUrl = `${tmdbUrlBase}${item.tmdbId}`;
      tmdbLink.href = movieUrl;
      tmdbLink.dataset.svcUrl = movieUrl;
    }
  }
  body.appendChild(links);

  // Radial status picker on cover
  const picker = buildStatusBtn({
    id: String(item.tmdbId),
    currentStatus: eff === 'NOT_IN_LCARS' ? 'UNTRACKED' : eff,
    statuses: STATUSES_6,
    onPick: (_wrap, _id, st) => onTmdbChipClick(card, item, st),
  });
  card.style.position = 'relative';
  card.appendChild(picker);

  // Spinner
  const spinner = document.createElement('div');
  spinner.className = 'cand-spinner';
  card.appendChild(spinner);

  card.appendChild(body);
  return card;
}

function formatLabel(fmt) {
  const labels = {
    TV: 'TV', TV_SHORT: 'Short', MOVIE: 'Movie',
    SPECIAL: 'Special', OVA: 'OVA', ONA: 'ONA',
  };
  return labels[fmt] || fmt;
}

function statusColor(status) {
  const colors = {
    WATCHING: 'var(--st-watching)',
    PLANNED: 'var(--st-planning)',
    PAUSED: 'var(--st-paused)',
    COMPLETED: 'var(--st-completed)',
    DROPPED: 'var(--st-dropped)',
    SKIPPED: 'var(--faint)',
  };
  return colors[status] || 'var(--accent)';
}

// ── Filtering ────────────────────────────────────────────

function applyFilters() {
  if (!resultsEl) return;
  const cards = resultsEl.querySelectorAll('.browse-card');
  for (const card of cards) {
    if (activeFilters.size === 0) {
      card.hidden = false;
    } else {
      card.hidden = !activeFilters.has(card.dataset.effectiveStatus);
    }
  }
}

// ── Anime chip click (add / status change) ───────────────

async function onAnimeChipClick(card, item, status) {
  if (loading) return;

  const eff = effectiveStatus(item);
  if (eff === status) return;

  // Season-level match: change season status
  if (item.lcarsSeasonId) {
    card.classList.add('loading');
    try {
      await setSeasonStatus(item.lcarsSeasonId, status);
      item.lcarsSeasonStatus = status;
      refreshCard(card, item);
      showBanner(`Season status → ${STATUS_LABELS[status]}`, 'ok');
    } catch (err) {
      showBanner(`Status change failed: ${err.message}`, 'error');
    } finally {
      card.classList.remove('loading');
    }
    return;
  }

  // Show-level match: change show status
  if (item.lcarsShowId) {
    card.classList.add('loading');
    try {
      await setStatus(item.lcarsShowId, status);
      item.lcarsStatus = status;
      refreshCard(card, item);
      showBanner(`Status → ${STATUS_LABELS[status]}`, 'ok');
    } catch (err) {
      showBanner(`Status change failed: ${err.message}`, 'error');
    } finally {
      card.classList.remove('loading');
    }
    return;
  }

  // Not in LCARS — add it (or skip it)
  card.classList.add('loading');
  try {
    const mediaShape = item.format === 'MOVIE' ? 'MOVIE' : 'EPISODIC';
    const input = {
      mediaShape,
      trackingSpace: 'ANIME',
      primaryTitle: item.titleEnglish ? 'ENGLISH' : 'ROMAJI',
      titleEnglish: item.titleEnglish || undefined,
      titleRomaji: item.titleRomaji || undefined,
      titleNative: item.titleNative || undefined,
      anilistId: item.anilistId || undefined,
      malId: item.malId || undefined,
    };

    let show;

    if (status === 'SKIPPED') {
      show = await skipShow(input);
      item.lcarsShowId = show.id;
      item.lcarsStatus = (show.status || 'SKIPPED').toUpperCase();
      refreshCard(card, item);
      showBanner(show.status === 'skipped' ? 'Skipped' : `Already ${STATUS_LABELS[item.lcarsStatus] || show.status}`, 'ok');
      card.classList.remove('loading');
      return;
    }

    if (status === 'COMPLETED' || status === 'DROPPED') {
      show = await addShow(input);
      if (show.status !== status) {
        await setStatus(show.id, status);
        show.status = status;
      }
    } else {
      const title = item.titleEnglish || item.titleRomaji;
      let arrInput = { ...input };
      if (status === 'PAUSED') arrInput.unmonitored = true;

      // Sonarr/Radarr candidate search — try exact title first, then
      // strip season suffixes ("Sasaki and Peeps Season 2" → base title)
      // since Sonarr indexes the series under the base name.
      try {
        let candidates = await searchArrCandidates(mediaShape, title);
        if (!candidates.length) {
          const base = stripSeasonSuffix(title);
          if (base !== title) {
            candidates = await searchArrCandidates(mediaShape, base);
          }
        }
        if (candidates.length) {
          if (candidates[0].tvdbId) arrInput.tvdbId = candidates[0].tvdbId;
          if (candidates[0].tmdbId) arrInput.tmdbId = candidates[0].tmdbId;
        }
      } catch {
        // Sonarr/Radarr search failed — proceed without external IDs
      }

      try {
        const result = await addShowWithArr(arrInput);
        show = result.show;
      } catch (err) {
        // Already tracked — not an error, just inform.
        const tracked = err.message.match(/already tracked \(show ([^)]+)\)/);
        if (tracked) {
          item.lcarsShowId = tracked[1];
          item.lcarsStatus = 'PLANNED';
          refreshCard(card, item);
          showBanner('Already tracked — use Add mode to manage seasons', 'info');
          card.classList.remove('loading');
          return;
        }

        // Sequel detected — ask user to confirm season attach.
        const sequel = parseSequelError(err.message);
        if (sequel) {
          const ok = await confirmSequelAttach(sequel);
          if (ok) {
            await attachSequel(sequel);
            // Apply the status the user originally selected
            if (status !== 'PLANNED') {
              await setStatus(sequel.parentShowId, status);
            }
            showBanner(
              `Attached as Season ${sequel.nextSeason} of ${sequel.parentTitle}`,
              'ok',
            );
            item.lcarsShowId = sequel.parentShowId;
            item.lcarsStatus = status;
            refreshCard(card, item);
          } else {
            showBanner('Cancelled', 'info');
          }
          card.classList.remove('loading');
          return;
        }

        // Any other error — surface it, do NOT silently create orphan stub.
        throw err;
      }

      if (show.status !== status && status !== 'PLANNED') {
        await setStatus(show.id, status);
        show.status = status;
      }
    }

    item.lcarsShowId = show.id;
    item.lcarsStatus = show.status || status;
    refreshCard(card, item);
    showBanner(`Added: ${show.displayTitle} [${STATUS_LABELS[status]}]`, 'ok');

  } catch (err) {
    showBanner(`Add failed: ${err.message}`, 'error');
  } finally {
    card.classList.remove('loading');
  }
}

// ── TMDB chip click (add / status change) ────────────────

async function onTmdbChipClick(card, item, status) {
  if (loading) return;

  const eff = effectiveStatus(item);
  if (eff === status) return;

  // Already tracked: change show status
  if (item.lcarsShowId) {
    card.classList.add('loading');
    try {
      await setStatus(item.lcarsShowId, status);
      item.lcarsStatus = status;
      refreshCard(card, item);
      showBanner(`Status → ${STATUS_LABELS[status]}`, 'ok');
    } catch (err) {
      showBanner(`Status change failed: ${err.message}`, 'error');
    } finally {
      card.classList.remove('loading');
    }
    return;
  }

  // Not in LCARS — add it (or skip it)
  card.classList.add('loading');
  try {
    const mediaShape = item.mediaType === 'MOVIE' ? 'MOVIE' : 'EPISODIC';
    const input = {
      mediaShape,
      trackingSpace: 'TV',
      primaryTitle: 'ENGLISH',
      titleEnglish: item.title,
      tmdbId: item.tmdbId,
    };

    let show;

    if (status === 'SKIPPED') {
      show = await skipShow(input);
      item.lcarsShowId = show.id;
      item.lcarsStatus = (show.status || 'SKIPPED').toUpperCase();
      refreshCard(card, item);
      showBanner(show.status === 'skipped' ? 'Skipped' : `Already ${STATUS_LABELS[item.lcarsStatus] || show.status}`, 'ok');
      card.classList.remove('loading');
      return;
    }

    if (status === 'COMPLETED' || status === 'DROPPED') {
      show = await addShow(input);
      if (show.status !== status) {
        await setStatus(show.id, status);
        show.status = status;
      }
    } else {
      let arrInput = { ...input };
      if (status === 'PAUSED') arrInput.unmonitored = true;

      // For TV shows, search arr for tvdbId since Sonarr needs it
      if (mediaShape === 'EPISODIC') {
        try {
          let candidates = await searchArrCandidates(mediaShape, item.title);
          if (!candidates.length) {
            const base = stripSeasonSuffix(item.title);
            if (base !== item.title) {
              candidates = await searchArrCandidates(mediaShape, base);
            }
          }
          if (candidates.length) {
            if (candidates[0].tvdbId) arrInput.tvdbId = candidates[0].tvdbId;
            if (candidates[0].tmdbId) arrInput.tmdbId = candidates[0].tmdbId;
          }
        } catch {
          // Sonarr search failed
        }
      }

      try {
        const result = await addShowWithArr(arrInput);
        show = result.show;
      } catch (err) {
        const tracked = err.message.match(/already tracked \(show ([^)]+)\)/);
        if (tracked) {
          item.lcarsShowId = tracked[1];
          item.lcarsStatus = 'PLANNED';
          refreshCard(card, item);
          showBanner('Already tracked', 'info');
          card.classList.remove('loading');
          return;
        }

        // Sequel detected — ask user to confirm season attach.
        const sequel = parseSequelError(err.message);
        if (sequel) {
          const ok = await confirmSequelAttach(sequel);
          if (ok) {
            await attachSequel(sequel);
            if (status !== 'PLANNED') {
              await setStatus(sequel.parentShowId, status);
            }
            showBanner(
              `Attached as Season ${sequel.nextSeason} of ${sequel.parentTitle}`,
              'ok',
            );
            item.lcarsShowId = sequel.parentShowId;
            item.lcarsStatus = status;
            refreshCard(card, item);
          } else {
            showBanner('Cancelled', 'info');
          }
          card.classList.remove('loading');
          return;
        }

        // Any other error — surface it, do NOT silently create orphan stub.
        throw err;
      }

      if (show.status !== status && status !== 'PLANNED') {
        await setStatus(show.id, status);
        show.status = status;
      }
    }

    item.lcarsShowId = show.id;
    item.lcarsStatus = show.status || status;
    refreshCard(card, item);
    showBanner(`Added: ${show.displayTitle} [${STATUS_LABELS[status]}]`, 'ok');

  } catch (err) {
    showBanner(`Add failed: ${err.message}`, 'error');
  } finally {
    card.classList.remove('loading');
  }
}

function refreshCard(card, item) {
  const eff = effectiveStatus(item);
  card.dataset.effectiveStatus = eff;
  card.classList.toggle('tracked', eff !== 'NOT_IN_LCARS');
  if (eff !== 'NOT_IN_LCARS') {
    card.style.setProperty('--tracked-color', statusColor(eff));
  }
  // Update radial picker
  const wrap = card.querySelector('.status-btn-wrap');
  if (wrap) {
    refreshStatusBtn(wrap, eff === 'NOT_IN_LCARS' ? 'PLANNED' : eff, STATUSES_6);
    wrap.style.opacity = eff === 'NOT_IN_LCARS' ? '0.5' : '';
  }
  // Re-apply filters in case status changed visibility
  applyFilters();
}

// ── Public API ───────────────────────────────────────────

/**
 * Set the browse type and refresh. Called when type buttons
 * are clicked in browse mode.
 * @param {'anime'|'tv'|'movie'} type
 */
export function setBrowseType(type) {
  if (type === browseType) return;
  browseType = type;

  // Reset navigation state
  if (type === 'anime') {
    const def = defaultSeasonYear();
    currentSeason = def.season;
    currentYear = def.year;
  } else {  // 'all', 'tv', 'movie'
    const def = currentMonthYear();
    currentMonth = def.month;
    tmdbYear = def.year;
  }

  currentPage = 1;
  items = [];
  hasNextPage = false;
  activeFilters.clear();

  renderControls();
  fetchPage(1);
}

/**
 * Initialize browse mode. Called when user switches to Browse tab.
 * @param {HTMLElement} results - the #browse-results container
 * @param {HTMLElement} controls - the .browse-controls container
 * @param {'anime'|'all'|'tv'|'movie'} [type] - initial browse type
 */
export function initBrowse(results, controls, type) {
  resultsEl = results;
  controlsEl = controls;

  browseType = type || 'anime';

  if (browseType === 'anime') {
    const def = defaultSeasonYear();
    currentSeason = def.season;
    currentYear = def.year;
  } else {  // 'all', 'tv', 'movie'
    const def = currentMonthYear();
    currentMonth = def.month;
    tmdbYear = def.year;
  }

  currentPage = 1;
  items = [];
  hasNextPage = false;
  activeFilters.clear();

  renderControls();
  fetchPage(1);
}

/**
 * Tear down browse mode. Called when user switches away from Browse tab.
 */
export function teardownBrowse() {
  if (resultsEl) resultsEl.innerHTML = '';
  if (controlsEl) controlsEl.innerHTML = '';
  items = [];
  activeFilters.clear();
}
