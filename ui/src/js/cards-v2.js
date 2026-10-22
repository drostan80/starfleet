/**
 * cards-v2.js — the redesigned episode cards (user 2026-10-22), the default; a "Design: Classic | New"
 * switch in Settings (localStorage) keeps the classic ones for whoever wants them.
 *
 *  - card:   art only, the left link bar (download button included), a status indicator circle,
 *            a small status-colour tab bottom right (hover → the flower picker); hovering the card
 *            adds a frosted title / SxxExx / air time.
 *  - banner: the 1D / 3D planner row: the whole banner (its height follows the image) with the same
 *            info as the classic planner card (no episode counters), same indicator and status tab.
 *            3D: the link bar lies along the foot of the banner. 1D: the regular card sits on the
 *            left of the banner (so the vertical link bar is the card's).
 *
 * calendar.js hands its helpers over with registerHelpers() (no import cycle).
 */

import { buildStatusBtn } from './status-picker.js?v=2';

let H = null;
export function registerHelpers(helpers) { H = helpers; }

/* ── The Design switch ───────────────────────────────────── */

const DESIGN_KEY = 'starfleet_design';

/** New is the default; only an explicit choice of Classic (Settings) keeps the old cards. */
export function getDesign() {
  try { return localStorage.getItem(DESIGN_KEY) === 'classic' ? 'classic' : 'new'; } catch { return 'new'; }
}

function setDesign(value) {
  try { localStorage.setItem(DESIGN_KEY, value); } catch { /* the choice just isn't remembered */ }
}

/** Put the Classic | New switch in `mount`; `onChange` runs after a change. */
export function mountDesignPicker(mount, onChange) {
  if (!mount) return;
  mount.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'design-picker';
  wrap.title = 'Card design';
  const label = document.createElement('span');
  label.className = 'design-picker-label';
  label.textContent = 'Design';
  wrap.appendChild(label);
  const buttons = {};
  for (const [value, text] of [['classic', 'Classic'], ['new', 'New']]) {
    const b = document.createElement('button');
    b.className = 'design-opt';
    b.textContent = text;
    b.addEventListener('click', () => {
      if (getDesign() === value) return;
      setDesign(value);
      sync();
      onChange?.(value);
    });
    buttons[value] = b;
    wrap.appendChild(b);
  }
  const sync = () => {
    for (const [value, b] of Object.entries(buttons)) b.classList.toggle('active', getDesign() === value);
  };
  sync();
  mount.appendChild(wrap);
}

/* ── Pieces ──────────────────────────────────────────────── */

const CLOCK = '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.5"/><path d="M10 5.5V10l3 2"/></svg>';
const DOT = '<svg viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.5"/><circle class="fill" cx="10" cy="10" r="3.8"/></svg>';
const TICK = '<svg viewBox="0 0 20 20"><circle class="ring" cx="10" cy="10" r="8.5"/><path d="M6 10.4l2.7 2.7L14 7.6"/></svg>';

function el(tag, className) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  return e;
}

/**
 * The airing / viewing indicator: grey clock circle (not aired), blinking blue dot (airing now),
 * grey tick (aired, not watched), blue ticked circle (watched). A button — it marks / unmarks
 * watched like the classic ✓ did (onWatchToggle keeps the `watched` class and data-state).
 */
function buildIndicator(ep) {
  const watched = ep.state === 'WATCHED';
  let phase = 'aired';
  if (H.isEpisodeAiringNow(ep)) phase = 'airing';
  else if (!H.hasAired(ep)) phase = 'future';

  const btn = el('button', `ind ind-${phase}${watched ? ' watched' : ''}`);
  btn.dataset.state = ep.state;
  btn.innerHTML = `<span class="g g-clock">${CLOCK}</span><span class="g g-dot">${DOT}</span>`
    + `<span class="g g-tick">${TICK}</span>`;
  const phaseText = { future: 'Not yet aired', airing: 'Airing now', aired: {
    ready: 'Available — not watched', downloading: 'Downloading — not watched',
  }[H.availState(ep)] || 'Aired — not available' }[phase];
  const apply = () => {
    btn.title = btn.classList.contains('watched') ? 'Watched — click to unmark'
      : `${phaseText} — click to mark watched`;
  };
  apply();
  const wid = ep.watchEvents?.edges?.[0]?.node?.id;
  if (wid) btn.dataset.watchEventId = wid;
  btn.addEventListener('click', async (e) => {
    e.stopPropagation();
    await H.onWatchToggle(btn, ep);
    apply();
  });
  return btn;
}

/** The status marker: a small square in the status colour (a folder tab); hover → the flower. */
function buildStatusTab(ep) {
  const status = ep.seasonEntity?.status || ep.show.status || 'WATCHING';
  const wrap = buildStatusBtn({ id: ep.show.id, currentStatus: status, onPick: H.onStatusChange });
  wrap.classList.add('status-tab');
  return wrap;
}

/** The left link bar of a card, with the download button added at its foot. */
function buildBar(ep, cfg, horizontal = false) {
  const filePath = ep.show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;
  const externalIds = ep.show.externalIds?.edges?.map((e) => e.node) || [];
  const strip = H.buildSvcStrip(externalIds, ep.seasonEntity?.malId ?? null, filePath,
    ep.availableLocally, cfg, H.episodeCtx(ep), ep.jellyfinUrl);
  if (horizontal) strip.classList.add('h');
  if (ep.availableLocally && filePath) {
    const dl = el('a', 'svc svc-dl on');
    dl.title = 'Download';
    dl.innerHTML = H.downloadSvg;
    dl.href = '#';
    dl.addEventListener('click', (e) => {
      e.preventDefault();
      H.startDownload({
        showTitle: ep.show.displayTitle,
        label: H.fmtEpBadge(ep) + (ep.title ? ` — ${ep.title}` : ''),
        filePath,
        episodeId: ep.id,
        showId: ep.show.id,
        season: ep.season ?? null,
        episode: ep.episode ?? null,
      });
      dl.classList.add('triggered');
      setTimeout(() => dl.classList.remove('triggered'), 1200);
    });
    strip.appendChild(dl);
  }
  return { strip, filePath };
}

function airTimeText(ep, withZone = true) {
  if (H.isDateOnly(ep)) return 'all day';
  return withZone ? H.fmtAirTime(ep.airDateUtc) : H.fmtTimeOnly(ep.airDateUtc);
}

/* ── The card ────────────────────────────────────────────── */

/** The cover-art picker: sets the poster of the show on every card showing it. */
function buildPosterPicker(ep) {
  const pick = el('button', 'card2-art-btn');
  pick.title = 'Choose cover art';
  pick.textContent = '🖼';
  pick.addEventListener('click', async (e) => {
    e.stopPropagation();
    pick.disabled = true;
    try {
      const result = await H.getShowArtAssets(ep.show.id);
      H.openArtPicker({ id: ep.show.id, artAssets: result.artAssets || [] }, null, 'poster', null, (url) => {
        ep.show.posterUrl = url;
        H.posterCache.set(ep.show.id, url);
        document.querySelectorAll(`.card-art[data-show-id="${ep.show.id}"]`).forEach((art) => {
          let img = art.querySelector('img');
          if (!img) {
            img = document.createElement('img');
            art.prepend(img);
          }
          img.src = url;
        });
      }, (msg) => H.showBanner(msg, 'error'));
    } catch {
      /* the picker reports its own errors; nothing to undo */
    }
    pick.disabled = false;
  });
  return pick;
}

/** `plain`: only the bar and the art (the 1D row puts its own indicator, info and status tab). */
export function buildCardV2(ep, cfg, { plain = false } = {}) {
  const card = el('article', plain ? 'show-card card2 card2-plain' : 'show-card card2');
  card.dataset.episodeId = ep.id;
  card.dataset.showId = ep.show.id;

  const { strip, filePath } = buildBar(ep, cfg);
  card.appendChild(strip);

  const art = el('div', 'card-art card2-art');
  art.dataset.showId = ep.show.id;
  if (ep.availableLocally) {
    art.classList.add('art-playable');
    art.addEventListener('click', () => H.launchMpv(filePath, cfg, H.episodeCtx(ep)));
  }
  const posterUrl = ep.show.posterUrl || H.posterCache.get(ep.show.id) || null;
  if (posterUrl) {
    const img = document.createElement('img');
    img.src = posterUrl;
    img.alt = ep.show.displayTitle;
    if (cfg.tmdb_api_key) {
      img.onerror = () => {
        img.remove();
        H.posterCache.delete(ep.show.id);
        H.fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
      };
    }
    art.appendChild(img);
  } else if (cfg.tmdb_api_key) {
    H.fetchTmdbPoster(ep.show, cfg.tmdb_api_key);
  }

  art.appendChild(buildPosterPicker(ep));
  if (plain) {
    card.appendChild(art);
    return card;
  }
  art.appendChild(buildIndicator(ep));

  // Hover, on frosted glass: the title centred on top; under it the air time (left) and SxxExx (right).
  const frost = el('div', 'card2-frost');
  const title = el('a', 'card2-title');
  title.href = `show.html?id=${encodeURIComponent(ep.show.id)}`;
  title.textContent = ep.show.displayTitle;
  title.title = ep.show.displayTitle;
  title.addEventListener('click', (e) => e.stopPropagation());
  const badge = el('span', 'card2-badge');
  badge.textContent = H.fmtEpBadge(ep);
  const time = el('span', 'card2-time');
  time.textContent = airTimeText(ep);
  const change = H.airChangeBadge(ep);
  if (change) time.appendChild(change);
  const line = el('div', 'card2-line');
  line.append(time, badge);
  const top = el('div', 'card2-top');
  top.append(title, line);
  frost.append(top);
  art.appendChild(frost);

  card.appendChild(art);
  card.appendChild(buildStatusTab(ep));
  return card;
}

/* ── The banner row (1D / 3D planner) ────────────────────── */

function setBannerImage(box, url, cfg, show) {
  box.querySelector('img.bn-img')?.remove();
  const img = document.createElement('img');
  img.className = 'bn-img';
  img.alt = '';
  img.src = url;
  img.onerror = () => {
    img.remove();
    box.classList.remove('has-img');
    if (cfg?.tmdb_api_key && show) H.fetchTmdbBackdrop(show, cfg.tmdb_api_key);
  };
  box.prepend(img);
  box.classList.add('has-img');
}

/** A banner found later (TMDB) is put on every banner row of that show that has none. */
export function patchBannerRows(showId, url) {
  document.querySelectorAll(`.bn-banner[data-show-id="${showId}"]:not(.has-img)`)
    .forEach((box) => setBannerImage(box, url, null, null));
}

/** The banner-art picker (top left of a banner): sets the banner of the show everywhere it shows. */
function buildBannerPicker(ep, cfg) {
  const pick = el('button', 'bn-art');
  pick.title = 'Choose banner art';
  pick.textContent = '🖼';
  pick.addEventListener('click', async () => {
    pick.disabled = true;
    try {
      const result = await H.getShowArtAssets(ep.show.id);
      H.openArtPicker({ id: ep.show.id, artAssets: result.artAssets || [] }, null, 'banner', null, (url) => {
        ep.show.bannerUrl = url;
        H.backdropCache.set(ep.show.id, url);
        document.querySelectorAll(`.bn-banner[data-show-id="${ep.show.id}"]`)
          .forEach((b) => setBannerImage(b, url, cfg, ep.show));
      }, (msg) => H.showBanner(msg, 'error'));
    } catch {
      /* the picker reports its own errors; nothing to undo */
    }
    pick.disabled = false;
  });
  return pick;
}

/** `withCard` (1D): the regular card on the left; else (3D) the link bar lies along the banner foot. */
export function buildBannerRow(ep, cfg, { withCard = false } = {}) {
  const row = el('article', withCard ? 'banner-row with-card' : 'banner-row');
  row.dataset.episodeId = ep.id;
  row.dataset.showId = ep.show.id;

  let filePath;
  let strip = null;
  if (withCard) {
    row.appendChild(buildCardV2(ep, cfg, { plain: true }));
    filePath = ep.show.mediaShape === 'MOVIE' ? ep.filePathRadarr : ep.filePathSonarr;
  } else {
    ({ strip, filePath } = buildBar(ep, cfg, true));
  }

  const box = el('div', 'bn-banner');
  box.dataset.showId = ep.show.id;
  const bannerSrc = ep.show.bannerUrl || H.backdropCache.get(ep.show.id) || null;
  if (bannerSrc) setBannerImage(box, bannerSrc, cfg, ep.show);
  else if (cfg.tmdb_api_key) H.fetchTmdbBackdrop(ep.show, cfg.tmdb_api_key);

  box.appendChild(el('div', 'bn-scrim'));

  const info = el('div', 'bn-info');
  const title = el('a', 'bn-title');
  title.href = `show.html?id=${encodeURIComponent(ep.show.id)}`;
  title.textContent = ep.show.displayTitle;
  title.title = ep.show.displayTitle;
  info.appendChild(title);
  const line = el('div', 'bn-line');
  const badge = el('span', 'bn-badge');
  badge.textContent = H.fmtEpBadge(ep);
  line.appendChild(badge);
  const time = el('span', 'bn-time');
  time.textContent = airTimeText(ep, false);
  const change = H.airChangeBadge(ep);
  if (change) time.appendChild(change);
  line.appendChild(time);
  info.appendChild(line);
  if (ep.title) {
    const epTitle = el('div', 'bn-ep-title');
    epTitle.textContent = ep.title;
    info.appendChild(epTitle);
  }
  box.appendChild(info);

  if (ep.availableLocally && !withCard) {  // with the card, its art already plays
    const play = el('button', 'bn-play');
    play.title = 'Play in mpv';
    play.textContent = '▶';
    play.addEventListener('click', () => H.launchMpv(filePath, cfg, H.episodeCtx(ep)));
    box.appendChild(play);
  }

  box.appendChild(buildIndicator(ep));

  box.appendChild(buildBannerPicker(ep, cfg));

  row.appendChild(box);
  if (strip) {
    strip.classList.add('under');  // 3D: the link bar lies under the banner
    row.appendChild(strip);
  }
  row.appendChild(buildStatusTab(ep));
  return row;
}

/* ── The month view tile ─────────────────────────────────── */

/**
 * One show airing on a day of the month grid: its banner, the play triangle when the episode is
 * available, the art picker on the left and the indicator on the right, the link bar under it;
 * the title, SxxExx, episode name and air time appear on hover.
 */
export function buildMonthTile(ep, cfg) {
  const tile = el('article', 'mt');
  tile.dataset.episodeId = ep.id;
  tile.dataset.showId = ep.show.id;

  const { strip, filePath } = buildBar(ep, cfg, true);
  strip.classList.add('under');

  const box = el('div', 'bn-banner mt-banner');
  box.dataset.showId = ep.show.id;
  const bannerSrc = ep.show.bannerUrl || H.backdropCache.get(ep.show.id) || null;
  if (bannerSrc) setBannerImage(box, bannerSrc, cfg, ep.show);
  else if (cfg.tmdb_api_key) H.fetchTmdbBackdrop(ep.show, cfg.tmdb_api_key);
  const fallback = el('div', 'mt-fallback');  // until a banner is found
  fallback.textContent = ep.show.displayTitle;
  box.appendChild(fallback);

  if (ep.availableLocally) {
    const play = el('button', 'mt-play');
    play.title = 'Play in mpv';
    play.textContent = '▶';
    play.addEventListener('click', () => H.launchMpv(filePath, cfg, H.episodeCtx(ep)));
    box.appendChild(play);
  }
  box.appendChild(buildBannerPicker(ep, cfg));
  box.appendChild(buildIndicator(ep));

  const frost = el('div', 'mt-frost');
  const title = el('a', 'mt-title');
  title.href = `show.html?id=${encodeURIComponent(ep.show.id)}`;
  title.textContent = ep.show.displayTitle;
  title.title = ep.show.displayTitle;
  const badge = el('span');
  badge.textContent = H.fmtEpBadge(ep);
  const time = el('span');
  time.textContent = airTimeText(ep);
  const change = H.airChangeBadge(ep);
  if (change) time.appendChild(change);
  const line = el('div', 'card2-line');
  line.append(time, badge);
  frost.append(title, line);
  if (ep.title) {
    const epTitle = el('div', 'mt-ep-title');
    epTitle.textContent = ep.title;
    frost.appendChild(epTitle);
  }
  box.appendChild(frost);

  tile.append(box, strip);
  return tile;
}
