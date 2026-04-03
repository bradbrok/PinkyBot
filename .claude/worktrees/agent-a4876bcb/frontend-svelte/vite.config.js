import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
    plugins: [svelte()],
    server: {
        proxy: {
            '/api': 'http://localhost:8888',
            '/agents': 'http://localhost:8888',
            '/sessions': 'http://localhost:8888',
            '/tasks': 'http://localhost:8888',
            '/projects': 'http://localhost:8888',
            '/skills': 'http://localhost:8888',
            '/outreach': 'http://localhost:8888',
            '/audit': 'http://localhost:8888',
            '/activity': 'http://localhost:8888',
            '/schedules': 'http://localhost:8888',
            '/autonomy': 'http://localhost:8888',
            '/groups': 'http://localhost:8888',
            '/hooks': 'http://localhost:8888',
            '/scheduler': 'http://localhost:8888',
            '/conversations': 'http://localhost:8888',
            '/heartbeats': 'http://localhost:8888',
        }
    },
    build: {
        outDir: '../frontend-dist',
    }
});
