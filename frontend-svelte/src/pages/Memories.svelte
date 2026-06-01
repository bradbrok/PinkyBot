<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import TabBar from '../components/TabBar.svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    import { escapeHtml } from '../lib/utils.js';

    let agentList = [];
    let currentAgent = '';
    let memories = [];
    let totalCount = 0;
    let currentOffset = 0;
    const PAGE_SIZE = 24;
    let isSearchMode = false;
    let activeOnly = true;
    let searchInput = '';
    let memLoading = false;
    let memSeq = 0; let statsSeq = 0; let chatSeq = 0; let kgSeq = 0;

    // Tab: memories vs chat vs dreams
    let activeTab = 'memories';
    let chatMessages = [];
    let chatCount = 0;
    let chatSearchInput = '';
    let chatAfter = '';
    let chatBefore = '';
    let chatRole = '';

    // Knowledge Graph tab state
    let kgNodes = [];
    let kgEdges = [];
    let kgStats = null;
    let kgLoading = false;
    let kgSimRunning = false;
    let kgWidth = 1000;
    let kgHeight = 640;
    let kgHoveredNode = null;
    let kgRaf = null;
    let kgDestroyed = false;
    // --- KG viz rework (#153): raw data, filters, cluster layout, pan/zoom ---
    let kgRawNodes = [];
    let kgRawEdges = [];
    let kgNodeMap = new Map();       // id -> persistent node obj (x/y survive reload/filter)
    let kgAdj = new Map();           // id -> Set(neighbor ids)
    let kgAllTypes = [];
    let kgActiveTypes = new Set();   // enabled entity types (filter chips)
    let kgMinDegree = 2;             // declutter: hide low-degree nodes by default
    let kgSearch = '';
    let kgSelectedId = null;         // click-to-focus a node's neighborhood
    let kgShowEdgeLabels = false;    // predicate labels off by default (kills soup)
    let kgSvgEl;
    let kgZoom = 1;
    let kgPanX = 0;
    let kgPanY = 0;
    let kgPanning = false;
    let kgPanLast = null;
    const KG_HUB_DEGREE = 5;         // always label nodes at/above this degree

    // Dreams tab state
    let dreamStates = [];
    let dreamLoading = false;
    let dreamingAgent = null;

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
        // reset cross-tab state so stale data from the previous agent is cleared
        // and the switchTab length-guards (chatMessages.length / kgNodes.length) retrigger
        chatMessages = []; chatCount = 0;
        kgNodes = []; kgEdges = []; kgStats = null;
        kgRawNodes = []; kgRawEdges = []; kgNodeMap = new Map(); kgAdj = new Map();
        kgSelectedId = null; kgSearch = ''; kgActiveTypes = new Set();
        kgZoom = 1; kgPanX = 0; kgPanY = 0;
        dreamStates = [];
        if (!currentAgent) { statsVisible = false; memories = []; return; }
        // always refresh the memories tab (the default), then reload whatever tab is active
        await Promise.all([loadStats(), loadMemories()]);
        if (activeTab === 'chat') loadChatHistory();
        else if (activeTab === 'graph') loadKnowledgeGraph();
        else if (activeTab === 'dreams') loadDreamHistory();
    }

    async function loadStats() {
        const seq = ++statsSeq; const agent = currentAgent;
        try {
            const stats = await api('GET', `/agents/${currentAgent}/memories/stats`);
            if (seq !== statsSeq || agent !== currentAgent) return;
            const byType = stats.by_type || {};
            const byProject = stats.by_project || {};
            statsHtml = `<div class="stat-item"><div class="stat-value">${stats.total || 0}</div><div class="stat-label">Total</div></div>` +
                Object.entries(byType).map(([k, v]) => `<div class="stat-item"><div class="stat-value">${v}</div><div class="stat-label">${k.replace('_', ' ')}</div></div>`).join('');
            projectOptions = Object.entries(byProject).map(([k, v]) => ({ value: k, label: `${k} (${v})` }));
            statsVisible = true;
        } catch (e) { console.error('Stats error:', e); toast('Failed to load memory stats', 'error'); }
    }

    async function loadMemories() {
        memLoading = true;
        const seq = ++memSeq; const agent = currentAgent;
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
            if (seq !== memSeq || agent !== currentAgent) return;
            memories = Array.isArray(data) ? data : (data.memories || data.items || []);
            totalCount = data.total || memories.length;
        } catch (e) { if (seq === memSeq && agent === currentAgent) memories = []; toast('Failed to load memories', 'error'); }
        finally { if (seq === memSeq) memLoading = false; }
    }

    async function doSearch() {
        if (!searchInput.trim() || !currentAgent) return;
        memLoading = true;
        isSearchMode = true; currentOffset = 0;
        const seq = ++memSeq; const agent = currentAgent;
        try {
            const params = new URLSearchParams(); params.set('q', searchInput); params.set('limit', PAGE_SIZE);
            const data = await api('GET', `/agents/${currentAgent}/memories/search?${params}`);
            if (seq !== memSeq || agent !== currentAgent) return;
            memories = Array.isArray(data) ? data : (data.memories || data.results || data.items || []);
            totalCount = data.total || memories.length;
            toast(`Found ${totalCount} results`);
        } catch (e) { toast('Search failed', 'error'); }
        finally { if (seq === memSeq) memLoading = false; }
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
            const safeTypeClass = String(type).replace(/[^a-z0-9_-]/gi, '');
            const safeTypeText = escapeHtml(type.replace('_', ' '));

            modalTitle = `${type.replace('_', ' ')} Memory`;
            modalBody = `
                <div class="detail-field"><div class="detail-label">Type</div><span class="type-badge ${safeTypeClass}">${safeTypeText}</span> <span style="margin-left:1rem">Salience: <span style="display:inline-flex;gap:3px;vertical-align:middle">${salienceDots}</span></span></div>
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

    async function loadChatHistory() {
        if (!currentAgent) { chatMessages = []; chatCount = 0; return; }
        const seq = ++chatSeq; const agent = currentAgent;
        try {
            const params = new URLSearchParams();
            if (chatSearchInput.trim()) params.set('q', chatSearchInput.trim());
            if (chatAfter) params.set('after', chatAfter);
            if (chatBefore) params.set('before', chatBefore);
            if (chatRole) params.set('role', chatRole);
            params.set('limit', '50');
            const data = await api('GET', `/agents/${currentAgent}/chat-history?${params}`);
            if (seq !== chatSeq || agent !== currentAgent) return;
            chatMessages = data.messages || [];
            chatCount = data.count || 0;
        } catch (e) { if (seq === chatSeq && agent === currentAgent) chatMessages = []; toast('Failed to load chat history', 'error'); }
    }

    async function searchChat() {
        await loadChatHistory();
        if (chatSearchInput.trim()) toast(`Found ${chatCount} messages`);
    }

    async function loadDreamHistory() {
        dreamLoading = true;
        try {
            if (currentAgent) {
                const data = await api('GET', `/agents/${currentAgent}/dream/history`);
                dreamStates = data.dream_state ? [data.dream_state] : [];
            } else {
                const data = await api('GET', '/dreams');
                dreamStates = data.dream_states || [];
            }
        } catch (e) { console.error('Dream history error:', e); dreamStates = []; }
        dreamLoading = false;
    }

    async function triggerDream(agentName) {
        if (dreamingAgent) return;            // ignore double-clicks / concurrent runs
        dreamingAgent = agentName;
        try {
            toast(`Starting dream for ${agentName}...`);
            await api('POST', `/agents/${agentName}/dream`);
            toast(`Dream complete for ${agentName}`);
            await loadDreamHistory();
        } catch (e) {
            toast(`Dream failed: ${e.message || e}`, 'error');
        } finally {
            dreamingAgent = null;
        }
    }

    async function loadKnowledgeGraph() {
        if (!currentAgent) return;
        kgLoading = true;
        const seq = ++kgSeq; const agent = currentAgent;
        try {
            const [graphData, stats] = await Promise.all([
                api('GET', `/agents/${currentAgent}/memory/kg-graph`),
                api('GET', `/agents/${currentAgent}/memory/kg-stats`),
            ]);
            if (seq !== kgSeq || agent !== currentAgent) return;
            kgStats = stats;
            if (graphData.nodes && graphData.nodes.length > 0) {
                initKgGraph(graphData);
            } else {
                kgNodes = [];
                kgEdges = [];
            }
        } catch (e) {
            console.error('KG graph error:', e);
            if (seq === kgSeq && agent === currentAgent) {
                kgNodes = [];
                kgEdges = [];
            }
        } finally {
            if (seq === kgSeq) kgLoading = false;
        }
    }

    function nodeRadius(degree) {
        // sqrt scaling + hard cap so hub nodes don't swallow the canvas
        return Math.max(9, Math.min(30, 8 + Math.sqrt(degree) * 5));
    }

    function initKgGraph(data) {
        // Reuse persistent node objects so positions survive reload/filter changes.
        const map = new Map();
        for (const n of data.nodes) {
            const prev = kgNodeMap.get(n.id);
            map.set(n.id, {
                ...n,
                x: prev?.x ?? (kgWidth / 2 + (Math.random() - 0.5) * 400),
                y: prev?.y ?? (kgHeight / 2 + (Math.random() - 0.5) * 260),
                vx: 0, vy: 0,
                r: nodeRadius(n.degree),
            });
        }
        kgNodeMap = map;
        kgRawNodes = data.nodes;
        kgRawEdges = data.edges;
        // adjacency for neighborhood/declutter lookups
        const adj = new Map();
        for (const n of data.nodes) adj.set(n.id, new Set());
        for (const e of data.edges) {
            adj.get(e.source)?.add(e.target);
            adj.get(e.target)?.add(e.source);
        }
        kgAdj = adj;
        kgAllTypes = [...new Set(data.nodes.map(n => n.type))].sort();
        if (kgActiveTypes.size === 0) kgActiveTypes = new Set(kgAllTypes);
        applyKgFilters();
    }

    function applyKgFilters() {
        const q = kgSearch.trim().toLowerCase();
        const typeOk = (n) => kgActiveTypes.size === 0 || kgActiveTypes.has(n.type);
        let visIds;
        if (kgSelectedId) {
            // neighborhood of the selected node (depth 1) — shown regardless of degree/type for context
            visIds = new Set([kgSelectedId]);
            for (const nb of (kgAdj.get(kgSelectedId) || [])) visIds.add(nb);
        } else if (q) {
            const matched = kgRawNodes.filter(n => typeOk(n) && n.label.toLowerCase().includes(q)).map(n => n.id);
            visIds = new Set(matched);
            for (const id of matched) for (const nb of (kgAdj.get(id) || [])) visIds.add(nb);
        } else {
            visIds = new Set(kgRawNodes.filter(n => typeOk(n) && n.degree >= kgMinDegree).map(n => n.id));
        }
        const nodes = [];
        for (const id of visIds) { const o = kgNodeMap.get(id); if (o) nodes.push(o); }
        kgNodes = nodes;
        kgEdges = kgRawEdges.filter(e => visIds.has(e.source) && visIds.has(e.target));
        restartKgSim();
    }

    // type-cluster centroids on a ring so each entity type claims its own region
    function clusterCenter(type) {
        const i = Math.max(0, kgAllTypes.indexOf(type));
        const n = Math.max(1, kgAllTypes.length);
        const angle = (i / n) * Math.PI * 2 - Math.PI / 2;
        const rx = kgWidth * 0.30, ry = kgHeight * 0.30;
        return { x: kgWidth / 2 + Math.cos(angle) * rx, y: kgHeight / 2 + Math.sin(angle) * ry };
    }

    function restartKgSim() {
        if (kgRaf) { cancelAnimationFrame(kgRaf); kgRaf = null; }
        kgSimRunning = false;
        runKgSimulation();
    }

    function runKgSimulation() {
        if (kgSimRunning) return;
        kgSimRunning = true;
        let iteration = 0;
        const maxIter = 240;
        function tick() {
            if (kgDestroyed) { kgSimRunning = false; return; }
            if (iteration >= maxIter) { kgSimRunning = false; kgRaf = null; return; }
            iteration++;
            const decay = 1 - iteration / maxIter;
            const len = kgNodes.length;
            // Repulsion (O(n^2), but on the FILTERED subset — small)
            for (let i = 0; i < len; i++) {
                for (let j = i + 1; j < len; j++) {
                    const a = kgNodes[i], b = kgNodes[j];
                    let dx = b.x - a.x, dy = b.y - a.y;
                    let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    const force = 900 / (dist * dist) * decay;
                    const fx = dx / dist * force, fy = dy / dist * force;
                    a.vx -= fx; a.vy -= fy;
                    b.vx += fx; b.vy += fy;
                }
            }
            // Attraction along edges
            for (const e of kgEdges) {
                const a = kgNodeMap.get(e.source), b = kgNodeMap.get(e.target);
                if (!a || !b) continue;
                let dx = b.x - a.x, dy = b.y - a.y;
                let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const force = (dist - 90) * 0.012 * decay;
                const fx = dx / dist * force, fy = dy / dist * force;
                a.vx += fx; a.vy += fy;
                b.vx -= fx; b.vy -= fy;
            }
            // Cluster gravity: pull each node toward its type's centroid
            for (const nd of kgNodes) {
                const c = clusterCenter(nd.type);
                nd.vx += (c.x - nd.x) * 0.012 * decay;
                nd.vy += (c.y - nd.y) * 0.012 * decay;
                nd.vx *= 0.9; nd.vy *= 0.9;
                nd.x += nd.vx; nd.y += nd.vy;
                nd.x = Math.max(nd.r + 8, Math.min(kgWidth - nd.r - 8, nd.x));
                nd.y = Math.max(nd.r + 8, Math.min(kgHeight - nd.r - 8, nd.y));
            }
            kgNodes = kgNodes; // trigger reactivity
            kgRaf = requestAnimationFrame(tick);
        }
        kgRaf = requestAnimationFrame(tick);
    }

    const KG_COLORS = {
        person: '#ff6b9d', project: '#f5a623', tool: '#4a9eff',
        concept: '#c678dd', agent: '#50c878', company: '#e06c75',
        location: '#56b6c2', server: '#d19a66', user: '#98c379', unknown: '#888',
    };
    function kgNodeColor(node) { return KG_COLORS[node.type] || KG_COLORS.unknown; }

    // --- filters / selection ---
    function kgToggleType(t) {
        const s = new Set(kgActiveTypes);
        if (s.has(t)) s.delete(t); else s.add(t);
        kgActiveTypes = s;
        applyKgFilters();
    }
    function kgOnSearch() { kgSelectedId = null; applyKgFilters(); }
    function kgOnMinDegree() { kgSelectedId = null; applyKgFilters(); }
    function kgSelectNode(node) {
        kgSelectedId = (kgSelectedId === node.id) ? null : node.id;
        applyKgFilters();
    }
    function kgResetView() {
        kgZoom = 1; kgPanX = 0; kgPanY = 0;
        kgSelectedId = null; kgSearch = ''; kgMinDegree = 2;
        kgActiveTypes = new Set(kgAllTypes);
        applyKgFilters();
    }
    function kgLabelVisible(node) {
        return kgHoveredNode?.id === node.id
            || kgSelectedId === node.id
            || (kgSelectedId && kgAdj.get(kgSelectedId)?.has(node.id))
            || node.degree >= KG_HUB_DEGREE;
    }
    function kgNodeDimmed(node) {
        if (!kgHoveredNode) return false;
        return kgHoveredNode.id !== node.id && !(kgAdj.get(kgHoveredNode.id)?.has(node.id));
    }

    // --- pan / zoom (background-drag pans, wheel zooms at cursor) ---
    function kgWheel(e) {
        e.preventDefault();
        const rect = kgSvgEl.getBoundingClientRect();
        const vbX = (e.clientX - rect.left) * (kgWidth / rect.width);
        const vbY = (e.clientY - rect.top) * (kgHeight / rect.height);
        const gx = (vbX - kgPanX) / kgZoom, gy = (vbY - kgPanY) / kgZoom;
        const factor = e.deltaY < 0 ? 1.12 : 1 / 1.12;
        kgZoom = Math.max(0.3, Math.min(4, kgZoom * factor));
        kgPanX = vbX - gx * kgZoom;
        kgPanY = vbY - gy * kgZoom;
    }
    function kgPointerDown(e) { kgPanning = true; kgPanLast = { x: e.clientX, y: e.clientY }; }
    function kgPointerMove(e) {
        if (!kgPanning || !kgPanLast) return;
        const rect = kgSvgEl.getBoundingClientRect();
        kgPanX += (e.clientX - kgPanLast.x) * (kgWidth / rect.width);
        kgPanY += (e.clientY - kgPanLast.y) * (kgHeight / rect.height);
        kgPanLast = { x: e.clientX, y: e.clientY };
    }
    function kgPointerUp() { kgPanning = false; kgPanLast = null; }

    function switchTab(tab) {
        activeTab = tab;
        if (tab === 'chat' && currentAgent && chatMessages.length === 0) loadChatHistory();
        if (tab === 'graph' && currentAgent && kgRawNodes.length === 0 && !kgLoading) loadKnowledgeGraph();
        if (tab === 'dreams') loadDreamHistory();
    }

    onMount(init);
    onDestroy(() => { kgDestroyed = true; if (kgRaf) cancelAnimationFrame(kgRaf); kgSimRunning = false; });
</script>

<div class="content">
    <div class="controls toolbar-surface">
        <div class="controls-group">
            <span class="controls-label">{$_('memories.agent_label')}</span>
            <select class="form-select" bind:value={currentAgent} on:change={onAgentChange}>
                <option value="">{$_('memories.select_agent')}</option>
                {#each agentList as a}
                    <option value={a.name}>{a.name}</option>
                {/each}
            </select>
        </div>
    </div>

    <!-- Tabs -->
    {#if currentAgent}
        <TabBar tabs={[{id:'memories'},{id:'graph'},{id:'chat'},{id:'dreams'}]} active={activeTab} i18nPrefix="memories.tab_" on:change={(e) => switchTab(e.detail)} />
    {/if}

    <!-- Search bar (context-aware) -->
    {#if activeTab === 'memories'}
        <div class="search-bar" style="margin-bottom:1rem">
            <input type="text" class="form-input" bind:value={searchInput} placeholder={$_('memories.search_placeholder')} on:keydown={e => { if (e.key === 'Enter') doSearch(); }}>
            <button class="btn btn-primary" on:click={doSearch} disabled={memLoading}>{$_('common.search')}</button>
        </div>
    {:else if activeTab === 'chat'}
        <div class="search-bar" style="margin-bottom:0.5rem">
            <input type="text" class="form-input" bind:value={chatSearchInput} placeholder={$_('memories.chat_search_placeholder')} on:keydown={e => { if (e.key === 'Enter') searchChat(); }}>
            <button class="btn btn-primary" on:click={searchChat}>{$_('common.search')}</button>
        </div>
        <div class="filter-bar" style="margin-bottom:1rem;padding:0.8rem 0;border:none">
            <span class="controls-label">{$_('memories.after')}</span>
            <input type="date" class="form-input" bind:value={chatAfter} on:change={loadChatHistory} style="font-size:0.75rem;padding:0.3rem 0.5rem">
            <span class="controls-label">{$_('memories.before')}</span>
            <input type="date" class="form-input" bind:value={chatBefore} on:change={loadChatHistory} style="font-size:0.75rem;padding:0.3rem 0.5rem">
            <span class="controls-label">{$_('memories.role')}</span>
            <select class="form-select" bind:value={chatRole} on:change={loadChatHistory} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                <option value="">{$_('common.all')}</option>
                <option value="user">{$_('memories.role_user')}</option>
                <option value="assistant">{$_('memories.role_assistant')}</option>
            </select>
            {#if chatAfter || chatBefore || chatRole}
                <button class="btn btn-sm" on:click={() => { chatAfter = ''; chatBefore = ''; chatRole = ''; loadChatHistory(); }}>{$_('common.clear')}</button>
            {/if}
        </div>
    {/if}

    {#if activeTab === 'memories'}
    <!-- Stats (memories tab only) -->
    {#if statsVisible}
        <div class="section">
            <div class="stats-bar">{@html statsHtml}</div>
            <div class="filter-bar">
                <span class="controls-label">{$_('memories.type_label')}</span>
                <select class="form-select" bind:value={filterType} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">{$_('memories.all_types')}</option>
                    <option value="insight">{$_('memories.type_insight')}</option>
                    <option value="fact">{$_('memories.type_fact')}</option>
                    <option value="project_state">{$_('memories.type_project_state')}</option>
                    <option value="interaction_pattern">{$_('memories.type_interaction_pattern')}</option>
                    <option value="continuation">{$_('memories.type_continuation')}</option>
                </select>
                <span class="controls-label">{$_('memories.project_label')}</span>
                <select class="form-select" bind:value={filterProject} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">{$_('memories.all_projects')}</option>
                    {#each projectOptions as p}<option value={p.value}>{p.label}</option>{/each}
                </select>
                <span class="controls-label">{$_('memories.salience_label')}</span>
                <select class="form-select" bind:value={filterSalience} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="">{$_('common.all')}</option><option value="high">{$_('memories.salience_high')}</option>
                    {#each [1,2,3,4,5] as n}<option value={n}>{n}</option>{/each}
                </select>
                <span class="controls-label">{$_('memories.sort_label')}</span>
                <select class="form-select" bind:value={filterSort} on:change={applyFilters} style="font-size:0.75rem;padding:0.3rem 0.5rem">
                    <option value="created_at">{$_('memories.sort_created')}</option><option value="accessed_at">{$_('memories.sort_accessed')}</option>
                    <option value="salience">{$_('memories.sort_salience')}</option><option value="access_count">{$_('memories.sort_access_count')}</option>
                </select>
                <button class="toggle-btn" class:active={activeOnly} on:click={toggleActiveOnly}>{$_('memories.active_only')}</button>
            </div>
        </div>
    {/if}

    <!-- Memory Grid -->
    {#if memLoading && memories.length === 0}
        <div class="empty">{$_('common.loading')}</div>
    {:else if memories.length === 0}
        <div class="empty">{isSearchMode ? $_('memories.no_results') : $_('memories.no_memories')}</div>
    {:else}
        <div class="memory-grid">
            {#each memories as m}
                {@const type = m.type || 'fact'}
                {@const salience = m.salience || 0}
                {@const truncated = (m.content || '').length > 200}
                {@const displayContent = truncated ? (m.content || '').substring(0, 200) + '...' : (m.content || '')}
                {@const isActive = m.active !== false}
                <div class="memory-card type-{type}" class:card-inactive={!isActive} role="button" tabindex="0" on:click={() => openDetail(m.id || m._id)} on:keydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openDetail(m.id || m._id); } }}>
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
            <button class="btn" on:click={prevPage} disabled={currentOffset === 0 || memLoading}>{$_('common.prev')}</button>
            <span class="controls-label" style="align-self:center">{$_('common.page_of', { values: { current: currentPage, total: totalPages } })}</span>
            <button class="btn" on:click={nextPage} disabled={currentOffset + PAGE_SIZE >= totalCount || memLoading}>{$_('common.next')}</button>
        </div>
    {/if}

    {:else if activeTab === 'graph'}
    <!-- Knowledge Graph Tab -->
    {#if kgLoading}
        <div class="empty">Loading knowledge graph...</div>
    {:else if kgRawNodes.length === 0}
        <div class="empty" style="text-align:center;padding:3rem">
            <span class="material-symbols-outlined" style="font-size:48px;opacity:0.3">hub</span>
            <p style="margin-top:0.5rem">No knowledge graph data yet.</p>
            <p style="font-size:0.78rem;color:var(--text-muted)">Use <code>kg_add()</code> to add entity relationships.</p>
        </div>
    {:else}
        {#if kgStats}
            <div class="stats-bar section" style="margin-bottom:1rem">
                <div class="stat-item"><span class="stat-value">{kgStats.entities}</span><span class="stat-label">Entities</span></div>
                <div class="stat-item"><span class="stat-value">{kgStats.triples_active}</span><span class="stat-label">Active Facts</span></div>
                <div class="stat-item"><span class="stat-value">{kgStats.triples_total}</span><span class="stat-label">Total Facts</span></div>
                <div class="stat-item"><span class="stat-value">{Object.keys(kgStats.predicates || {}).length}</span><span class="stat-label">Predicates</span></div>
            </div>
        {/if}
        <div class="section" style="padding:1rem">
            <!-- Controls -->
            <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap;margin-bottom:0.6rem">
                <input
                    type="text"
                    class="form-input"
                    placeholder="Search nodes…"
                    bind:value={kgSearch}
                    on:input={kgOnSearch}
                    style="flex:1;min-width:140px;font-size:0.78rem"
                />
                <label style="display:flex;align-items:center;gap:0.3rem;font-family:var(--font-grotesk);font-size:0.7rem;color:var(--text-muted)">
                    min&nbsp;deg
                    <select class="form-select" bind:value={kgMinDegree} on:change={kgOnMinDegree} style="font-size:0.75rem;padding:0.2rem">
                        <option value={0}>all</option>
                        <option value={2}>2+</option>
                        <option value={3}>3+</option>
                        <option value={5}>5+</option>
                    </select>
                </label>
                <label style="display:flex;align-items:center;gap:0.3rem;font-family:var(--font-grotesk);font-size:0.7rem;color:var(--text-muted);cursor:pointer">
                    <input type="checkbox" bind:checked={kgShowEdgeLabels} /> edge labels
                </label>
                <button class="btn btn-sm" style="background:var(--surface-3);color:var(--text-muted);font-size:0.72rem" on:click={kgResetView}>Reset</button>
                <button class="btn btn-sm" style="background:var(--surface-3);color:var(--text-muted);font-size:0.72rem" on:click={loadKnowledgeGraph}>Refresh</button>
            </div>

            <!-- Type filter chips (click to toggle) -->
            <div style="display:flex;gap:0.4rem;flex-wrap:wrap;margin-bottom:0.5rem">
                {#each kgAllTypes as type}
                    {@const on = kgActiveTypes.has(type)}
                    <button
                        on:click={() => kgToggleType(type)}
                        style="display:flex;align-items:center;gap:0.3rem;border:1px solid {on ? KG_COLORS[type] || KG_COLORS.unknown : 'var(--surface-3)'};background:{on ? 'transparent' : 'var(--surface-2)'};border-radius:99px;padding:0.12rem 0.55rem;cursor:pointer;font-family:var(--font-grotesk);font-size:0.68rem;color:{on ? 'var(--text-primary)' : 'var(--text-muted)'};opacity:{on ? 1 : 0.55}"
                    >
                        <span style="width:9px;height:9px;border-radius:50%;background:{KG_COLORS[type] || KG_COLORS.unknown};display:inline-block"></span>
                        {type}
                    </button>
                {/each}
            </div>

            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.4rem">
                <span style="font-family:var(--font-grotesk);font-size:0.72rem;color:var(--text-muted)">
                    {kgNodes.length} / {kgRawNodes.length} nodes · {kgEdges.length} edges
                    {#if kgSelectedId}· <span style="color:var(--accent-contrast)">focus: {kgSelectedId}</span> <button on:click={() => { kgSelectedId = null; applyKgFilters(); }} style="background:none;border:none;color:var(--text-muted);cursor:pointer;text-decoration:underline;font-size:0.7rem">clear</button>{/if}
                </span>
                <span style="font-size:0.62rem;color:var(--text-muted)">scroll = zoom · drag = pan · click node = focus</span>
            </div>

            <!-- svelte-ignore a11y_no_static_element_interactions a11y_no_noninteractive_element_interactions -->
            <svg
                bind:this={kgSvgEl}
                width={kgWidth}
                height={kgHeight}
                style="background:var(--surface-inverse);border-radius:8px;cursor:{kgPanning ? 'grabbing' : 'grab'};width:100%;touch-action:none"
                viewBox="0 0 {kgWidth} {kgHeight}"
                role="application"
                aria-label="Knowledge graph"
                on:wheel={kgWheel}
                on:mousedown={kgPointerDown}
                on:mousemove={kgPointerMove}
                on:mouseup={kgPointerUp}
                on:mouseleave={kgPointerUp}
            >
                <g transform="translate({kgPanX} {kgPanY}) scale({kgZoom})">
                    <!-- Edges -->
                    {#each kgEdges as edge}
                        {@const src = kgNodeMap.get(edge.source)}
                        {@const tgt = kgNodeMap.get(edge.target)}
                        {#if src && tgt}
                            {@const lit = (kgHoveredNode && (kgHoveredNode.id === edge.source || kgHoveredNode.id === edge.target)) || (kgSelectedId && (kgSelectedId === edge.source || kgSelectedId === edge.target))}
                            <line
                                x1={src.x} y1={src.y}
                                x2={tgt.x} y2={tgt.y}
                                stroke={lit ? 'rgba(255,255,255,0.55)' : 'rgba(255,255,255,0.14)'}
                                stroke-width={lit ? 2 : 1}
                            />
                            {#if kgShowEdgeLabels || lit}
                                <text
                                    x={(src.x + tgt.x) / 2}
                                    y={(src.y + tgt.y) / 2 - 4}
                                    text-anchor="middle"
                                    fill="rgba(255,255,255,0.5)"
                                    font-size="9px"
                                    font-family="monospace"
                                >{edge.label}</text>
                            {/if}
                        {/if}
                    {/each}

                    <!-- Nodes -->
                    {#each kgNodes as node (node.id)}
                        {@const dim = kgNodeDimmed(node)}
                        {@const sel = kgSelectedId === node.id}
                        <!-- svelte-ignore a11y_click_events_have_key_events -->
                        <g
                            role="button"
                            tabindex="0"
                            style="cursor:pointer"
                            on:click|stopPropagation={() => kgSelectNode(node)}
                            on:keydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); kgSelectNode(node); } }}
                            on:mouseenter={() => kgHoveredNode = node}
                            on:mouseleave={() => kgHoveredNode = null}
                        >
                            <circle
                                cx={node.x} cy={node.y} r={node.r}
                                fill={kgNodeColor(node)}
                                stroke={sel || kgHoveredNode?.id === node.id ? '#fff' : 'rgba(255,255,255,0.2)'}
                                stroke-width={sel ? 3 : kgHoveredNode?.id === node.id ? 2.5 : 1}
                                opacity={dim ? 0.25 : 0.92}
                            />
                            {#if kgLabelVisible(node)}
                                <text
                                    x={node.x} y={node.y + node.r + 13}
                                    text-anchor="middle"
                                    fill={dim ? 'rgba(255,255,255,0.3)' : kgHoveredNode?.id === node.id || sel ? '#fff' : 'rgba(255,255,255,0.7)'}
                                    font-size="11px"
                                    font-family="monospace"
                                >
                                    {node.label.length > 22 ? node.label.slice(0, 20) + '…' : node.label}
                                </text>
                            {/if}
                        </g>
                    {/each}

                    <!-- Tooltip -->
                    {#if kgHoveredNode}
                        <rect
                            x={kgHoveredNode.x + kgHoveredNode.r + 8}
                            y={kgHoveredNode.y - 16}
                            width={Math.max(140, kgHoveredNode.label.length * 7 + 60)}
                            height="32"
                            rx="4"
                            fill="rgba(0,0,0,0.85)"
                            stroke="rgba(255,255,255,0.2)"
                        />
                        <text
                            x={kgHoveredNode.x + kgHoveredNode.r + 14}
                            y={kgHoveredNode.y + 1}
                            fill="#fff"
                            font-size="12px"
                            font-family="monospace"
                        >
                            {kgHoveredNode.label} ({kgHoveredNode.type}) · {kgHoveredNode.degree}
                        </text>
                    {/if}
                </g>
            </svg>

            <div style="display:flex;justify-content:center;margin-top:0.5rem;font-family:var(--font-grotesk);font-size:0.62rem;color:var(--text-muted)">
                node size = connections · nodes cluster by type · labels show on hover &amp; for hubs
            </div>
        </div>
    {/if}

    {:else if activeTab === 'chat'}
    <!-- Chat History Tab -->
    {#if chatMessages.length === 0}
        <div class="empty">{chatSearchInput ? $_('memories.no_messages_found') : $_('memories.no_chat_history')}</div>
    {:else}
        <div class="chat-count" style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid);margin-bottom:1rem">{chatCount} message{chatCount !== 1 ? 's' : ''}{chatSearchInput ? ` matching "${chatSearchInput}"` : ''}</div>
        <div class="chat-list">
            {#each chatMessages as msg}
                <div class="chat-item" class:chat-user={msg.role === 'user'} class:chat-assistant={msg.role === 'assistant'}>
                    <div class="chat-item-header">
                        <span class="chat-role-badge" class:user={msg.role === 'user'} class:assistant={msg.role === 'assistant'}>{msg.role}</span>
                        <span class="chat-session">{msg.session_id}</span>
                        <span class="chat-time">{msg.timestamp ? new Date(msg.timestamp * 1000).toLocaleString() : '--'}</span>
                        {#if msg.duration_ms}
                            <span class="chat-duration">{(msg.duration_ms / 1000).toFixed(1)}s</span>
                        {/if}
                    </div>
                    <div class="chat-item-content">{msg.content}</div>
                </div>
            {/each}
        </div>
    {/if}

    {:else if activeTab === 'dreams'}
    <!-- Dreams Tab -->
    {#if dreamLoading}
        <div class="empty">{$_('memories.loading_dreams')}</div>
    {:else if dreamStates.length === 0}
        <div class="empty">{$_('memories.no_dreams')}{#if currentAgent} {$_('memories.enable_dreaming')}{:else} {$_('memories.select_agent_or_all')}{/if}</div>
    {:else}
        <div style="display:flex;flex-direction:column;gap:1rem">
            {#each dreamStates as ds}
                <div class="section" style="padding:1.2rem 1.5rem">
                    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem">
                        <span style="font-family:var(--font-grotesk);font-size:0.9rem;font-weight:700">{ds.agent_name}</span>
                        <div style="display:flex;gap:0.5rem;align-items:center">
                            <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">
                                {ds.last_dream_at ? new Date(ds.last_dream_at * 1000).toLocaleString() : 'Never'}
                            </span>
                            <button class="btn btn-sm btn-primary" disabled={dreamingAgent !== null} on:click={() => triggerDream(ds.agent_name)}>{dreamingAgent === ds.agent_name ? $_('agents.dreaming') : $_('memories.dream_now')}</button>
                        </div>
                    </div>
                    {#if ds.last_summary}
                        <div style="font-size:0.85rem;line-height:1.6;white-space:pre-wrap;background:var(--gray-light);padding:1rem;border-radius:var(--radius-lg)">{ds.last_summary}</div>
                    {:else}
                        <div style="font-size:0.85rem;color:var(--gray-mid)">{$_('memories.no_dream_summary')}</div>
                    {/if}
                </div>
            {/each}
        </div>
    {/if}
    {/if}
</div>

<Modal bind:show={modalOpen} title={modalTitle} width="700px" maxWidth="700px">
    {@html modalBody}
</Modal>

<style>
    .controls { display: flex; gap: 1rem; flex-wrap: wrap; align-items: center; margin-bottom: 1.5rem; }
    .controls-group { display: flex; gap: 0.5rem; align-items: center; }
    .controls-label { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); white-space: nowrap; }
    .search-bar { display: flex; gap: 0.5rem; flex: 1; min-width: 250px; }

    .stats-bar { display: flex; gap: 2rem; flex-wrap: wrap; padding: 1rem 1.5rem; font-family: var(--font-grotesk); font-size: 0.8rem; }
    :global(.stat-item) { display: flex; flex-direction: column; }
    :global(.stat-value) { font-size: 1.4rem; font-weight: 700; }
    :global(.stat-label) { font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); letter-spacing: 0.05em; }

    .filter-bar { display: flex; gap: 0.8rem; flex-wrap: wrap; align-items: center; padding: 1rem 1.5rem; background: var(--surface-2); border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
    .toggle-btn { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; padding: 0.3rem 0.6rem; border: none; background: var(--surface-3); border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; color: var(--text-muted); transition: all 0.1s; }
    .toggle-btn.active { color: var(--on-primary-container); background: var(--primary-container); }

    .memory-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(380px, 1fr)); gap: 1rem; }
    .memory-card { background: var(--surface-container-lowest, var(--white)); border: none; border-left: 5px solid var(--text-muted); border-radius: var(--radius-lg); padding: 1.2rem; cursor: pointer; transition: box-shadow 0.15s; position: relative; }
    .memory-card:hover { box-shadow: 4px 4px 0 var(--shadow-color); }
    .memory-card.type-insight { border-left-color: var(--blue); }
    .memory-card.type-fact { border-left-color: var(--gray-dark); }
    .memory-card.type-project_state { border-left-color: var(--green); }
    .memory-card.type-interaction_pattern { border-left-color: var(--yellow); }
    .memory-card.type-continuation { border-left-color: var(--text-muted); }
    .card-inactive { opacity: 0.7; border-style: dashed; }

    .card-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.6rem; }
    .type-badge { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.15rem 0.5rem; text-transform: uppercase; border-radius: var(--radius); }
    :global(.type-badge.insight) { background: var(--tone-info-bg); color: var(--tone-info-text); }
    :global(.type-badge.fact) { background: var(--tone-neutral-bg); color: var(--tone-neutral-text); }
    :global(.type-badge.project_state) { background: var(--tone-success-bg); color: var(--tone-success-text); }
    :global(.type-badge.interaction_pattern) { background: var(--tone-warning-bg); color: var(--tone-warning-text); }
    :global(.type-badge.continuation) { background: var(--surface-3); color: var(--text-muted); }

    .salience-dots { display: flex; gap: 3px; }
    .salience-dot { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--text-muted); }
    .salience-dot.filled { background: var(--yellow); border-color: var(--yellow); }

    .card-content { font-size: 0.88rem; line-height: 1.5; margin-bottom: 0.6rem; word-break: break-word; }
    .card-content.truncated { max-height: 4.5em; overflow: hidden; position: relative; }
    .card-context { font-size: 0.78rem; color: var(--text-muted); margin-bottom: 0.6rem; }
    .card-tags { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.6rem; }
    .card-tag { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.4rem; background: var(--tone-neutral-bg); color: var(--tone-neutral-text); text-transform: uppercase; border-radius: var(--radius); }
    .card-tag.project { background: var(--tone-lilac-bg); color: var(--tone-lilac-text); }
    .card-tag.entity { background: var(--tone-info-bg); color: var(--tone-info-text); }
    .card-tag.source { background: var(--tone-warning-bg); color: var(--tone-warning-text); }
    .card-footer { display: flex; justify-content: space-between; align-items: center; font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); padding-top: 0.6rem; }
    .card-footer-left { display: flex; gap: 1rem; }
    .pagination { display: flex; justify-content: center; gap: 0.5rem; margin-top: 1.5rem; }

    :global(.detail-field) { margin-bottom: 1rem; }
    :global(.detail-label) { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.3rem; }
    :global(.detail-value) { font-family: var(--font-body); font-size: 0.85rem; line-height: 1.6; }
    :global(.detail-meta-grid) { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; }

    /* Tabs — chip pattern */
    .tab-bar { display: flex; gap: 0.3rem; margin-bottom: 1rem; }
    .tab-btn { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; padding: 0.5rem 1.2rem; border: none; background: var(--surface-2); border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; color: var(--text-muted); transition: all 0.1s; }
    .tab-btn.active { background: var(--surface-inverse); color: var(--primary-container); }
    .tab-btn:hover:not(.active) { background: var(--surface-3); }

    /* Chat History */
    .chat-list { display: flex; flex-direction: column; gap: 0.5rem; }
    .chat-item { background: var(--surface-1); border: none; padding: 1rem; border-left: 4px solid var(--text-muted); border-radius: var(--radius-lg); }
    .chat-item.chat-user { border-left-color: var(--primary-container); }
    .chat-item.chat-assistant { border-left-color: var(--blue); }
    .chat-item-header { display: flex; gap: 0.8rem; align-items: center; margin-bottom: 0.5rem; flex-wrap: wrap; }
    .chat-role-badge { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.4rem; text-transform: uppercase; border-radius: var(--radius); }
    .chat-role-badge.user { background: var(--primary-container); color: var(--on-primary-container); }
    .chat-role-badge.assistant { background: var(--tone-info-bg); color: var(--tone-info-text); }
    .chat-session { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); }
    .chat-time { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); }
    .chat-duration { font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--text-muted); background: var(--surface-2); padding: 0.1rem 0.3rem; border-radius: var(--radius); }
    .chat-item-content { font-size: 0.88rem; line-height: 1.5; white-space: pre-wrap; word-break: break-word; }

    @media (max-width: 900px) {
        .memory-grid { grid-template-columns: 1fr; }
        .controls { flex-direction: column; align-items: stretch; }
        :global(.detail-meta-grid) { grid-template-columns: 1fr; }
    }
</style>
