/**
 * status-picker.js — Reusable radial "flower" status picker.
 *
 * Extracted from calendar.js.  Configurable status list (5 or 6 petals)
 * and injectable click handler so browse, calendar, and show pages can
 * all share the same widget.
 */

/* ── Shared palette ─────────────────────────────────────── */

export const STATUSES_5 = ['WATCHING', 'COMPLETED', 'PLANNED', 'PAUSED', 'DROPPED'];
export const STATUSES_6 = ['WATCHING', 'COMPLETED', 'PLANNED', 'PAUSED', 'DROPPED', 'SKIPPED'];

export const STATUS_LABELS = {
  WATCHING: 'Watching', COMPLETED: 'Completed', PLANNED: 'Planned',
  PAUSED: 'Paused', DROPPED: 'Dropped', SKIPPED: 'Skipped',
  UNTRACKED: 'Not tracked',
};

export const STATUS_ICONS = {
  WATCHING: '👁', COMPLETED: '✓', PLANNED: '◷',
  PAUSED: '⏸', DROPPED: '✕', SKIPPED: '⊘',
  UNTRACKED: '＋',
};

export const STATUS_ICON_CLASS = {
  WATCHING: 'si-watching', COMPLETED: 'si-completed',
  PLANNED: 'si-planning', PAUSED: 'si-paused',
  DROPPED: 'si-dropped', SKIPPED: 'si-skipped',
  UNTRACKED: 'si-untracked',
};

export const STATUS_CLASS = {
  WATCHING: 'st-watching', COMPLETED: 'st-completed',
  PLANNED: 'st-planning', PAUSED: 'st-paused',
  DROPPED: 'st-dropped', SKIPPED: 'st-skipped',
};

export const STATUS_COLOR = {
  WATCHING: 'var(--st-watching)', COMPLETED: 'var(--st-completed)',
  PLANNED: 'var(--st-planning)', PAUSED: 'var(--st-paused)',
  DROPPED: 'var(--st-dropped)', SKIPPED: 'var(--faint)',
  UNTRACKED: 'var(--muted)',
};

/**
 * Compute evenly-spaced petal angles over a 130° arc, left-biased
 * above the button.  Works for any petal count.
 */
function computeAngles(count) {
  const ARC = 130;      // total arc width in degrees
  const START = -85;     // leftmost angle
  const step = count > 1 ? ARC / (count - 1) : 0;
  return Array.from({ length: count }, (_, i) => START + step * i);
}

/* ── Build the radial status button ─────────────────────── */

/**
 * Build a compact status icon button with a semicircle hover-picker.
 *
 * @param {object} opts
 * @param {string}   opts.id             - entity id (show / season)
 * @param {string}   opts.currentStatus  - one of the statuses array
 * @param {string[]} [opts.statuses]     - status list (default: STATUSES_5)
 * @param {function} [opts.onPick]       - (wrap, id, newStatus) => void
 * @returns {HTMLElement}  .status-btn-wrap
 */
export function buildStatusBtn({
  id,
  currentStatus,
  statuses = STATUSES_5,
  onPick = null,
}) {
  const angles = computeAngles(statuses.length);
  const wrap = document.createElement('div');
  wrap.className = 'status-btn-wrap';
  wrap.dataset.showId = id;
  wrap.dataset.status = currentStatus;

  // Main icon button
  const mainBtn = document.createElement('button');
  mainBtn.className = `status-icon-btn ${STATUS_ICON_CLASS[currentStatus] || 'si-watching'}`;
  mainBtn.title = STATUS_LABELS[currentStatus] || currentStatus;
  mainBtn.textContent = STATUS_ICONS[currentStatus] || '●';
  wrap.appendChild(mainBtn);

  // Open/close logic
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
    if (_moveHandler) return;
    _moveHandler = e => {
      const r  = wrap.getBoundingClientRect();
      const cx = r.left + r.width  / 2;
      const cy = r.top  + r.height / 2;
      if (Math.hypot(e.clientX - cx, e.clientY - cy) > 95) closeRadial();
    };
    document.addEventListener('mousemove', _moveHandler);
  };

  wrap.addEventListener('mouseenter', openRadial);

  // Radial picker petals
  for (let i = 0; i < statuses.length; i++) {
    const s = statuses[i];
    const opt = document.createElement('button');
    opt.className = `sr-opt ${STATUS_ICON_CLASS[s] || ''}${s === currentStatus ? ' cur' : ''}`;
    opt.style.setProperty('--a', `${angles[i]}deg`);
    opt.title = STATUS_LABELS[s] || s;
    opt.textContent = STATUS_ICONS[s] || '●';
    opt.dataset.status = s;
    opt.addEventListener('click', e => {
      e.stopPropagation();
      if (onPick) onPick(wrap, id, s);
    });
    wrap.appendChild(opt);
  }

  return wrap;
}

/**
 * Refresh an existing status-btn-wrap after a status change.
 * Updates main button icon/class and petal highlights.
 *
 * @param {HTMLElement} wrap        - .status-btn-wrap element
 * @param {string}     newStatus   - the new current status
 * @param {string[]}   [statuses]  - same status list used to build
 */
export function refreshStatusBtn(wrap, newStatus, statuses = STATUSES_5) {
  wrap.dataset.status = newStatus;

  const mainBtn = wrap.querySelector('.status-icon-btn');
  if (mainBtn) {
    for (const s of statuses) mainBtn.classList.remove(STATUS_ICON_CLASS[s]);
    mainBtn.classList.add(STATUS_ICON_CLASS[newStatus] || 'si-watching');
    mainBtn.textContent = STATUS_ICONS[newStatus] || '●';
    mainBtn.title = STATUS_LABELS[newStatus] || newStatus;
  }

  wrap.querySelectorAll('.sr-opt').forEach(opt => {
    opt.classList.toggle('cur', opt.dataset.status === newStatus);
  });
}
