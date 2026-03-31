import { mount } from 'svelte';
import './app.css';
import App from './App.svelte';
import { initializeTheme } from './lib/theme.js';

initializeTheme();

const app = mount(App, {
    target: document.getElementById('app'),
});

export default app;
