/**
 * browse.js — Seasonal anime browse module for the Add page.
 *
 * Manages season selection, status filtering, card rendering, pagination,
 * and add-to-LCARS flows. Wired from add.html's mode toggle.
 */

import { browseSeasonalAnime, searchArrCandidates, addShowWithArr, addShow, setStatus, setSeasonStatus } from './api.js?v=12';
import { showBanner } from './calendar.js?v=20';

// ── Constants ────────────────────────────────────────────

const SEASONS = ['WINTER', 'SPRING', 'SUMMER', 'FALL'];
const SEASON_LABELS = { WINTER: 'Winter', SPRING: 'Spring', SUMMER: 'Summer', FALL: 'Fall' };

const STATUSES = ['PLANNED', 'WATCHING', 'PAUSED', 'COMPLETED', 'DROPPED'];
const STATUS_LABELS = { PLANNED: 'Plan', WATCHING: 'Watch', PAUSED: 'Pause', COMPLETED: 'Done', DROPPED: 'Drop' };

// ── State ────────────────────────────────────────────────

let resultsEl = null;
let controlsEl = null;
let currentSeason = '';
let currentYear = 0;
let currentPage = 1;
let items = [];          // all loaded items (accumulates across pages)
let hasNextPage = false;
let activeFilters = new Set();  // active status filters (empty = show all)
let loading = false;

// ── Season math ──────────────────────────────────────────

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

function canGoNext() {
  // Cap at current year + 2
  const maxYear = new Date().getFullYear() + 2;
  if (currentYear > maxYear) return false;
  if (currentYear === maxYear && currentSeason === 'FALL') return false;
  return true;
}

// ── Effective status ─────────────────────────────────────

/** Compute the single display status for filtering/highlighting. */
function effectiveStatus(item) {
  return item.lcarsSeasonStatus ?? item.lcarsStatus ?? 'NOT_IN_LCARS';
}

// ── Data fetching ────────────────────────────────────────

async function fetchPage(page) {
  loading = true;
  updateLoadingState();
  try {
    const result = await browseSeasonalAnime(currentSeason, currentYear, page);
    if (page === 1) {
      items = result.items;
    } else {
      items = items.concat(result.items);
    }
    currentPage = result.currentPage;
    hasNextPage = result.hasNextPage;
    renderCards();
  } catch (err) {
    resultsEl.innerHTML = `<div class="add-empty">Browse failed: ${err.message}</div>`;
  } finally {
    loading = false;
    updateLoadingState();
  }
}

function updateLoadingState() {
  const bar = document.getElementById('loading-bar');
  if (bar) bar.classList.toggle('active', loading);
}

// ── Season selector UI ───────────────────────────────────

function renderControls() {
  controlsEl.innerHTML = '';

  // Season nav: ◀ [select] ▶
  const nav = document.createElement('div');
  nav.className = 'browse-season-nav';

  const prevBtn = document.createElement('button');
  prevBtn.className = 'browse-nav-btn';
  prevBtn.textContent = '◀';
  prevBtn.addEventListener('click', () => {
    prevSeason();
    updateSeasonSelect();
    loadSeason();
  });

  const select = document.createElement('select');
  select.className = 'browse-season-select';
  select.id = 'browse-season-select';
  populateSeasonOptions(select);
  select.addEventListener('change', () => {
    const [s, y] = select.value.split(':');
    currentSeason = s;
    currentYear = parseInt(y);
    loadSeason();
  });

  const nextBtn = document.createElement('button');
  nextBtn.className = 'browse-nav-btn';
  nextBtn.disabled = !canGoNext();
  nextBtn.textContent = '▶';
  nextBtn.addEventListener('click', () => {
    if (!canGoNext()) return;
    nextSeason();
    updateSeasonSelect();
    loadSeason();
  });

  nav.append(prevBtn, select, nextBtn);
  controlsEl.appendChild(nav);

  // Status filter pills (using .bf-pill to avoid calendar.js collision)
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
  // Build options newest-first
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

function updateSeasonSelect() {
  const select = document.getElementById('browse-season-select');
  if (select) select.value = `${currentSeason}:${currentYear}`;
  // Update next button disabled state
  const nextBtn = controlsEl.querySelector('.browse-nav-btn:last-child');
  if (nextBtn) nextBtn.disabled = !canGoNext();
}

function loadSeason() {
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
    resultsEl.innerHTML = '<div class="add-empty">No anime found for this season</div>';
    return;
  }

  for (const item of items) {
    const card = createBrowseCard(item);
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

function createBrowseCard(item) {
  const card = document.createElement('div');
  card.className = 'browse-card';
  card.dataset.anilistId = item.anilistId;

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

  // Status chips
  const chipsRow = document.createElement('div');
  chipsRow.className = 'cand-chips';
  for (const st of STATUSES) {
    const chip = document.createElement('button');
    chip.className = 'cand-chip';
    chip.dataset.status = st;
    chip.textContent = STATUS_LABELS[st];
    if (eff === st) chip.classList.add('active');
    chip.addEventListener('click', () => onBrowseChipClick(card, item, st));
    chipsRow.appendChild(chip);
  }
  body.appendChild(chipsRow);

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

// ── Chip click (add / status change) ─────────────────────

async function onBrowseChipClick(card, item, status) {
  if (loading) return;

  const eff = effectiveStatus(item);

  // Already at this status — no-op (no soft-delete from browse)
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

  // Not in LCARS — add it
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
      anilistId: item.anilistId,
      malId: item.malId || undefined,
    };

    let show;

    if (status === 'COMPLETED' || status === 'DROPPED') {
      // LCARS-only — no arr interaction
      show = await addShow(input);
      if (show.status !== status) {
        await setStatus(show.id, status);
        show.status = status;
      }
    } else {
      // Try arr-backed add: first search for tvdbId/tmdbId
      const title = item.titleEnglish || item.titleRomaji;
      let arrInput = { ...input };
      if (status === 'PAUSED') arrInput.unmonitored = true;

      try {
        const candidates = await searchArrCandidates(mediaShape, title);
        if (candidates.length) {
          // Use the top Sonarr/Radarr hit's IDs
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
        // "already tracked" → mark card as tracked, don't mutate the existing show
        const match = err.message.match(/already tracked \(show ([^)]+)\)/);
        if (match) {
          item.lcarsShowId = match[1];
          item.lcarsStatus = 'PLANNED'; // show exists, but we don't know its exact status
          refreshCard(card, item);
          showBanner('Already tracked — use Add mode to manage seasons', 'info');
          card.classList.remove('loading');
          return;
        }
        // Arr failed — fall back to LCARS-only
        show = await addShow(input);
      }

      if (show.status !== status && status !== 'PLANNED') {
        await setStatus(show.id, status);
        show.status = status;
      }
    }

    // Update item state so card reflects it
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
  // Update chip highlights
  card.querySelectorAll('.cand-chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.status === eff);
  });
  // Re-apply filters in case status changed visibility
  applyFilters();
}

// ── Public API ───────────────────────────────────────────

/**
 * Initialize browse mode. Called when user switches to Browse tab.
 * @param {HTMLElement} results - the #browse-results container
 * @param {HTMLElement} controls - the .browse-controls container
 */
export function initBrowse(results, controls) {
  resultsEl = results;
  controlsEl = controls;

  // Default to upcoming season
  const def = defaultSeasonYear();
  currentSeason = def.season;
  currentYear = def.year;
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
