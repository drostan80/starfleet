/**
 * Art picker modal — shared between show page and planner.
 *
 * Displays a grid of art assets for a show, allowing selection/deselection.
 * Extracted to avoid circular dependency between show.js and calendar.js.
 */

import { fetchShowArt, selectArtAsset, deselectArtAsset } from './api.js?v=19';

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
 */
export function openArtPicker(show, seasonId, kind, targetImg, onSelect, onError) {
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
      openArtPicker(show, seasonId, kind, targetImg, onSelect, onError);
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

  // Close button
  const closeBtn = el('button', 'sp-art-close-btn', '✕');
  closeBtn.addEventListener('click', () => overlay.remove());
  modal.appendChild(closeBtn);

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}
