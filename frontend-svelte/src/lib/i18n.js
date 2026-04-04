import { addMessages, init, getLocaleFromNavigator, locale } from 'svelte-i18n';
import { api } from './api.js';
import en from '../locales/en.json';
import es from '../locales/es.json';
import ru from '../locales/ru.json';
import uk from '../locales/uk.json';
import ja from '../locales/ja.json';
import zh from '../locales/zh.json';
import ko from '../locales/ko.json';

export const SUPPORTED_LOCALES = [
    { code: 'en', label: 'English' },
    { code: 'es', label: 'Español' },
    { code: 'ru', label: 'Русский' },
    { code: 'uk', label: 'Українська' },
    { code: 'ja', label: '日本語' },
    { code: 'zh', label: '中文' },
    { code: 'ko', label: '한국어' },
];

const STORAGE_KEY = 'pinkybot_locale';

function getSupportedCode(raw) {
    if (!raw) return null;
    const lower = raw.toLowerCase();
    // exact match first
    if (SUPPORTED_LOCALES.some((l) => l.code === lower)) return lower;
    // language prefix match (e.g. "ru-RU" → "ru")
    const prefix = lower.split('-')[0];
    if (SUPPORTED_LOCALES.some((l) => l.code === prefix)) return prefix;
    return null;
}

export function setupI18n() {
    addMessages('en', en);
    addMessages('ru', ru);

    const stored = getSupportedCode(localStorage.getItem(STORAGE_KEY));
    const browser = getSupportedCode(getLocaleFromNavigator());
    const initialLocale = stored || browser || 'en';

    init({
        fallbackLocale: 'en',
        initialLocale,
    });
}

export function setLocale(code) {
    locale.set(code);
    localStorage.setItem(STORAGE_KEY, code);
}
