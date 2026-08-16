<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import TabBar from '../components/TabBar.svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    import { escapeHtml } from '../lib/utils.js';
    import { createForceSim, fitView, animateView, computeLabelSet, edgePath, zoomAt, hexToRgba } from '../lib/forceGraph.js';

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
    let memSeq = 0; let statsSeq = 0; let chatSeq = 0; let kgSeq = 0; let dreamSeq = 0;

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
    let kgWidth = 1000;
    let kgHeight = 640;
    let kgHoveredNode = null;
    let kgSim = null;            // createForceSim handle
    let kgFitCancel = null;      // in-flight auto-fit animation canceller
    let kgLabelSet = new Set();  // decluttered label set (recomputed at settle)
    let kgUserMoved = false;     // user panned/zoomed — don't yank the view on settle
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

    // Edit / delete (#463)
    let editOpen = false;
    let editTarget = null;
    let editContent = '';
    let editSaving = false;
    let deleteOpen = false;
    let deleteTarget = null;
    let deleteHardStep = false;   // second confirmation, only for the hard delete
    let deleteBusy = false;

    // Dream edit / delete
    let dreamEditOpen = false;
    let dreamEditAgent = '';
    let dreamEditContent = '';
    let dreamEditSaving = false;
    let dreamDeleteOpen = false;
    let dreamDeleteAgent = '';
    let dreamDeleteBusy = false;

    function openDreamEdit(ds) {
        dreamEditAgent = ds.agent_name;
        dreamEditContent = ds.last_summary || '';
        dreamEditOpen = true;
    }
    async function saveDreamEdit() {
        if (!dreamEditContent.trim() || !dreamEditAgent) return;
        dreamEditSaving = true;
        try {
            await api('PATCH', `/agents/${dreamEditAgent}/dream`, { summary: dreamEditContent });
            const idx = dreamStates.findIndex(d => d.agent_name === dreamEditAgent);
            if (idx >= 0) dreamStates[idx].last_summary = dreamEditContent;
            dreamStates = dreamStates;
            dreamEditOpen = false;
            toast($_('memories.dream_edit_saved') || 'Dream updated');
        } catch (e) { toast(`Edit failed: ${e.message}`, 'error'); }
        finally { dreamEditSaving = false; }
    }
    function openDreamDelete(ds) {
        dreamDeleteAgent = ds.agent_name;
        dreamDeleteOpen = true;
    }
    async function confirmDreamDelete() {
        dreamDeleteBusy = true;
        try {
            await api('DELETE', `/agents/${dreamDeleteAgent}/dream`);
            dreamStates = dreamStates.filter(d => d.agent_name !== dreamDeleteAgent);
            dreamDeleteOpen = false;
            toast($_('memories.dream_deleted') || 'Dream deleted');
        } catch (e) { toast(`Delete failed: ${e.message}`, 'error'); }
        finally { dreamDeleteBusy = false; }
    }

    // Chat message edit / delete
    let chatEditOpen = false;
    let chatEditMsg = null;
    let chatEditContent = '';
    let chatEditSaving = false;
    let chatDeleteOpen = false;
    let chatDeleteMsg = null;
    let chatDeleteBusy = false;

    function openChatEdit(msg) {
        chatEditMsg = msg;
        chatEditContent = msg.content || '';
        chatEditOpen = true;
    }
    async function saveChatEdit() {
        if (!chatEditContent.trim() || !chatEditMsg) return;
        chatEditSaving = true;
        try {
            await api('PATCH', `/agents/${currentAgent}/chat-history/${chatEditMsg.id}`, { content: chatEditContent });
            chatEditMsg.content = chatEditContent;
            chatMessages = chatMessages;
            chatEditOpen = false;
            toast('Message updated');
        } catch (e) { toast(`Edit failed: ${e.message}`, 'error'); }
        finally { chatEditSaving = false; }
    }
    function openChatDelete(msg) {
        chatDeleteMsg = msg;
        chatDeleteOpen = true;
    }
    async function confirmChatDelete() {
        chatDeleteBusy = true;
        try {
            await api('DELETE', `/agents/${currentAgent}/chat-history/${chatDeleteMsg.id}`);
            chatMessages = chatMessages.filter(m => m.id !== chatDeleteMsg.id);
            chatDeleteOpen = false;
            toast('Message deleted');
        } catch (e) { toast(`Delete failed: ${e.message}`, 'error'); }
        finally { chatDeleteBusy = false; }
    }

    function memId(m) { return m.id || m._id; }

    function openEdit(m) {
        editTarget = m;
        editContent = m.content || '';
        editOpen = true;
    }

    async function saveEdit() {
        if (!editTarget || editSaving) return;
        const content = editContent.trim();
        if (!content) { toast($_('memories.edit_empty'), 'error'); return; }
        const id = memId(editTarget);
        editSaving = true;
        try {
            const updated = await api('PATCH', `/agents/${currentAgent}/memories/${id}`, { content });
            memories = memories.map(m => (memId(m) === id ? { ...m, ...updated } : m));
            toast($_('memories.updated'));
            editOpen = false;
        } catch (e) { toast(`${$_('memories.edit_failed')}: ${e.message}`, 'error'); }
        finally { editSaving = false; }
    }

    function openDelete(m) {
        deleteTarget = m;
        deleteHardStep = false;
        deleteOpen = true;
    }

    async function confirmDelete(hard) {
        if (!deleteTarget || deleteBusy) return;
        // The permanent delete is the one that cannot be walked back, so it
        // costs a second, explicit click.
        if (hard && !deleteHardStep) { deleteHardStep = true; return; }
        const id = memId(deleteTarget);
        deleteBusy = true;
        try {
            await api('DELETE', `/agents/${currentAgent}/memories/${id}${hard ? '?hard=true' : ''}`);
            if (hard || activeOnly) {
                // Gone, or no longer matching the active-only filter.
                memories = memories.filter(m => memId(m) !== id);
                totalCount = Math.max(0, totalCount - 1);
            } else {
                memories = memories.map(m => (memId(m) === id ? { ...m, active: false } : m));
            }
            toast(hard ? $_('memories.deleted') : $_('memories.archived'));
            deleteOpen = false;
        } catch (e) { toast(`${$_('memories.delete_failed')}: ${e.message}`, 'error'); }
        finally { deleteBusy = false; }
    }

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
                Object.entries(byType).map(([k, v]) => `<div class="stat-item"><div class="stat-value">${v}</div><div class="stat-label">${escapeHtml(k.replace('_', ' '))}</div></div>`).join('');
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
            if (filterSalience === 'high') params.set('salience_min', 4);
            else if (filterSalience) { params.set('salience_min', filterSalience); params.set('salience_max', filterSalience); }
            if (filterSort) params.set('sort_by', filterSort);
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
        const seq = ++dreamSeq; const agent = currentAgent;
        try {
            if (agent) {
                const data = await api('GET', `/agents/${agent}/dream/history`);
                if (seq !== dreamSeq || agent !== currentAgent) return;
                dreamStates = data.dream_state ? [data.dream_state] : [];
            } else {
                const data = await api('GET', '/dreams');
                if (seq !== dreamSeq || agent !== currentAgent) return;
                dreamStates = data.dream_states || [];
            }
        } catch (e) {
            if (seq === dreamSeq && agent === currentAgent) {
                console.error('Dream history error:', e);
                dreamStates = [];
            }
        } finally {
            if (seq === dreamSeq && agent === currentAgent) dreamLoading = false;
        }
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
                // Empty graph: clear ALL raw state too, or the no-data branch
                // (gated on kgRawNodes.length) is skipped and stale chips/counts
                // linger over an empty canvas. (Murzik review, PR #655.)
                clearKgState();
            }
        } catch (e) {
            console.error('KG graph error:', e);
            if (seq === kgSeq && agent === currentAgent) clearKgState();
        } finally {
            if (seq === kgSeq) kgLoading = false;
        }
    }

    function nodeRadius(degree) {
        // sqrt scaling + hard cap so hub nodes don't swallow the canvas
        return Math.max(7, Math.min(26, 6 + Math.sqrt(degree) * 4.5));
    }

    // Reset every piece of KG state (rendered + raw + filters + view + sim) so
    // an empty/errored (re)load shows a truthful no-data state. (Murzik review.)
    function clearKgState() {
        if (kgSim) { kgSim.stop(); kgSim = null; }
        if (kgFitCancel) { kgFitCancel(); kgFitCancel = null; }
        kgNodes = []; kgEdges = [];
        kgRawNodes = []; kgRawEdges = [];
        kgNodeMap = new Map(); kgAdj = new Map();
        kgAllTypes = []; kgActiveTypes = new Set();
        kgSelectedId = null; kgHoveredNode = null; kgSearch = '';
        kgLabelSet = new Set(); kgUserMoved = false;
        kgZoom = 1; kgPanX = 0; kgPanY = 0;
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

    // type-cluster centroids on a ring so each entity type claims its own region.
    // Wide radius + strong gravity (below) make the types separate into islands.
    function clusterCenter(type) {
        const i = Math.max(0, kgAllTypes.indexOf(type));
        const n = Math.max(1, kgAllTypes.length);
        const angle = (i / n) * Math.PI * 2 - Math.PI / 2;
        const rx = kgWidth * 0.40, ry = kgHeight * 0.38;
        return { x: kgWidth / 2 + Math.cos(angle) * rx, y: kgHeight / 2 + Math.sin(angle) * ry };
    }

    // "unknown" isn't a semantic cluster — it's missing taxonomy. Piling those
    // nodes into their own island rendered a giant dead-gray continent, so
    // anchor each one near the centroid of its TYPED neighbors' islands with a
    // deterministic per-node scatter — without the scatter, every gray whose
    // neighbors span types lands on the same centroid and knots up; with no
    // anchor at all, the link web drags the whole layout into one center blob.
    function kgIdHash(id) {
        let h = 0;
        const s = String(id);
        for (let i = 0; i < s.length; i++) h = ((h << 5) - h + s.charCodeAt(i)) | 0;
        return Math.abs(h);
    }
    function computeKgAnchors() {
        for (const node of kgNodes) {
            node._anchor = null;
            if (node.type !== 'unknown') continue;
            let sx = 0, sy = 0, k = 0;
            for (const nb of (kgAdj.get(node.id) || [])) {
                const o = kgNodeMap.get(nb);
                if (!o || o.type === 'unknown') continue;
                const c = clusterCenter(o.type);
                sx += c.x; sy += c.y; k++;
            }
            if (k === 0) continue;
            const h = kgIdHash(node.id);
            const angle = (h % 360) * Math.PI / 180;
            const radius = 60 + (h >> 3) % 110;
            node._anchor = {
                x: sx / k + Math.cos(angle) * radius,
                y: sy / k + Math.sin(angle) * radius,
            };
        }
    }

    function restartKgSim() {
        if (kgSim) { kgSim.stop(); kgSim = null; }
        if (kgDestroyed || kgNodes.length === 0) { kgLabelSet = new Set(); return; }
        computeKgAnchors();
        kgSim = createForceSim({
            nodes: kgNodes,
            edges: kgEdges,
            nodeById: kgNodeMap,
            clusterCenter: (n) => n._anchor || clusterCenter(n.type),
            gravity: 0.016,        // soft islands — repulsion does the spreading
            repulsion: 2800,
            linkBase: 110,         // longer leashes = looser hub neighborhoods
            onTick: (it) => {
                kgNodes = kgNodes;                          // trigger reactivity
                // keep the growing layout framed while it settles
                if (!kgUserMoved && it % 20 === 0) {
                    const v = fitView(kgNodes, kgWidth, kgHeight);
                    kgZoom = v.zoom; kgPanX = v.panX; kgPanY = v.panY;
                }
            },
            onSettle: () => {
                kgSim = null;
                // labels are screen-constant: declutter against the zoom the
                // view is about to animate to, not wherever it is mid-flight
                const target = kgUserMoved
                    ? { zoom: kgZoom }
                    : fitView(kgNodes, kgWidth, kgHeight);
                kgLabelSet = computeLabelSet(kgNodes, { zoom: target.zoom });
                kgAutoFit();
            },
        });
    }

    // Auto-zoom: frame the settled layout. Skipped when the user has already
    // panned/zoomed by hand (their view, their rules — Fit button re-engages).
    function kgAutoFit(force = false) {
        if (kgUserMoved && !force) return;
        if (kgFitCancel) kgFitCancel();
        const target = fitView(kgNodes, kgWidth, kgHeight);
        kgLabelSet = computeLabelSet(kgNodes, { zoom: target.zoom });
        kgFitCancel = animateView(
            { zoom: kgZoom, panX: kgPanX, panY: kgPanY },
            target,
            (v) => { kgZoom = v.zoom; kgPanX = v.panX; kgPanY = v.panY; },
        );
        if (force) kgUserMoved = false;
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
        kgSelectedId = null; kgSearch = ''; kgMinDegree = 2;
        kgActiveTypes = new Set(kgAllTypes);
        kgUserMoved = false;
        applyKgFilters();
    }
    function kgLabelVisible(node, labelSet) {
        return kgHoveredNode?.id === node.id
            || kgSelectedId === node.id
            || (kgSelectedId && kgAdj.get(kgSelectedId)?.has(node.id))
            || (kgHoveredNode && kgAdj.get(kgHoveredNode.id)?.has(node.id))
            || labelSet.has(node.id);
    }
    function kgNodeDimmed(node) {
        if (!kgHoveredNode) return false;
        return kgHoveredNode.id !== node.id && !(kgAdj.get(kgHoveredNode.id)?.has(node.id));
    }

    // --- pan / zoom (background-drag pans, wheel zooms at cursor) ---
    let kgLabelTimer = null;
    function kgWheel(e) {
        e.preventDefault();
        if (kgFitCancel) { kgFitCancel(); kgFitCancel = null; }
        kgUserMoved = true;
        const rect = kgSvgEl.getBoundingClientRect();
        const vbX = (e.clientX - rect.left) * (kgWidth / rect.width);
        const vbY = (e.clientY - rect.top) * (kgHeight / rect.height);
        const next = zoomAt({ zoom: kgZoom, panX: kgPanX, panY: kgPanY }, vbX, vbY, e.deltaY < 0 ? 1.12 : 1 / 1.12);
        kgZoom = next.zoom; kgPanX = next.panX; kgPanY = next.panY;
        // screen-constant labels free up room as you zoom in — re-declutter
        clearTimeout(kgLabelTimer);
        kgLabelTimer = setTimeout(() => { kgLabelSet = computeLabelSet(kgNodes, { zoom: kgZoom }); }, 150);
    }
    function kgPointerDown(e) { kgPanning = true; kgPanLast = { x: e.clientX, y: e.clientY }; }
    function kgPointerMove(e) {
        if (!kgPanning || !kgPanLast) return;
        if (kgFitCancel) { kgFitCancel(); kgFitCancel = null; }
        kgUserMoved = true;
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
    onDestroy(() => {
        kgDestroyed = true;
        if (kgLabelTimer) clearTimeout(kgLabelTimer);
        if (kgSim) { kgSim.stop(); kgSim = null; }
        if (kgFitCancel) { kgFitCancel(); kgFitCancel = null; }
    });
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
                        <div class="card-actions">
                            {#if !isActive}<span style="color:var(--red)">INACTIVE</span>{/if}
                            <button class="card-action" title={$_('common.edit')} aria-label={$_('common.edit')} on:click|stopPropagation={() => openEdit(m)}>✎</button>
                            <button class="card-action danger" title={$_('common.delete')} aria-label={$_('common.delete')} on:click|stopPropagation={() => openDelete(m)}>✕</button>
                        </div>
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
                <button class="btn btn-sm" style="background:var(--surface-3);color:var(--text-muted);font-size:0.72rem" on:click={() => kgAutoFit(true)}>Fit</button>
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
                <span style="font-size:0.62rem;color:var(--text-muted)">scroll = zoom · drag = pan · click node = focus · double-click = fit</span>
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
                on:dblclick={() => kgAutoFit(true)}
            >
                <g transform="translate({kgPanX} {kgPanY}) scale({kgZoom})">
                    <!-- Edges: curved + tinted by source-node cluster color (flavor-network look) -->
                    {#each kgEdges as edge}
                        {@const src = kgNodeMap.get(edge.source)}
                        {@const tgt = kgNodeMap.get(edge.target)}
                        {#if src && tgt}
                            {@const lit = (kgHoveredNode && (kgHoveredNode.id === edge.source || kgHoveredNode.id === edge.target)) || (kgSelectedId && (kgSelectedId === edge.source || kgSelectedId === edge.target))}
                            {@const sameType = src.type === tgt.type}
                            {@const heavy = (edge.weight || 1) > 1}
                            <!-- strokes/fonts divide by zoom: constant SCREEN size at any fit level.
                                 weight-graded alpha: repeated relations read strong, one-off facts
                                 fade back — kills the edge soup around hub nodes -->
                            <path
                                d={edgePath(src, tgt)}
                                fill="none"
                                stroke={lit ? 'rgba(255,255,255,0.6)' : hexToRgba(kgNodeColor(src), (sameType ? 0.42 : 0.26) * (heavy ? 1 : 0.55))}
                                stroke-width={(lit ? 2 : heavy ? 1.5 : 1) / kgZoom}
                            />
                            {#if (kgShowEdgeLabels && kgZoom >= 1.05) || lit}
                                <text
                                    x={(src.x + tgt.x) / 2}
                                    y={(src.y + tgt.y) / 2 - 4 / kgZoom}
                                    text-anchor="middle"
                                    fill="rgba(255,255,255,0.5)"
                                    font-size="{9 / kgZoom}px"
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
                            aria-label={`${node.label} (${node.type}), ${node.degree} connections`}
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
                                stroke-width={(sel ? 3 : kgHoveredNode?.id === node.id ? 2.5 : 1.2) / kgZoom}
                                opacity={dim ? 0.25 : 0.92}
                            />
                            {#if kgLabelVisible(node, kgLabelSet)}
                                <text
                                    x={node.x} y={node.y + node.r + 13 / kgZoom}
                                    text-anchor="middle"
                                    fill={dim ? 'rgba(255,255,255,0.3)' : kgHoveredNode?.id === node.id || sel ? '#fff' : 'rgba(255,255,255,0.78)'}
                                    font-size="{(node.degree >= 8 ? 12 : 11) / kgZoom}px"
                                    font-family="monospace"
                                    style="pointer-events:none"
                                >
                                    {node.label.length > 22 ? node.label.slice(0, 20) + '…' : node.label}
                                </text>
                            {/if}
                        </g>
                    {/each}

                    <!-- Tooltip (inverse-scaled group = constant screen size) -->
                    {#if kgHoveredNode}
                        <g transform="translate({kgHoveredNode.x} {kgHoveredNode.y}) scale({1 / kgZoom})">
                            <rect
                                x={kgHoveredNode.r * kgZoom + 8}
                                y="-16"
                                width={Math.max(140, kgHoveredNode.label.length * 7 + 60)}
                                height="32"
                                rx="4"
                                fill="rgba(0,0,0,0.85)"
                                stroke="rgba(255,255,255,0.2)"
                            />
                            <text
                                x={kgHoveredNode.r * kgZoom + 14}
                                y="4"
                                fill="#fff"
                                font-size="12px"
                                font-family="monospace"
                            >
                                {kgHoveredNode.label} ({kgHoveredNode.type}) · {kgHoveredNode.degree}
                            </text>
                        </g>
                    {/if}
                </g>
            </svg>

            <div style="display:flex;justify-content:center;margin-top:0.5rem;font-family:var(--font-grotesk);font-size:0.62rem;color:var(--text-muted)">
                node size = connections · nodes cluster by type · zoom in to reveal more labels
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
                        <span style="margin-left:auto;display:flex;gap:0.3rem">
                            <button class="card-action" title="Edit" on:click={() => openChatEdit(msg)}>✎</button>
                            <button class="card-action danger" title="Delete" on:click={() => openChatDelete(msg)}>✕</button>
                        </span>
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
                            <button class="card-action" title="Edit" on:click={() => openDreamEdit(ds)}>✎</button>
                            <button class="card-action danger" title="Delete" on:click={() => openDreamDelete(ds)}>✕</button>
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

<!-- Edit memory (#463) -->
<Modal bind:show={editOpen} title={$_('memories.edit_title')} width="640px" maxWidth="640px">
    <textarea class="edit-textarea" bind:value={editContent} rows="10" placeholder={$_('memories.edit_title')}></textarea>
    <div class="edit-hint">{$_('memories.edit_hint')}</div>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => editOpen = false} disabled={editSaving}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={saveEdit} disabled={editSaving || !editContent.trim()}>
            {editSaving ? $_('common.saving') : $_('common.save')}
        </button>
    </svelte:fragment>
</Modal>

<!-- Delete memory (#463) -->
<Modal bind:show={deleteOpen} title={$_('memories.delete_title')} width="560px" maxWidth="560px">
    {#if deleteTarget}
        <div class="delete-preview">{(deleteTarget.content || '').substring(0, 300)}</div>
    {/if}
    <div class="edit-hint">{deleteHardStep ? $_('memories.delete_hard_confirm') : $_('memories.delete_explain')}</div>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => deleteOpen = false} disabled={deleteBusy}>{$_('common.cancel')}</button>
        {#if !deleteHardStep}
            <button class="btn" on:click={() => confirmDelete(true)} disabled={deleteBusy}>{$_('memories.delete_hard')}</button>
            <button class="btn btn-primary" on:click={() => confirmDelete(false)} disabled={deleteBusy}>{$_('memories.delete_archive')}</button>
        {:else}
            <button class="btn btn-danger" on:click={() => confirmDelete(true)} disabled={deleteBusy}>{$_('memories.delete_hard')}</button>
        {/if}
    </svelte:fragment>
</Modal>

<!-- Edit chat message -->
<Modal bind:show={chatEditOpen} title="Edit Message" width="640px" maxWidth="640px">
    <textarea class="edit-textarea" bind:value={chatEditContent} rows="8" placeholder="Message content..."></textarea>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => chatEditOpen = false} disabled={chatEditSaving}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={saveChatEdit} disabled={chatEditSaving || !chatEditContent.trim()}>
            {chatEditSaving ? $_('common.saving') : $_('common.save')}
        </button>
    </svelte:fragment>
</Modal>

<!-- Delete chat message -->
<Modal bind:show={chatDeleteOpen} title="Delete Message" width="560px" maxWidth="560px">
    {#if chatDeleteMsg}
        <div class="delete-preview">{(chatDeleteMsg.content || '').substring(0, 300)}</div>
    {/if}
    <div class="edit-hint">This will permanently delete this message from chat history.</div>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => chatDeleteOpen = false} disabled={chatDeleteBusy}>{$_('common.cancel')}</button>
        <button class="btn btn-danger" on:click={confirmChatDelete} disabled={chatDeleteBusy}>Delete</button>
    </svelte:fragment>
</Modal>

<!-- Edit dream summary -->
<Modal bind:show={dreamEditOpen} title="Edit Dream Summary" width="640px" maxWidth="640px">
    <textarea class="edit-textarea" bind:value={dreamEditContent} rows="10" placeholder="Dream summary..."></textarea>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => dreamEditOpen = false} disabled={dreamEditSaving}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={saveDreamEdit} disabled={dreamEditSaving || !dreamEditContent.trim()}>
            {dreamEditSaving ? $_('common.saving') : $_('common.save')}
        </button>
    </svelte:fragment>
</Modal>

<!-- Delete dream -->
<Modal bind:show={dreamDeleteOpen} title="Delete Dream" width="560px" maxWidth="560px">
    <div class="edit-hint">This will permanently delete the dream state for <strong>{dreamDeleteAgent}</strong>. The agent can dream again to create a new one.</div>
    <svelte:fragment slot="footer">
        <button class="btn" on:click={() => dreamDeleteOpen = false} disabled={dreamDeleteBusy}>{$_('common.cancel')}</button>
        <button class="btn btn-danger" on:click={confirmDreamDelete} disabled={dreamDeleteBusy}>Delete</button>
    </svelte:fragment>
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
    .card-actions { display: flex; gap: 0.35rem; align-items: center; }
    .card-action { border: none; background: transparent; cursor: pointer; color: var(--text-muted); font-size: 0.85rem; line-height: 1; padding: 0.2rem 0.35rem; border-radius: var(--radius-md); }
    .card-action:hover { background: var(--surface-3); color: var(--text); }
    .card-action.danger:hover { background: var(--tone-error-bg); color: var(--tone-error-text); }
    .edit-textarea { width: 100%; box-sizing: border-box; font-family: var(--font-body); font-size: 0.85rem; line-height: 1.6; padding: 0.7rem; border: 1px solid var(--border); border-radius: var(--radius-md); background: var(--surface-1); color: var(--text); resize: vertical; }
    .edit-hint { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-top: 0.6rem; }
    .delete-preview { font-family: var(--font-body); font-size: 0.85rem; line-height: 1.6; white-space: pre-wrap; padding: 0.7rem; background: var(--surface-2); border-radius: var(--radius-md); }
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
