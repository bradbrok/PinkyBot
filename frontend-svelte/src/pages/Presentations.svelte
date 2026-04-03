<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let presentations = [];
    let loading = true;
    let selected = null;
    let versions = [];
    let versionContent = null;
    let viewingVersion = null;
    let shareUrl = '';
    let showVersions = false;
    let refreshInterval;

    $: displayHtml = versionContent ?? selected?.html_content ?? '';

    async function load() {
        try {
            const data = await api('GET', '/presentations');
            presentations = data.presentations ?? [];
        } catch (e) {
            toast(`Failed to load presentations: ${e.message}`, 'error');
        } finally {
            loading = false;
        }
    }

    async function selectPresentation(p) {
        try {
            const [detail, vers, share] = await Promise.all([
                api('GET', `/presentations/${p.id}`),
                api('GET', `/presentations/${p.id}/versions`),
                api('GET', `/presentations/${p.id}/share-link`),
            ]);
            selected = detail;
            versions = vers.versions ?? [];
            shareUrl = share.url ?? '';
            versionContent = null;
            viewingVersion = null;
            showVersions = false;
        } catch (e) {
            toast(`Failed to load presentation: ${e.message}`, 'error');
        }
    }

    function back() {
        selected = null;
        versions = [];
        versionContent = null;
        viewingVersion = null;
        shareUrl = '';
        showVersions = false;
    }

    async function viewVersion(v) {
        try {
            const data = await api('GET', `/presentations/${selected.id}/versions/${v.version}`);
            versionContent = data.html_content ?? '';
            viewingVersion = v.version;
        } catch (e) {
            toast(`Failed to load version: ${e.message}`, 'error');
        }
    }

    async function restoreVersion(v) {
        try {
            await api('POST', `/presentations/${selected.id}/restore`, { version: v.version });
            toast(`Restored to v${v.version}`);
            await selectPresentation(selected);
        } catch (e) {
            toast(`Restore failed: ${e.message}`, 'error');
        }
    }

    function copyShareLink() {
        navigator.clipboard.writeText(shareUrl).then(() => {
            toast('Share link copied');
        }).catch(() => {
            toast('Failed to copy link', 'error');
        });
    }

    onMount(() => {
        load();
        refreshInterval = setInterval(load, 30000);
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
    });
</script>

<div class="content">
    {#if !selected}
        <!-- Gallery view -->
        <div class="section">
            <div class="section-header">
                <span class="section-title">Presentations</span>
                <span class="badge" style="background: var(--surface-3); color: var(--text-muted);">
                    {presentations.length}
                </span>
            </div>
            <div class="section-body">
                {#if loading}
                    <div class="empty">Loading...</div>
                {:else if presentations.length === 0}
                    <div class="empty">No presentations yet.</div>
                {:else}
                    <div class="pres-grid">
                        {#each presentations as p}
                            <button class="pres-card" on:click={() => selectPresentation(p)}>
                                <div class="pres-card-header">
                                    <span class="pres-title">{p.title}</span>
                                    <span class="badge" style="background: var(--primary-container); font-family: var(--font-mono, monospace); font-size: 0.7rem;">
                                        v{p.current_version}
                                    </span>
                                </div>
                                {#if p.description}
                                    <div class="pres-desc">{p.description}</div>
                                {/if}
                                {#if p.tags && p.tags.length}
                                    <div class="pres-tags">
                                        {#each p.tags as tag}
                                            <span class="badge" style="background: var(--surface-3); color: var(--text-muted); font-size: 0.68rem;">{tag}</span>
                                        {/each}
                                    </div>
                                {/if}
                                <div class="pres-meta">
                                    <span style="color: var(--text-muted); font-size: 0.75rem;">{p.created_by}</span>
                                    <span style="color: var(--text-muted); font-size: 0.75rem;">{timeAgo(p.updated_at)}</span>
                                </div>
                            </button>
                        {/each}
                    </div>
                {/if}
            </div>
        </div>
    {:else}
        <!-- Detail view -->
        <div class="section">
            <div class="section-header">
                <button class="btn btn-sm" on:click={back} style="margin-right: 0.5rem;">← Back</button>
                <span class="section-title">{selected.title}</span>
            </div>
            <div class="section-body">
                {#if viewingVersion !== null}
                    <div class="version-banner">
                        Viewing v{viewingVersion} — not the current version
                        <button class="btn btn-sm btn-primary" style="margin-left: 0.75rem;" on:click={() => restoreVersion({ version: viewingVersion })}>
                            Restore This
                        </button>
                        <button class="btn btn-sm" style="margin-left: 0.5rem;" on:click={() => { versionContent = null; viewingVersion = null; }}>
                            Back to Current
                        </button>
                    </div>
                {/if}

                <div class="detail-layout">
                    <!-- Left: iframe preview -->
                    <div class="detail-preview">
                        <iframe
                            srcdoc={displayHtml}
                            sandbox="allow-scripts"
                            title={selected.title}
                            class="pres-iframe"
                        ></iframe>
                    </div>

                    <!-- Right: sidebar -->
                    <div class="detail-sidebar">
                        <div class="sidebar-meta">
                            <div class="inline-spread" style="align-items: flex-start; flex-wrap: wrap; gap: 0.4rem;">
                                <h2 class="pres-detail-title">{selected.title}</h2>
                                <span class="badge" style="background: var(--primary-container); font-family: var(--font-mono, monospace); font-size: 0.75rem; white-space: nowrap;">
                                    v{selected.current_version}
                                </span>
                            </div>
                            {#if selected.description}
                                <p class="pres-detail-desc">{selected.description}</p>
                            {/if}
                            {#if selected.tags && selected.tags.length}
                                <div class="pres-tags" style="margin-top: 0.4rem;">
                                    {#each selected.tags as tag}
                                        <span class="badge" style="background: var(--surface-3); color: var(--text-muted); font-size: 0.7rem;">{tag}</span>
                                    {/each}
                                </div>
                            {/if}
                            <div style="margin-top: 0.5rem; font-size: 0.75rem; color: var(--text-muted);">
                                by {selected.created_by}
                            </div>
                        </div>

                        <hr class="sidebar-divider" />

                        <!-- Share section -->
                        <div class="sidebar-section">
                            <div class="sidebar-section-title">Share</div>
                            <div class="share-actions">
                                <button class="btn btn-sm btn-primary" on:click={copyShareLink}>Copy Link</button>
                                <a class="btn btn-sm" href={shareUrl} target="_blank" rel="noopener noreferrer">Open in New Tab</a>
                            </div>
                            {#if shareUrl}
                                <div class="share-url">{shareUrl}</div>
                            {/if}
                        </div>

                        <hr class="sidebar-divider" />

                        <!-- Version history -->
                        <div class="sidebar-section">
                            <button
                                class="btn btn-sm"
                                style="width: 100%; text-align: left; justify-content: space-between; display: flex;"
                                on:click={() => showVersions = !showVersions}
                            >
                                <span>Version History</span>
                                <span>{showVersions ? '▲' : '▼'}</span>
                            </button>
                            {#if showVersions}
                                <div class="version-list">
                                    {#if versions.length === 0}
                                        <div class="empty" style="padding: 0.5rem 0; font-size: 0.8rem;">No versions found.</div>
                                    {:else}
                                        {#each versions as v}
                                            <div class="version-row" class:version-row-active={viewingVersion === v.version}>
                                                <div class="version-info">
                                                    <span class="version-num" class:version-current={v.version === selected.current_version}>
                                                        v{v.version}
                                                        {#if v.version === selected.current_version}<span style="color: var(--green); margin-left: 0.25rem;">●</span>{/if}
                                                    </span>
                                                    {#if v.description}
                                                        <span class="version-desc">{v.description}</span>
                                                    {/if}
                                                    <span class="version-meta">{v.created_by} · {timeAgo(v.created_at)}</span>
                                                </div>
                                                <div class="version-btns">
                                                    <button class="btn btn-sm" on:click={() => viewVersion(v)}>View</button>
                                                    {#if v.version !== selected.current_version}
                                                        <button class="btn btn-sm btn-danger" on:click={() => restoreVersion(v)}>Restore</button>
                                                    {/if}
                                                </div>
                                            </div>
                                        {/each}
                                    {/if}
                                </div>
                            {/if}
                        </div>
                    </div>
                </div>
            </div>
        </div>
    {/if}
</div>

<style>
    .pres-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 1rem;
    }

    .pres-card {
        background: var(--surface-2);
        border: 1px solid var(--surface-3);
        border-radius: var(--radius-lg);
        padding: 1rem;
        text-align: left;
        cursor: pointer;
        transition: border-color 0.15s, background 0.15s;
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        color: var(--text-primary);
        font-family: var(--font-body);
        width: 100%;
    }

    .pres-card:hover {
        border-color: var(--yellow);
        background: var(--surface-3);
    }

    .pres-card-header {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 0.5rem;
    }

    .pres-title {
        font-family: var(--font-grotesk);
        font-size: 0.95rem;
        font-weight: 600;
        color: var(--text-primary);
        line-height: 1.3;
    }

    .pres-desc {
        font-size: 0.8rem;
        color: var(--text-muted);
        line-height: 1.4;
        display: -webkit-box;
        -webkit-line-clamp: 3;
        -webkit-box-orient: vertical;
        overflow: hidden;
    }

    .pres-tags {
        display: flex;
        flex-wrap: wrap;
        gap: 0.3rem;
    }

    .pres-meta {
        display: flex;
        justify-content: space-between;
        align-items: center;
        margin-top: auto;
        padding-top: 0.25rem;
    }

    /* Detail layout */
    .version-banner {
        background: var(--surface-3);
        border: 1px solid var(--yellow);
        border-radius: var(--radius);
        padding: 0.5rem 0.75rem;
        font-size: 0.82rem;
        color: var(--yellow);
        display: flex;
        align-items: center;
        margin-bottom: 1rem;
        flex-wrap: wrap;
        gap: 0.25rem;
    }

    .detail-layout {
        display: flex;
        gap: 1rem;
        align-items: flex-start;
    }

    .detail-preview {
        flex: 0 0 65%;
        min-width: 0;
    }

    .pres-iframe {
        width: 100%;
        height: calc(100vh - 160px);
        border: 1px solid var(--surface-3);
        border-radius: var(--radius);
        background: #fff;
    }

    .detail-sidebar {
        flex: 0 0 35%;
        min-width: 0;
        display: flex;
        flex-direction: column;
        gap: 0;
        background: var(--surface-2);
        border: 1px solid var(--surface-3);
        border-radius: var(--radius-lg);
        padding: 1rem;
    }

    .sidebar-meta {
        margin-bottom: 0.25rem;
    }

    .pres-detail-title {
        font-family: var(--font-grotesk);
        font-size: 1rem;
        font-weight: 700;
        color: var(--text-primary);
        margin: 0;
        line-height: 1.3;
    }

    .pres-detail-desc {
        font-size: 0.82rem;
        color: var(--text-muted);
        line-height: 1.4;
        margin: 0.4rem 0 0;
    }

    .sidebar-divider {
        border: none;
        border-top: 1px solid var(--surface-3);
        margin: 0.75rem 0;
    }

    .sidebar-section {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
    }

    .sidebar-section-title {
        font-size: 0.75rem;
        font-weight: 600;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    .share-actions {
        display: flex;
        gap: 0.5rem;
        flex-wrap: wrap;
    }

    .share-url {
        font-family: var(--font-mono, monospace);
        font-size: 0.7rem;
        color: var(--text-muted);
        word-break: break-all;
        background: var(--surface-3);
        border-radius: var(--radius);
        padding: 0.35rem 0.5rem;
    }

    .version-list {
        display: flex;
        flex-direction: column;
        gap: 0.4rem;
        margin-top: 0.25rem;
    }

    .version-row {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 0.5rem;
        padding: 0.4rem 0.5rem;
        border-radius: var(--radius);
        background: var(--surface-1);
        border: 1px solid transparent;
    }

    .version-row-active {
        border-color: var(--yellow);
    }

    .version-info {
        display: flex;
        flex-direction: column;
        gap: 0.15rem;
        min-width: 0;
    }

    .version-num {
        font-family: var(--font-mono, monospace);
        font-size: 0.78rem;
        font-weight: 600;
        color: var(--text-primary);
    }

    .version-desc {
        font-size: 0.75rem;
        color: var(--text-muted);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
    }

    .version-meta {
        font-size: 0.7rem;
        color: var(--text-muted);
    }

    .version-btns {
        display: flex;
        gap: 0.3rem;
        flex-shrink: 0;
        align-items: flex-start;
        padding-top: 0.1rem;
    }
</style>
