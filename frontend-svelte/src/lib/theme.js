import { get, writable } from 'svelte/store';

export const THEME_STORAGE_KEY = 'pinky-theme';
export const THEME_OPTIONS = ['system', 'light', 'dark'];

export const themeMode = writable('system');
export const resolvedTheme = writable('light');

let mediaQuery;
let mediaHandler;

function isBrowser() {
    return typeof window !== 'undefined' && typeof document !== 'undefined';
}

function normalizeMode(mode) {
    return THEME_OPTIONS.includes(mode) ? mode : null;
}

function resolveMode(mode) {
    if (!isBrowser()) return 'light';
    if (mode === 'system') {
        return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light';
    }
    return mode;
}

function detachSystemListener() {
    if (!mediaQuery || !mediaHandler) return;
    mediaQuery.removeEventListener('change', mediaHandler);
    mediaHandler = null;
}

function attachSystemListener(mode) {
    if (!isBrowser()) return;
    mediaQuery = window.matchMedia('(prefers-color-scheme: dark)');
    detachSystemListener();

    if (mode !== 'system') return;

    mediaHandler = () => {
        const nextResolved = resolveMode('system');
        document.documentElement.dataset.theme = nextResolved;
        document.documentElement.style.colorScheme = nextResolved;
        resolvedTheme.set(nextResolved);
    };
    mediaQuery.addEventListener('change', mediaHandler);
}

function applyTheme(mode, persist = true) {
    if (!isBrowser()) return;

    const normalized = normalizeMode(mode) || 'system';
    const nextResolved = resolveMode(normalized);

    themeMode.set(normalized);
    resolvedTheme.set(nextResolved);
    document.documentElement.dataset.theme = nextResolved;
    document.documentElement.style.colorScheme = nextResolved;
    attachSystemListener(normalized);

    if (persist) {
        window.localStorage.setItem(THEME_STORAGE_KEY, normalized);
    }
}

export function initializeTheme() {
    if (!isBrowser()) return;

    const stored = normalizeMode(window.localStorage.getItem(THEME_STORAGE_KEY));
    const initial = stored || 'system';
    applyTheme(initial, false);
}

export function setThemeMode(mode) {
    applyTheme(mode, true);
}

export function cycleThemeMode() {
    const current = get(resolvedTheme);
    const next = current === 'dark' ? 'light' : 'dark';
    applyTheme(next, true);
}
