/**
 * keep.js — the `keep` tag in Sonarr/Radarr (Maintainerr cleans a show off the server unless
 * it carries it). A switch on the show page, and a reminder when an old show is added or a
 * show is completed. Self-contained: it only needs `gql` from api.js, so it works with
 * whichever api.js version a page has cached.
 */

import { gql } from './api.js?v=28';

const KEEP_QUERY = `
  query ShowKeep($id: ID!) {
    show(id: $id) {
      id displayTitle status mediaShape keep
      episodes(first: 200) { edges { node { airDateUtc } } }
    }
  }`;

const SET_KEEP = `
  mutation SetShowKeep($showId: ID!, $keep: Boolean!) {
    setShowKeep(showId: $showId, keep: $keep) { id keep }
  }`;

function banner(html, type) {
  const el = document.getElementById('status-banner');
  if (!el) return null;
  el.className = `status-banner ${type}`;
  el.innerHTML = html;
  return el;
}

function esc(s) {
  return String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
}

export async function setKeep(showId, keep) {
  const data = await gql(SET_KEEP, { showId, keep });
  return data.setShowKeep.keep;
}

/** The "keep" switch for the show page's id-badge row. Hidden while loading and when the show is
 *  not in Sonarr/Radarr (nothing to tag). Click toggles the tag. */
export function renderKeepBadge(container, showId) {
  const btn = document.createElement('button');
  btn.className = 'sp-ext-badge sp-keep-badge';
  btn.style.display = 'none';
  container.appendChild(btn);

  let kept = null;
  const paint = () => {
    btn.style.display = kept === null ? 'none' : '';
    btn.textContent = kept ? '★ keep' : '☆ keep';
    btn.title = kept
      ? 'Kept: Maintainerr will not clean this show off the server. Click to un-keep.'
      : 'Not kept: its watched seasons are cleaned off the server after they finish airing. Click to keep.';
    btn.style.color = kept ? 'var(--accent)' : '';
  };

  gql(KEEP_QUERY, { id: showId })
    .then((d) => { kept = d.show ? d.show.keep : null; paint(); })
    .catch(() => { kept = null; paint(); });

  btn.addEventListener('click', async () => {
    btn.disabled = true;
    try {
      kept = await setKeep(showId, !kept);
      banner(kept ? 'Kept — it will not be cleaned off the server.' : 'No longer kept.', 'ok');
    } catch (e) {
      banner(`Keep failed: ${esc(e.message)}`, 'error');
    } finally {
      btn.disabled = false;
      paint();
    }
  });
}

/** A one-off reminder: this show is not kept and will be auto-cleaned. `why` is "completed" (the
 *  show was just completed) or "added" (an already-aired show was just added). */
export async function remindKeep(showId, why) {
  let show;
  try {
    show = (await gql(KEEP_QUERY, { id: showId })).show;
  } catch {
    return;
  }
  if (!show || show.keep !== false) return; // null: not in Sonarr/Radarr; true: already kept
  const status = String(show.status || '').toUpperCase();
  if (status === 'DROPPED' || status === 'SKIPPED') return;
  const now = Date.now();
  const aired = (show.episodes?.edges || []).some((e) => {
    const t = e.node.airDateUtc && Date.parse(e.node.airDateUtc);
    return t && t < now;
  });
  if (why === 'added' && !aired && show.mediaShape !== 'MOVIE') return;
  const title = esc(show.displayTitle);
  const text = why === 'completed'
    ? `<b>${title}</b> is completed: its seasons are cleaned off the server about 25 days after they finish airing, unless you keep it.`
    : `<b>${title}</b> has already aired: once you complete it, its seasons are cleaned off the server about 25 days after they finish airing, unless you keep it.`;
  const el = banner(`${text} <button class="keep-remind-btn" style="margin-left:.6rem;cursor:pointer">★ Keep it</button>`, 'warn');
  const btn = el && el.querySelector('.keep-remind-btn');
  if (btn) {
    btn.addEventListener('click', async () => {
      btn.disabled = true;
      try {
        await setKeep(showId, true);
        banner(`<b>${title}</b> is kept: it will not be cleaned off the server.`, 'ok');
      } catch (e) {
        banner(`Keep failed: ${esc(e.message)}`, 'error');
      }
    });
  }
}
