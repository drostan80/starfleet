/**
 * Shared "show watched" toggle — persisted in localStorage, used by
 * pages with episode-level cards to hide/show watched episodes.
 */

const WATCHED_TOGGLE_KEY = 'starfleet_show_watched';

export function loadShowWatched() {
  try {
    const v = localStorage.getItem(WATCHED_TOGGLE_KEY);
    if (v !== null) return v !== 'false';
  } catch {}
  return true; // default: show watched
}

export function saveShowWatched(show) {
  try { localStorage.setItem(WATCHED_TOGGLE_KEY, String(show)); } catch {}
}

/**
 * Build the toggle element. Returns the wrapper <label>.
 * @param {object} opts
 * @param {boolean} opts.disabled - render greyed-out / non-interactive
 * @param {function} opts.onChange - called with (showWatched: boolean) on toggle
 */
export function buildWatchedToggle({ disabled = false, onChange } = {}) {
  const label = document.createElement('label');
  label.className = 'wt-toggle' + (disabled ? ' wt-disabled' : '');

  const text = document.createElement('span');
  text.className = 'wt-label';
  text.textContent = 'Watched';

  const track = document.createElement('span');
  track.className = 'wt-track';

  const thumb = document.createElement('span');
  thumb.className = 'wt-thumb';
  track.appendChild(thumb);

  const input = document.createElement('input');
  input.type = 'checkbox';
  input.className = 'wt-input';
  input.checked = loadShowWatched();
  if (disabled) input.disabled = true;

  label.appendChild(text);
  label.appendChild(track);
  label.appendChild(input);

  if (!disabled) {
    label.classList.toggle('wt-off', !input.checked);
    input.addEventListener('change', () => {
      const on = input.checked;
      saveShowWatched(on);
      label.classList.toggle('wt-off', !on);
      if (onChange) onChange(on);
    });
  }

  return label;
}
