<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { escapeHtml } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let agentList = [];
    let currentAgent = '';
    let memories = [];
    let totalCount = 0;
    let currentOffset = 0;
    const PAGE_SIZE = 24;
    let isSearchMode = false;
    let activeOnly = true;
    let searchInput = '';

    // Stats
    let statsVisible = false;
    let statsHtml = '';
    let projectOptions = [];

    // Filters
    let filterType = '';
    let filterProject = '';
    let filterSalience = '';
    let filterSort = 'created_at';

    // Modal
    let modalOpen = false;
    let modalTitle = '';
    let modalBody = '';

    async function init() {
        try {
            const agents = await api('GET', '/agents');
            agentList = (agents.agents || []);
        } catch (e) { toast('Failed to load agents', 'error'); }
    }

    async function onAgentChange() {
        currentOffset = 0; isSearchMode = false; searchInput = '';
        if (!currentAgent) { statsVisible = false; memories = []; return; }
        await Promise.all([loadStats(), loadMemories()]);
    }

    async function loadStats() {
        try {
            const stats = await api('GET', `/agents/${currentAgent}/memories/stats`);
            const byType = stats.by_type || {};
            const byProject = stats.by_project || {};
            statsHtml = `<div class="stat-item"><div class="stat-value">${stats.total || 0}</div><div class="stat-label">Total</div></div>` +
                Object.entries(byType).map(([k, v]) => `<div class="stat-item"><div class="stat-value">${v}</div><div class="stat-label">${k.replace('_', ' ')}</div></div>`).join('');
            projectOptions = Object.entries(byProject).map(([k, v]) => ({ value: k, label: `${k} (${v})` }));
            statsVisible = true;
        } catch (e) { console.error('Stats error:', e); }
    }

    async function loadMemories() {
        try {
            const params = new URLSearchParams();
            params.set('limit', PAGE_SIZE);
            params.set('offset', currentOffset);
            if (filterType) params.set('type', filterType);
            if (filterProject) params.set('project', filterProject);
            if (filterSalience === 'high') params.set('min_salience', 4);
            else if (filterSalience) { params.set('min_salience', filterSalience); params.set('max_salience', filterSalience); }
            if (filterSort) params.set('sort', filterSort);
            if (activeOnly) params.set('active', 'true');

            const data = await api('GET', `/agents/${currentAgent}/memories?${params}`);
            memories = Array.isArray(data) ? data : (data.memories || data.items || []);
            totalCount = data.total || memories.length;
        } catch (e) { memories = []; toast('Failed to load memories', 'error'); }
    }

    async function doSearch() {
        if (!searchInput.trim() || !currentAgent) return;
        isSearchMode = true; currentOffset = 0;
        try {
            const params = new URLSearchParams(); params.set('q', searchInput); params.set('limit', PAGE_SIZE);
            const data = await api('GET', `/agents/${currentAgent}/memories/search?${params}`);
            memories = Array.isArray(data) ? data : (data.memories || data.results || data.items || []);
            totalCount = data.total || memories.length;
            toast(`Found ${totalCount} results`);
        } catch (e) { toast('Search failed', 'error'); }
    }

    function applyFilters() { isSearchMode = false; currentOffset = 0; loadMemories(); }
    function toggleActiveOnly() { activeOnly = !activeOnly; applyFilters(); }
    function prevPage() { currentOffset = Math.max(0, currentOffset - PAGE_SIZE); loadMemories(); }
    function nextPage() { currentOffset += PAGE_SIZE; loadMemories(); }

    $: totalPages = Math.ceil(totalCount / PAGE_SIZE);
    $: currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;
    $: showPagination = totalCount > PAGE_SIZE || currentOffset > 0;

    async function openDetail(id) {
        if (!id || !currentAgent) return;
        try {
            const [memory, links] = await Promise.all([
                api('GET', `/agents/${currentAgent}/memories/${id}`),
                api('GET', `/agents/${currentAgent}/memories/${id}/links`).catch(() => [])
            ]);
            const m = memory.memory || memory;
            const linkedItems = Array.isArray(links) ? links : (links.links || links.memories || []);
            const type = m.type || 'fact';
            const salience = m.salience || 0;
            const salienceDots = Array.from({length: 5}, (_, i) => `<div class="salience-dot${i < salience ? ' filled' : ''}" style="display:inline-block"></div>`).join('');
            const created = m.created_at ? new Date(m.created_at).toLocaleString() : '--';
            const accessed = m.accessed_at ? new Date(m.accessed_at).toLocaleString() : '--';
            const entities = (m.entities || []).map(e => typeof e === 'string' ? e : e.name || e).join(', ') || '--';

            modalTitle = `${type.replace('_', ' ')} Memory`;
            modalBody = `
                <div class="detail-field"><div class="detail-label">Type</div><span class="type-badge ${type}">${type.replace('_', ' ')}</span> <span style="margin-left:1rem">Salience: <span style="display:inline-flex;gap:3px;vertical-align:middle">${salienceDots}</span></span></div>
                <div class="detail-field"><div class="detail-label">Content</div><div class="detail-value" style="white-space:pre-wrap">${escapeHtml(m.content || '')}</div></div>
                ${m.context ? `<div class="detail-field"><div class="detail-label">Context</div><div class="detail-value" style="color:var(--gray-mid);white-space:pre-wrap">${escapeHtml(m.context)}</div></div>` : ''}
                <div class="detail-meta-grid">
                    <div class="detail-field"><div class="detail-label">Created</div><div class="detail-value">${created}</div></div>
                    <div class="detail-field"><div class="detail-label">Last Accessed</div><div class="detail-value">${accessed}</div></div>
                    <div class="detail-field"><div class="detail-label">Access Count</div><div class="detail-value">${m.access_count || 0}</div></div>
                    <div class="detail-field"><div class="detail-label">Weight</div><div class="detail-value">${m.weight != null ? m.weight.toFixed(2) : '--'}</div></div>
                    <div class="detail-field"><div class="detail-label">Project</div><div class="detail-value">${escapeHtml(m.project || '--')}</div></div>
                    <div class="detail-field"><div class="detail-label">Entities</div><div class="detail-value">${escapeHtml(entities)}</div></div>
                </div>
            `;
            modalOpen = true;
        } catch (e) { toast('Failed to load memory detail', 'error'); }
    }

    onMount(init);
</script>

<div class="content">
    <!-- Controls -->
    <div class="controls">
        <div class="controls-group">
            <span class="controls-label">Agent:</span>
            <select class="form-select" bind:value={currentAgent} on:change={onAgentChange}>
                <option value="">Select agent...</option>
                {#each agentList as a}
                    <option value={a.name}>{a.name}</option>
                {/each}
            </select>
        </div>
        <div class="search-bar">
            <input type="text" class="form-input" bind:value={searchInput} placeholder="Search memories..." on:keydown={e => { if (e.key === 'Enter') doSearch(); }}>
            <button class="btn btn-primary" on:click={doSearch}>Search</button>
        </div>
    </div>

    <!-- Stats -->
    {#if statsVisible}
        <div class="section">
            <div class="stats-bar">{@html statsHtml}</div>
            <div class="filter-bar">
                <span class="controls-label">Type:</span>
                <select class="form-select" bind:value={filterType} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">All types</option>
                    <option value="insight">Insight</option>
                    <option value="fact">Fact</option>
                    <option value="project_state">Project State</option>
                    <option value="interaction_pattern">Interaction Pattern</option>
                    <option value="continuation">Continuation</option>
                </select>
                <span class="controls-label">Project:</span>
                <select class="form-select" bind:value={filterProject} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">All projects</option>
                    {#each projectOptions as p}<option value={p.value}>{p.label}</option>{/each}
                </select>
                <span class="controls-label">Salience:</span>
                <select class="form-select" bind:value={filterSalience} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">All</option><option value="high">High (4-5)</option>
                    {#each [1,2,3,4,5] as n}<option value={n}>{n}</option>{/each}
                </select>
                <span class="controls-label">Sort:</span>
                <select class="form-select" bind:value={filterSort} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="created_at">Created</option><option value="accessed_at">Accessed</option>
                    <option value="salience">Salience</option><option value="access_count">Access Count</option>
                </select>
                <button class="toggle-btn" class:active={activeOnly} on:click={toggleActiveOnly}>Active Only</button>
            </div>
        </div>
    {/if}

    <!-- Memory Grid -->
    {#if memories.length === 0}
        <div class="empty">{isSearchMode ? 'No results found.' : 'No memories found. Select an agent above to browse.'}</div>
    {:else}
        <div class="memory-grid">
            {#each memories as m}
                {@const type = m.type || 'fact'}
                {@const salience = m.salience || 0}
                {@const truncated = (m.content || '').length > 200}
                {@const displayContent = truncated ? (m.content || '').substring(0, 200) + '...' : (m.content || '')}
                {@const isActive = m.active !== false}
                <div class="memory-card type-{type}" class:card-inactive={!isActive} on:click={() => openDetail(m.id || m._id)}>
                    <div class="card-header">
                        <span class="type-badge {type}">{type.replace('_', ' ')}</span>
                        <div class="salience-dots">
                            {#each Array(5) as _, i}<div class="salience-dot" class:filled={i < salience}></div>{/each}
                        </div>
                    </div>
                    <div class="card-content" class:truncated>{displayContent}</div>
                    {#if m.context}<div class="card-context">{(m.context || '').substring(0, 120)}</div>{/if}
                    <div class="card-tags">
                        {#if m.project}<span class="card-tag project">{m.project}</span>{/if}
                        {#if m.source_channel}<span class="card-tag source">{m.source_channel}</span>{/if}
                        {#each (m.entities || []) as e}<span class="card-tag entity">{typeof e === 'string' ? e : e.name || e}</span>{/each}
                    </div>
                    <div class="card-footer">
                        <div class="card-footer-left">
                            <span>{m.created_at ? new Date(m.created_at).toLocaleDateString() : '--'}</span>
                            <span>{m.access_count || 0} views</span>
                            <span>w: {m.weight != null ? m.weight.toFixed(2) : '--'}</span>
                        </div>
                        {#if !isActive}<span style="color:var(--red)">INACTIVE</span>{/if}
                    </div>
                </div>
            {/each}
        </div>
    {/if}

    <!-- Pagination -->
    {#if showPagination}
        <div class="pagination">
            <button class="btn" on:click={prevPage} disabled={currentOffset === 0}>Prev</button>
            <span class="controls-label" style="align-self:center">Page {currentPage} of {totalPages}</span>
            <button class="btn" on:click={nextPage} disabled={currentOffset + PAGE_SIZE >= totalCount}>Next</button>
        </div>
    {/if}
</div>

<!-- Detail Modal -->
{#if modalOpen}
    <div class="modal-overlay" on:click|self={() => modalOpen = false}>
        <div class="modal" style="max-width:700px">
            <div class="modal-header">
                <div class="modal-title">{modalTitle}</div>
                <button class="modal-close" on:click={() => modalOpen = false}>&times;</button>
            </div>
            <div class="modal-body">{@html modalBody}</div>
        </div>
    </div>
{/if}

<style>
    .controls { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; margin-bottom: 1.5rem; }
    .controls-group { display: flex; gap: 0.5rem; align-items: center; }
    .controls-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); white-space: nowrap; }
    .search-bar { display: flex; gap: 0; flex: 1; min-width: 250px; }
    .search-bar .form-input { flex: 1; border-right: none; }
    .search-bar .btn { border-left: none; }

    .stats-bar { display: flex; gap: 2rem; flex-wrap: wrap; padding: 1rem 1.5rem; font-family: var(--font-mono); font-size: 0.8rem; }
    :global(.stat-item) { display: flex; flex-direction: column; }
    :global(.stat-value) { font-size: 1.4rem; font-weight: 700; }
    :global(.stat-label) { font-size: 0.65rem; text-transform: uppercase; color: var(--gray-mid); letter-spacing: 0.05em; }

    .filter-bar { display: flex; gap: 0.8rem; flex-wrap: wrap; align-items: center; padding: 1rem 1.5rem; border-top: 1px solid #e2e8f0; }
    .toggle-btn { font-family: var(--font-mono); font-size: 0.65rem; font-weight: 700; padding: 0.3rem 0.6rem; border: 2px solid var(--gray-mid); background: var(--white); cursor: pointer; text-transform: uppercase; color: var(--gray-mid); }
    .toggle-btn.active { border-color: var(--black); color: var(--black); background: var(--yellow); }

    .memory-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }
    .memory-card { background: var(--white); border: 2px solid #e2e8f0; border-left: 5px solid var(--gray-mid); padding: 1.2rem; cursor: pointer; transition: border-color 0.15s, box-shadow 0.15s; position: relative; }
    .memory-card:hover { border-color: var(--black); box-shadow: 4px 4px 0 rgba(30,41,59,0.08); }
    .memory-card.type-insight { border-left-color: var(--blue); }
    .memory-card.type-fact { border-left-color: var(--gray-dark); }
    .memory-card.type-project_state { border-left-color: var(--green); }
    .memory-card.type-interaction_pattern { border-left-color: var(--yellow); }
    .memory-card.type-continuation { border-left-color: var(--gray-mid); }
    .card-inactive { opacity: 0.5; }

    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.6rem; }
    .type-badge { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.15rem 0.5rem; text-transform: uppercase; }
    :global(.type-badge.insight) { background: #dbeafe; color: #1e40af; }
    :global(.type-badge.fact) { background: #e2e8f0; color: var(--gray-dark); }
    :global(.type-badge.project_state) { background: #dcfce7; color: #166534; }
    :global(.type-badge.interaction_pattern) { background: #fef9c3; color: #854d0e; }
    :global(.type-badge.continuation) { background: #f1f5f9; color: var(--gray-mid); }

    .salience-dots { display: flex; gap: 3px; }
    .salience-dot { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--gray-mid); }
    .salience-dot.filled { background: var(--yellow); border-color: var(--yellow); }

    .card-content { font-size: 0.88rem; line-height: 1.5; margin-bottom: 0.6rem; word-break: break-word; }
    .card-content.truncated { max-height: 4.5em; overflow: hidden; position: relative; }
    .card-context { font-size: 0.78rem; color: var(--gray-mid); margin-bottom: 0.6rem; }
    .card-tags { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
    .card-tag { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.4rem; background: #f1f5f9; color: var(--gray-dark); text-transform: uppercase; }
    .card-tag.project { background: #f3e8ff; color: #6b21a8; }
    .card-tag.entity { background: #dbeafe; color: #1e40af; }
    .card-tag.source { background: #fef3c7; color: #92400e; }
    .card-footer { display: flex; justify-content: space-between; align-items: center; font-family: var(--font-mono); font-size: 0.65rem; color: var(--gray-mid); padding-top: 0.6rem; border-top: 1px solid #f1f5f9; }
    .card-footer-left { display: flex; gap: 1rem; }
    .pagination { display: flex; justify-content: center; gap: 0.5rem; margin-top: 1.5rem; }

    :global(.detail-field) { margin-bottom: 1rem; }
    :global(.detail-label) { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); margin-bottom: 0.3rem; }
    :global(.detail-value) { font-family: var(--font-mono); font-size: 0.85rem; line-height: 1.6; }
    :global(.detail-meta-grid) { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

    @media (max-width: 900px) {
        .memory-grid { grid-template-columns: 1fr; }
        .controls { flex-direction: column; align-items: stretch; }
        :global(.detail-meta-grid) { grid-template-columns: 1fr; }
    }
</style>
