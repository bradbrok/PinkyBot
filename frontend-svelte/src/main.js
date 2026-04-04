import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { initializeTheme } from './lib/theme.js';
import { setupI18n } from './lib/i18n.js';

initializeTheme();
setupI18n();

const app = mount(App, {
    target: document.getElementById('app'),
});

export default app;
