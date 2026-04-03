import { vitePreprocess } from '@sveltejs/vite-plugin-svelte';

/** @type {import("@sveltejs/vite-plugin-svelte").SvelteConfig} */
export default {
    preprocess: vitePreprocess(),
    onwarn: (warning, handler) => {
        // Suppress a11y warnings for this migration - original pages didn't have them
        if (warning.code && warning.code.startsWith('a11y')) return;
        handler(warning);
    },
};
