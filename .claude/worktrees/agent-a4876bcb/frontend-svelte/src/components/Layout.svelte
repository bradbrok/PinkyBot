<script>
    import { onMount, onDestroy } from 'svelte';
    import { writable } from 'svelte/store';
    import Toast from './Toast.svelte';
    import { api } from '../lib/api.js';

    let statusText = 'connecting...';
    const currentPath = writable(window.location.hash.replace('#', '') || '/');

    function updatePath() {
        currentPath.set(window.location.hash.replace('#', '') || '/');
    }

    const navLinks = [
        { path: '/', label: 'Dashboard' },
        { path: '/chat', label: 'Chat' },
        { path: '/fleet', label: 'Fleet' },
        { path: '/agents', label: 'Agents' },
        { path: '/memories', label: 'Memories' },
        { path: '/research', label: 'Research' },
        { path: '/tasks', label: 'Tasks' },
        { path: '/settings', label: 'Settings' },
    ];

    onMount(async () => {
        window.addEventListener('hashchange', updatePath);
        try {
            const root = await api('GET', '/api');
            statusText = `connected | v${root.version} | Claude ${root.claude_version || '?'}`;
        } catch {
            statusText = 'disconnected';
        }
    });

    onDestroy(() => {
        window.removeEventListener('hashchange', updatePath);
    });

    function isActive(linkPath, loc) {
        if (linkPath === '/' && (loc === '/' || loc === '/dashboard')) return true;
        if (linkPath !== '/' && loc.startsWith(linkPath)) return true;
        return false;
    }
</script>

<div class="header">
    <a href="#/" class="header-logo">PINKY<span class="accent">.</span></a>
    <nav class="header-nav">
        {#each navLinks as link}
            <a href="#{link.path}" class:active={isActive(link.path, $currentPath)}>{link.label}</a>
        {/each}
    </nav>
    <div class="header-status">{statusText}</div>
</div>

<slot />

<Toast />

<style>
    .header {
        flex-wrap: wrap;
    }
    .header-nav {
        overflow-x: auto;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: none;
        flex-wrap: nowrap;
        white-space: nowrap;
    }
    .header-nav::-webkit-scrollbar {
        display: none;
    }
    @media (max-width: 768px) {
        .header {
            padding: 0.8rem 1rem;
            gap: 0.3rem;
        }
        .header-nav {
            order: 3;
            width: 100%;
            gap: 1rem;
            font-size: 0.7rem;
            padding-bottom: 0.3rem;
        }
        .header-status {
            order: 2;
        }
    }
</style>
