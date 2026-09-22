/**
 * Art picker modal — shared between show page and planner.
 *
 * Displays a grid of art assets for a show, allowing selection/deselection.
 * Extracted to avoid circular dependency between show.js and calendar.js.
 */

import { fetchShowArt, selectArtAsset, deselectArtAsset, addManualArtUrl, deleteArtAsset } from './api.js?v=22';

/* ── Helpers ─────────────────────────────────────────────── */

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/**
 * Open an art-picker modal for a given show + slot.
 *
 * @param {object}              show      - Show object (must have .id; .artAssets populated or empty)
 * @param {string|null}         seasonId  - Season ID filter, or null for show-level
 * @param {string}              kind      - 'poster', 'banner', or 'background'
 * @param {HTMLImageElement|null} targetImg - Optional image element to update on selection
 * @param {function}            onSelect  - Callback(assetUrl) after selection
 * @param {function}            onError   - Callback(message) on error (e.g. showBanner)
 * @param {string}              episodeKind - 'REGULAR' (default), 'SPECIAL', 'OVA', or
 *   'BONUS_MOVIE' — a shared cover for every episode of that kind (df50e70a1faa,
 *   2026-09-22), scoped independently from the regular season/show slot. No automated
 *   source targets these directly, but "Fetch Art from Sources" still runs the normal
 *   cascade (populating the regular slot) and this picker offers those results as
 *   candidates to borrow into the special/OVA/bonus-movie slot too.
 */
export function openArtPicker(show, seasonId, kind, targetImg, onSelect, onError, episodeKind = 'REGULAR') {
  const kindSet = kind === 'banner' ? new Set(['banner', 'background']) : new Set([kind]);
  const isRegular = episodeKind === 'REGULAR';
  const slotMatch = a => {
    const kindMatch = kindSet.has(a.kind.toLowerCase());
    if (seasonId) return kindMatch && a.seasonId === seasonId;
    return kindMatch && !a.seasonId;
  };
  const assets = (show.artAssets || [])
    .filter(a => slotMatch(a) && (a.episodeKind || 'REGULAR') === episodeKind);
  // Regular-slot candidates offered as "borrow" options for a special/OVA/
  // bonus-movie picker — none of AniList/TVDB/TMDB/TVmaze/MAL know about a
  // per-episode-kind cover, so this is the only way "Fetch Art from
  // Sources" is actually useful here: it populates the regular pool, and
  // this list lets the user adopt one of those into this kind's own slot.
  const borrowable = isRegular ? [] : (show.artAssets || [])
    .filter(a => slotMatch(a) && (a.episodeKind || 'REGULAR') === 'REGULAR');

  // Build overlay
  const overlay = el('div', 'sp-art-overlay');
  overlay.addEventListener('click', e => {
    if (e.target === overlay) overlay.remove();
  });

  const modal = el('div', 'sp-art-modal');
  const titleText = isRegular
    ? `Choose ${kind} art${seasonId ? '' : ' (show-level)'}`
    : `Choose ${kind} art for ${episodeKind.replace('_', ' ').toLowerCase()} episodes`;
  const title = el('h3', 'sp-art-modal-title', titleText);
  modal.appendChild(title);

  // Fetch Art button — always available. For a non-regular slot this
  // populates the regular pool (below), not this slot directly.
  const fetchBtn = el('button', 'sp-art-fetch-btn', '⟳ Fetch Art from Sources');
  fetchBtn.addEventListener('click', async () => {
    fetchBtn.disabled = true;
    fetchBtn.textContent = 'Fetching…';
    try {
      const result = await fetchShowArt(show.id);
      show.artAssets = result.artAssets;
      overlay.remove();
      openArtPicker(show, seasonId, kind, targetImg, onSelect, onError, episodeKind);
    } catch (err) {
      fetchBtn.textContent = '✗ ' + err.message;
      setTimeout(() => {
        fetchBtn.disabled = false;
        fetchBtn.textContent = '⟳ Fetch Art from Sources';
      }, 3000);
    }
  });
  modal.appendChild(fetchBtn);

  // Add art manually via a pasted URL — doesn't have to come from any
  // known source (e.g. filling in art the fetch cascade can't find).
  const addForm = el('form', 'sp-art-add-form');
  const addInput = document.createElement('input');
  addInput.type = 'url';
  addInput.placeholder = 'Paste an image URL…';
  addInput.className = 'sp-art-add-input';
  addInput.required = true;
  const addBtn = el('button', 'sp-art-add-btn', '+ Add');
  addBtn.type = 'submit';
  addForm.appendChild(addInput);
  addForm.appendChild(addBtn);

  /** Adopt a URL (freshly pasted, or borrowed from the regular pool)
   * into this picker's own (kind, episodeKind) slot and select it. */
  async function adoptUrl(url) {
    const newAsset = await addManualArtUrl(
      show.id, seasonId, kind.toUpperCase(), url, isRegular ? null : episodeKind,
    );
    // Server selects the new asset immediately, deselecting whatever
    // held this slot before — mirror that locally rather than re-fetch.
    for (const a of (show.artAssets || [])) {
      if (a.kind.toLowerCase() === newAsset.kind.toLowerCase() &&
          (a.episodeKind || 'REGULAR') === (newAsset.episodeKind || 'REGULAR') &&
          a.seasonId === newAsset.seasonId) {
        a.selected = false;
      }
    }
    show.artAssets = [...(show.artAssets || []), { ...newAsset, selected: true }];
    if (targetImg) targetImg.src = newAsset.url;
    if (onSelect) onSelect(newAsset.url);
    overlay.remove();
    openArtPicker(show, seasonId, kind, targetImg, onSelect, onError, episodeKind);
  }

  addForm.addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = addInput.value.trim();
    if (!url) return;
    addBtn.disabled = true;
    try {
      await adoptUrl(url);
    } catch (err) {
      if (onError) onError(`Add art failed: ${err.message}`);
      addBtn.disabled = false;
    }
  });
  modal.appendChild(addForm);

  if (!assets.length && !borrowable.length) {
    modal.appendChild(el('p', 'sp-art-empty', isRegular
      ? 'No art assets yet. Click "Fetch Art" to retrieve from AniList & TVDB.'
      : 'No cover set for these episodes yet. Paste a URL, fetch the show\'s own art below to borrow from, or click "Fetch Art" first.'));
  }

  // Art grid — use wider columns for banner/background art
  const isBannerKind = kind === 'banner' || kind === 'background';
  const grid = el('div', `sp-art-grid${isBannerKind ? ' sp-art-grid-wide' : ''}`);

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
      const ratio = (asset.width / asset.height).toFixed(2);
      info.appendChild(el('span', 'sp-art-dims', `${asset.width}×${asset.height} (${ratio})`));
    }
    if (asset.selected) {
      info.appendChild(el('span', 'sp-art-selected-badge', '✓ Active'));
    }
    card.appendChild(info);

    const deleteBtn = el('button', 'sp-art-delete-btn', '🗑');
    deleteBtn.type = 'button';
    deleteBtn.title = 'Delete this art candidate';
    deleteBtn.addEventListener('click', async (e) => {
      e.stopPropagation();
      if (!confirm('Delete this art candidate? A manual re-fetch will find it again if it\'s still available from its source.')) return;
      try {
        await deleteArtAsset(asset.id);
        show.artAssets = (show.artAssets || []).filter(a => a.id !== asset.id);
        if (asset.selected) {
          // Deleting the active selection reverts to the fallback
          // (server already cleared the denormalised show column).
          if (targetImg) targetImg.removeAttribute('src');
          if (onSelect) onSelect(null);
        }
        overlay.remove();
        openArtPicker(show, seasonId, kind, targetImg, onSelect, onError, episodeKind);
      } catch (err) {
        if (onError) onError(`Delete art failed: ${err.message}`);
      }
    });
    card.appendChild(deleteBtn);

    card.addEventListener('click', async () => {
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
        if (onError) onError(`Art selection failed: ${err.message}`);
      }
    });
    grid.appendChild(card);
  }
  modal.appendChild(grid);

  // Borrow-from-regular section — only for a special/OVA/bonus-movie
  // picker, and only once there's actually something to borrow.
  if (borrowable.length) {
    modal.appendChild(el('p', 'sp-art-borrow-label',
      `Or use one of the show's own ${kind} candidates:`));
    const borrowGrid = el('div', `sp-art-grid sp-art-grid-borrow${isBannerKind ? ' sp-art-grid-wide' : ''}`);
    for (const asset of borrowable) {
      const card = el('div', 'sp-art-card sp-art-card-borrow');
      const img = el('img');
      img.src = asset.url;
      img.alt = `${asset.source} ${kind}`;
      img.loading = 'lazy';
      img.onerror = () => { img.style.opacity = '0.3'; };
      card.appendChild(img);

      const info = el('div', 'sp-art-card-info');
      info.appendChild(el('span', 'sp-art-source', asset.source));
      card.appendChild(info);

      card.title = 'Use this for this episode kind too';
      card.addEventListener('click', async () => {
        try {
          await adoptUrl(asset.url);
        } catch (err) {
          if (onError) onError(`Add art failed: ${err.message}`);
        }
      });
      borrowGrid.appendChild(card);
    }
    modal.appendChild(borrowGrid);
  }

  // Close button
  const closeBtn = el('button', 'sp-art-close-btn', '✕');
  closeBtn.addEventListener('click', () => overlay.remove());
  modal.appendChild(closeBtn);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}
