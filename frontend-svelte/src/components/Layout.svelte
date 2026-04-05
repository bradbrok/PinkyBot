<script>
    import { onMount, onDestroy } from 'svelte';
    import { writable } from 'svelte/store';
    import Toast from './Toast.svelte';
    import { api } from '../lib/api.js';
    import { cycleThemeMode, resolvedTheme } from '../lib/theme.js';
    import { _ } from 'svelte-i18n';


    let statusText = 'connecting...';
    let authenticated = false;
    let agents = [];
    let sidebarOpen = false;
    const currentPath = writable(window.location.hash.replace('#', '') || '/');

    function updatePath() {
        currentPath.set(window.location.hash.replace('#', '') || '/');
    }

    const navLinks = [
        { path: '/', key: 'nav.dashboard', icon: 'dashboard' },
        { path: '/chat', key: 'nav.chat', icon: 'chat' },
        { path: '/agents', key: 'nav.agents', icon: 'smart_toy' },
        // Fleet consolidated into Agents
        { path: '/tasks', key: 'nav.tasks', icon: 'task_alt' },
        { path: '/research', key: 'nav.research', icon: 'science' },
        { path: '/presentations', key: 'nav.presentations', icon: 'present_to_all' },
        { path: '/people', key: 'nav.people', icon: 'people' },
        { path: '/memories', key: 'nav.memories', icon: 'psychology' },
        { path: '/settings', key: 'nav.settings', icon: 'settings' },
    ];

    onMount(async () => {
        window.addEventListener('hashchange', updatePath);
        try {
            const [root, auth, agentsResp] = await Promise.all([
                api('GET', '/api'),
                api('GET', '/auth/status'),
                api('GET', '/agents').catch(() => []),
            ]);
            statusText = `v${root.version}`;
            authenticated = !!auth.authenticated;
            if (Array.isArray(agentsResp)) agents = agentsResp;

            // First-run: redirect to onboarding if no agents and not yet completed
            if (authenticated) {
                try {
                    const obs = await api('GET', '/system/onboarding-status');
                    if (!obs.onboarding_completed && !obs.has_agents) {
                        const cur = window.location.hash.replace('#', '') || '/';
                        if (cur !== '/onboarding') window.location.hash = '#/onboarding';
                    }
                } catch { /* non-critical */ }
            }
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

    async function logout() {
        try {
            await api('POST', '/auth/logout');
        } finally {
            window.location.href = '/login';
        }
    }

    function toggleSidebar() {
        sidebarOpen = !sidebarOpen;
    }

    function closeSidebar() {
        sidebarOpen = false;
    }
</script>

<!-- Mobile hamburger -->
<button class="mobile-hamburger" on:click={toggleSidebar} aria-label="Toggle navigation">
    <span class="material-symbols-outlined">menu</span>
</button>

<!-- App Shell -->
<div class="app-shell">
    <!-- Mobile overlay -->
    {#if sidebarOpen}
        <div class="sidebar-overlay" on:click={closeSidebar} on:keydown={closeSidebar} role="presentation"></div>
    {/if}

    <!-- Sidebar -->
    <aside class="sidebar" class:open={sidebarOpen}>
        <div class="sidebar-inner">
            <!-- Branding -->
            <div class="sidebar-brand">
                <div class="sidebar-brand-name">PinkyBot AI</div>
                <div class="sidebar-brand-version">{statusText}</div>
            </div>

            <!-- Navigation -->
            <nav class="sidebar-nav">
                <div class="sidebar-group-label">{$_('nav.label')}</div>
                {#each navLinks as link}
                    <a
                        href="#{link.path}"
                        class="sidebar-nav-item"
                        class:active={isActive(link.path, $currentPath)}
                        on:click={closeSidebar}
                    >
                        <span class="material-symbols-outlined sidebar-icon">{link.icon}</span>
                        <span>{$_(link.key)}</span>
                    </a>
                {/each}
            </nav>

            <!-- Agents list -->
            {#if agents.length > 0}
                <div class="sidebar-nav">
                    <div class="sidebar-group-label">{$_('nav.active_agents')}</div>
                    {#each agents as agent}
                        <a
                            href="#/chat"
                            class="sidebar-nav-item sidebar-agent"
                            on:click={closeSidebar}
                        >
                            <span class="agent-dot" class:alive={agent.status === 'running'} class:idle={agent.status !== 'running'}></span>
                            <span>{agent.name}</span>
                        </a>
                    {/each}
                </div>
            {/if}

            <!-- Footer -->
            <div class="sidebar-footer">
                <div class="sidebar-actions">
                    <button
                        class="icon-btn"
                        on:click={cycleThemeMode}
                        title={$resolvedTheme === 'dark' ? 'Switch to light' : 'Switch to dark'}
                        aria-label="Toggle theme"
                    >
                        <span class="material-symbols-outlined">{$resolvedTheme === 'dark' ? 'light_mode' : 'dark_mode'}</span>
                    </button>
                    {#if authenticated}
                        <button class="icon-btn" on:click={logout} title="Logout">
                            <span class="material-symbols-outlined">logout</span>
                        </button>
                    {/if}
                    <span class="sidebar-status">{statusText}</span>
                </div>
            </div>
        </div>
    </aside>

    <!-- Main content -->
    <main class="main-content">
        <slot />
    </main>
</div>

<Toast />

<style>
    /* Mobile hamburger — only visible on small screens */
    .mobile-hamburger {
        display: none;
        position: fixed;
        top: 0.6rem;
        left: 0.6rem;
        z-index: 50;
        background: var(--surface-1);
        border: none;
        border-radius: var(--radius-lg);
        color: var(--text-primary);
        cursor: pointer;
        padding: 0.4rem;
        box-shadow: 0 0 20px var(--shadow-color);
    }
    .mobile-hamburger:hover { background: var(--primary-container); color: #000; }

    /* Icon buttons */
    .icon-btn {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 2rem;
        height: 2rem;
        border: none;
        border-radius: var(--radius-lg);
        background: var(--surface-2);
        color: var(--text-muted);
        cursor: pointer;
        transition: all 0.15s;
    }
    .icon-btn .material-symbols-outlined { font-size: 18px; }
    .icon-btn:hover {
        background: var(--primary-container);
        color: #000;
    }

    /* App shell layout — full viewport, no header */
    .app-shell {
        display: flex;
        min-height: 100vh;
    }

    /* Sidebar overlay (mobile) */
    .sidebar-overlay {
        display: none;
        position: fixed;
        inset: 0;
        background: var(--overlay-scrim);
        z-index: 40;
    }

    /* Sidebar — full height */
    .sidebar {
        width: 240px;
        background: var(--surface-1);
        flex-shrink: 0;
        position: sticky;
        top: 0;
        height: 100vh;
        overflow-y: auto;
        z-index: 45;
        scrollbar-width: thin;
    }
    .sidebar-inner {
        display: flex;
        flex-direction: column;
        height: 100%;
        padding: 1.25rem 0;
    }
    .sidebar-brand {
        padding: 0 1.25rem 1.25rem;
    }
    .sidebar-brand-name {
        font-family: var(--font-grotesk);
        font-weight: 900;
        font-size: 1.15rem;
        color: var(--text-primary);
    }
    .sidebar-brand-version {
        font-size: 0.65rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.15em;
        color: var(--text-subtle);
    }

    /* Sidebar navigation */
    .sidebar-nav {
        padding: 0 0.75rem;
        margin-bottom: 1.5rem;
    }
    .sidebar-group-label {
        font-family: var(--font-grotesk);
        font-size: 0.6rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.2em;
        color: var(--text-subtle);
        padding: 0 0.5rem 0.5rem;
    }
    .sidebar-nav-item {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        padding: 0.5rem 0.6rem;
        border-radius: var(--radius-lg);
        font-family: var(--font-body);
        font-size: 0.85rem;
        font-weight: 500;
        color: var(--text-muted);
        transition: all 0.12s;
        text-decoration: none;
    }
    .sidebar-nav-item:hover {
        background: var(--surface-2);
        color: var(--text-primary);
    }
    .sidebar-nav-item.active {
        background: var(--primary-container);
        color: var(--on-primary-container);
        font-weight: 700;
    }
    .sidebar-icon {
        font-size: 20px;
    }

    /* Sidebar agents */
    .sidebar-agent {
        font-size: 0.82rem;
    }
    .agent-dot {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .agent-dot.alive { background: var(--green); }
    .agent-dot.idle { background: var(--text-subtle); }

    /* Sidebar footer */
    .sidebar-footer {
        margin-top: auto;
        padding: 1rem 0.75rem 0;
    }
    .sidebar-actions {
        display: flex;
        align-items: center;
        gap: 0.4rem;
        padding: 0.4rem 0;
    }
    .sidebar-status {
        font-family: var(--font-grotesk);
        font-size: 0.65rem;
        color: var(--text-subtle);
        margin-left: auto;
    }

    /* Main content */
    .main-content {
        flex: 1;
        min-width: 0;
        overflow-x: hidden;
    }

    /* Responsive */
    @media (max-width: 1024px) {
        .mobile-hamburger { display: flex; }

        .sidebar {
            position: fixed;
            top: 0;
            left: 0;
            height: 100vh;
            transform: translateX(-100%);
            transition: transform 0.25s ease;
        }
        .sidebar.open {
            transform: translateX(0);
        }
        .sidebar-overlay {
            display: block;
        }
    }
</style>
