<script>
    import { onMount, onDestroy } from 'svelte';
    import { writable } from 'svelte/store';
    import Toast from './Toast.svelte';
    import { api } from '../lib/api.js';
    import { cycleThemeMode, resolvedTheme } from '../lib/theme.js';

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
    <div class="header-controls">
        <button
            class="theme-toggle"
            on:click={cycleThemeMode}
            title={$resolvedTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
            aria-label={$resolvedTheme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'}
        >
            {$resolvedTheme === 'dark' ? '☀' : '☾'}
        </button>
        <div class="header-status">{statusText}</div>
    </div>
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
    .header-controls {
        display: flex;
        align-items: center;
        gap: 0.8rem;
        margin-left: auto;
    }
    .theme-toggle {
        font-family: var(--font-mono);
        font-size: 1rem;
        font-weight: 700;
        line-height: 1;
        width: 2.25rem;
        height: 2.25rem;
        border: 2px solid var(--border-strong);
        background: var(--surface-2);
        color: var(--text-primary);
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        justify-content: center;
    }
    .theme-toggle:hover {
        background: var(--accent);
        color: var(--accent-contrast);
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
        .header-controls {
            order: 2;
            width: 100%;
            justify-content: space-between;
            margin-left: 0;
        }
        .header-status {
            order: 2;
        }
    }
</style>
