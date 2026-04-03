// Theme state — dark (default) / light
// Persists to localStorage, applies data-theme attribute to <html>

let _theme = $state('dark');

function _apply(t) {
  document.documentElement.setAttribute('data-theme', t);
}

function init() {
  const saved = localStorage.getItem('pinky_theme') || 'dark';
  _theme = saved;
  _apply(saved);
}

function getTheme() {
  return _theme;
}

function toggleTheme() {
  const next = _theme === 'dark' ? 'light' : 'dark';
  _theme = next;
  localStorage.setItem('pinky_theme', next);
  _apply(next);
}

export { getTheme, toggleTheme, init };
