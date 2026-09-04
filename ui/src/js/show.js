/**
 * Show detail page — parse URL params, fetch show, render.
 *
 * Implemented: status change (radial picker), score edit (click-to-edit,
 * show + season level), watch/unwatch toggle per episode, mpv play per
 * episode, external IDs (sidebar: add/edit/delete + per-season AL/MAL
 * links), season mapping editor (setSeasonMapping), episode kind badges.
 *
 * Episode synopsis toggles — click to expand, lazy-fetched from
 * TVDB/TMDB on page load when missing.  Season posters fall back to show posterUrl
 * until Season.posterUrl is added (batch 6). Episode interleave
 * ordering TBD (batch 5 — specials placement decision pending).
 */

import { bootstrapConfig, requireConfig, rewriteHost } from './config.js?v=3';
import {
  fetchShow, addWatchEvent, deleteWatchEvent, setScore, setSeasonScore,
  setSeasonStatus, setSeasonMapping, reconcileSeasonMapping,
  fetchShowArt, selectArtAsset, deselectArtAsset, fetchEpisodeSynopses,
  setShowSynopsis, setEpisodeSynopsis, fetchSynopsisCandidates,
  linkShowExternalId, unlinkShowExternalId, refreshShowMetadata,
  setEpisodeNumber, splitSeason,
} from './api.js?v=12';
import {
  fmtEpBadge, availState, showBanner, hideBanner, launchMpv,
  buildStatusBtn,
} from './calendar.js?v=20';
import { SVC_ICONS, _mpvSvg, _downloadSvg } from './icons.js?v=9';
import { startDownload } from './downloads.js?v=2';

/* ── Constants ───────────────────────────────────────────── */

const STATUS_LABELS = {
  WATCHING: 'Watching', COMPLETED: 'Completed',
  PLANNED: 'Planned', PAUSED: 'Paused', DROPPED: 'Dropped',
};

/** URL templates for known services — derive URL from externalId.
 *  Templates take (id, mediaShape) so movie shows get correct paths. */
const SVC_URL_TEMPLATES = {
  anilist: id => `https://anilist.co/anime/${id}`,
  mal:     id => `https://myanimelist.net/anime/${id}`,
  tmdb:    (id, shape) => shape === 'MOVIE'
    ? `https://www.themoviedb.org/movie/${id}`
    : `https://www.themoviedb.org/tv/${id}`,
  tvdb:    id => `https://thetvdb.com/dereferrer/series/${id}`,
  imdb:    id => `https://www.imdb.com/title/${id}`,
};

const SVC_COLORS = {
  anilist: 'var(--svc-al)', mal: 'var(--svc-mal)',
  tmdb: 'var(--svc-tmdb)', tvdb: 'var(--svc-tvdb)',
  sonarr: 'var(--svc-sonarr)', radarr: 'var(--svc-radarr)',
  imdb: 'var(--svc-imdb)',
};
const SVC_ABBREVS = {
  anilist: 'AL', mal: 'ML', tmdb: 'TM', tvdb: 'TV',
  sonarr: 'S', radarr: 'R', imdb: 'IM',
};
const SVC_NAMES = {
  anilist: 'AniList', mal: 'MAL', tmdb: 'TMDB', tvdb: 'TVDB',
  sonarr: 'Sonarr', radarr: 'Radarr', imdb: 'IMDb',
};
/** Services managed by LCARS crosswalk — read-only in the UI. */
const READONLY_SVCS = new Set(['sonarr', 'radarr']);
/** All known services in display order. */
const ALL_SERVICES = ['anilist', 'mal', 'tmdb', 'tvdb', 'imdb', 'sonarr', 'radarr'];
const REWRITE_SVCS = new Set(['sonarr', 'radarr']);

/** Known episode kind badges. */
const KIND_BADGE = {
  BONUS_MOVIE: { label: 'Film',    cls: 'film' },
  SPECIAL:     { label: 'Special', cls: 'special' },
  OVA:         { label: 'OVA',     cls: 'ova' },
};

/* ── Helpers ─────────────────────────────────────────────── */

/** Create an element with optional className and textContent. */
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/** Format a date string as YYYY-MM-DD, or return '—'. */
function fmtDate(iso) {
  if (!iso) return '—';
  return iso.slice(0, 10);
}

/**
 * Compute availability state for an episode within the show page context.
 * availState() in calendar.js reads ep.show.mediaShape — episodes here
 * lack that back-reference, so we synthesize it.
 */
function epAvailState(ep, mediaShape) {
  // Attach the minimal .show stub that availState expects
  const patched = { ...ep, show: { mediaShape } };
  return availState(patched);
}

/** First air date = earliest non-null airDateUtc across all episodes. */
function firstAirDate(episodes) {
  let earliest = null;
  for (const ep of episodes) {
    if (!ep.airDateUtc) continue;
    if (!earliest || ep.airDateUtc < earliest) earliest = ep.airDateUtc;
  }
  return earliest;
}

/** Count watched episodes (those with at least one watchEvent). */
function countWatched(episodes) {
  return episodes.filter(ep => ep.watchEvents?.edges?.length > 0 || ep.watchEvents?.length > 0).length;
}

/**
 * Attach click-to-edit score behaviour to a container.
 * @param {HTMLElement} container - the clickable element
 * @param {HTMLElement} valSpan - the span showing the current value
 * @param {function} getCurrentVal - returns the current score (number|null)
 * @param {function} commitFn - async (rounded) => void; persists the new score
 * @param {object} [opts] - { maxEl: optional "/20" suffix span to hide during edit }
 */
function attachScoreEditor(container, valSpan, getCurrentVal, commitFn, opts = {}) {
  container.addEventListener('click', (e) => {
    e.stopPropagation(); // prevent season card collapse
    if (container.querySelector('input')) return; // already editing

    const currentVal = getCurrentVal();
    const input = document.createElement('input');
    input.type = 'number';
    input.className = 'sp-score-input';
    input.min = '0'; input.max = '20'; input.step = '0.25';
    input.value = currentVal != null ? currentVal : '';
    input.placeholder = '—';

    valSpan.hidden = true;
    if (opts.maxEl) opts.maxEl.hidden = true;
    container.insertBefore(input, valSpan);
    input.focus();
    input.select();

    // Guard against double-commit (blur fired by input.disabled or input.remove)
    let settled = false;

    const commit = async () => {
      if (settled) return;
      settled = true;
      const raw = parseFloat(input.value);
      if (isNaN(raw) || raw < 0 || raw > 20) {
        input.remove();
        valSpan.hidden = false;
        if (opts.maxEl) opts.maxEl.hidden = false;
        return;
      }
      const rounded = Math.round(raw * 4) / 4;
      input.disabled = true;
      try {
        await commitFn(rounded);
        valSpan.textContent = String(rounded);
        showBanner(`Score set to ${rounded}`, 'info');
        setTimeout(hideBanner, 2000);
      } catch (err) {
        showBanner(`Score error: ${err.message}`, 'error');
      }
      input.remove();
      valSpan.hidden = false;
      if (opts.maxEl) opts.maxEl.hidden = false;
    };

    const revert = () => {
      if (settled) return;
      settled = true;
      input.remove();
      valSpan.hidden = false;
      if (opts.maxEl) opts.maxEl.hidden = false;
    };

    input.addEventListener('click', e => e.stopPropagation());
    input.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); commit(); }
      if (e.key === 'Escape') { revert(); }
    });
    input.addEventListener('blur', commit);
  });
}

/* ── Art Picker ─────────────────────────────────────────── */

/**
 * Open an art picker overlay for a given slot (show poster, show banner,
 * season poster, etc.).
 *
 * @param {object} show - The show object (with artAssets array)
 * @param {string|null} seasonId - Season id (null = show-level)
 * @param {string} kind - 'poster' | 'banner' | 'background'
 * @param {HTMLImageElement|null} targetImg - The image element to update on selection
 * @param {function} onSelect - Callback(assetUrl) after selection
 */
function openArtPicker(show, seasonId, kind, targetImg, onSelect) {
  // Filter assets for this slot; banner slot also shows backgrounds
  const kindSet = kind === 'banner' ? new Set(['banner', 'background']) : new Set([kind]);
  const assets = (show.artAssets || []).filter(a => {
    const kindMatch = kindSet.has(a.kind.toLowerCase());
    if (seasonId) return kindMatch && a.seasonId === seasonId;
    return kindMatch && !a.seasonId;
  });

  // Build overlay
  const overlay = el('div', 'sp-art-overlay');
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.remove();
  });

  const modal = el('div', 'sp-art-modal');
  const title = el('h3', 'sp-art-modal-title',
    `Choose ${kind} art${seasonId ? '' : ' (show-level)'}`);
  modal.appendChild(title);

  // Fetch Art button
  const fetchBtn = el('button', 'sp-art-fetch-btn', '⟳ Fetch Art from Sources');
  fetchBtn.addEventListener('click', async () => {
    fetchBtn.disabled = true;
    fetchBtn.textContent = 'Fetching…';
    try {
      const result = await fetchShowArt(show.id);
      show.artAssets = result.artAssets;
      overlay.remove();
      openArtPicker(show, seasonId, kind, targetImg, onSelect);
    } catch (err) {
      fetchBtn.textContent = '✗ ' + err.message;
      setTimeout(() => {
        fetchBtn.disabled = false;
        fetchBtn.textContent = '⟳ Fetch Art from Sources';
      }, 3000);
    }
  });
  modal.appendChild(fetchBtn);

  if (!assets.length) {
    modal.appendChild(el('p', 'sp-art-empty',
      'No art assets yet. Click "Fetch Art" to retrieve from AniList & TVDB.'));
  }

  // Art grid
  const grid = el('div', 'sp-art-grid');
  for (const asset of assets) {
    const card = el('div', `sp-art-card${asset.selected ? ' selected' : ''}`);

    const img = el('img');
    img.src = asset.url;
    img.alt = `${asset.source} ${kind}`;
    img.loading = 'lazy';
    img.onerror = () => { img.style.opacity = '0.3'; };
    card.appendChild(img);

    const info = el('div', 'sp-art-card-info');
    const srcBadge = el('span', 'sp-art-source', asset.source);
    info.appendChild(srcBadge);
    if (asset.width && asset.height) {
      info.appendChild(el('span', 'sp-art-dims', `${asset.width}×${asset.height}`));
    }
    if (asset.selected) {
      info.appendChild(el('span', 'sp-art-selected-badge', '✓ Active'));
    }
    card.appendChild(info);

    card.addEventListener('click', async () => {
      // Deselect currently selected, select this one
      const prevSelected = assets.find(a => a.selected);
      try {
        if (prevSelected && prevSelected.id !== asset.id) {
          await deselectArtAsset(prevSelected.id);
          prevSelected.selected = false;
        }
        if (!asset.selected) {
          await selectArtAsset(asset.id);
          asset.selected = true;
        }
        if (targetImg) targetImg.src = asset.url;
        if (onSelect) onSelect(asset.url);
        overlay.remove();
      } catch (err) {
        showBanner(`Art selection failed: ${err.message}`, 'error');
      }
    });
    grid.appendChild(card);
  }
  modal.appendChild(grid);

  // Close button
  const closeBtn = el('button', 'sp-art-close-btn', '✕');
  closeBtn.addEventListener('click', () => overlay.remove());
  modal.appendChild(closeBtn);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}


/* ── Rendering ───────────────────────────────────────────── */

function renderBanner(show, root) {
  const banner = el('div', 'sp-banner sp-banner-clickable');
  let bannerImg = null;

  // Pick banner URL: honor explicit selection, otherwise rotate randomly
  // from available banner/background art assets
  let bannerSrc = null;
  const assets = show.artAssets || [];
  const isBannerKind = (k) => { const lk = k?.toLowerCase(); return lk === 'banner' || lk === 'background'; };
  const selectedBanner = assets.find(a =>
    a.selected && isBannerKind(a.kind) && !a.seasonId);
  if (selectedBanner) {
    bannerSrc = selectedBanner.url;
  } else {
    const bannerPool = assets.filter(a =>
      isBannerKind(a.kind) && !a.seasonId);
    if (bannerPool.length) {
      bannerSrc = bannerPool[Math.floor(Math.random() * bannerPool.length)].url;
    }
  }
  // Fall back to resolved bannerUrl (legacy field)
  if (!bannerSrc) bannerSrc = show.bannerUrl;

  if (bannerSrc) {
    bannerImg = el('img');
    bannerImg.src = bannerSrc;
    bannerImg.alt = '';
    bannerImg.onerror = () => { bannerImg.style.display = 'none'; };
    banner.appendChild(bannerImg);
  }
  banner.addEventListener('click', () => {
    openArtPicker(show, null, 'banner', bannerImg, (url) => {
      show.bannerUrl = url;
    });
  });
  root.appendChild(banner);
}

/**
 * Build an ext-ID badge (icon + id text). Returns an <a> or <span>.
 */
/** Services whose logos are wide wordmarks (need a wider icon box). */
const WIDE_ICON_SVCS = new Set(['imdb']);
const EXTRA_WIDE_ICON_SVCS = new Set(['tmdb']);

function buildExtBadge(svc, extId, url) {
  const isLink = !!url;
  const badge = document.createElement(isLink ? 'a' : 'span');
  badge.className = 'sp-ext-badge';
  if (isLink) {
    badge.href = url;
    badge.target = '_blank';
    badge.rel = 'noopener noreferrer';
  }
  const icon = el('span', 'sp-ext-badge-icon');
  if (EXTRA_WIDE_ICON_SVCS.has(svc)) icon.classList.add('extra-wide');
  else if (WIDE_ICON_SVCS.has(svc)) icon.classList.add('wide');
  if (SVC_ICONS[svc]) {
    icon.innerHTML = SVC_ICONS[svc];
    icon.style.background = 'none';
  } else {
    icon.style.background = SVC_COLORS[svc] || 'var(--muted)';
    icon.textContent = SVC_ABBREVS[svc] || svc.slice(0, 2).toUpperCase();
  }
  badge.appendChild(icon);
  badge.appendChild(document.createTextNode(extId));
  return badge;
}

/**
 * Render all show-level external ID badges into `container`, with
 * click-to-edit on editable services and an "add" button for missing ones.
 */
function renderExtBadges(container, show, cfg) {
  container.innerHTML = '';
  const extMap = {};
  for (const ext of (show.externalIds || [])) extMap[ext.service] = ext;

  // Render existing IDs in canonical order, then any extras
  const rendered = new Set();
  const orderedSvcs = [...ALL_SERVICES];
  for (const ext of (show.externalIds || [])) {
    if (!orderedSvcs.includes(ext.service)) orderedSvcs.push(ext.service);
  }

  for (const svc of orderedSvcs) {
    const ext = extMap[svc];
    if (!ext) continue;
    rendered.add(svc);
    const url = REWRITE_SVCS.has(svc)
      ? rewriteHost(ext.url, cfg.home_server_host)
      : ext.url;
    const badge = buildExtBadge(svc, ext.externalId, url);

    // Editable badge: right-click or long-press to edit
    if (!READONLY_SVCS.has(svc)) {
      badge.classList.add('sp-ext-editable');
      badge.addEventListener('contextmenu', (e) => {
        e.preventDefault();
        openExtEditor(container, show, cfg, svc, ext.externalId, ext.url);
      });
    }
    container.appendChild(badge);
  }

  // "+" button to add a new external ID
  const addBtn = el('button', 'sp-ext-badge sp-ext-add', '+');
  addBtn.title = 'Add external ID';
  addBtn.addEventListener('click', () => {
    openExtEditor(container, show, cfg, '', '', '');
  });
  container.appendChild(addBtn);

  // "↻" button to refresh metadata (re-runs server-side ID resolution)
  const refreshBtn = el('button', 'sp-ext-badge sp-ext-add', '↻');
  refreshBtn.title = 'Refresh metadata (re-fetch IDs from AniList/TVDB)';
  refreshBtn.addEventListener('click', async () => {
    refreshBtn.disabled = true;
    refreshBtn.textContent = '…';
    try {
      const updated = await refreshShowMetadata(show.id);
      // Update local show data with refreshed externalIds
      if (updated.externalIds?.edges) {
        show.externalIds = updated.externalIds.edges.map(e => e.node);
      }
      renderExtBadges(container, show, cfg);
      showBanner('Metadata refreshed.', 'ok');
    } catch (e) {
      showBanner(`Refresh failed: ${e.message}`, 'error');
      refreshBtn.disabled = false;
      refreshBtn.textContent = '↻';
    }
  });
  container.appendChild(refreshBtn);
}

/* ── TMDB auto-search for external IDs ──────────────────── */

/** TMDB search URLs for manual fallback (when API finds nothing). */
const TMDB_SEARCH_URLS = {
  tmdb: (q, shape) => `https://www.themoviedb.org/search/${shape === 'MOVIE' ? 'movie' : 'tv'}?query=${encodeURIComponent(q)}`,
  tvdb: q => `https://thetvdb.com/search?query=${encodeURIComponent(q)}`,
  imdb: q => `https://www.imdb.com/find/?q=${encodeURIComponent(q)}`,
};

/**
 * Search TMDB for a show by title and return external IDs for tmdb/tvdb/imdb.
 * Returns { tmdb, tvdb, imdb } where each is { id, url } or null.
 */
async function searchTmdbExternalIds(title, mediaShape, apiKey) {
  if (!apiKey) return null;

  const isJwt = apiKey.startsWith('eyJ');
  const headers = isJwt ? { Authorization: `Bearer ${apiKey}` } : {};
  const tmdbFetch = (path, extraParams = {}) => {
    const params = new URLSearchParams(extraParams);
    if (!isJwt) params.set('api_key', apiKey);
    return fetch(`https://api.themoviedb.org/3${path}?${params}`, { headers });
  };

  try {
    const type = mediaShape === 'MOVIE' ? 'movie' : 'tv';

    // 1. Search by title
    const searchRes = await tmdbFetch(`/search/${type}`, { query: title, page: 1 });
    if (!searchRes.ok) return null;
    const searchData = await searchRes.json();
    const firstResult = searchData.results?.[0];
    if (!firstResult) return null;

    const tmdbId = String(firstResult.id);
    const result = {
      tmdb: { id: tmdbId, url: SVC_URL_TEMPLATES.tmdb(tmdbId, mediaShape) },
      tvdb: null,
      imdb: null,
    };

    // 2. Fetch external IDs from TMDB to get IMDb + TVDB
    const extRes = await tmdbFetch(`/${type}/${tmdbId}/external_ids`);
    if (extRes.ok) {
      const extData = await extRes.json();
      if (extData.imdb_id) {
        result.imdb = { id: extData.imdb_id, url: SVC_URL_TEMPLATES.imdb(extData.imdb_id) };
      }
      if (extData.tvdb_id) {
        result.tvdb = { id: String(extData.tvdb_id), url: SVC_URL_TEMPLATES.tvdb(String(extData.tvdb_id)) };
      }
    }

    return result;
  } catch {
    return null;
  }
}

/**
 * Open an inline editor for adding/editing/deleting an external ID.
 */
function openExtEditor(container, show, cfg, service, extId, url) {
  // Remove any existing editor
  container.querySelector('.sp-ext-editor')?.remove();

  const editor = el('div', 'sp-ext-editor');

  // Service selector (only for new entries)
  const svcSelect = document.createElement('select');
  svcSelect.className = 'sp-ext-input sp-ext-svc-select';
  if (service) {
    // Editing existing — fixed service
    const opt = document.createElement('option');
    opt.value = service;
    opt.textContent = SVC_NAMES[service] || service;
    svcSelect.appendChild(opt);
    svcSelect.disabled = true;
  } else {
    // Adding new — show available services
    const extMap = {};
    for (const ext of (show.externalIds || [])) extMap[ext.service] = ext;
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Service…';
    placeholder.disabled = true;
    placeholder.selected = true;
    svcSelect.appendChild(placeholder);
    for (const svc of ALL_SERVICES) {
      if (extMap[svc] || READONLY_SVCS.has(svc)) continue;
      const opt = document.createElement('option');
      opt.value = svc;
      opt.textContent = SVC_NAMES[svc] || svc;
      svcSelect.appendChild(opt);
    }
    // Option for custom service name
    const customOpt = document.createElement('option');
    customOpt.value = '__custom__';
    customOpt.textContent = 'Other…';
    svcSelect.appendChild(customOpt);
  }
  editor.appendChild(svcSelect);

  // Custom service name input (hidden unless "Other…" selected)
  const customSvcInput = document.createElement('input');
  customSvcInput.type = 'text';
  customSvcInput.className = 'sp-ext-input';
  customSvcInput.placeholder = 'service name';
  customSvcInput.hidden = true;
  editor.appendChild(customSvcInput);
  // ID input
  const idInput = document.createElement('input');
  idInput.type = 'text';
  idInput.className = 'sp-ext-input';
  idInput.placeholder = 'ID';
  idInput.value = extId || '';
  editor.appendChild(idInput);

  // URL input + verify link wrapper
  const urlWrap = el('span', 'sp-ext-url-wrap');
  const urlInput = document.createElement('input');
  urlInput.type = 'text';
  urlInput.className = 'sp-ext-input sp-ext-url';
  urlInput.placeholder = 'URL';
  urlInput.value = url || '';
  urlWrap.appendChild(urlInput);

  const verifyLink = el('a', 'sp-ext-verify');
  verifyLink.textContent = '↗';
  verifyLink.title = 'Open to verify';
  verifyLink.target = '_blank';
  verifyLink.rel = 'noopener noreferrer';
  verifyLink.hidden = true;
  urlWrap.appendChild(verifyLink);
  editor.appendChild(urlWrap);

  // Status indicator for auto-search
  const searchStatus = el('span', 'sp-ext-search-status');
  searchStatus.hidden = true;
  editor.appendChild(searchStatus);

  function updateVerifyLink() {
    const u = urlInput.value.trim();
    if (u && u.startsWith('http')) {
      verifyLink.href = u;
      verifyLink.hidden = false;
    } else {
      verifyLink.hidden = true;
    }
  }

  function autoFillUrl() {
    const svc = svcSelect.value === '__custom__' ? customSvcInput.value : svcSelect.value;
    const id = idInput.value.trim();
    if (id && SVC_URL_TEMPLATES[svc]) {
      urlInput.value = SVC_URL_TEMPLATES[svc](id, show.mediaShape);
    }
    updateVerifyLink();
  }
  idInput.addEventListener('input', autoFillUrl);
  urlInput.addEventListener('input', updateVerifyLink);

  /** Auto-search TMDB for the selected service's ID. */
  const AUTO_SEARCH_SVCS = new Set(['tmdb', 'tvdb', 'imdb']);
  let _cachedSearch = null; // cache the TMDB search result across service switches

  async function autoSearchForService(svc) {
    if (!AUTO_SEARCH_SVCS.has(svc) || !cfg.tmdb_api_key) return;

    // Show searching status
    searchStatus.textContent = 'Searching…';
    searchStatus.hidden = false;

    try {
      // Use cached result if available (same show, same search)
      if (!_cachedSearch) {
        _cachedSearch = await searchTmdbExternalIds(show.displayTitle, show.mediaShape, cfg.tmdb_api_key);
      }

      if (_cachedSearch && _cachedSearch[svc]) {
        const match = _cachedSearch[svc];
        idInput.value = match.id;
        urlInput.value = match.url;
        updateVerifyLink();
        searchStatus.textContent = 'Found — verify ↗';
        searchStatus.className = 'sp-ext-search-status sp-ext-search-ok';
      } else {
        // No match — show link to search manually
        searchStatus.textContent = '';
        searchStatus.className = 'sp-ext-search-status sp-ext-search-miss';
        if (TMDB_SEARCH_URLS[svc]) {
          const searchLink = el('a', 'sp-ext-search-link');
          searchLink.textContent = `Search ${SVC_NAMES[svc] || svc} ↗`;
          searchLink.href = TMDB_SEARCH_URLS[svc](show.displayTitle, show.mediaShape);
          searchLink.target = '_blank';
          searchLink.rel = 'noopener noreferrer';
          searchStatus.appendChild(searchLink);
        } else {
          searchStatus.textContent = 'Not found';
        }
      }
    } catch {
      searchStatus.textContent = 'Search failed';
      searchStatus.className = 'sp-ext-search-status sp-ext-search-miss';
    }
  }

  svcSelect.addEventListener('change', () => {
    customSvcInput.hidden = svcSelect.value !== '__custom__';
    searchStatus.hidden = true;
    // Auto-fill URL from template when ID is present
    autoFillUrl();
    // Auto-search for tmdb/tvdb/imdb services
    if (!service && AUTO_SEARCH_SVCS.has(svcSelect.value)) {
      autoSearchForService(svcSelect.value);
    }
  });

  // If adding and no url yet, try auto-fill after selecting service
  if (!service) autoFillUrl();

  // Action buttons
  const actions = el('span', 'sp-ext-actions');

  const saveBtn = el('button', 'sp-ext-save', '✓');
  saveBtn.title = 'Save';
  saveBtn.addEventListener('click', async () => {
    const svc = svcSelect.value === '__custom__' ? customSvcInput.value.trim() : svcSelect.value;
    const id = idInput.value.trim();
    const u = urlInput.value.trim();
    if (!svc || !id || !u) {
      showBanner('Service, ID, and URL are all required.', 'error');
      return;
    }
    try {
      saveBtn.disabled = true;
      const result = await linkShowExternalId(show.id, svc, id, u);
      // Update local data
      const existing = (show.externalIds || []).find(e => e.service === svc);
      if (existing) {
        existing.externalId = result.externalId;
        existing.url = result.url;
      } else {
        show.externalIds = show.externalIds || [];
        show.externalIds.push({ service: svc, externalId: result.externalId, url: result.url });
      }
      renderExtBadges(container, show, cfg);
      showBanner(`${SVC_NAMES[svc] || svc} ID saved.`, 'ok');
    } catch (e) {
      showBanner(`Failed to save: ${e.message}`, 'error');
      saveBtn.disabled = false;
    }
  });
  actions.appendChild(saveBtn);

  // Delete button (only for existing entries)
  if (service) {
    const delBtn = el('button', 'sp-ext-del', '✕');
    delBtn.title = 'Remove';
    delBtn.addEventListener('click', async () => {
      if (!confirm(`Remove ${SVC_NAMES[service] || service} link?`)) return;
      try {
        delBtn.disabled = true;
        await unlinkShowExternalId(show.id, service);
        show.externalIds = (show.externalIds || []).filter(e => e.service !== service);
        renderExtBadges(container, show, cfg);
        showBanner(`${SVC_NAMES[service] || service} ID removed.`, 'ok');
      } catch (e) {
        showBanner(`Failed to remove: ${e.message}`, 'error');
        delBtn.disabled = false;
      }
    });
    actions.appendChild(delBtn);
  }

  const cancelBtn = el('button', 'sp-ext-cancel', '←');
  cancelBtn.title = 'Cancel';
  cancelBtn.addEventListener('click', () => editor.remove());
  actions.appendChild(cancelBtn);

  editor.appendChild(actions);
  container.appendChild(editor);

  // Focus the first relevant input
  if (service) idInput.focus();
  else svcSelect.focus();
}

function renderHero(show, root, cfg) {
  const hero = el('div', 'sp-hero');

  // Poster (clickable for art picker)
  const posterWrap = el('div', 'sp-poster sp-poster-clickable');
  let heroImg = null;
  if (show.posterUrl) {
    heroImg = el('img');
    heroImg.src = show.posterUrl;
    heroImg.alt = show.displayTitle;
    heroImg.onerror = () => { posterWrap.style.background = 'var(--surface-3)'; };
    posterWrap.appendChild(heroImg);
  }
  const editHint = el('span', 'sp-poster-edit-hint', '🖼');
  posterWrap.appendChild(editHint);
  posterWrap.addEventListener('click', () => {
    openArtPicker(show, null, 'poster', heroImg, (url) => {
      show.posterUrl = url;
    });
  });
  hero.appendChild(posterWrap);

  // Header
  const header = el('div', 'sp-header');

  // Title row
  const titleRow = el('div', 'sp-title-row');
  const h1 = el('h1', 'sp-title', show.displayTitle);
  titleRow.appendChild(h1);
  // Native title as alt
  const altTitle = show.titleNative || show.titleRomaji;
  if (altTitle && altTitle !== show.displayTitle) {
    titleRow.appendChild(el('span', 'sp-alt-title', altTitle));
  }
  header.appendChild(titleRow);

  // Meta row
  const metaRow = el('div', 'sp-meta-row');

  // Interactive status picker (radial, reused from calendar)
  const statusWrap = el('div', 'sp-status-wrap');
  const btnWrap = buildStatusBtn(show.id, show.status || 'PLANNED');
  statusWrap.appendChild(btnWrap);
  const statusLabel = el('span', 'sp-status-label');
  statusLabel.dataset.status = show.status || 'PLANNED';
  statusLabel.textContent = STATUS_LABELS[show.status] || show.status || '—';
  statusWrap.appendChild(statusLabel);
  // Sync label when onStatusChange updates the wrap's dataset
  new MutationObserver(() => {
    const s = btnWrap.dataset.status;
    statusLabel.dataset.status = s;
    statusLabel.textContent = STATUS_LABELS[s] || s || '—';
  }).observe(btnWrap, { attributes: true, attributeFilter: ['data-status'] });

  // When show is marked COMPLETED, offer to bulk-mark all episodes as watched
  btnWrap.addEventListener('status-confirmed', async (e) => {
    if (e.detail.status !== 'COMPLETED') return;
    const allEps = show.episodes || [];
    const unwatched = allEps.filter(ep => {
      const isWatched = (ep.watchEvents?.edges?.length > 0) ||
                        (Array.isArray(ep.watchEvents) && ep.watchEvents.length > 0);
      return !isWatched;
    });
    if (unwatched.length === 0) return;
    if (!confirm(`Mark all ${unwatched.length} unwatched episode${unwatched.length > 1 ? 's' : ''} as watched?`)) return;

    showBanner(`Marking ${unwatched.length} episodes as watched…`, 'loading');
    let ok = 0, fail = 0;
    for (const ep of unwatched) {
      try {
        const wid = await addWatchEvent(show.id, ep.season, ep.episode);
        if (!ep.watchEvents) ep.watchEvents = { edges: [] };
        if (ep.watchEvents.edges) ep.watchEvents.edges.push({ node: { id: wid } });
        else ep.watchEvents.push({ id: wid });
        ok++;
        // Update watch button in DOM
        const btn = document.querySelector(`.watch-btn[data-episode-id="${ep.id}"]`);
        if (btn) {
          btn.classList.add('watched');
          btn.dataset.state = 'WATCHED';
          btn.dataset.watchEventId = wid;
          btn.title = 'Watched — click to unmark';
        }
      } catch (err) {
        fail++;
        console.warn(`Watch failed for S${ep.season}E${ep.episode}:`, err);
      }
    }
    // Update all ep count labels
    document.querySelectorAll('.sp-season-ep-count').forEach(lbl => {
      const card = lbl.closest('.sp-season');
      if (!card) return;
      const sn = parseInt(card.dataset?.season);
      if (isNaN(sn)) return;
      const seasonEps = allEps.filter(e => e.season === sn);
      const watched = countWatched(seasonEps);
      lbl.textContent = `${watched}/${seasonEps.length} eps`;
    });
    showBanner(`✓ ${ok} marked watched${fail ? `, ${fail} failed` : ''}`, fail ? 'error' : 'ok');
    setTimeout(hideBanner, 3000);
  });

  metaRow.appendChild(statusWrap);

  // Tracking space badge
  if (show.trackingSpace) {
    const tsBadge = el('span', `sp-ts-badge sp-ts-${show.trackingSpace.toLowerCase()}`,
      show.trackingSpace);
    metaRow.appendChild(tsBadge);
  }

  metaRow.appendChild(el('div', 'sp-meta-sep'));

  // Score (click-to-edit)
  const scoreDiv = el('div', 'sp-score sp-score-editable');
  scoreDiv.title = 'Click to edit score';
  const scoreValSpan = el('span', 'sp-score-value', show.score != null ? String(show.score) : '—');
  const scoreMaxSpan = el('span', 'sp-score-max', '/20');
  scoreDiv.appendChild(scoreValSpan);
  scoreDiv.appendChild(scoreMaxSpan);

  attachScoreEditor(scoreDiv, scoreValSpan, () => show.score, async (rounded) => {
    await setScore(show.id, rounded);
    show.score = rounded;
  }, { maxEl: scoreMaxSpan });

  metaRow.appendChild(scoreDiv);
  metaRow.appendChild(el('div', 'sp-meta-sep'));

  // First air date — earliest non-null airDateUtc across all episodes
  const firstAir = firstAirDate(show.episodes);
  if (firstAir) {
    metaRow.appendChild(el('span', 'sp-meta-label', 'First aired'));
    metaRow.appendChild(el('span', 'sp-meta-value', fmtDate(firstAir)));
    metaRow.appendChild(el('div', 'sp-meta-sep'));
  }

  // Started
  const startedAt = earliestSeasonDate(show.seasons, 'startedAt');
  if (startedAt) {
    metaRow.appendChild(el('span', 'sp-meta-label', 'Started'));
    metaRow.appendChild(el('span', 'sp-meta-value', fmtDate(startedAt)));
    metaRow.appendChild(el('div', 'sp-meta-sep'));
  }

  // Episode progress
  const watched = countWatched(show.episodes);
  const total = show.totalEpisodes || show.episodes.length;
  metaRow.appendChild(el('span', 'sp-meta-label', 'Episodes'));
  metaRow.appendChild(el('span', 'sp-meta-value', `${watched} / ${total}`));

  header.appendChild(metaRow);

  // Details row (inline — replaces sidebar details card)
  const detailsRow = el('div', 'sp-details-row');
  const detailItems = [];
  detailItems.push(show.mediaShape === 'MOVIE' ? 'Film' : 'Episodic');
  if (show.durationMinutes) detailItems.push(`${show.durationMinutes} min`);
  if (show.genresRaw?.length) detailItems.push(show.genresRaw.join(', '));
  if (show.studioCredits?.length) {
    const studios = show.studioCredits.map(s => s.studio.name);
    if (studios.length) detailItems.push(studios.join(', '));
  }
  for (let i = 0; i < detailItems.length; i++) {
    if (i > 0) detailsRow.appendChild(el('span', 'sp-detail-sep', '·'));
    detailsRow.appendChild(el('span', 'sp-detail-item', detailItems[i]));
  }
  header.appendChild(detailsRow);

  // Show-level external IDs (inline badges — editable)
  const extRow = el('div', 'sp-ext-inline');
  renderExtBadges(extRow, show, cfg);
  header.appendChild(extRow);

  hero.appendChild(header);
  root.appendChild(hero);
}

/** Earliest date across all seasons for a given field. */
function earliestSeasonDate(seasons, field) {
  let earliest = null;
  for (const s of seasons) {
    const val = s[field];
    if (!val) continue;
    if (!earliest || val < earliest) earliest = val;
  }
  return earliest;
}

function renderBody(show, root, cfg, targetSeason) {
  const body = el('div', 'sp-body');

  renderSynopsis(show, body);

  // Movie — no seasons/episodes section
  if (show.mediaShape === 'MOVIE') {
    renderMovieInfo(show, body, cfg);
  } else {
    renderSeasons(show, body, cfg, targetSeason);
  }

  root.appendChild(body);
}

function renderSynopsis(show, body) {
  const col = el('div', 'sp-synopsis-section');

  // Header row: label + Sources button
  const hdr = el('div', 'sp-synopsis-hdr');
  hdr.appendChild(el('div', 'sp-section-label', 'Synopsis'));
  const srcBtn = el('button', 'sp-sources-btn', '⟳ Sources');
  srcBtn.title = 'Compare synopses from AniList, TVDB, TMDB';
  srcBtn.addEventListener('click', () => openSynopsisSourcesModal(show));
  hdr.appendChild(srcBtn);
  col.appendChild(hdr);

  // Synopsis text (click to edit)
  const synP = el('p', 'sp-synopsis sp-synopsis-editable',
    show.synopsis || 'No synopsis available.');
  if (!show.synopsis) synP.classList.add('empty');
  synP.title = 'Click to edit';
  synP.addEventListener('click', () => {
    if (col.querySelector('.sp-syn-editor')) return; // already editing
    openInlineSynopsisEditor(col, synP, show);
  });
  col.appendChild(synP);

  body.appendChild(col);
}

function openInlineSynopsisEditor(container, synP, show) {
  synP.hidden = true;
  const editor = el('div', 'sp-syn-editor');
  const textarea = document.createElement('textarea');
  textarea.className = 'sp-syn-textarea';
  textarea.value = show.synopsis || '';
  textarea.rows = 5;
  textarea.placeholder = 'Enter synopsis…';
  editor.appendChild(textarea);

  const btnRow = el('div', 'sp-syn-editor-btns');
  const saveBtn = el('button', 'sp-syn-save', 'Save');
  const cancelBtn = el('button', 'sp-syn-cancel', 'Cancel');

  cancelBtn.addEventListener('click', () => {
    editor.remove();
    synP.hidden = false;
  });

  saveBtn.addEventListener('click', async () => {
    const text = textarea.value.trim();
    if (!text) { editor.remove(); synP.hidden = false; return; }
    saveBtn.disabled = true;
    try {
      await setShowSynopsis(show.id, text);
      show.synopsis = text;
      synP.textContent = text;
      synP.classList.remove('empty');
      showBanner('Synopsis saved', 'info');
      setTimeout(hideBanner, 2000);
    } catch (err) {
      showBanner(`Synopsis error: ${err.message}`, 'error');
    }
    editor.remove();
    synP.hidden = false;
  });

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') { cancelBtn.click(); }
  });

  btnRow.appendChild(saveBtn);
  btnRow.appendChild(cancelBtn);
  editor.appendChild(btnRow);
  container.insertBefore(editor, synP.nextSibling);
  textarea.focus();
}

/* ── Synopsis Sources Modal ─────────────────────────────── */

function openSynopsisSourcesModal(show, episodeId = null, onPick = null) {
  const overlay = el('div', 'sp-art-overlay');
  overlay.addEventListener('click', e => { if (e.target === overlay) overlay.remove(); });

  const modal = el('div', 'sp-sources-modal');
  const title = episodeId
    ? el('h3', 'sp-art-modal-title', 'Episode Synopsis Sources')
    : el('h3', 'sp-art-modal-title', 'Show Synopsis Sources');
  modal.appendChild(title);

  const loading = el('p', 'sp-sources-loading', 'Fetching from sources…');
  modal.appendChild(loading);

  const closeBtn = el('button', 'sp-art-close-btn', '✕');
  closeBtn.addEventListener('click', () => overlay.remove());
  modal.appendChild(closeBtn);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  // Fetch candidates
  fetchSynopsisCandidates(show.id, episodeId)
    .then(candidates => {
      loading.remove();
      if (!candidates.length) {
        modal.insertBefore(
          el('p', 'sp-sources-empty', 'No external sources configured or available.'),
          closeBtn);
        return;
      }

      const grid = el('div', 'sp-sources-grid');
      for (const c of candidates) {
        const card = el('div', 'sp-source-card');
        const srcLabel = el('div', 'sp-source-label',
          SVC_NAMES[c.source] || c.source.toUpperCase());
        card.appendChild(srcLabel);

        if (c.text) {
          const textP = el('p', 'sp-source-text', c.text);
          card.appendChild(textP);
          const useBtn = el('button', 'sp-source-use-btn', 'Use this');
          useBtn.addEventListener('click', async () => {
            useBtn.disabled = true;
            useBtn.textContent = 'Saving…';
            try {
              if (episodeId) {
                await setEpisodeSynopsis(episodeId, c.text);
              } else {
                await setShowSynopsis(show.id, c.text);
                show.synopsis = c.text;
                // Update inline display
                const synEl = document.querySelector('.sp-synopsis');
                if (synEl) {
                  synEl.textContent = c.text;
                  synEl.classList.remove('empty');
                }
              }
              if (onPick) onPick(c.text);
              showBanner(`Synopsis set from ${SVC_NAMES[c.source] || c.source}`, 'info');
              setTimeout(hideBanner, 2000);
              overlay.remove();
            } catch (err) {
              useBtn.disabled = false;
              useBtn.textContent = 'Use this';
              showBanner(`Error: ${err.message}`, 'error');
            }
          });
          card.appendChild(useBtn);
        } else {
          card.appendChild(el('p', 'sp-source-text empty', 'No synopsis from this source.'));
        }
        grid.appendChild(card);
      }
      modal.insertBefore(grid, closeBtn);
    })
    .catch(err => {
      loading.textContent = `Error: ${err.message}`;
    });
}

function openEpisodeSynopsisEditor(synRow, synText, ep, show) {
  if (synRow.querySelector('.sp-ep-syn-edit-area')) return;
  synText.hidden = true;
  const area = el('div', 'sp-ep-syn-edit-area');
  const textarea = document.createElement('textarea');
  textarea.className = 'sp-ep-syn-textarea';
  textarea.value = ep.synopsis || '';
  textarea.rows = 3;
  textarea.placeholder = 'Enter episode synopsis…';
  area.appendChild(textarea);

  const btnRow = el('div', 'sp-syn-editor-btns');
  const saveBtn = el('button', 'sp-syn-save', 'Save');
  const cancelBtn = el('button', 'sp-syn-cancel', 'Cancel');

  cancelBtn.addEventListener('click', () => {
    area.remove();
    synText.hidden = false;
  });

  saveBtn.addEventListener('click', async () => {
    const text = textarea.value.trim();
    if (!text) { area.remove(); synText.hidden = false; return; }
    saveBtn.disabled = true;
    try {
      await setEpisodeSynopsis(ep.id, text);
      ep.synopsis = text;
      synText.textContent = text;
      // Update toggle button appearance
      const epRow = synRow.previousElementSibling;
      const toggleBtn = epRow?.querySelector('.sp-syn-toggle');
      if (toggleBtn) {
        toggleBtn.textContent = 'ℹ';
        toggleBtn.classList.remove('add-mode');
      }
      showBanner('Episode synopsis saved', 'info');
      setTimeout(hideBanner, 2000);
    } catch (err) {
      showBanner(`Synopsis error: ${err.message}`, 'error');
    }
    area.remove();
    synText.hidden = false;
  });

  textarea.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') cancelBtn.click();
  });

  btnRow.appendChild(saveBtn);
  btnRow.appendChild(cancelBtn);
  area.appendChild(btnRow);
  synRow.insertBefore(area, synText);
  textarea.focus();
}

/* renderSidebar removed — details + ext IDs now inline in hero */

function renderMovieInfo(show, body, cfg) {
  const section = el('div', 'sp-movie-info');
  section.appendChild(el('div', 'sp-section-label', 'Movie'));

  const avail = show.availableViaRadarr;
  const statusText = avail === 'AVAILABLE' ? 'Available'
    : avail === 'DOWNLOADING' ? 'Downloading'
    : 'Not available';
  section.appendChild(el('p', 'sp-meta-value', statusText));

  body.appendChild(section);
}

function renderSeasons(show, body, cfg, targetSeason) {
  const section = el('div', 'sp-seasons');
  const sectionLabel = el('div', 'sp-section-label-row');
  sectionLabel.appendChild(el('span', 'sp-section-label', 'Seasons & Episodes'));
  const addSeasonBtn = el('button', 'sp-add-season-btn', '+ Add Season');
  addSeasonBtn.addEventListener('click', () => openAddSeasonForm(section, show, cfg, targetSeason));
  sectionLabel.appendChild(addSeasonBtn);
  section.appendChild(sectionLabel);

  // ── 1. Group episodes by season number ──
  const epsBySeason = new Map();
  for (const ep of show.episodes) {
    const sn = ep.season ?? 0;
    if (!epsBySeason.has(sn)) epsBySeason.set(sn, []);
    epsBySeason.get(sn).push(ep);
  }

  // Build season map from Season rows
  const seasonMap = new Map();
  for (const s of show.seasons) {
    seasonMap.set(s.seasonNumber, s);
  }

  // ── 2. Partition season-0 episodes: interleave / minisode / remainder ──
  const s0Eps = epsBySeason.get(0) || [];
  const interleaveEps = []; // non-REGULAR with integer absoluteNumber → special cards
  const minisodeEps = [];   // fractional absoluteNumber (e.g. 1.1, 2.1) → inline rows
  const s0Remainder = [];   // stay in the Specials card at the bottom
  for (const ep of s0Eps) {
    if (ep.kind !== 'REGULAR' && ep.absoluteNumber != null) {
      if (ep.absoluteNumber % 1 !== 0) {
        minisodeEps.push(ep);
      } else {
        interleaveEps.push(ep);
      }
    } else {
      s0Remainder.push(ep);
    }
  }

  // ── 3. Collect non-zero season numbers, descending ──
  const allSeasonNums = new Set([...epsBySeason.keys(), ...seasonMap.keys()]);
  const hasSeasonZero = allSeasonNums.has(0);
  allSeasonNums.delete(0);
  const sorted = [...allSeasonNums].sort((a, b) => b - a);

  // ── 4. Derive abs range per season ──
  function seasonAbsRange(sn) {
    const sd = seasonMap.get(sn);
    if (sd?.absStart != null && sd?.absEnd != null) return [sd.absStart, sd.absEnd];
    // Fall back to min/max of regular episodes' absoluteNumber
    const eps = (epsBySeason.get(sn) || []).filter(e => e.kind === 'REGULAR' && e.absoluteNumber != null);
    if (!eps.length) return null;
    const nums = eps.map(e => e.absoluteNumber);
    return [Math.min(...nums), Math.max(...nums)];
  }

  // ── 5. Build render plan: [{type:'season'|'special', ...}] in descending order ──
  const plan = [];
  const placed = new Set(); // track interleaved eps by id

  for (const sn of sorted) {
    const seasonData = seasonMap.get(sn) || null;
    const eps = (epsBySeason.get(sn) || []).slice();
    eps.sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));
    const range = seasonAbsRange(sn);

    // Find specials that fall inside this season's abs range
    const insideSpecials = [];
    if (range) {
      for (const sp of interleaveEps) {
        if (placed.has(sp.id)) continue;
        if (sp.absoluteNumber > range[0] && sp.absoluteNumber < range[1]) {
          insideSpecials.push(sp);
        }
      }
      insideSpecials.sort((a, b) => a.absoluteNumber - b.absoluteNumber);
    }

    if (insideSpecials.length > 0) {
      // Split season at each special's position
      // Work out abs number for each regular ep: use absStart + (episode-1) if season has absStart
      const sdAbsStart = seasonData?.absStart;
      function epAbsNum(ep) {
        if (ep.absoluteNumber != null) return ep.absoluteNumber;
        if (sdAbsStart != null) return sdAbsStart + (ep.episode - 1);
        return null;
      }

      // Build segments in ascending order, then reverse for descending display
      const segments = []; // [{type:'season'|'special', ...}]
      let remaining = eps.slice();
      for (const sp of insideSpecials) {
        const before = [];
        const after = [];
        for (const ep of remaining) {
          const absN = epAbsNum(ep);
          if (absN != null && absN <= sp.absoluteNumber) before.push(ep);
          else after.push(ep);
        }
        if (before.length) {
          const firstEp = before[0].episode;
          const lastEp = before[before.length - 1].episode;
          segments.push({
            type: 'season', sn, seasonData, eps: before, show,
            rangeLabel: `(ep ${firstEp}–${lastEp})`,
          });
        }
        segments.push({ type: 'special', ep: sp });
        placed.add(sp.id);
        remaining = after;
      }
      if (remaining.length) {
        const firstEp = remaining[0].episode;
        const lastEp = remaining[remaining.length - 1].episode;
        segments.push({
          type: 'season', sn, seasonData, eps: remaining, show,
          rangeLabel: `(ep ${firstEp}–${lastEp})`,
        });
      }
      // Reverse so higher episodes appear first (descending page order)
      segments.reverse();
      let splitIdx = 0;
      for (const seg of segments) {
        if (seg.type === 'season') {
          seg.suffix = splitIdx > 0 ? `-split-${splitIdx}` : '';
          splitIdx++;
        }
        plan.push(seg);
      }
    } else {
      // No splits — render whole season
      plan.push({ type: 'season', sn, seasonData, eps, show, rangeLabel: null, suffix: '' });
    }

  }

  // Place any remaining interleave candidates that weren't placed inside a season
  // Sort by absoluteNumber descending to match the page order
  const unplaced = interleaveEps.filter(ep => !placed.has(ep.id));
  unplaced.sort((a, b) => b.absoluteNumber - a.absoluteNumber);

  // Insert unplaced specials into the plan at their correct descending position.
  // Plan is descending (highest abs first). A special at absNum X goes before
  // the first season segment whose absEnd < X (i.e., after all higher content).
  for (const sp of unplaced) {
    let insertIdx = plan.length; // default: end (before season 0)
    for (let i = 0; i < plan.length; i++) {
      const item = plan[i];
      if (item.type === 'season') {
        const range = seasonAbsRange(item.sn);
        if (range && range[1] < sp.absoluteNumber) {
          insertIdx = i;
          break;
        }
      }
    }
    plan.splice(insertIdx, 0, { type: 'special', ep: sp });
    placed.add(sp.id);
  }

  // ── 5b. Attach minisode episodes to the season they belong to ──
  // Minisodes have fractional abs numbers (e.g. 1.1 follows regular ep #1)
  // Group by Math.floor(absoluteNumber) to find host season
  for (const item of plan) {
    if (item.type !== 'season') continue;
    const range = seasonAbsRange(item.sn);
    if (!range) continue;
    const attached = [];
    for (const mini of minisodeEps) {
      const hostAbs = Math.floor(mini.absoluteNumber);
      if (hostAbs >= range[0] && hostAbs <= range[1]) {
        attached.push(mini);
      }
    }
    if (attached.length) item.minisodes = attached;
  }

  // ── 6. Determine target / startOpen ──
  const targetExists = targetSeason != null &&
    (allSeasonNums.has(targetSeason) || (targetSeason === 0 && hasSeasonZero));
  const effectiveTarget = targetExists ? targetSeason : null;
  const firstSeason = sorted[0]; // newest season number

  // Track score value spans per season for cross-segment updates
  const seasonScoreSpans = new Map(); // sn → [span, ...]

  // ── 7. Render the plan ──
  for (const item of plan) {
    if (item.type === 'special') {
      renderSpecialCard(item.ep, show, section, cfg);
    } else {
      const { sn, seasonData: sd, eps, rangeLabel, suffix } = item;
      const isTarget = effectiveTarget != null && sn === effectiveTarget;
      const isFirst = sn === firstSeason;
      const startOpen = isTarget || (effectiveTarget == null && isFirst);

      renderSeasonCard(sn, sd, eps, show, section, cfg, startOpen, {
        idSuffix: suffix,
        rangeLabel,
        scoreSpans: seasonScoreSpans,
        minisodes: item.minisodes || [],
      });
    }
  }

  // ── 8. Season 0 remainder ──
  if (s0Remainder.length) {
    s0Remainder.sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));
    const sd = seasonMap.get(0) || null;
    const startOpen = effectiveTarget != null && effectiveTarget === 0;
    renderSeasonCard(0, sd, s0Remainder, show, section, cfg, startOpen, {
      idSuffix: '', rangeLabel: null, scoreSpans: seasonScoreSpans,
    });
  }

  body.appendChild(section);

  // Scroll to target season if specified and exists
  if (effectiveTarget != null) {
    requestAnimationFrame(() => {
      const targetEl = document.getElementById(`sp-season-${effectiveTarget}`);
      if (targetEl) targetEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }
}

/* ── Special / film interleave card ────────────────────── */

function renderSpecialCard(ep, show, container, cfg) {
  const card = el('div', 'sp-special-card');

  // Poster
  const posterCol = el('div', 'sp-special-poster');
  const posterSrc = ep.linkedMovieShow?.posterUrl || show.posterUrl;
  if (posterSrc) {
    const img = el('img');
    img.src = posterSrc;
    img.alt = ep.title || 'Special';
    img.onerror = () => { img.remove(); posterCol.style.background = 'var(--surface-3)'; };
    posterCol.appendChild(img);
  }
  card.appendChild(posterCol);

  // Info
  const info = el('div', 'sp-special-info');

  // Top row: badge + title (linked if BONUS_MOVIE with linkedMovieShow)
  const topRow = el('div', 'sp-special-top-row');
  const kindInfo = KIND_BADGE[ep.kind] || { label: 'Special', cls: 'special' };
  topRow.appendChild(el('span', `sp-special-badge ${kindInfo.cls}`, kindInfo.label));

  const title = ep.linkedMovieShow
    ? document.createElement('a')
    : el('span', 'sp-special-title');
  if (ep.linkedMovieShow) {
    title.className = 'sp-special-title';
    title.href = `show.html?id=${ep.linkedMovieShow.id}`;
    title.textContent = ep.linkedMovieShow.displayTitle || ep.title || 'Film';
  } else {
    title.textContent = ep.title || 'TBA';
  }
  topRow.appendChild(title);
  info.appendChild(topRow);

  // Bottom row: abs number, air date, play button, watch toggle
  const bottomRow = el('div', 'sp-special-bottom-row');
  bottomRow.appendChild(el('span', 'sp-ep-abs', `#${ep.absoluteNumber}`));
  bottomRow.appendChild(el('span', 'sp-ep-airdate', fmtDate(ep.airDateUtc)));

  // mpv play button
  const mpvCell = el('div', 'sp-ep-mpv');
  const filePath = ep.filePathRadarr || ep.filePathSonarr;
  const canPlay = ep.availableLocally && filePath;
  const effectiveShape = ep.kind === 'BONUS_MOVIE' ? 'MOVIE' : show.mediaShape;
  const avail = epAvailState(ep, effectiveShape);
  const mpvIcon = el('span', `sp-mpv-icon ${canPlay ? 'available' : 'unavailable'}`);
  if (canPlay) {
    mpvIcon.innerHTML = _mpvSvg;
    mpvIcon.addEventListener('click', () => launchMpv(filePath, cfg));
  } else {
    mpvIcon.textContent = avail === 'downloading' ? '⬇' : '◷';
  }
  mpvCell.appendChild(mpvIcon);
  bottomRow.appendChild(mpvCell);

  // Download button
  const dlCell = el('div', 'sp-ep-dl');
  if (canPlay) {
    const dlIcon = el('span', 'sp-dl-icon available');
    dlIcon.innerHTML = _downloadSvg;
    dlIcon.title = 'Download';
    dlIcon.addEventListener('click', () => {
      startDownload({
        showTitle: show.displayTitle,
        label: ep.title || `Special #${ep.absoluteNumber}`,
        filePath,
      });
      dlIcon.classList.add('triggered');
      setTimeout(() => dlIcon.classList.remove('triggered'), 1200);
    });
    dlCell.appendChild(dlIcon);
  }
  bottomRow.appendChild(dlCell);

  // Watch button
  const watchCell = el('div', 'sp-ep-watch');
  const isWatched = (ep.watchEvents?.edges?.length > 0) ||
                    (Array.isArray(ep.watchEvents) && ep.watchEvents.length > 0);
  if (avail !== 'future') {
    const btn = el('button', `watch-btn${isWatched ? ' watched' : ''}`, '✓');
    btn.title = isWatched ? 'Watched' : 'Mark watched';
    btn.dataset.state = isWatched ? 'WATCHED' : 'UNWATCHED';
    btn.dataset.watchEventId = ep.watchEvents?.edges?.[0]?.node?.id || '';
    const patchedEp = { ...ep, show: { id: show.id } };
    btn.addEventListener('click', async () => {
      if (btn.classList.contains('loading')) return;
      btn.classList.add('loading');
      try {
        if (btn.dataset.state === 'WATCHED') {
          if (btn.dataset.watchEventId) {
            await deleteWatchEvent(btn.dataset.watchEventId);
            btn.dataset.watchEventId = '';
          }
          btn.dataset.state = 'UNWATCHED';
          btn.classList.remove('watched');
          btn.title = 'Mark watched';
        } else {
          const id = await addWatchEvent(show.id, ep.season, ep.episode);
          btn.dataset.state = 'WATCHED';
          btn.dataset.watchEventId = id;
          btn.classList.add('watched');
          btn.title = 'Watched';
        }
      } catch (err) {
        showBanner(`Watch error: ${err.message}`, 'error');
      } finally {
        btn.classList.remove('loading');
      }
    });
    watchCell.appendChild(btn);
  }
  bottomRow.appendChild(watchCell);

  info.appendChild(bottomRow);
  card.appendChild(info);
  container.appendChild(card);
}

/* ── Per-episode remap (inline S/E editor) ─────────────── */

function openEpRemapEditor(numBadge, ep, show, cfg) {
  // Close any existing remap editor
  document.querySelector('.sp-ep-remap-editor')?.remove();

  const editor = el('div', 'sp-ep-remap-editor');
  editor.addEventListener('click', e => e.stopPropagation());

  const sInput = document.createElement('input');
  sInput.type = 'number';
  sInput.className = 'sp-remap-input';
  sInput.min = '0';
  sInput.value = ep.season;
  sInput.placeholder = 'S';
  sInput.title = 'Season';

  const eInput = document.createElement('input');
  eInput.type = 'number';
  eInput.className = 'sp-remap-input';
  eInput.min = '1';
  eInput.value = ep.episode;
  eInput.placeholder = 'E';
  eInput.title = 'Episode';

  const saveBtn = el('button', 'sp-remap-save', '✓');
  saveBtn.title = 'Apply';

  const cancelBtn = el('button', 'sp-remap-cancel', '✕');
  cancelBtn.title = 'Cancel';

  cancelBtn.addEventListener('click', () => editor.remove());

  saveBtn.addEventListener('click', async () => {
    const newS = parseInt(sInput.value, 10);
    const newE = parseInt(eInput.value, 10);
    if (isNaN(newS) || isNaN(newE) || newE < 1 || newS < 0) {
      showBanner('Season must be ≥ 0, episode ≥ 1', 'error');
      return;
    }
    if (newS === ep.season && newE === ep.episode) {
      editor.remove();
      return;
    }

    saveBtn.disabled = true;
    try {
      // Ensure target season row exists to prevent Sonarr sync revert
      const hasSeason = show.seasons.some(s => s.seasonNumber === newS);
      if (!hasSeason) {
        const created = await setSeasonMapping(show.id, newS, null, null);
        show.seasons.push(created);
      }

      await setEpisodeNumber(ep.id, newS, newE);
      ep.season = newS;
      ep.episode = newE;

      showBanner(`Moved to S${newS}E${newE}`, 'ok');
      setTimeout(hideBanner, 2000);

      // Full re-render — episode moved between season cards
      const root = document.getElementById('show-root');
      root.innerHTML = '';
      renderBanner(show, root);
      renderHero(show, root, cfg);
      renderBody(show, root, cfg, newS);
    } catch (err) {
      showBanner(`Remap failed: ${err.message}`, 'error');
    } finally {
      saveBtn.disabled = false;
    }
  });

  // Enter to save, Escape to cancel
  const onKey = (e) => {
    if (e.key === 'Enter') saveBtn.click();
    if (e.key === 'Escape') editor.remove();
  };
  sInput.addEventListener('keydown', onKey);
  eInput.addEventListener('keydown', onKey);

  editor.appendChild(el('span', 'sp-remap-label', 'S'));
  editor.appendChild(sInput);
  editor.appendChild(el('span', 'sp-remap-label', 'E'));
  editor.appendChild(eInput);
  editor.appendChild(saveBtn);
  editor.appendChild(cancelBtn);

  // Position below the badge
  const row = numBadge.closest('.sp-ep-row');
  row.after(editor);
  sInput.focus();
  sInput.select();
}

/* ── Bulk episode remap (move range to another season) ──── */

function openBulkRemapEditor(card, sn, episodes, show, cfg) {
  // Close any existing bulk remap editor
  card.querySelector('.sp-bulk-remap-editor')?.remove();

  // Use ALL episodes for this season (not just the segment) so ranges
  // spanning split-season segments work as expected
  const seasonEps = show.episodes
    .filter(ep => ep.season === sn)
    .sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));

  const editor = el('div', 'sp-bulk-remap-editor');

  const title = el('div', 'sp-mapping-editor-title', `Move episodes → another season`);
  editor.appendChild(title);

  const fields = el('div', 'sp-mapping-editor-fields');

  // From–To episode range
  const fromGroup = el('div', 'sp-mapping-field-group');
  fromGroup.appendChild(el('label', 'sp-mapping-label', 'From ep'));
  const fromInput = document.createElement('input');
  fromInput.type = 'number';
  fromInput.className = 'sp-mapping-input';
  fromInput.min = '1';
  fromInput.value = seasonEps.length ? seasonEps[0].episode : 1;
  fromGroup.appendChild(fromInput);
  fields.appendChild(fromGroup);

  const toGroup = el('div', 'sp-mapping-field-group');
  toGroup.appendChild(el('label', 'sp-mapping-label', 'To ep'));
  const toInput = document.createElement('input');
  toInput.type = 'number';
  toInput.className = 'sp-mapping-input';
  toInput.min = '1';
  toInput.value = seasonEps.length ? seasonEps[seasonEps.length - 1].episode : 1;
  toGroup.appendChild(toInput);
  fields.appendChild(toGroup);

  // Target season
  const snGroup = el('div', 'sp-mapping-field-group');
  snGroup.appendChild(el('label', 'sp-mapping-label', 'Target season'));
  const snInput = document.createElement('input');
  snInput.type = 'number';
  snInput.className = 'sp-mapping-input';
  snInput.min = '0';
  snInput.placeholder = 'e.g. 2';
  snGroup.appendChild(snInput);
  fields.appendChild(snGroup);

  // Starting episode in target
  const startGroup = el('div', 'sp-mapping-field-group');
  startGroup.appendChild(el('label', 'sp-mapping-label', 'Start at ep'));
  const startInput = document.createElement('input');
  startInput.type = 'number';
  startInput.className = 'sp-mapping-input';
  startInput.min = '1';
  startInput.value = '1';
  startGroup.appendChild(startInput);
  fields.appendChild(startGroup);

  editor.appendChild(fields);

  // Buttons
  const btnRow = el('div', 'sp-mapping-editor-btns');
  const moveBtn = el('button', 'sp-mapping-save', 'Move');
  const cancelBtn = el('button', 'sp-mapping-cancel', 'Cancel');

  cancelBtn.addEventListener('click', () => editor.remove());

  moveBtn.addEventListener('click', async () => {
    const fromEp = parseInt(fromInput.value, 10);
    const toEp = parseInt(toInput.value, 10);
    const targetSn = parseInt(snInput.value, 10);
    const startEp = parseInt(startInput.value, 10);

    if (isNaN(fromEp) || isNaN(toEp) || isNaN(targetSn) || isNaN(startEp)) {
      showBanner('All fields are required', 'error');
      return;
    }
    if (fromEp > toEp) {
      showBanner('"From" must be ≤ "To"', 'error');
      return;
    }
    if (targetSn < 0 || startEp < 1) {
      showBanner('Target season ≥ 0, start ep ≥ 1', 'error');
      return;
    }

    // Find matching episodes across the whole season
    const toMove = seasonEps.filter(ep => ep.episode >= fromEp && ep.episode <= toEp);
    if (!toMove.length) {
      showBanner(`No episodes in range E${fromEp}–E${toEp} for season ${sn}`, 'error');
      return;
    }

    const count = toMove.length;
    if (!confirm(`Move ${count} episode${count > 1 ? 's' : ''} (E${fromEp}–E${toEp}) from season ${sn} → season ${targetSn} starting at E${startEp}?`)) {
      return;
    }

    moveBtn.disabled = true;
    try {
      // Ensure target season row exists to prevent Sonarr sync revert
      const hasSeason = show.seasons.some(s => s.seasonNumber === targetSn);
      if (!hasSeason) {
        const created = await setSeasonMapping(show.id, targetSn, null, null);
        show.seasons.push(created);
      }

      // Compute per-episode delta; sort order matters for same-season shifts
      // to avoid collisions with not-yet-moved episodes
      const delta = startEp - fromEp;
      toMove.sort((a, b) => a.episode - b.episode);
      if (targetSn === sn && delta > 0) toMove.reverse();

      showBanner(`Moving ${count} episodes…`, 'loading');
      let ok = 0, fail = 0;
      const failedEps = [];
      for (const ep of toMove) {
        const newEp = ep.episode + delta;
        try {
          await setEpisodeNumber(ep.id, targetSn, newEp);
          ep.season = targetSn;
          ep.episode = newEp;
          ok++;
        } catch (err) {
          fail++;
          failedEps.push(`S${sn}E${ep.episode}: ${err.message}`);
          console.warn(`Remap failed for S${sn}E${ep.episode}:`, err);
        }
      }

      if (fail) {
        // Don't re-render — keep the error banner visible
        showBanner(`✓ ${ok} moved, ${fail} failed: ${failedEps.join('; ')}`, 'error');
      } else {
        showBanner(`✓ ${ok} episode${ok > 1 ? 's' : ''} moved to season ${targetSn}`, 'ok');
        setTimeout(hideBanner, 3000);

        // Full re-render only on complete success
        const root = document.getElementById('show-root');
        root.innerHTML = '';
        renderBanner(show, root);
        renderHero(show, root, cfg);
        renderBody(show, root, cfg, targetSn);
      }
    } catch (err) {
      showBanner(`Bulk remap failed: ${err.message}`, 'error');
    } finally {
      moveBtn.disabled = false;
    }
  });

  btnRow.appendChild(moveBtn);
  btnRow.appendChild(cancelBtn);
  editor.appendChild(btnRow);

  // Insert after the header
  const hdr = card.querySelector('.sp-season-hdr');
  hdr.after(editor);
}

/* ── Split season editor (subdivision) ─────────────────── */

function openSplitSeasonEditor(card, sn, episodes, show, cfg) {
  card.querySelector('.sp-split-editor')?.remove();

  const seasonData = show.seasons.find(s => s.seasonNumber === sn);
  const seasonEps = show.episodes
    .filter(ep => ep.season === sn)
    .sort((a, b) => (a.episode ?? 0) - (b.episode ?? 0));

  if (seasonEps.length < 2) {
    showBanner('Need at least 2 episodes to split', 'error');
    return;
  }

  const editor = el('div', 'sp-split-editor');
  editor.appendChild(el('div', 'sp-mapping-editor-title', `Split season ${sn}`));

  const fields = el('div', 'sp-mapping-editor-fields');

  // After-episode input
  const afterGroup = el('div', 'sp-mapping-field-group');
  afterGroup.appendChild(el('label', 'sp-mapping-label', 'After ep'));
  const afterInput = document.createElement('input');
  afterInput.type = 'number';
  afterInput.className = 'sp-mapping-input';
  afterInput.min = String(seasonEps[0].episode);
  afterInput.max = String(seasonEps[seasonEps.length - 1].episode - 1);
  afterInput.placeholder = `${seasonEps[0].episode}–${seasonEps[seasonEps.length - 1].episode - 1}`;
  afterGroup.appendChild(afterInput);
  fields.appendChild(afterGroup);

  // New AniList ID
  const alGroup = el('div', 'sp-mapping-field-group');
  alGroup.appendChild(el('label', 'sp-mapping-label', 'New AL id'));
  const alInput = document.createElement('input');
  alInput.type = 'number';
  alInput.className = 'sp-mapping-input';
  alInput.placeholder = 'AniList';
  alGroup.appendChild(alInput);
  fields.appendChild(alGroup);

  // New MAL ID
  const malGroup = el('div', 'sp-mapping-field-group');
  malGroup.appendChild(el('label', 'sp-mapping-label', 'New MAL id'));
  const malInput = document.createElement('input');
  malInput.type = 'number';
  malInput.className = 'sp-mapping-input';
  malInput.placeholder = 'MAL';
  malGroup.appendChild(malInput);
  fields.appendChild(malGroup);

  editor.appendChild(fields);

  // Preview area
  const preview = el('div', 'sp-split-preview');
  preview.style.cssText = 'font-size:.85em;color:var(--muted);padding:.3rem .5rem;white-space:pre-line';

  function updatePreview() {
    const after = parseInt(afterInput.value, 10);
    if (isNaN(after)) { preview.textContent = ''; return; }
    const lower = seasonEps.filter(e => e.episode <= after);
    const upper = seasonEps.filter(e => e.episode > after);
    if (!lower.length || !upper.length) { preview.textContent = 'Split must leave episodes in both halves'; return; }

    const absS = seasonData?.absStart, absE = seasonData?.absEnd;
    const lines = [];
    if (absS != null && absE != null) {
      const lEnd = absS + lower.length - 1;
      const uStart = lEnd + 1;
      lines.push(`S${sn} keeps E1–E${after} (abs ${absS}–${lEnd}${seasonData.anilistId ? ', AL ' + seasonData.anilistId : ''})`);
      lines.push(`New S${sn + 1}: E${after + 1}–E${seasonEps[seasonEps.length - 1].episode} → E1–E${upper.length} (abs ${uStart}–${absE}${alInput.value ? ', AL ' + alInput.value : ''})`);
    } else {
      lines.push(`S${sn} keeps E1–E${after} (${lower.length} eps)`);
      lines.push(`New S${sn + 1}: ${upper.length} eps (renumbered from E1)`);
    }

    // Show shifted seasons
    const shifted = show.seasons.filter(s => s.seasonNumber > sn);
    if (shifted.length) {
      lines.push(`${shifted.length} season${shifted.length > 1 ? 's' : ''} shifted: ${shifted.map(s => `S${s.seasonNumber} → S${s.seasonNumber + 1}`).join(', ')}`);
    }
    preview.textContent = lines.join('\n');
  }
  afterInput.addEventListener('input', updatePreview);
  alInput.addEventListener('input', updatePreview);
  editor.appendChild(preview);

  // Buttons
  const btnRow = el('div', 'sp-mapping-editor-btns');
  const splitBtn = el('button', 'sp-mapping-save', 'Split');
  const cancelBtn = el('button', 'sp-mapping-cancel', 'Cancel');

  cancelBtn.addEventListener('click', () => editor.remove());

  splitBtn.addEventListener('click', async () => {
    const after = parseInt(afterInput.value, 10);
    const newAlId = alInput.value ? parseInt(alInput.value, 10) : null;
    const newMalId = malInput.value ? parseInt(malInput.value, 10) : null;

    if (isNaN(after)) {
      showBanner('"After ep" is required', 'error');
      return;
    }

    const lower = seasonEps.filter(e => e.episode <= after);
    const upper = seasonEps.filter(e => e.episode > after);
    if (!lower.length || !upper.length) {
      showBanner('Split must leave episodes in both halves', 'error');
      return;
    }

    if (!confirm(`Split season ${sn} after episode ${after}?\n${lower.length} eps stay, ${upper.length} eps move to new S${sn + 1}.`)) {
      return;
    }

    splitBtn.disabled = true;
    showBanner('Splitting…', 'loading');
    try {
      const result = await splitSeason(show.id, sn, after, newAlId, newMalId);
      showBanner(
        `✓ Split complete — S${result.lower.seasonNumber} (${result.lower.absStart ?? '?'}–${result.lower.absEnd ?? '?'}) + S${result.upper.seasonNumber} (${result.upper.absStart ?? '?'}–${result.upper.absEnd ?? '?'})` +
        (result.seasonsShifted ? `, ${result.seasonsShifted} season${result.seasonsShifted > 1 ? 's' : ''} shifted` : ''),
        'ok'
      );
      // Full reload to pick up all renumbered seasons + episodes
      setTimeout(() => location.reload(), 1500);
    } catch (err) {
      showBanner(`Split failed: ${err.message}`, 'error');
    } finally {
      splitBtn.disabled = false;
    }
  });

  btnRow.appendChild(splitBtn);
  btnRow.appendChild(cancelBtn);
  editor.appendChild(btnRow);

  const hdr = card.querySelector('.sp-season-hdr');
  hdr.after(editor);
}

function renderSeasonCard(sn, seasonData, episodes, show, container, cfg, startOpen, opts = {}) {
  const { idSuffix = '', rangeLabel = null, scoreSpans = null, minisodes = [] } = opts;
  const card = el('div', `sp-season-card${startOpen ? ' open' : ''}`);
  // First segment gets the canonical ID (for scroll-to-target)
  const cardId = `sp-season-${sn}${idSuffix}`;
  card.id = cardId;

  // Header
  const hdr = el('div', 'sp-season-hdr');

  const label = sn === 0 ? 'Specials' : `Season ${sn}`;
  hdr.appendChild(el('span', 'sp-season-num', label));

  // Range label for split seasons, or unmapped indicator
  if (rangeLabel) {
    hdr.appendChild(el('span', 'sp-season-range-label', rangeLabel));
  } else if (!seasonData && sn !== 0) {
    hdr.appendChild(el('span', 'sp-season-range-label', '(unmapped)'));
  }

  const meta = el('div', 'sp-season-meta');

  if (seasonData) {
    // Score (click-to-edit) — track span for cross-segment updates
    const seasonScoreWrap = el('span', 'sp-season-score sp-season-score-editable');
    seasonScoreWrap.title = 'Click to edit season score';
    seasonScoreWrap.appendChild(el('span', 'sp-season-score-label', 'score: '));
    const seasonScoreVal = el('span', 'sp-season-score-value',
      seasonData.score != null ? String(seasonData.score) : '—');
    seasonScoreWrap.appendChild(seasonScoreVal);

    // Register this span for cross-segment score sync
    if (scoreSpans) {
      if (!scoreSpans.has(sn)) scoreSpans.set(sn, []);
      scoreSpans.get(sn).push(seasonScoreVal);
    }

    attachScoreEditor(seasonScoreWrap, seasonScoreVal, () => seasonData.score, async (rounded) => {
      await setSeasonScore(seasonData.id, rounded);
      seasonData.score = rounded;
      // Update all sibling score spans (split season segments)
      if (scoreSpans?.has(sn)) {
        for (const span of scoreSpans.get(sn)) {
          span.textContent = String(rounded);
        }
      }
    });
    meta.appendChild(seasonScoreWrap);

    // Dates
    const dates = [];
    if (seasonData.startedAt) dates.push(`started ${fmtDate(seasonData.startedAt)}`);
    if (seasonData.completedAt) dates.push(`completed ${fmtDate(seasonData.completedAt)}`);
    if (dates.length) {
      meta.appendChild(el('span', 'sp-season-dates', dates.join(' · ')));
    }

    // Abs range
    if (seasonData.absStart != null || seasonData.absEnd != null) {
      const rangeText = `abs ${seasonData.absStart ?? '?'}–${seasonData.absEnd ?? '?'}`;
      meta.appendChild(el('span', 'sp-season-abs-range', rangeText));
    }

    // AniList / MAL IDs (clickable, with service icons)
    if (seasonData.anilistId) {
      const alLink = document.createElement('a');
      alLink.className = 'sp-season-al-id sp-season-ext-link';
      alLink.href = `https://anilist.co/anime/${seasonData.anilistId}`;
      alLink.target = '_blank';
      alLink.rel = 'noopener noreferrer';
      const alIcon = el('span', 'sp-season-ext-icon');
      alIcon.innerHTML = SVC_ICONS.anilist;
      alLink.appendChild(alIcon);
      alLink.appendChild(document.createTextNode(seasonData.anilistId));
      alLink.addEventListener('click', e => e.stopPropagation());
      meta.appendChild(alLink);
    }
    if (seasonData.malId) {
      const malLink = document.createElement('a');
      malLink.className = 'sp-season-mal-id sp-season-ext-link';
      malLink.href = `https://myanimelist.net/anime/${seasonData.malId}`;
      malLink.target = '_blank';
      malLink.rel = 'noopener noreferrer';
      const malIcon = el('span', 'sp-season-ext-icon');
      malIcon.innerHTML = SVC_ICONS.mal;
      malLink.appendChild(malIcon);
      malLink.appendChild(document.createTextNode(seasonData.malId));
      malLink.addEventListener('click', e => e.stopPropagation());
      meta.appendChild(malLink);
    }

  }

  // Edit mapping button — works for mapped AND unmapped seasons
  if (sn !== 0) {
    const editBtn = el('button', 'sp-season-edit-btn', '✎');
    editBtn.title = 'Edit season mapping';
    editBtn.addEventListener('click', e => {
      e.stopPropagation();
      openSeasonMappingEditor(card, sn, seasonData, show);
    });
    meta.appendChild(editBtn);

    // Bulk remap button
    const remapBtn = el('button', 'sp-season-edit-btn sp-remap-btn', '⇄');
    remapBtn.title = 'Move episodes to another season';
    remapBtn.addEventListener('click', e => {
      e.stopPropagation();
      openBulkRemapEditor(card, sn, episodes, show, cfg);
    });
    meta.appendChild(remapBtn);

    // Split season button
    const splitBtn = el('button', 'sp-season-edit-btn sp-split-btn', '✂');
    splitBtn.title = 'Split season (subdivision)';
    splitBtn.addEventListener('click', e => {
      e.stopPropagation();
      openSplitSeasonEditor(card, sn, episodes, show, cfg);
    });
    meta.appendChild(splitBtn);
  }

  // Reconcile button — only for mapped seasons
  if (seasonData && sn !== 0) {
    const reconBtn = el('button', 'sp-season-edit-btn sp-reconcile-btn', '⟲');
    reconBtn.title = 'Reconcile with Fribb';
    reconBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      reconBtn.disabled = true;
      // Insert result area after the header
      let resultArea = card.querySelector('.sp-reconcile-result');
      if (!resultArea) {
        resultArea = el('div', 'sp-reconcile-result');
        resultArea.hidden = true;
        hdr.after(resultArea);
      }
      try {
        const reconciled = await reconcileSeasonMapping(show.id, sn);
        resultArea.hidden = false;
        const source = reconciled.source;
        const fribbAl = reconciled.anilistId;
        const currentAl = seasonData.anilistId;

        if (source === 'FRIBB' && fribbAl && currentAl && fribbAl === currentAl) {
          resultArea.className = 'sp-reconcile-result ok';
          resultArea.textContent = `✅ Fribb confirms AniList ID ${fribbAl}`;
        } else if (source === 'FRIBB' && fribbAl && currentAl && fribbAl !== currentAl) {
          resultArea.className = 'sp-reconcile-result warn';
          resultArea.innerHTML = `⚠️ Fribb suggests AniList ID ${fribbAl} (current: ${currentAl})`;

          const actions = el('div', 'sp-reconcile-actions');

          const keepBtn = el('button', 'sp-mapping-save', 'Keep mine');
          keepBtn.addEventListener('click', async () => {
            keepBtn.disabled = true;
            try {
              await setSeasonMapping(show.id, sn, currentAl, null);
              resultArea.className = 'sp-reconcile-result ok';
              resultArea.textContent = `✅ Keeping AniList ID ${currentAl} (manual override)`;
              actions.remove();
            } catch (err) {
              showBanner(`Failed: ${err.message}`, 'error');
              keepBtn.disabled = false;
            }
          });

          const useFribbBtn = el('button', 'sp-mapping-save', "Use Fribb's");
          useFribbBtn.addEventListener('click', async () => {
            useFribbBtn.disabled = true;
            try {
              await setSeasonMapping(show.id, sn, fribbAl, reconciled.malId);
              seasonData.anilistId = fribbAl;
              if (reconciled.malId) seasonData.malId = reconciled.malId;
              resultArea.className = 'sp-reconcile-result ok';
              resultArea.textContent = `✅ Updated to Fribb's AniList ID ${fribbAl}`;
              actions.remove();
              // Re-render to show updated IDs
              const root = document.getElementById('show-root');
              root.innerHTML = '';
              renderBanner(show, root);
              renderHero(show, root, cfg);
              renderBody(show, root, cfg, sn);
            } catch (err) {
              showBanner(`Failed: ${err.message}`, 'error');
              useFribbBtn.disabled = false;
            }
          });

          actions.append(keepBtn, useFribbBtn);
          resultArea.appendChild(actions);
        } else if (source === 'FRIBB' && fribbAl && !currentAl) {
          resultArea.className = 'sp-reconcile-result ok';
          resultArea.textContent = `✅ Fribb mapped to AniList ID ${fribbAl}`;
          seasonData.anilistId = fribbAl;
        } else {
          resultArea.className = 'sp-reconcile-result info';
          resultArea.textContent = 'ℹ️ Fribb has no data for this season yet';
        }
      } catch (err) {
        resultArea.hidden = false;
        resultArea.className = 'sp-reconcile-result warn';
        resultArea.textContent = `⚠️ Reconcile failed: ${err.message}`;
      } finally {
        reconBtn.disabled = false;
      }
    });
    meta.appendChild(reconBtn);
  }

  hdr.appendChild(meta);

  // Per-season status chips (only on first segment for split seasons)
  if (seasonData && idSuffix === '') {
    const statusChips = el('div', 'sp-season-status-chips');
    statusChips.addEventListener('click', e => e.stopPropagation());
    const initStatus = seasonData.status || show.status || 'PLANNED';
    for (const [val, chipLabel] of Object.entries(STATUS_LABELS)) {
      const chip = el('button', `sp-season-chip${val === initStatus ? ' active' : ''}`,
        chipLabel);
      chip.dataset.status = val;
      chip.addEventListener('click', async () => {
        // Read live value — seasonData.status is mutated on each click
        const effectiveNow = seasonData.status || show.status || 'PLANNED';
        const newStatus = val === effectiveNow ? null : val;
        try {
          chip.disabled = true;
          try {
            await setSeasonStatus(seasonData.id, newStatus);
          } catch (err) {
            // Completion guard — prompt for confirmation
            if (newStatus === 'COMPLETED' && err.message?.includes('confirmed: true')) {
              if (!confirm(`Season ${sn} still has unaired episodes. Mark completed anyway?`)) {
                return;
              }
              await setSeasonStatus(seasonData.id, newStatus, true);
            } else {
              throw err;
            }
          }
          seasonData.status = newStatus;
          // Update all chips in this set
          for (const c of statusChips.querySelectorAll('.sp-season-chip')) {
            const effective = newStatus || show.status || 'PLANNED';
            c.classList.toggle('active', c.dataset.status === effective);
          }
          const bannerText = newStatus
            ? `Season ${sn} → ${STATUS_LABELS[newStatus]}`
            : `Season ${sn} → inherited (${STATUS_LABELS[show.status] || show.status})`;
          showBanner(bannerText, 'info');
          setTimeout(hideBanner, 2000);
        } catch (err) {
          showBanner(`Status error: ${err.message}`, 'error');
        } finally {
          chip.disabled = false;
        }
      });
      statusChips.appendChild(chip);
    }
    hdr.appendChild(statusChips);
  }

  // Minisode toggle (when season has interwoven minisodes)
  if (minisodes.length > 0) {
    const miniToggle = el('button', 'sp-mini-toggle', `${minisodes.length} mini`);
    miniToggle.title = 'Toggle minisodes';
    miniToggle.addEventListener('click', (e) => {
      e.stopPropagation();
      miniToggle.classList.toggle('hiding');
      const hide = miniToggle.classList.contains('hiding');
      miniToggle.textContent = hide
        ? `▸ ${minisodes.length} mini`
        : `${minisodes.length} mini`;
      const miniRows = card.querySelectorAll('.sp-ep-row.minisode, .sp-ep-synopsis.minisode');
      for (const row of miniRows) row.hidden = hide;
    });
    hdr.appendChild(miniToggle);
  }

  // Episode count
  const watchedCount = countWatched(episodes);
  const epCountLabel = el('span', 'sp-season-ep-count',
    `${watchedCount}/${episodes.length} eps`);
  hdr.appendChild(epCountLabel);

  // "Mark season watched" button — marks all aired+unwatched episodes
  if (sn !== 0 && episodes.length > 0) {
    const markAllBtn = el('button', 'sp-mark-season-btn', '✓ all');
    markAllBtn.title = 'Mark all aired episodes as watched';
    markAllBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      markAllBtn.disabled = true;

      // Determine which episodes to mark
      const unwatched = episodes.filter(ep => {
        const isWatched = (ep.watchEvents?.edges?.length > 0) ||
                          (Array.isArray(ep.watchEvents) && ep.watchEvents.length > 0);
        return !isWatched;
      });
      const aired = unwatched.filter(ep => {
        const avail = epAvailState(ep, show.mediaShape);
        return avail !== 'future';
      });
      const unaired = unwatched.filter(ep => {
        const avail = epAvailState(ep, show.mediaShape);
        return avail === 'future';
      });

      let toMark = aired;
      if (aired.length === 0 && unaired.length > 0) {
        // All aired episodes already watched — offer to mark unaired too
        if (!confirm(`All aired episodes are watched. Mark ${unaired.length} unaired episode${unaired.length > 1 ? 's' : ''} as watched too?`)) {
          markAllBtn.disabled = false;
          return;
        }
        toMark = unaired;
      } else if (aired.length === 0) {
        showBanner('All episodes already watched', 'info');
        setTimeout(hideBanner, 2000);
        markAllBtn.disabled = false;
        return;
      }

      showBanner(`Marking ${toMark.length} episode${toMark.length > 1 ? 's' : ''} as watched…`, 'loading');
      let ok = 0, fail = 0;
      for (const ep of toMark) {
        try {
          const wid = await addWatchEvent(show.id, ep.season, ep.episode);
          // Update the episode's local data so UI reflects it
          if (!ep.watchEvents) ep.watchEvents = { edges: [] };
          if (ep.watchEvents.edges) ep.watchEvents.edges.push({ node: { id: wid } });
          else ep.watchEvents.push({ id: wid });
          ok++;
          // Update the watch button in the DOM
          const epRow = card.querySelector(`.sp-ep-row [data-episode-id="${ep.id}"]`);
          if (epRow) {
            epRow.classList.add('watched');
            epRow.dataset.state = 'WATCHED';
            epRow.dataset.watchEventId = wid;
            epRow.title = 'Watched — click to unmark';
          }
        } catch (err) {
          fail++;
          console.warn(`Watch failed for S${ep.season}E${ep.episode}:`, err);
        }
      }

      // Update all watch buttons inside the season card
      const allWatchBtns = card.querySelectorAll('.sp-ep-watch .watch-btn');
      for (const btn of allWatchBtns) {
        // Find the episode this button belongs to by walking up to the row
        const row = btn.closest('.sp-ep-row');
        if (!row) continue;
        const epBadge = row.querySelector('.sp-ep-num')?.textContent;
        if (!epBadge) continue;
        // Check if this episode is now watched
        const ep = toMark.find(e => fmtEpBadge(e) === epBadge);
        if (ep) {
          const wid = ep.watchEvents?.edges?.[0]?.node?.id || '';
          btn.classList.add('watched');
          btn.dataset.state = 'WATCHED';
          btn.dataset.watchEventId = wid;
          btn.title = 'Watched — click to unmark';
        }
      }

      // Update ep count label
      const newWatched = countWatched(episodes);
      epCountLabel.textContent = `${newWatched}/${episodes.length} eps`;

      showBanner(`✓ ${ok} marked watched${fail ? `, ${fail} failed` : ''}`, fail ? 'error' : 'ok');
      setTimeout(hideBanner, 3000);
      markAllBtn.disabled = false;
    });
    hdr.appendChild(markAllBtn);
  }

  hdr.appendChild(el('span', 'sp-season-toggle', '▾'));

  // Toggle handler
  hdr.addEventListener('click', () => {
    card.classList.toggle('open');
    const bodyEl = card.querySelector('.sp-season-body');
    if (bodyEl) bodyEl.hidden = !bodyEl.hidden;
  });

  card.appendChild(hdr);

  // Body: poster + episode list
  const bodyEl = el('div', 'sp-season-body');
  if (!startOpen) bodyEl.hidden = true;

  // Season poster: clickable for art picker
  const posterCol = el('div', 'sp-season-poster sp-poster-clickable');
  const posterUrl = seasonData?.posterUrl || show.posterUrl;
  let snImg = null;
  if (posterUrl) {
    snImg = el('img');
    snImg.src = posterUrl;
    snImg.alt = `${label} cover`;
    snImg.onerror = () => {
      snImg.remove();
      posterCol.appendChild(el('span', 'sp-season-poster-empty', 'No\ncover'));
    };
    posterCol.appendChild(snImg);
  } else {
    posterCol.appendChild(el('span', 'sp-season-poster-empty', 'No\ncover'));
  }
  if (seasonData?.id) {
    const snEditHint = el('span', 'sp-poster-edit-hint sp-poster-edit-hint-sm', '🖼');
    posterCol.appendChild(snEditHint);
    posterCol.addEventListener('click', (e) => {
      e.stopPropagation();
      openArtPicker(show, seasonData.id, 'poster', snImg, () => {});
    });
  }
  bodyEl.appendChild(posterCol);

  // Episode list — merge minisodes inline, sorted by absoluteNumber
  const epList = el('div', 'sp-ep-list');
  const sdAbsStart = seasonData?.absStart;
  function epAbsForSort(ep) {
    if (ep.absoluteNumber != null) return ep.absoluteNumber;
    if (sdAbsStart != null) return sdAbsStart + ((ep.episode ?? 1) - 1);
    return ep.episode ?? 0;
  }
  const mergedEps = [...episodes, ...minisodes];
  mergedEps.sort((a, b) => epAbsForSort(a) - epAbsForSort(b));

  for (const ep of mergedEps) {
    const isMini = minisodes.includes(ep);
    const row = el('div', `sp-ep-row${isMini ? ' minisode' : ''}`);

    const avail = epAvailState(ep, show.mediaShape);
    if (avail === 'future') row.classList.add('future');

    // Episode number badge (click to remap)
    const numBadge = el('span', 'sp-ep-num sp-ep-num-editable', fmtEpBadge(ep));
    numBadge.title = 'Click to reassign S/E number';
    numBadge.addEventListener('click', (e) => {
      e.stopPropagation();
      openEpRemapEditor(numBadge, ep, show, cfg);
    });
    row.appendChild(numBadge);

    // Absolute number
    row.appendChild(el('span', 'sp-ep-abs',
      ep.absoluteNumber != null ? `#${ep.absoluteNumber}` : '—'));

    // Title cell (with kind badge for non-REGULAR episodes, "Mini" for minisodes)
    const titleCell = el('div', 'sp-ep-title-cell');
    if (isMini) {
      titleCell.appendChild(el('span', 'sp-kind-badge mini', 'Mini'));
    } else {
      const kindInfo = KIND_BADGE[ep.kind];
      if (kindInfo) {
        titleCell.appendChild(el('span', `sp-kind-badge ${kindInfo.cls}`, kindInfo.label));
      }
    }
    titleCell.appendChild(el('span', 'sp-ep-title', ep.title || 'TBA'));
    // Synopsis toggle — always show if episode has one, or offer edit
    const synBtn = el('button', 'sp-syn-toggle', ep.synopsis ? 'ℹ' : '✎');
    synBtn.title = ep.synopsis ? 'Toggle synopsis' : 'Add synopsis';
    if (!ep.synopsis) synBtn.classList.add('add-mode');
    titleCell.appendChild(synBtn);
    row.appendChild(titleCell);

    // Air date
    row.appendChild(el('span', 'sp-ep-airdate', fmtDate(ep.airDateUtc)));

    // mpv play button
    const mpvCell = el('div', 'sp-ep-mpv');
    const epFilePath = show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;
    const canPlay = ep.availableLocally && epFilePath;
    const mpvIcon = el('span', `sp-mpv-icon ${canPlay ? 'available' : 'unavailable'}`);
    if (canPlay) {
      mpvIcon.innerHTML = _mpvSvg;
      mpvIcon.addEventListener('click', () => launchMpv(epFilePath, cfg));
    } else {
      mpvIcon.textContent = avail === 'downloading' ? '⬇' : '◷';
    }
    mpvCell.appendChild(mpvIcon);
    row.appendChild(mpvCell);

    // Download button — triggers browser-native download
    const dlCell = el('div', 'sp-ep-dl');
    if (canPlay) {
      const dlIcon = el('span', 'sp-dl-icon available');
      dlIcon.innerHTML = _downloadSvg;
      dlIcon.title = 'Download episode';
      dlIcon.addEventListener('click', () => {
        const epLabel = show.mediaShape === 'MOVIE'
          ? show.displayTitle
          : `S${ep.season} E${ep.episode}${ep.title ? ' — ' + ep.title : ''}`;
        startDownload({
          showTitle: show.displayTitle,
          label: epLabel,
          filePath: epFilePath,
        });
        // Brief visual feedback
        dlIcon.classList.add('triggered');
        setTimeout(() => dlIcon.classList.remove('triggered'), 1200);
      });
      dlCell.appendChild(dlIcon);
    } else {
      dlCell.appendChild(el('span', 'sp-dl-icon unavailable'));
    }
    row.appendChild(dlCell);

    // Watch / unwatch button
    const watchCell = el('div', 'sp-ep-watch');
    const isWatched = (ep.watchEvents?.edges?.length > 0) ||
                      (Array.isArray(ep.watchEvents) && ep.watchEvents.length > 0);
    {
      const isFuture = avail === 'future';
      const btn = el('button',
        `watch-btn${isWatched ? ' watched' : ''}${isFuture ? ' future' : ''}`, '✓');
      btn.title = isWatched ? 'Watched — click to unmark'
        : isFuture ? 'Unaired — click to override' : 'Mark watched';
      btn.dataset.state = isWatched ? 'WATCHED' : 'UNWATCHED';
      btn.dataset.watchEventId = ep.watchEvents?.edges?.[0]?.node?.id || '';
      btn.dataset.episodeId = ep.id;
      // Patch episode with show back-reference for onWatchToggle
      const patchedEp = { ...ep, show: { id: show.id } };
      btn.addEventListener('click', async () => {
        if (btn.classList.contains('loading')) return;
        // Confirmation for unaired episodes
        if (isFuture && btn.dataset.state !== 'WATCHED') {
          if (!confirm(`S${ep.season}E${ep.episode} hasn't aired yet. Mark as watched anyway?`)) return;
        }
        btn.classList.add('loading');
        try {
          if (btn.dataset.state === 'WATCHED') {
            if (btn.dataset.watchEventId) {
              await deleteWatchEvent(btn.dataset.watchEventId);
              btn.classList.remove('watched');
              btn.dataset.state = 'UNWATCHED';
              btn.title = isFuture ? 'Unaired — click to override' : 'Mark watched';
              btn.dataset.watchEventId = '';
            } else {
              throw new Error('No watch event id — try refreshing the page');
            }
          } else {
            const wid = await addWatchEvent(patchedEp.show.id, patchedEp.season, patchedEp.episode);
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
      });
      watchCell.appendChild(btn);
    }
    row.appendChild(watchCell);

    epList.appendChild(row);

    // Synopsis row (hidden by default) — always created for edit capability
    {
      const synRow = el('div', `sp-ep-synopsis${isMini ? ' minisode' : ''}`);
      synRow.hidden = true;

      // Text display
      const synText = el('span', 'sp-ep-syn-text', ep.synopsis || '');

      // Edit button
      const editBtn = el('button', 'sp-syn-edit-btn', '✎');
      editBtn.title = 'Edit synopsis';
      editBtn.addEventListener('click', () => {
        openEpisodeSynopsisEditor(synRow, synText, ep, show);
      });

      // Sources button (for episode)
      const epSrcBtn = el('button', 'sp-syn-src-btn', '⟳');
      epSrcBtn.title = 'Fetch from sources';
      epSrcBtn.addEventListener('click', () => {
        openSynopsisSourcesModal(show, ep.id, (text) => {
          ep.synopsis = text;
          synText.textContent = text;
          synBtn.textContent = 'ℹ';
          synBtn.classList.remove('add-mode');
        });
      });

      synRow.appendChild(synText);
      synRow.appendChild(editBtn);
      synRow.appendChild(epSrcBtn);
      epList.appendChild(synRow);

      // Wire toggle
      const synBtnRef = row.querySelector('.sp-syn-toggle');
      if (synBtnRef) {
        synBtnRef.addEventListener('click', () => {
          synRow.hidden = !synRow.hidden;
          synBtnRef.classList.toggle('active', !synRow.hidden);
        });
      }
    }
  }

  bodyEl.appendChild(epList);
  card.appendChild(bodyEl);
  container.appendChild(card);
}

/* ── Season mapping editor ──────────────────────────────── */

function openSeasonMappingEditor(card, seasonNumber, seasonData, show) {
  // Remove any existing editor in this card
  card.querySelector('.sp-mapping-editor')?.remove();

  const editor = el('div', 'sp-mapping-editor');

  const title = el('div', 'sp-mapping-editor-title', `Season ${seasonNumber} mapping`);
  editor.appendChild(title);

  const fields = el('div', 'sp-mapping-editor-fields');

  // AniList ID
  const alGroup = el('div', 'sp-mapping-field-group');
  alGroup.appendChild(el('label', 'sp-mapping-label', 'AniList ID'));
  const alInput = document.createElement('input');
  alInput.type = 'number';
  alInput.className = 'sp-mapping-input';
  alInput.placeholder = 'none';
  alInput.value = seasonData?.anilistId ?? '';
  alGroup.appendChild(alInput);
  fields.appendChild(alGroup);

  // MAL ID
  const malGroup = el('div', 'sp-mapping-field-group');
  malGroup.appendChild(el('label', 'sp-mapping-label', 'MAL ID'));
  const malInput = document.createElement('input');
  malInput.type = 'number';
  malInput.className = 'sp-mapping-input';
  malInput.placeholder = 'none';
  malInput.value = seasonData?.malId ?? '';
  malGroup.appendChild(malInput);
  fields.appendChild(malGroup);

  editor.appendChild(fields);

  // Buttons
  const btnRow = el('div', 'sp-mapping-editor-btns');
  const saveBtn = el('button', 'sp-mapping-save', 'Save');
  const cancelBtn = el('button', 'sp-mapping-cancel', 'Cancel');

  cancelBtn.addEventListener('click', () => editor.remove());

  saveBtn.addEventListener('click', async () => {
    // Parse: empty → null, filled → int
    const alRaw = alInput.value.trim();
    const malRaw = malInput.value.trim();
    const anilistId = alRaw === '' ? null : parseInt(alRaw, 10);
    const malId = malRaw === '' ? null : parseInt(malRaw, 10);

    if ((alRaw !== '' && isNaN(anilistId)) || (malRaw !== '' && isNaN(malId))) {
      showBanner('IDs must be numeric', 'error');
      return;
    }

    saveBtn.disabled = true;
    try {
      const result = await setSeasonMapping(show.id, seasonNumber, anilistId, malId);
      showBanner(`Season ${seasonNumber} mapping updated`, 'info');
      setTimeout(hideBanner, 2000);

      // Update in-memory season data
      const idx = show.seasons.findIndex(s => s.seasonNumber === seasonNumber);
      if (idx >= 0) {
        Object.assign(show.seasons[idx], result);
      } else {
        show.seasons.push(result);
      }

      // Re-render the season header inline
      const hdr = card.querySelector('.sp-season-hdr');
      const meta = hdr.querySelector('.sp-season-meta');

      // Update or add AL/MAL links
      meta.querySelector('.sp-season-al-id')?.remove();
      meta.querySelector('.sp-season-mal-id')?.remove();

      // Find the edit button to insert links before it
      const editBtnEl = meta.querySelector('.sp-season-edit-btn');

      if (result.anilistId) {
        const alLink = document.createElement('a');
        alLink.className = 'sp-season-al-id sp-season-ext-link';
        alLink.href = `https://anilist.co/anime/${result.anilistId}`;
        alLink.target = '_blank';
        alLink.rel = 'noopener noreferrer';
        const alIcon = el('span', 'sp-season-ext-icon');
        alIcon.innerHTML = SVC_ICONS.anilist;
        alLink.appendChild(alIcon);
        alLink.appendChild(document.createTextNode(result.anilistId));
        alLink.addEventListener('click', e => e.stopPropagation());
        meta.insertBefore(alLink, editBtnEl);
      }
      if (result.malId) {
        const malLink = document.createElement('a');
        malLink.className = 'sp-season-mal-id sp-season-ext-link';
        malLink.href = `https://myanimelist.net/anime/${result.malId}`;
        malLink.target = '_blank';
        malLink.rel = 'noopener noreferrer';
        const malIcon = el('span', 'sp-season-ext-icon');
        malIcon.innerHTML = SVC_ICONS.mal;
        malLink.appendChild(malIcon);
        malLink.appendChild(document.createTextNode(result.malId));
        malLink.addEventListener('click', e => e.stopPropagation());
        meta.insertBefore(malLink, editBtnEl);
      }

      // Remove unmapped indicator if it existed
      const unmapped = hdr.querySelector('.sp-season-range-label');
      if (unmapped && unmapped.textContent === '(unmapped)') unmapped.remove();

      editor.remove();
    } catch (err) {
      showBanner(`Mapping error: ${err.message}`, 'error');
      saveBtn.disabled = false;
    }
  });

  btnRow.appendChild(saveBtn);
  btnRow.appendChild(cancelBtn);
  editor.appendChild(btnRow);

  // Insert after header, before body
  const bodyEl = card.querySelector('.sp-season-body');
  card.insertBefore(editor, bodyEl);
}

/* ── Add Season form (show page) ───────────────────────── */

function openAddSeasonForm(section, show, cfg, targetSeason) {
  // Remove any existing form
  section.querySelector('.sp-add-season-form')?.remove();

  const existing = (show.seasons || []).filter(s => s.seasonNumber > 0);
  const highestSeason = existing.length ? Math.max(...existing.map(s => s.seasonNumber)) : 0;
  const nextSeason = highestSeason + 1;

  const form = el('div', 'sp-add-season-form');

  const title = el('div', 'sp-mapping-editor-title', `Add Season ${nextSeason}`);
  form.appendChild(title);

  const fields = el('div', 'sp-mapping-editor-fields');

  // Season number
  const snGroup = el('div', 'sp-mapping-field-group');
  snGroup.appendChild(el('label', 'sp-mapping-label', 'Season #'));
  const snInput = document.createElement('input');
  snInput.type = 'number';
  snInput.className = 'sp-mapping-input';
  snInput.value = nextSeason;
  snInput.min = '1';
  snGroup.appendChild(snInput);
  fields.appendChild(snGroup);

  // AniList ID
  const alGroup = el('div', 'sp-mapping-field-group');
  alGroup.appendChild(el('label', 'sp-mapping-label', 'AniList ID'));
  const alInput = document.createElement('input');
  alInput.type = 'number';
  alInput.className = 'sp-mapping-input';
  alInput.placeholder = 'optional';
  alGroup.appendChild(alInput);
  fields.appendChild(alGroup);

  // MAL ID
  const malGroup = el('div', 'sp-mapping-field-group');
  malGroup.appendChild(el('label', 'sp-mapping-label', 'MAL ID'));
  const malInput = document.createElement('input');
  malInput.type = 'number';
  malInput.className = 'sp-mapping-input';
  malInput.placeholder = 'optional';
  malGroup.appendChild(malInput);
  fields.appendChild(malGroup);

  form.appendChild(fields);

  // Status chips
  const STATUS_CHOICES = ['PLANNED', 'WATCHING', 'PAUSED', 'COMPLETED', 'DROPPED'];
  const chipsRow = el('div', 'sp-season-status-chips');
  chipsRow.style.marginBottom = '6px';
  let selectedStatus = 'PLANNED';
  for (const st of STATUS_CHOICES) {
    const chip = el('button', `sp-season-chip${st === 'PLANNED' ? ' active' : ''}`,
      STATUS_LABELS[st]);
    chip.dataset.status = st;
    chip.addEventListener('click', () => {
      selectedStatus = st;
      chipsRow.querySelectorAll('.sp-season-chip').forEach(c => c.classList.remove('active'));
      chip.classList.add('active');
    });
    chipsRow.appendChild(chip);
  }
  form.appendChild(chipsRow);

  // Result area for reconciliation / gap errors
  const resultArea = el('div', 'sp-add-season-result');
  resultArea.hidden = true;
  form.appendChild(resultArea);

  // Buttons
  const btnRow = el('div', 'sp-mapping-editor-btns');
  const saveBtn = el('button', 'sp-mapping-save', 'Add Season');
  const cancelBtn = el('button', 'sp-mapping-cancel', 'Cancel');
  cancelBtn.addEventListener('click', () => form.remove());

  saveBtn.addEventListener('click', async () => {
    const seasonNum = parseInt(snInput.value, 10);
    const anilistId = alInput.value.trim() ? parseInt(alInput.value, 10) : null;
    const malId = malInput.value.trim() ? parseInt(malInput.value, 10) : null;

    if (!seasonNum || seasonNum < 1) {
      showBanner('Season number must be ≥ 1', 'error');
      return;
    }
    if (existing.some(s => s.seasonNumber === seasonNum)) {
      showBanner(`Season ${seasonNum} already exists`, 'error');
      return;
    }

    saveBtn.disabled = true;
    resultArea.hidden = true;

    try {
      // Step 1: create season mapping (gap-validated)
      const newSeason = await setSeasonMapping(show.id, seasonNum, anilistId, malId);

      // Step 2: set status
      if (selectedStatus !== 'PLANNED') {
        await setSeasonStatus(newSeason.id, selectedStatus);
      }

      // Step 3: reconcile (advisory)
      let reconciled = null;
      try {
        reconciled = await reconcileSeasonMapping(show.id, seasonNum);
      } catch (err) {
        console.warn('Reconciliation failed:', err);
      }

      // Show reconciliation result
      if (reconciled) {
        resultArea.hidden = false;
        const source = reconciled.source;
        const fribbAl = reconciled.anilistId;

        if (source === 'FRIBB' && fribbAl && anilistId && fribbAl === anilistId) {
          resultArea.className = 'sp-add-season-result ok';
          resultArea.textContent = `✅ Fribb confirms AniList ID ${fribbAl}`;
        } else if (source === 'FRIBB' && fribbAl && anilistId && fribbAl !== anilistId) {
          resultArea.className = 'sp-add-season-result warn';
          resultArea.textContent = `⚠️ Fribb suggests AniList ID ${fribbAl} (you entered ${anilistId})`;
        } else if (source === 'FRIBB' && fribbAl && !anilistId) {
          resultArea.className = 'sp-add-season-result ok';
          resultArea.textContent = `✅ Fribb mapped to AniList ID ${fribbAl}`;
        } else {
          resultArea.className = 'sp-add-season-result info';
          resultArea.textContent = 'ℹ️ Fribb has no data for this season yet';
        }
      }

      // Update in-memory show and re-render
      show.seasons.push({ ...newSeason, status: selectedStatus !== 'PLANNED' ? selectedStatus : null });
      showBanner(`Season ${seasonNum} added`, 'ok');
      form.remove();

      // Re-render the seasons section
      const root = document.getElementById('show-root');
      root.innerHTML = '';
      renderBanner(show, root);
      renderHero(show, root, cfg);
      renderBody(show, root, cfg, seasonNum);

    } catch (err) {
      // Gap validation error
      const gapMatch = err.message.match(/Season(?:s)?\s+([\d,\s]+)\s+do(?:es)?\s+not exist/);
      if (gapMatch) {
        resultArea.hidden = false;
        resultArea.className = 'sp-add-season-result warn';
        resultArea.innerHTML = '';
        resultArea.appendChild(document.createTextNode(`⚠️ ${err.message} `));

        const missing = gapMatch[1].split(',').map(s => parseInt(s.trim(), 10)).filter(n => n > 0);
        const bulkBtn = el('button', 'sp-mapping-save',
          `Create ${missing.join(', ')} as COMPLETED`);
        bulkBtn.style.fontSize = '10px';
        bulkBtn.style.padding = '3px 8px';
        bulkBtn.addEventListener('click', async () => {
          bulkBtn.disabled = true;
          try {
            for (const sn of missing.sort((a, b) => a - b)) {
              const created = await setSeasonMapping(show.id, sn, null, null);
              await setSeasonStatus(created.id, 'COMPLETED');
              show.seasons.push({ ...created, status: 'COMPLETED' });
            }
            showBanner(`Created ${missing.length} missing season${missing.length > 1 ? 's' : ''}`, 'ok');
            resultArea.hidden = true;
            // Retry
            saveBtn.disabled = false;
            saveBtn.click();
          } catch (e2) {
            showBanner(`Bulk create failed: ${e2.message}`, 'error');
            bulkBtn.disabled = false;
          }
        });
        resultArea.appendChild(bulkBtn);
      } else {
        showBanner(`Add season failed: ${err.message}`, 'error');
      }
    } finally {
      saveBtn.disabled = false;
    }
  });

  btnRow.appendChild(saveBtn);
  btnRow.appendChild(cancelBtn);
  form.appendChild(btnRow);

  // Insert after the section label, before the first season card
  const firstCard = section.querySelector('.sp-season-card, .sp-special-card');
  section.insertBefore(form, firstCard);
  snInput.focus();
}

/* ── Auto-fetch art ──────────────────────────────────────── */

/**
 * Auto-fetch art assets from external sources, auto-select the best
 * poster and banner, then re-render the hero if anything changed.
 */
async function autoFetchArt(show, root, cfg, targetSeason) {
  // Only fetch if we don't already have art assets for this show
  const existingPosters = (show.artAssets || []).filter(a => a.kind.toLowerCase() === 'poster');
  const existingBanners = (show.artAssets || []).filter(a =>
    a.kind.toLowerCase() === 'banner' || a.kind.toLowerCase() === 'background');

  // If we already have fetched assets with selections, skip
  if (existingPosters.some(a => a.selected) && existingBanners.some(a => a.selected)) return;

  // Fetch from external sources if no art assets exist at all
  let assets = show.artAssets || [];
  if (!assets.length) {
    try {
      const result = await fetchShowArt(show.id);
      assets = result.artAssets || [];
      show.artAssets = assets;
    } catch (err) {
      console.warn('Auto art fetch mutation failed:', err);
      return;
    }
  }
  if (!assets.length) return;

  let changed = false;

  // Auto-select best poster if none selected
  const posters = assets.filter(a => a.kind.toLowerCase() === 'poster' && !a.seasonId);
  if (posters.length && !posters.some(a => a.selected)) {
    const best = posters[0]; // first is typically the best
    try {
      await selectArtAsset(best.id);
      best.selected = true;
      show.posterUrl = best.url;
      changed = true;
    } catch (err) { console.warn('Auto poster select failed:', err); }
  }

  // Auto-select best banner if none selected
  const banners = assets.filter(a =>
    (a.kind.toLowerCase() === 'banner' || a.kind.toLowerCase() === 'background') && !a.seasonId);
  if (banners.length && !banners.some(a => a.selected)) {
    const best = banners[0];
    try {
      await selectArtAsset(best.id);
      best.selected = true;
      show.bannerUrl = best.url;
      changed = true;
    } catch (err) { console.warn('Auto banner select failed:', err); }
  }

  // Re-render hero + banner if art was selected
  if (changed) {
    // Skip if user is actively editing
    if (root.querySelector('textarea, input, .sp-mapping-editor, .sp-syn-editor')) return;
    root.innerHTML = '';
    renderBanner(show, root);
    renderHero(show, root, cfg);
    renderBody(show, root, cfg, targetSeason);
  }
}

/* ── Init ────────────────────────────────────────────────── */

export async function init() {
  const cfg = await bootstrapConfig();
  if (!cfg) return;

  const params = new URLSearchParams(window.location.search);
  const showId = params.get('id');
  const targetSeason = params.has('season') ? Number(params.get('season')) : null;

  if (!showId) {
    const root = document.getElementById('show-root');
    root.innerHTML = '';
    const err = el('div', 'sp-error');
    err.appendChild(el('h2', null, 'No show specified'));
    err.appendChild(el('p', null, 'Navigate to a show from the Calendar, Backlog, or List page.'));
    root.appendChild(err);
    return;
  }

  // Update page title
  document.title = 'Starfleet — Loading…';
  showBanner('Loading show…', 'loading');

  try {
    const show = await fetchShow(showId);
    hideBanner();

    document.title = `Starfleet — ${show.displayTitle}`;

    const root = document.getElementById('show-root');
    root.innerHTML = '';

    renderBanner(show, root);
    renderHero(show, root, cfg);
    renderBody(show, root, cfg, targetSeason);

    // Lazy-fetch episode synopses if any are missing
    const hasMissing = show.episodes.some(ep => ep.synopsis == null);
    const showMissing = show.synopsis == null;
    if (hasMissing || showMissing) {
      fetchEpisodeSynopses(show.id)
        .then(async () => {
          // Skip re-render if user is actively editing something
          if (root.querySelector('textarea, input, .sp-mapping-editor, .sp-syn-editor, .sp-ep-syn-edit-area')) return;
          // Re-fetch and check if anything actually changed
          const updated = await fetchShow(showId);
          const newSynCount = updated.episodes.filter(e => e.synopsis != null).length;
          const oldSynCount = show.episodes.filter(e => e.synopsis != null).length;
          if (newSynCount <= oldSynCount && updated.synopsis === show.synopsis) return;
          document.title = `Starfleet — ${updated.displayTitle}`;
          root.innerHTML = '';
          renderBanner(updated, root);
          renderHero(updated, root, cfg);
          renderBody(updated, root, cfg, targetSeason);
        })
        .catch(err => console.warn('Synopsis fetch failed:', err));
    }

    // Auto-fetch and auto-select art when a show has no poster or banner yet
    const hasPosters = (show.artAssets || []).some(a => a.kind.toLowerCase() === 'poster');
    const hasBanners = (show.artAssets || []).some(a =>
      a.kind.toLowerCase() === 'banner' || a.kind.toLowerCase() === 'background');
    if (!show.posterUrl || !show.bannerUrl || !hasPosters || !hasBanners) {
      autoFetchArt(show, root, cfg, targetSeason).catch(err =>
        console.warn('Auto art fetch failed:', err));
    }

  } catch (err) {
    hideBanner();
    showBanner(`Error loading show: ${err.message}`, 'error');
    console.error('Show load failed:', err);
  }
}
