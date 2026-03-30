<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo, escapeHtml, renderMarkdown } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    // Stats
    let stats = { open: 0, assigned: 0, researching: 0, in_review: 0, published: 0 };

    // View toggle
    let activeView = 'pipeline';

    // Topics
    let topics = [];
    let agentsList = [];
    let refreshInterval;

    // Filters
    let filterStatus = '';
    let filterPriority = '';

    // Pipeline columns
    const STATUSES = [
        { key: 'open', label: 'Open' },
        { key: 'assigned', label: 'Assigned' },
        { key: 'researching', label: 'Researching' },
        { key: 'in_review', label: 'In Review' },
        { key: 'published', label: 'Published' },
    ];

    $: columns = {
        open: topics.filter(t => t.status === 'open'),
        assigned: topics.filter(t => t.status === 'assigned'),
        researching: topics.filter(t => t.status === 'researching'),
        in_review: topics.filter(t => t.status === 'in_review' || t.status === 'revising'),
        published: topics.filter(t => t.status === 'published'),
    };

    // New topic modal
    let createModalOpen = false;
    let newTitle = '';
    let newDescription = '';
    let newPriority = 'normal';
    let newTags = '';
    let newScope = '';
    let newSubmittedBy = 'admin';

    // Detail modal
    let detailModalOpen = false;
    let detailTopic = null;
    let detailBriefs = [];
    let detailReviews = [];
    let detailTab = 'brief';

    // Assign dropdown
    let assignDropdownOpen = false;

    function priorityColor(p) {
        if (p === 'urgent') return 'var(--red)';
        if (p === 'high') return 'var(--orange)';
        if (p === 'normal') return 'var(--blue)';
        return 'var(--gray-mid)';
    }

    function priorityClass(p) {
        return `priority-${p}`;
    }

    function verdictColor(v) {
        if (v === 'approve') return 'var(--green)';
        if (v === 'request_changes') return 'var(--orange)';
        if (v === 'reject') return 'var(--red)';
        return 'var(--gray-mid)';
    }

    function verdictLabel(v) {
        if (v === 'approve') return 'Approved';
        if (v === 'request_changes') return 'Changes Requested';
        if (v === 'reject') return 'Rejected';
        return v;
    }

    async function loadStats() {
        try {
            const data = await api('GET', '/research/stats');
            stats = {
                open: data.open || 0,
                assigned: data.assigned || 0,
                researching: data.researching || 0,
                in_review: (data.in_review || 0) + (data.revising || 0),
                published: data.published || 0,
            };
        } catch (e) { console.error('Stats load error:', e); }
    }

    async function loadTopics() {
        try {
            let qs = '?limit=200&include_cancelled=false';
            if (filterStatus) qs += `&status=${filterStatus}`;
            const data = await api('GET', `/research${qs}`);
            let all = data.topics || data || [];
            if (filterPriority) all = all.filter(t => t.priority === filterPriority);
            topics = all;
        } catch (e) { console.error('Topics load error:', e); topics = []; }
    }

    async function loadAgents() {
        try {
            const data = await api('GET', '/agents');
            agentsList = data.agents || [];
        } catch (e) { agentsList = []; }
    }

    async function refresh() {
        await Promise.all([loadStats(), loadTopics(), loadAgents()]);
    }

    // Create topic
    function openCreateModal() {
        newTitle = ''; newDescription = ''; newPriority = 'normal';
        newTags = ''; newScope = ''; newSubmittedBy = 'admin';
        createModalOpen = true;
    }

    async function createTopic() {
        if (!newTitle.trim()) { toast('Title is required', 'error'); return; }
        const tags = newTags.split(',').map(t => t.trim()).filter(Boolean);
        try {
            await api('POST', '/research', {
                title: newTitle,
                description: newDescription,
                priority: newPriority,
                tags,
                scope: newScope,
                submitted_by: newSubmittedBy,
            });
            toast('Topic created');
            createModalOpen = false;
            refresh();
        } catch (e) { toast(`Failed to create topic: ${e.message}`, 'error'); }
    }

    // Detail modal
    async function openDetail(topicId) {
        try {
            const data = await api('GET', `/research/${topicId}`);
            detailTopic = data.topic || data;
            detailBriefs = data.briefs || [];
            detailReviews = data.reviews || [];
            detailTab = 'brief';
            assignDropdownOpen = false;
            detailModalOpen = true;
        } catch (e) { toast(`Failed to load topic: ${e.message}`, 'error'); }
    }

    // Actions
    async function assignAgent(agentName) {
        if (!detailTopic) return;
        try {
            await api('POST', `/research/${detailTopic.id}/assign`, { agent_name: agentName });
            toast(`Assigned to ${agentName}`);
            assignDropdownOpen = false;
            await openDetail(detailTopic.id);
            refresh();
        } catch (e) { toast(`Assign failed: ${e.message}`, 'error'); }
    }

    async function publishTopic() {
        if (!detailTopic) return;
        try {
            await api('POST', `/research/${detailTopic.id}/publish`);
            toast('Topic published');
            await openDetail(detailTopic.id);
            refresh();
        } catch (e) { toast(`Publish failed: ${e.message}`, 'error'); }
    }

    async function cancelTopic() {
        if (!detailTopic || !confirm('Cancel this research topic?')) return;
        try {
            await api('PUT', `/research/${detailTopic.id}`, { status: 'cancelled' });
            toast('Topic cancelled');
            detailModalOpen = false;
            refresh();
        } catch (e) { toast(`Cancel failed: ${e.message}`, 'error'); }
    }

    // Computed helpers for detail
    $: latestBrief = detailBriefs.length > 0
        ? detailBriefs.reduce((a, b) => (b.version || 0) > (a.version || 0) ? b : a, detailBriefs[0])
        : null;

    $: reviewSummary = (() => {
        const approved = detailReviews.filter(r => r.verdict === 'approve').length;
        const changes = detailReviews.filter(r => r.verdict === 'request_changes').length;
        const rejected = detailReviews.filter(r => r.verdict === 'reject').length;
        return { approved, changes, rejected, total: detailReviews.length };
    })();

    $: timeline = (() => {
        if (!detailTopic) return [];
        const events = [];
        events.push({ time: detailTopic.created_at, label: 'Topic created', actor: detailTopic.submitted_by || 'system' });
        if (detailTopic.assigned_agent) {
            events.push({ time: detailTopic.updated_at, label: `Assigned to ${detailTopic.assigned_agent}`, actor: 'system' });
        }
        for (const b of detailBriefs) {
            events.push({ time: b.created_at, label: `Brief v${b.version} submitted`, actor: b.author_agent });
            if (b.published_at && b.published_at > 0) {
                events.push({ time: b.published_at, label: `Brief v${b.version} published`, actor: b.author_agent });
            }
        }
        for (const r of detailReviews) {
            events.push({ time: r.created_at, label: `Review: ${verdictLabel(r.verdict)}`, actor: r.reviewer_agent });
        }
        events.sort((a, b) => (a.time || 0) - (b.time || 0));
        return events;
    })();

    $: canPublish = detailTopic && latestBrief &&
        ['in_review', 'revising'].includes(detailTopic.status);

    $: canAssign = detailTopic && detailTopic.status === 'open';

    $: canExport = detailTopic && latestBrief;

    function exportMd() {
        if (!detailTopic) return;
        window.open(`/research/${detailTopic.id}/export?format=md`, '_blank');
    }

    function exportHtml() {
        if (!detailTopic) return;
        window.open(`/research/${detailTopic.id}/export?format=html`, '_blank');
    }

    function exportPdf() {
        if (!detailTopic) return;
        window.open(`/research/${detailTopic.id}/export?format=pdf`, '_blank');
    }

    async function copyMd() {
        if (!detailTopic) return;
        try {
            const data = await api('GET', `/research/${detailTopic.id}/export/content?format=md`);
            await navigator.clipboard.writeText(data.content);
            toast('Markdown copied to clipboard');
        } catch (e) { toast(`Copy failed: ${e.message}`, 'error'); }
    }

    onMount(() => { refresh(); refreshInterval = setInterval(refresh, 15000); });
    onDestroy(() => { clearInterval(refreshInterval); });
</script>

<div class="content" style="max-width:1600px">
    <!-- Stats Bar -->
    <div class="stats-bar">
        {#each STATUSES as s}
            <div class="stat-card">
                <div class="stat-value">{stats[s.key]}</div>
                <div class="stat-label">{s.label}</div>
            </div>
        {/each}
    </div>

    <!-- Toolbar -->
    <div class="toolbar">
        <button class="btn btn-primary" on:click={openCreateModal}>+ New Topic</button>
        <div class="view-toggle">
            <button class="toggle-btn" class:active={activeView === 'pipeline'} on:click={() => activeView = 'pipeline'}>Pipeline</button>
            <button class="toggle-btn" class:active={activeView === 'list'} on:click={() => activeView = 'list'}>List</button>
        </div>
        <select class="filter-select" bind:value={filterStatus} on:change={loadTopics}>
            <option value="">All Statuses</option>
            {#each STATUSES as s}<option value={s.key}>{s.label}</option>{/each}
            <option value="revising">Revising</option>
            <option value="cancelled">Cancelled</option>
        </select>
        <select class="filter-select" bind:value={filterPriority} on:change={loadTopics}>
            <option value="">All Priorities</option>
            <option value="urgent">Urgent</option>
            <option value="high">High</option>
            <option value="normal">Normal</option>
            <option value="low">Low</option>
        </select>
    </div>

    <!-- Pipeline View -->
    {#if activeView === 'pipeline'}
        <div class="pipeline">
            {#each STATUSES as s}
                <div class="column">
                    <div class="column-header">
                        {s.label}
                        <span class="column-count">{columns[s.key].length}</span>
                    </div>
                    <div class="column-body">
                        {#each columns[s.key] as topic}
                            <div class="topic-card" style="border-left-color: {priorityColor(topic.priority)}" on:click={() => openDetail(topic.id)}>
                                <div class="topic-title">{topic.title}</div>
                                <div class="topic-meta">
                                    <span class="badge {priorityClass(topic.priority)}">{topic.priority}</span>
                                    {#if topic.assigned_agent}
                                        <span class="badge badge-agent">{topic.assigned_agent}</span>
                                    {/if}
                                    {#each (topic.tags || []) as tag}
                                        <span class="badge badge-tag">{tag}</span>
                                    {/each}
                                </div>
                                <div class="topic-footer">
                                    <span>{timeAgo(topic.created_at)}</span>
                                </div>
                            </div>
                        {:else}
                            <div class="column-empty">No topics</div>
                        {/each}
                    </div>
                </div>
            {/each}
        </div>
    {/if}

    <!-- List View -->
    {#if activeView === 'list'}
        <div class="list-container">
            {#if topics.length === 0}
                <div class="empty">No research topics found.</div>
            {:else}
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>Title</th>
                            <th>Status</th>
                            <th>Agent</th>
                            <th>Priority</th>
                            <th>Tags</th>
                            <th>Created</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each topics as topic}
                            <tr class="list-row" on:click={() => openDetail(topic.id)}>
                                <td class="mono" style="font-weight:700">{topic.title}</td>
                                <td><span class="badge badge-status-{topic.status}">{topic.status.replace('_', ' ')}</span></td>
                                <td class="mono">{topic.assigned_agent || '--'}</td>
                                <td><span class="badge {priorityClass(topic.priority)}">{topic.priority}</span></td>
                                <td>
                                    {#each (topic.tags || []) as tag}
                                        <span class="badge badge-tag">{tag}</span>
                                    {/each}
                                </td>
                                <td class="mono" style="font-size:0.75rem">{timeAgo(topic.created_at)}</td>
                                <td>
                                    <button class="btn btn-sm" on:click|stopPropagation={() => openDetail(topic.id)}>View</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    {/if}
</div>

<!-- Create Topic Modal -->
{#if createModalOpen}
    <div class="modal-overlay" on:click|self={() => createModalOpen = false}>
        <div class="modal" style="width:600px">
            <div class="modal-header">
                <div class="modal-title">New Research Topic</div>
                <button class="btn btn-sm" on:click={() => createModalOpen = false}>X</button>
            </div>
            <div class="modal-body">
                <div class="form-row">
                    <label class="form-label">Title *</label>
                    <input type="text" class="form-input" bind:value={newTitle} placeholder="e.g. MCP Server Hot-Reload Patterns" style="width:100%">
                </div>
                <div class="form-row">
                    <label class="form-label">Description</label>
                    <textarea class="form-input" bind:value={newDescription} rows="4" placeholder="Full research question / context..." style="width:100%"></textarea>
                </div>
                <div class="form-row-inline">
                    <div class="form-row">
                        <label class="form-label">Priority</label>
                        <select class="form-select" bind:value={newPriority} style="width:100%">
                            <option value="low">Low</option>
                            <option value="normal">Normal</option>
                            <option value="high">High</option>
                            <option value="urgent">Urgent</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <label class="form-label">Submitted By</label>
                        <input type="text" class="form-input" bind:value={newSubmittedBy} style="width:100%">
                    </div>
                </div>
                <div class="form-row">
                    <label class="form-label">Tags</label>
                    <input type="text" class="form-input" bind:value={newTags} placeholder="Comma-separated tags" style="width:100%">
                </div>
                <div class="form-row">
                    <label class="form-label">Scope / Constraints</label>
                    <textarea class="form-input" bind:value={newScope} rows="2" placeholder="Optional focus areas or constraints..." style="width:100%"></textarea>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn" on:click={() => createModalOpen = false}>Cancel</button>
                <button class="btn btn-primary" on:click={createTopic}>Create Topic</button>
            </div>
        </div>
    </div>
{/if}

<!-- Detail Modal -->
{#if detailModalOpen && detailTopic}
    <div class="modal-overlay" on:click|self={() => detailModalOpen = false}>
        <div class="modal" style="width:800px;max-height:85vh;display:flex;flex-direction:column">
            <div class="modal-header">
                <div style="flex:1">
                    <div class="modal-title">{detailTopic.title}</div>
                    <div class="detail-header-meta">
                        <span class="badge badge-status-{detailTopic.status}">{detailTopic.status.replace('_', ' ')}</span>
                        <span class="badge {priorityClass(detailTopic.priority)}">{detailTopic.priority}</span>
                        {#if detailTopic.assigned_agent}
                            <span class="badge badge-agent">{detailTopic.assigned_agent}</span>
                        {/if}
                        {#each (detailTopic.tags || []) as tag}
                            <span class="badge badge-tag">{tag}</span>
                        {/each}
                    </div>
                </div>
                <button class="btn btn-sm" on:click={() => detailModalOpen = false}>X</button>
            </div>

            <!-- Detail Tabs -->
            <div class="detail-tabs">
                <button class="detail-tab" class:active={detailTab === 'brief'} on:click={() => detailTab = 'brief'}>Brief</button>
                <button class="detail-tab" class:active={detailTab === 'reviews'} on:click={() => detailTab = 'reviews'}>Reviews ({detailReviews.length})</button>
                <button class="detail-tab" class:active={detailTab === 'timeline'} on:click={() => detailTab = 'timeline'}>Timeline</button>
            </div>

            <div class="modal-body" style="flex:1;overflow-y:auto">
                <!-- Brief Tab -->
                {#if detailTab === 'brief'}
                    {#if latestBrief}
                        <div class="brief-meta">
                            <span class="mono">v{latestBrief.version}</span>
                            <span class="mono">by {latestBrief.author_agent || 'unknown'}</span>
                            <span class="badge badge-status-{latestBrief.status}">{latestBrief.status}</span>
                            <span class="mono" style="color:var(--gray-mid)">{timeAgo(latestBrief.created_at)}</span>
                        </div>
                        {#if latestBrief.summary}
                            <div class="brief-section">
                                <div class="brief-section-label">Summary</div>
                                <div class="brief-content">{latestBrief.summary}</div>
                            </div>
                        {/if}
                        {#if latestBrief.key_findings && latestBrief.key_findings.length > 0}
                            <div class="brief-section">
                                <div class="brief-section-label">Key Findings</div>
                                <ul class="brief-list">
                                    {#each latestBrief.key_findings as finding}
                                        <li>{finding}</li>
                                    {/each}
                                </ul>
                            </div>
                        {/if}
                        {#if latestBrief.content}
                            <div class="brief-section">
                                <div class="brief-section-label">Full Brief</div>
                                <div class="brief-rendered">{@html renderMarkdown(latestBrief.content)}</div>
                            </div>
                        {/if}
                        {#if latestBrief.sources && latestBrief.sources.length > 0}
                            <div class="brief-section">
                                <div class="brief-section-label">Sources</div>
                                <ul class="brief-list source-list">
                                    {#each latestBrief.sources as source}
                                        <li>{source}</li>
                                    {/each}
                                </ul>
                            </div>
                        {/if}
                    {:else}
                        <div class="empty-state">
                            <div class="empty-state-icon">&#128269;</div>
                            <div class="empty-state-text">Awaiting research</div>
                            <div class="empty-state-sub">No brief has been submitted for this topic yet.</div>
                        </div>
                    {/if}
                {/if}

                <!-- Reviews Tab -->
                {#if detailTab === 'reviews'}
                    {#if detailReviews.length > 0}
                        <div class="review-summary">
                            {#if reviewSummary.approved > 0}<span class="review-count" style="color:var(--green)">{reviewSummary.approved} approved</span>{/if}
                            {#if reviewSummary.changes > 0}<span class="review-count" style="color:var(--orange)">{reviewSummary.changes} changes requested</span>{/if}
                            {#if reviewSummary.rejected > 0}<span class="review-count" style="color:var(--red)">{reviewSummary.rejected} rejected</span>{/if}
                        </div>
                        {#each detailReviews as review}
                            <div class="review-card">
                                <div class="review-header">
                                    <span class="mono" style="font-weight:700">{review.reviewer_agent}</span>
                                    <span class="badge" style="background:{verdictColor(review.verdict)};color:var(--white)">{verdictLabel(review.verdict)}</span>
                                    <div class="confidence-dots">
                                        {#each Array(5) as _, i}
                                            <div class="confidence-dot" class:filled={i < (review.confidence || 0)}></div>
                                        {/each}
                                    </div>
                                    <span class="mono" style="color:var(--gray-mid);font-size:0.7rem;margin-left:auto">{timeAgo(review.created_at)}</span>
                                </div>
                                {#if review.comments}
                                    <div class="review-comments">{review.comments}</div>
                                {/if}
                                {#if review.suggested_additions && review.suggested_additions.length > 0}
                                    <div class="review-additions">
                                        <div class="brief-section-label">Suggested Additions</div>
                                        <ul class="brief-list">{#each review.suggested_additions as item}<li>{item}</li>{/each}</ul>
                                    </div>
                                {/if}
                                {#if review.corrections && review.corrections.length > 0}
                                    <div class="review-corrections">
                                        <div class="brief-section-label">Corrections</div>
                                        <ul class="brief-list">{#each review.corrections as item}<li>{item}</li>{/each}</ul>
                                    </div>
                                {/if}
                            </div>
                        {/each}
                    {:else}
                        <div class="empty-state">
                            <div class="empty-state-text">No reviews yet</div>
                            <div class="empty-state-sub">Reviews will appear here once peer agents evaluate the brief.</div>
                        </div>
                    {/if}
                {/if}

                <!-- Timeline Tab -->
                {#if detailTab === 'timeline'}
                    {#if timeline.length > 0}
                        <div class="timeline">
                            {#each timeline as event}
                                <div class="timeline-item">
                                    <div class="timeline-dot"></div>
                                    <div class="timeline-content">
                                        <div class="timeline-label">{event.label}</div>
                                        <div class="timeline-meta">
                                            <span>{event.actor}</span>
                                            <span>{timeAgo(event.time)}</span>
                                        </div>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {:else}
                        <div class="empty-state">
                            <div class="empty-state-text">No events yet</div>
                        </div>
                    {/if}
                {/if}
            </div>

            <!-- Action Footer -->
            <div class="modal-footer">
                <div style="display:flex;gap:0.5rem;align-items:center">
                    {#if canAssign}
                        <div style="position:relative">
                            <button class="btn btn-primary btn-sm" on:click={() => assignDropdownOpen = !assignDropdownOpen}>Assign</button>
                            {#if assignDropdownOpen}
                                <div class="assign-dropdown">
                                    {#each agentsList as a}
                                        <button class="assign-option" on:click={() => assignAgent(a.name)}>{a.display_name || a.name}</button>
                                    {/each}
                                    {#if agentsList.length === 0}
                                        <div class="assign-option" style="color:var(--gray-mid);cursor:default">No agents available</div>
                                    {/if}
                                </div>
                            {/if}
                        </div>
                    {/if}
                    {#if canPublish}
                        <button class="btn btn-primary btn-sm" on:click={publishTopic}>Publish</button>
                    {/if}
                </div>
                <div style="display:flex;gap:0.5rem;margin-left:auto">
                    {#if canExport}
                        <button class="btn btn-sm" on:click={copyMd} title="Copy Markdown to clipboard">Copy MD</button>
                        <button class="btn btn-sm" on:click={exportMd} title="Download as Markdown">Export MD</button>
                        <button class="btn btn-sm" on:click={exportPdf} title="Download as PDF">Export PDF</button>
                    {/if}
                    <button class="btn btn-sm btn-danger" on:click={cancelTopic}>Cancel Topic</button>
                    <button class="btn btn-sm" on:click={() => detailModalOpen = false}>Close</button>
                </div>
            </div>
        </div>
    </div>
{/if}

<style>
    /* Stats Bar */
    .stats-bar { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; margin-bottom: 2rem; }
    .stat-card { padding: 1.2rem; background: var(--white); border: var(--border); margin: -1.5px; text-align: center; }
    .stat-value { font-family: var(--font-mono); font-size: 1.8rem; font-weight: 700; }
    .stat-label { font-family: var(--font-mono); font-size: 0.65rem; text-transform: uppercase; color: var(--gray-mid); }

    /* Toolbar */
    .toolbar { display: flex; gap: 0.8rem; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .view-toggle { display: flex; gap: 0; }
    .toggle-btn { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; padding: 0.4rem 0.8rem; border: 2px solid var(--black); background: var(--white); cursor: pointer; }
    .toggle-btn:first-child { border-right: none; }
    .toggle-btn.active { background: var(--yellow); }
    .filter-select { font-family: var(--font-mono); font-size: 0.75rem; padding: 0.3rem 0.6rem; border: 2px solid var(--black); background: var(--white); }

    /* Pipeline */
    .pipeline { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; }
    .column { border: var(--border); margin: -1.5px; min-height: 300px; }
    .column-header { padding: 0.8rem 1rem; background: var(--gray-light); border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: flex; justify-content: space-between; }
    .column-count { background: var(--black); color: var(--yellow); padding: 0.1rem 0.4rem; font-size: 0.65rem; }
    .column-body { padding: 0.5rem; background: var(--white); }
    .column-empty { padding: 1rem; text-align: center; color: var(--gray-mid); font-family: var(--font-mono); font-size: 0.75rem; }

    /* Topic Cards */
    .topic-card { border: 2px solid var(--black); border-left: 5px solid var(--gray-mid); padding: 0.8rem; margin-bottom: 0.5rem; cursor: pointer; transition: background 0.1s; background: var(--white); }
    .topic-card:hover { background: #fefce8; }
    .topic-title { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; margin-bottom: 0.4rem; }
    .topic-meta { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.3rem; }
    .topic-footer { display: flex; justify-content: space-between; align-items: center; font-family: var(--font-mono); font-size: 0.6rem; color: var(--gray-mid); }

    /* Badges */
    .badge { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.15rem 0.5rem; text-transform: uppercase; display: inline-block; }
    .priority-urgent { background: #fef2f2; color: var(--red); }
    .priority-high { background: #fff7ed; color: var(--orange); }
    .priority-normal { background: #eff6ff; color: var(--blue); }
    .priority-low { background: #f1f5f9; color: var(--gray-mid); }
    .badge-agent { background: #f3e8ff; color: #6b21a8; }
    .badge-tag { background: #f1f5f9; color: var(--gray-dark); }
    .badge-status-open { background: #eff6ff; color: var(--blue); }
    .badge-status-assigned { background: #f3e8ff; color: #6b21a8; }
    .badge-status-researching { background: #fef9c3; color: #854d0e; }
    .badge-status-in_review { background: #fff7ed; color: var(--orange); }
    .badge-status-revising { background: #fff7ed; color: var(--orange); }
    .badge-status-published { background: #dcfce7; color: #166534; }
    .badge-status-cancelled { background: #fef2f2; color: var(--red); }
    .badge-status-draft { background: #f1f5f9; color: var(--gray-mid); }

    /* List View */
    .list-container { background: var(--white); border: var(--border); }
    .list-row { cursor: pointer; }
    .list-row:hover { background: #fefce8; }
    .mono { font-family: var(--font-mono); font-size: 0.8rem; }

    /* Detail Header */
    .detail-header-meta { display: flex; gap: 0.4rem; flex-wrap: wrap; margin-top: 0.5rem; }

    /* Detail Tabs */
    .detail-tabs { display: flex; gap: 0; border-bottom: var(--border); background: var(--gray-light); }
    .detail-tab { font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; padding: 0.6rem 1.2rem; cursor: pointer; border: none; background: none; border-bottom: 3px solid transparent; margin-bottom: -3px; }
    .detail-tab:hover { background: #fefce8; }
    .detail-tab.active { border-bottom-color: var(--yellow); background: var(--white); }

    /* Brief */
    .brief-meta { display: flex; gap: 1rem; align-items: center; padding: 0.8rem 0; border-bottom: 1px solid #e2e8f0; margin-bottom: 1rem; font-size: 0.8rem; }
    .brief-section { margin-bottom: 1.2rem; }
    .brief-section-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); margin-bottom: 0.4rem; }
    .brief-content { font-size: 0.88rem; line-height: 1.6; }
    .brief-list { padding-left: 1.2rem; margin: 0; }
    .brief-list li { font-size: 0.85rem; line-height: 1.5; margin-bottom: 0.3rem; }
    .source-list li { font-family: var(--font-mono); font-size: 0.75rem; word-break: break-all; }
    .brief-rendered { font-size: 0.88rem; line-height: 1.6; }
    .brief-rendered :global(h1) { font-size: 1.2rem; font-weight: 700; margin: 1rem 0 0.5rem; }
    .brief-rendered :global(h2) { font-size: 1rem; font-weight: 700; margin: 0.8rem 0 0.4rem; }
    .brief-rendered :global(h3) { font-size: 0.9rem; font-weight: 700; margin: 0.6rem 0 0.3rem; }
    .brief-rendered :global(pre) { background: var(--gray-light); border: 1px solid #e2e8f0; padding: 0.8rem; overflow-x: auto; font-size: 0.8rem; margin: 0.5rem 0; }
    .brief-rendered :global(code) { font-family: var(--font-mono); font-size: 0.8rem; background: #f1f5f9; padding: 0.1rem 0.3rem; }
    .brief-rendered :global(pre code) { background: none; padding: 0; }
    .brief-rendered :global(blockquote) { border-left: 3px solid var(--yellow); padding-left: 1rem; color: var(--gray-dark); margin: 0.5rem 0; }
    .brief-rendered :global(ul) { padding-left: 1.2rem; }
    .brief-rendered :global(table) { border-collapse: collapse; width: 100%; margin: 0.5rem 0; }
    .brief-rendered :global(th), .brief-rendered :global(td) { border: 1px solid #e2e8f0; padding: 0.4rem 0.6rem; font-size: 0.8rem; text-align: left; }
    .brief-rendered :global(th) { background: var(--gray-light); font-family: var(--font-mono); font-weight: 700; }

    /* Reviews */
    .review-summary { display: flex; gap: 1.2rem; padding: 0.8rem 0; border-bottom: 1px solid #e2e8f0; margin-bottom: 1rem; }
    .review-count { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; }
    .review-card { border: 2px solid #e2e8f0; padding: 1rem; margin-bottom: 0.8rem; }
    .review-header { display: flex; gap: 0.6rem; align-items: center; margin-bottom: 0.6rem; flex-wrap: wrap; }
    .review-comments { font-size: 0.85rem; line-height: 1.5; margin-bottom: 0.6rem; white-space: pre-wrap; }
    .review-additions, .review-corrections { margin-top: 0.6rem; padding-top: 0.6rem; border-top: 1px solid #f1f5f9; }
    .confidence-dots { display: flex; gap: 3px; }
    .confidence-dot { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--gray-mid); }
    .confidence-dot.filled { background: var(--yellow); border-color: var(--yellow); }

    /* Timeline */
    .timeline { padding: 0.5rem 0; }
    .timeline-item { display: flex; gap: 1rem; align-items: flex-start; padding: 0.6rem 0; position: relative; }
    .timeline-item:not(:last-child) { border-left: 2px solid #e2e8f0; margin-left: 6px; padding-left: calc(1rem + 6px); }
    .timeline-item:last-child { margin-left: 6px; padding-left: calc(1rem + 6px); }
    .timeline-dot { width: 12px; height: 12px; border-radius: 50%; background: var(--yellow); border: 2px solid var(--black); position: absolute; left: 0; top: 0.8rem; flex-shrink: 0; }
    .timeline-content { flex: 1; }
    .timeline-label { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; }
    .timeline-meta { display: flex; gap: 1rem; font-family: var(--font-mono); font-size: 0.7rem; color: var(--gray-mid); margin-top: 0.2rem; }

    /* Empty State */
    .empty-state { text-align: center; padding: 3rem 1rem; }
    .empty-state-icon { font-size: 2rem; margin-bottom: 0.5rem; }
    .empty-state-text { font-family: var(--font-mono); font-size: 0.9rem; font-weight: 700; color: var(--gray-dark); }
    .empty-state-sub { font-family: var(--font-mono); font-size: 0.75rem; color: var(--gray-mid); margin-top: 0.3rem; }

    /* Assign Dropdown */
    .assign-dropdown { position: absolute; top: 100%; left: 0; z-index: 100; background: var(--white); border: var(--border); min-width: 180px; box-shadow: 4px 4px 0 rgba(30,41,59,0.1); }
    .assign-option { display: block; width: 100%; padding: 0.5rem 0.8rem; font-family: var(--font-mono); font-size: 0.8rem; border: none; background: none; cursor: pointer; text-align: left; }
    .assign-option:hover { background: #fefce8; }

    /* Responsive */
    @media (max-width: 1200px) {
        .pipeline { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 800px) {
        .pipeline { grid-template-columns: repeat(2, 1fr); }
        .stats-bar { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 600px) {
        .pipeline { grid-template-columns: 1fr; }
    }
</style>
