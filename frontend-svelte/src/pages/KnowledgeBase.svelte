<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import Badge from '../components/Badge.svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    import { timeAgo, formatDate, truncate, escapeHtml, renderMarkdown } from '../lib/utils.js';

    let loading = true;
    let refreshInterval;

    // Stats
    let stats = { raw_count: 0, wiki_count: 0, total_tags: 0, top_tags: [], last_filed: null, last_wiki_update: null };

    // View: 'sources' or 'wiki'
    let activeView = 'sources';

    // Graph data
    let graphData = null;
    let graphNodes = [];
    let graphEdges = [];
    let graphSimRunning = false;
    let showRawInGraph = false;
    let graphContainer;
    let graphWidth = 800;
    let graphHeight = 500;
    let hoveredNode = null;
    let dragNode = null;

    // Sources list
    let sources = [];
    let sourceTotal = 0;
    let sourceOffset = 0;
    const PAGE_SIZE = 24;

    // Wiki pages list
    let wikiPages = [];

    // Filters
    let filterTag = '';
    let filterType = '';
    let searchInput = '';
    let searchResults = [];
    let isSearchMode = false;

    // Source types for filter
    const SOURCE_TYPES = ['article', 'tweet_thread', 'pdf', 'conversation', 'note', 'link', 'video', 'image'];

    // Detail modal
    let detailModalOpen = false;
    let detailItem = null;
    let detailContent = '';
    let detailKind = 'raw'; // 'raw' or 'wiki'

    // Ingest modal
    let ingestModalOpen = false;
    let ingestTitle = '';
    let ingestUrl = '';
    let ingestContent = '';
    let ingestType = 'note';
    let ingestTags = '';
    let ingestNotes = '';
    let ingesting = false;

    // --- Data loading ---

    async function loadStats() {
        try {
            const data = await api('GET', '/kb/stats');
            stats = data;
        } catch (e) { console.error('KB stats error:', e); }
    }

    async function loadSources() {
        try {
            const params = new URLSearchParams();
            params.set('limit', PAGE_SIZE);
            params.set('offset', sourceOffset);
            if (filterTag) params.set('tag', filterTag);
            if (filterType) params.set('source_type', filterType);
            const data = await api('GET', `/kb/raw?${params}`);
            sources = data.sources || [];
            sourceTotal = data.total || sources.length;
        } catch (e) {
            sources = [];
            toast('Failed to load sources', 'error');
        }
    }

    async function loadWiki() {
        try {
            const data = await api('GET', '/kb/wiki?limit=200');
            wikiPages = data.pages || data.wiki || [];
        } catch (e) {
            wikiPages = [];
            toast('Failed to load wiki pages', 'error');
        }
    }

    async function doSearch() {
        if (!searchInput.trim()) { clearSearch(); return; }
        isSearchMode = true;
        try {
            const scope = activeView === 'wiki' ? 'wiki' : 'raw';
            const params = new URLSearchParams();
            params.set('q', searchInput);
            params.set('scope', scope);
            params.set('limit', 50);
            const data = await api('GET', `/kb/search?${params}`);
            searchResults = data.results || [];
            toast(`Found ${searchResults.length} results`);
        } catch (e) { toast('Search failed', 'error'); }
    }

    function clearSearch() {
        isSearchMode = false;
        searchInput = '';
        searchResults = [];
    }

    async function loadGraph() {
        try {
            const data = await api('GET', '/kb/graph');
            graphData = data;
            initGraph();
        } catch (e) {
            console.error('KB graph error:', e);
            toast('Failed to load graph', 'error');
        }
    }

    function initGraph() {
        if (!graphData) return;

        const nodes = graphData.nodes
            .filter(n => showRawInGraph || n.type === 'wiki')
            .map(n => ({
                ...n,
                x: graphWidth / 2 + (Math.random() - 0.5) * 300,
                y: graphHeight / 2 + (Math.random() - 0.5) * 200,
                vx: 0, vy: 0,
                r: n.type === 'wiki' ? Math.max(18, 12 + n.degree * 3) : 10,
            }));

        const nodeIds = new Set(nodes.map(n => n.id));
        const edges = graphData.edges.filter(e => nodeIds.has(e.source) && nodeIds.has(e.target));

        graphNodes = nodes;
        graphEdges = edges;
        runSimulation();
    }

    function runSimulation() {
        if (graphSimRunning) return;
        graphSimRunning = true;

        const alpha = 1;
        let iteration = 0;
        const maxIter = 200;
        const centerX = graphWidth / 2;
        const centerY = graphHeight / 2;

        function tick() {
            if (iteration >= maxIter) { graphSimRunning = false; return; }
            iteration++;
            const decay = 1 - iteration / maxIter;

            // Repulsion between nodes
            for (let i = 0; i < graphNodes.length; i++) {
                for (let j = i + 1; j < graphNodes.length; j++) {
                    const a = graphNodes[i], b = graphNodes[j];
                    let dx = b.x - a.x, dy = b.y - a.y;
                    let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                    const force = 800 / (dist * dist) * decay;
                    const fx = dx / dist * force, fy = dy / dist * force;
                    a.vx -= fx; a.vy -= fy;
                    b.vx += fx; b.vy += fy;
                }
            }

            // Attraction along edges
            for (const e of graphEdges) {
                const a = graphNodes.find(n => n.id === e.source);
                const b = graphNodes.find(n => n.id === e.target);
                if (!a || !b) continue;
                let dx = b.x - a.x, dy = b.y - a.y;
                let dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const force = (dist - 100) * 0.01 * decay;
                const fx = dx / dist * force, fy = dy / dist * force;
                a.vx += fx; a.vy += fy;
                b.vx -= fx; b.vy -= fy;
            }

            // Center gravity
            for (const n of graphNodes) {
                if (n === dragNode) continue;
                n.vx += (centerX - n.x) * 0.005 * decay;
                n.vy += (centerY - n.y) * 0.005 * decay;
                n.vx *= 0.9; n.vy *= 0.9;
                n.x += n.vx; n.y += n.vy;
                // Bounds
                n.x = Math.max(n.r + 10, Math.min(graphWidth - n.r - 10, n.x));
                n.y = Math.max(n.r + 10, Math.min(graphHeight - n.r - 10, n.y));
            }

            graphNodes = graphNodes; // trigger reactivity
            requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
    }

    function nodeColor(node) {
        if (node.type === 'raw') return '#555';
        const colors = {
            topics: '#4a9eff', people: '#ff6b9d', projects: '#f5a623',
            places: '#50c878', events: '#c678dd', organizations: '#e06c75', other: '#888'
        };
        return colors[node.category] || colors.other;
    }

    function edgeColor(edge) {
        return edge.type === 'related' ? 'rgba(255,255,255,0.25)' : 'rgba(255,255,255,0.08)';
    }

    function onGraphNodeClick(node) {
        if (node.type === 'wiki') {
            openWikiDetail({ slug: node.id, title: node.label });
        } else {
            openSourceDetail({ id: node.id, title: node.label });
        }
    }

    async function refresh() {
        await Promise.all([loadStats(), loadSources(), loadWiki()]);
        loading = false;
    }

    // --- Detail ---

    async function openSourceDetail(source) {
        detailKind = 'raw';
        detailItem = source;
        detailContent = '';
        detailModalOpen = true;
        try {
            const data = await api('GET', `/kb/raw/${source.id}?include_content=true`);
            detailContent = data.content || data.source?.content || '';
        } catch (e) { detailContent = '_Failed to load content_'; }
    }

    async function openWikiDetail(page) {
        detailKind = 'wiki';
        detailItem = page;
        detailContent = '';
        detailModalOpen = true;
        try {
            const data = await api('GET', `/kb/wiki/${page.slug}?include_content=true`);
            detailContent = data.content || data.page?.content || '';
            // Populate related and backlinks from the page data
            if (data.page) {
                detailItem = { ...detailItem, ...data.page };
            }
        } catch (e) { detailContent = '_Failed to load content_'; }
    }

    // Navigate to a wiki page by slug or title (from [[wiki link]] clicks)
    function navigateWiki(slugOrTitle) {
        // Try to find the wiki page by matching slug suffix or title
        const needle = slugOrTitle.toLowerCase();
        const match = wikiPages.find(p =>
            p.slug === needle ||
            p.title?.toLowerCase() === needle ||
            p.slug.endsWith(`/${needle}`)
        );
        if (match) {
            openWikiDetail(match);
        } else {
            toast(`Wiki page not found: ${slugOrTitle}`);
        }
    }

    // Handle clicks on wiki links inside rendered content
    function handleWikiLinkClick(event) {
        const link = event.target.closest('.wiki-link');
        if (link) {
            event.preventDefault();
            event.stopPropagation();
            const slug = link.dataset.wikiLink;
            if (slug) navigateWiki(slug);
        }
    }

    // --- Ingest ---

    async function submitIngest() {
        if (!ingestTitle.trim()) { toast('Title is required', 'error'); return; }
        if (!ingestContent.trim() && !ingestUrl.trim()) { toast('Content or URL is required', 'error'); return; }
        ingesting = true;
        try {
            const body = {
                title: ingestTitle.trim(),
                source_type: ingestType,
                tags: ingestTags.split(',').map(t => t.trim()).filter(Boolean),
            };
            if (ingestUrl.trim()) body.source_url = ingestUrl.trim();
            if (ingestContent.trim()) body.content = ingestContent.trim();
            if (ingestNotes.trim()) body.owner_notes = ingestNotes.trim();

            const resp = await api('POST', '/kb/ingest', body);
            if (resp.status === 'duplicate') {
                toast('Duplicate — already filed', 'error');
            } else {
                toast('Source filed');
                ingestModalOpen = false;
                resetIngestForm();
                refresh();
            }
        } catch (e) {
            toast(`Ingest failed: ${e.message}`, 'error');
        } finally { ingesting = false; }
    }

    function resetIngestForm() {
        ingestTitle = ''; ingestUrl = ''; ingestContent = ''; ingestType = 'note';
        ingestTags = ''; ingestNotes = '';
    }

    // --- Pagination ---

    function nextPage() {
        if (sourceOffset + PAGE_SIZE < sourceTotal) {
            sourceOffset += PAGE_SIZE;
            loadSources();
        }
    }

    function prevPage() {
        if (sourceOffset > 0) {
            sourceOffset = Math.max(0, sourceOffset - PAGE_SIZE);
            loadSources();
        }
    }

    // --- Filter change ---

    function onFilterChange() {
        sourceOffset = 0;
        clearSearch();
        loadSources();
    }

    // --- Source type icons ---

    function typeIcon(t) {
        const icons = {
            article: 'article',
            tweet_thread: 'tag',
            pdf: 'picture_as_pdf',
            conversation: 'forum',
            note: 'edit_note',
            link: 'link',
            video: 'videocam',
            image: 'image',
        };
        return icons[t] || 'description';
    }

    // Format wiki slug to human-readable label
    function formatSlug(slug) {
        // "topics/llm-knowledge-bases" → "LLM Knowledge Bases"
        // "people/boris-cherny" → "Boris Cherny"
        const name = slug.split('/').pop() || slug;
        return name.split('-').map(w => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    }

    // Get category from slug
    function slugCategory(slug) {
        const type = slug.split('/')[0];
        const labels = {
            people: 'person', topics: 'topic', projects: 'project',
            places: 'place', events: 'event', organizations: 'org'
        };
        return labels[type] || type;
    }

    function wikiIcon(slug) {
        const type = slug.split('/')[0];
        const icons = {
            people: 'person', topics: 'topic', projects: 'rocket_launch',
            places: 'place', events: 'event', organizations: 'business'
        };
        return icons[type] || 'article';
    }

    // --- Lifecycle ---

    onMount(() => {
        refresh();
        refreshInterval = setInterval(refresh, 30000);
    });

    onDestroy(() => {
        if (refreshInterval) clearInterval(refreshInterval);
    });
</script>

{#if loading}
    <div class="loading-screen"><div class="loading-text">Loading knowledge base...</div></div>
{:else}
<div class="content">
    <!-- Stats bar -->
    <div class="stats-row">
        <div class="stat-chip">
            <span class="material-symbols-outlined" style="font-size:16px">source</span>
            {stats.raw_count} sources
        </div>
        <div class="stat-chip">
            <span class="material-symbols-outlined" style="font-size:16px">auto_stories</span>
            {stats.wiki_count} wiki pages
        </div>
        {#if stats.last_filed}
            <div class="stat-chip muted">
                last filed {timeAgo(stats.last_filed)}
            </div>
        {/if}
        {#if stats.top_tags?.length}
            <div class="stat-chip-tags">
                {#each stats.top_tags.slice(0, 6) as tag}
                    <button class="tag-chip" class:active={filterTag === tag.tag}
                        on:click={() => { filterTag = filterTag === tag.tag ? '' : tag.tag; onFilterChange(); }}>
                        {tag.tag} <span class="tag-count">{tag.count}</span>
                    </button>
                {/each}
            </div>
        {/if}
    </div>

    <!-- Tab bar + search -->
    <div class="toolbar">
        <div class="tab-bar">
            <button class="tab" class:active={activeView === 'sources'} on:click={() => { activeView = 'sources'; clearSearch(); }}>
                <span class="material-symbols-outlined" style="font-size:16px">source</span>
                Sources
            </button>
            <button class="tab" class:active={activeView === 'wiki'} on:click={() => { activeView = 'wiki'; clearSearch(); }}>
                <span class="material-symbols-outlined" style="font-size:16px">auto_stories</span>
                Wiki
            </button>
            <button class="tab" class:active={activeView === 'graph'} on:click={() => { activeView = 'graph'; clearSearch(); loadGraph(); }}>
                <span class="material-symbols-outlined" style="font-size:16px">hub</span>
                Graph
            </button>
        </div>

        <div class="search-bar">
            <input
                type="text"
                placeholder="Search {activeView}..."
                bind:value={searchInput}
                on:keydown={(e) => e.key === 'Enter' && doSearch()}
            />
            {#if isSearchMode}
                <button class="btn btn-sm" on:click={clearSearch}>✕</button>
            {:else}
                <button class="btn btn-sm" on:click={doSearch}>
                    <span class="material-symbols-outlined" style="font-size:16px">search</span>
                </button>
            {/if}
        </div>

        {#if activeView === 'sources'}
            <select class="filter-select" bind:value={filterType} on:change={onFilterChange}>
                <option value="">All types</option>
                {#each SOURCE_TYPES as t}
                    <option value={t}>{t.replace('_', ' ')}</option>
                {/each}
            </select>
        {/if}

        <button class="btn btn-sm btn-primary" on:click={() => { resetIngestForm(); ingestModalOpen = true; }}>
            <span class="material-symbols-outlined" style="font-size:16px">add</span>
            File source
        </button>
    </div>

    <!-- Search results -->
    {#if isSearchMode}
        <div class="results-list">
            {#if searchResults.length === 0}
                <div class="empty-state">No results for "{searchInput}"</div>
            {:else}
                {#each searchResults as r}
                    <button class="source-card" on:click={() => {
                        if (r.kind === 'wiki') openWikiDetail({ slug: r.ref_id, title: r.title });
                        else openSourceDetail({ id: r.ref_id, title: r.title });
                    }}>
                        <div class="card-top">
                            <Badge text={r.kind} variant={r.kind === 'wiki' ? 'model' : 'tag'} />
                            <span class="card-title">{r.title}</span>
                        </div>
                        {#if r.snippet}
                            <div class="card-snippet">{r.snippet}</div>
                        {/if}
                    </button>
                {/each}
            {/if}
        </div>

    <!-- Sources view -->
    {:else if activeView === 'sources'}
        {#if sources.length === 0}
            <div class="empty-state">
                <span class="material-symbols-outlined" style="font-size:48px;opacity:0.3">source</span>
                <p>No sources filed yet</p>
                <button class="btn btn-sm btn-primary" on:click={() => { resetIngestForm(); ingestModalOpen = true; }}>
                    File your first source
                </button>
            </div>
        {:else}
            <div class="source-grid">
                {#each sources as src}
                    <button class="source-card" on:click={() => openSourceDetail(src)}>
                        <div class="card-top">
                            <span class="material-symbols-outlined type-icon" title={src.source_type}>{typeIcon(src.source_type)}</span>
                            <span class="card-title">{src.title}</span>
                        </div>
                        <div class="card-meta">
                            {#if src.source_url}
                                <span class="meta-url" title={src.source_url}>{truncate(src.source_url, 40)}</span>
                            {/if}
                            <span class="meta-date">{timeAgo(src.filed_at)}</span>
                            {#if src.filed_by}
                                <Badge text={src.filed_by} variant="model" />
                            {/if}
                        </div>
                        {#if src.tags?.length}
                            <div class="card-tags">
                                {#each src.tags.slice(0, 4) as tag}
                                    <Badge text={tag} variant="tag" />
                                {/each}
                                {#if src.tags.length > 4}
                                    <span class="more-tags">+{src.tags.length - 4}</span>
                                {/if}
                            </div>
                        {/if}
                    </button>
                {/each}
            </div>

            <!-- Pagination -->
            {#if sourceTotal > PAGE_SIZE}
                <div class="pagination">
                    <button class="btn btn-sm" disabled={sourceOffset === 0} on:click={prevPage}>← Prev</button>
                    <span class="page-info">{sourceOffset + 1}–{Math.min(sourceOffset + PAGE_SIZE, sourceTotal)} of {sourceTotal}</span>
                    <button class="btn btn-sm" disabled={sourceOffset + PAGE_SIZE >= sourceTotal} on:click={nextPage}>Next →</button>
                </div>
            {/if}
        {/if}

    <!-- Wiki view -->
    {:else if activeView === 'wiki'}
        {#if wikiPages.length === 0}
            <div class="empty-state">
                <span class="material-symbols-outlined" style="font-size:48px;opacity:0.3">auto_stories</span>
                <p>No wiki pages yet</p>
                <p class="muted-text">Wiki pages are generated from raw sources in a later phase.</p>
            </div>
        {:else}
            <div class="wiki-list">
                {#each wikiPages as page}
                    <button class="wiki-card" on:click={() => openWikiDetail(page)}>
                        <div class="card-top">
                            <span class="material-symbols-outlined type-icon">{wikiIcon(page.slug)}</span>
                            <span class="card-title">{page.title}</span>
                            <span class="wiki-category">{slugCategory(page.slug)}</span>
                        </div>
                        <div class="card-meta">
                            {#if page.last_updated}
                                <span class="meta-date">updated {timeAgo(page.last_updated)}</span>
                            {/if}
                            {#if page.sources?.length}
                                <span class="meta-sources">{page.sources.length} {page.sources.length === 1 ? 'source' : 'sources'}</span>
                            {/if}
                        </div>
                        {#if page.related?.length}
                            <div class="card-related">
                                {#each page.related.slice(0, 3) as rel}
                                    <button class="related-chip related-chip-link" on:click|stopPropagation={() => navigateWiki(rel)}>
                                        <span class="material-symbols-outlined" style="font-size:12px">{wikiIcon(rel)}</span>
                                        {formatSlug(rel)}
                                    </button>
                                {/each}
                                {#if page.related.length > 3}
                                    <span class="more-tags">+{page.related.length - 3}</span>
                                {/if}
                            </div>
                        {/if}
                    </button>
                {/each}
            </div>
        {/if}

    <!-- Graph view -->
    {:else if activeView === 'graph'}
        <div class="graph-container" bind:this={graphContainer}>
            {#if !graphData}
                <div class="empty-state">
                    <span class="material-symbols-outlined" style="font-size:48px;opacity:0.3">hub</span>
                    <p>Loading graph...</p>
                </div>
            {:else if graphNodes.length === 0}
                <div class="empty-state">
                    <span class="material-symbols-outlined" style="font-size:48px;opacity:0.3">hub</span>
                    <p>No nodes to display</p>
                </div>
            {:else}
                <div class="graph-controls">
                    <label class="toggle-label">
                        <input type="checkbox" bind:checked={showRawInGraph} on:change={initGraph} />
                        Show raw sources
                    </label>
                    <span class="graph-stats">
                        {graphNodes.length} nodes · {graphEdges.length} edges
                    </span>
                </div>
                <svg
                    width={graphWidth}
                    height={graphHeight}
                    style="background: rgba(0,0,0,0.2); border-radius: 8px; cursor: grab;"
                    viewBox="0 0 {graphWidth} {graphHeight}"
                >
                    <!-- Edges -->
                    {#each graphEdges as edge}
                        {@const src = graphNodes.find(n => n.id === edge.source)}
                        {@const tgt = graphNodes.find(n => n.id === edge.target)}
                        {#if src && tgt}
                            <line
                                x1={src.x} y1={src.y}
                                x2={tgt.x} y2={tgt.y}
                                stroke={edgeColor(edge)}
                                stroke-width={edge.type === 'related' ? 2 : 1}
                            />
                        {/if}
                    {/each}

                    <!-- Nodes -->
                    {#each graphNodes as node}
                        <g
                            style="cursor: pointer;"
                            on:mouseenter={() => hoveredNode = node}
                            on:mouseleave={() => hoveredNode = null}
                            on:click={() => onGraphNodeClick(node)}
                        >
                            <circle
                                cx={node.x} cy={node.y} r={node.r}
                                fill={nodeColor(node)}
                                stroke={hoveredNode === node ? '#fff' : 'rgba(255,255,255,0.2)'}
                                stroke-width={hoveredNode === node ? 2.5 : 1}
                                opacity={hoveredNode && hoveredNode !== node ? 0.4 : 0.9}
                            />
                            <text
                                x={node.x} y={node.y + node.r + 14}
                                text-anchor="middle"
                                fill={hoveredNode === node ? '#fff' : 'rgba(255,255,255,0.6)'}
                                font-size={node.type === 'wiki' ? '11px' : '9px'}
                                font-family="monospace"
                            >
                                {node.label.length > 20 ? node.label.slice(0, 18) + '…' : node.label}
                            </text>
                        </g>
                    {/each}

                    <!-- Tooltip -->
                    {#if hoveredNode}
                        <rect
                            x={hoveredNode.x + hoveredNode.r + 8}
                            y={hoveredNode.y - 16}
                            width={Math.max(120, hoveredNode.label.length * 7 + 40)}
                            height="32"
                            rx="4"
                            fill="rgba(0,0,0,0.85)"
                            stroke="rgba(255,255,255,0.2)"
                        />
                        <text
                            x={hoveredNode.x + hoveredNode.r + 14}
                            y={hoveredNode.y + 1}
                            fill="#fff"
                            font-size="12px"
                            font-family="monospace"
                        >
                            {hoveredNode.label} ({hoveredNode.degree})
                        </text>
                    {/if}
                </svg>

                <!-- Legend -->
                <div class="graph-legend">
                    <span class="legend-item"><span class="legend-dot" style="background:#4a9eff"></span> Topics</span>
                    <span class="legend-item"><span class="legend-dot" style="background:#ff6b9d"></span> People</span>
                    {#if showRawInGraph}
                        <span class="legend-item"><span class="legend-dot" style="background:#555"></span> Raw sources</span>
                    {/if}
                    <span class="legend-item" style="opacity:0.5">Node size = connections</span>
                </div>
            {/if}
        </div>
    {/if}
</div>
{/if}

<!-- Detail modal -->
<Modal bind:show={detailModalOpen} title={detailItem?.title || 'Detail'} maxWidth="800px">
    {#if detailItem}
        <div class="detail-meta">
            {#if detailKind === 'raw'}
                <Badge text={detailItem.source_type || 'source'} variant="tag" />
                {#if detailItem.filed_by}
                    <Badge text={'by ' + detailItem.filed_by} variant="model" />
                {/if}
                {#if detailItem.filed_at}
                    <span class="meta-date">{formatDate(detailItem.filed_at)}</span>
                {/if}
                {#if detailItem.source_url}
                    <a href={detailItem.source_url} target="_blank" rel="noopener" class="source-link">
                        <span class="material-symbols-outlined" style="font-size:14px">open_in_new</span>
                        source
                    </a>
                {/if}
            {:else}
                <Badge text="wiki" variant="model" />
                {#if detailItem.last_updated}
                    <span class="meta-date">updated {timeAgo(detailItem.last_updated)}</span>
                {/if}
            {/if}
        </div>

        {#if detailItem.tags?.length}
            <div class="detail-tags">
                {#each detailItem.tags as tag}
                    <Badge text={tag} variant="tag" />
                {/each}
            </div>
        {/if}

        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <!-- svelte-ignore a11y-no-static-element-interactions -->
        <div class="detail-content" on:click={handleWikiLinkClick}>
            {#if detailContent}
                {@html renderMarkdown(detailContent)}
            {:else}
                <div class="loading-text">Loading...</div>
            {/if}
        </div>

        {#if detailKind === 'wiki' && (detailItem?.related?.length || detailItem?.sources?.length)}
            <div class="wiki-nav-section">
                {#if detailItem.related?.length}
                    <div class="wiki-nav-group">
                        <span class="wiki-nav-label">Related</span>
                        <div class="wiki-nav-chips">
                            {#each detailItem.related as rel}
                                <button class="wiki-nav-chip" on:click={() => navigateWiki(rel)}>
                                    <span class="material-symbols-outlined" style="font-size:13px">{wikiIcon(rel)}</span>
                                    {formatSlug(rel)}
                                </button>
                            {/each}
                        </div>
                    </div>
                {/if}
                {#if detailItem.sources?.length}
                    <div class="wiki-nav-group">
                        <span class="wiki-nav-label">Sources</span>
                        <div class="wiki-nav-chips">
                            {#each detailItem.sources as srcId}
                                <button class="wiki-nav-chip source-chip" on:click={() => {
                                    const src = sources.find(s => s.id === srcId);
                                    if (src) openSourceDetail(src);
                                    else toast(`Source ${srcId} not loaded`);
                                }}>
                                    <span class="material-symbols-outlined" style="font-size:13px">source</span>
                                    {srcId}
                                </button>
                            {/each}
                        </div>
                    </div>
                {/if}
            </div>
        {/if}
    {/if}
</Modal>

<!-- Ingest modal -->
<Modal bind:show={ingestModalOpen} title="File a new source" maxWidth="600px">
    <div class="ingest-form">
        <label>
            <span class="field-label">Title *</span>
            <input type="text" bind:value={ingestTitle} placeholder="What is this about?" />
        </label>

        <label>
            <span class="field-label">URL</span>
            <input type="url" bind:value={ingestUrl} placeholder="https://..." />
        </label>

        <label>
            <span class="field-label">Content</span>
            <textarea bind:value={ingestContent} rows="6" placeholder="Paste text, article body, notes..."></textarea>
        </label>

        <div class="form-row">
            <label class="form-half">
                <span class="field-label">Type</span>
                <select bind:value={ingestType}>
                    {#each SOURCE_TYPES as t}
                        <option value={t}>{t.replace('_', ' ')}</option>
                    {/each}
                </select>
            </label>
            <label class="form-half">
                <span class="field-label">Tags</span>
                <input type="text" bind:value={ingestTags} placeholder="ai, tools, research" />
            </label>
        </div>

        <label>
            <span class="field-label">Notes</span>
            <input type="text" bind:value={ingestNotes} placeholder="Why is this worth filing?" />
        </label>
    </div>

    <div slot="footer">
        <button class="btn" on:click={() => ingestModalOpen = false}>Cancel</button>
        <button class="btn btn-primary" on:click={submitIngest} disabled={ingesting}>
            {ingesting ? 'Filing...' : 'File source'}
        </button>
    </div>
</Modal>

<style>
    /* Stats row */
    .stats-row {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 16px;
        flex-wrap: wrap;
    }
    .stat-chip {
        display: flex;
        align-items: center;
        gap: 4px;
        font-size: 0.8rem;
        color: var(--text-secondary);
        font-family: var(--font-mono, monospace);
    }
    .stat-chip.muted { color: var(--text-muted); }
    .stat-chip-tags {
        display: flex;
        gap: 5px;
        flex-wrap: wrap;
        margin-left: 4px;
    }
    .tag-chip {
        background: var(--surface-2);
        border: 1px solid var(--border);
        border-radius: 4px;
        padding: 2px 7px;
        font-size: 0.73rem;
        font-family: var(--font-mono, monospace);
        color: var(--text-secondary);
        cursor: pointer;
        transition: background 0.15s;
    }
    .tag-chip:hover { background: var(--surface-3); }
    .tag-chip.active {
        background: var(--tone-info-bg);
        color: var(--tone-info-text);
        border-color: var(--tone-info-text);
    }
    .tag-count {
        opacity: 0.5;
        font-size: 0.68rem;
    }

    /* Toolbar */
    .toolbar {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 16px;
        padding-bottom: 12px;
        border-bottom: 1px solid var(--border);
        flex-wrap: wrap;
    }
    .tab-bar {
        display: flex;
        gap: 2px;
        background: var(--surface-1);
        border-radius: 6px;
        padding: 2px;
    }
    .tab {
        display: flex;
        align-items: center;
        gap: 4px;
        padding: 4px 12px;
        border: none;
        background: transparent;
        border-radius: 4px;
        font-size: 0.82rem;
        font-family: var(--font-mono, monospace);
        color: var(--text-secondary);
        cursor: pointer;
        transition: background 0.15s;
    }
    .tab:hover { background: var(--surface-2); }
    .tab.active {
        background: var(--surface-3);
        color: var(--text-primary);
    }
    .search-bar {
        display: flex;
        align-items: center;
        gap: 4px;
        flex: 1;
        min-width: 180px;
    }
    .search-bar input {
        flex: 1;
        padding: 5px 8px;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: 4px;
        color: var(--text-primary);
        font-family: var(--font-mono, monospace);
        font-size: 0.82rem;
    }
    .filter-select {
        padding: 5px 8px;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: 4px;
        color: var(--text-primary);
        font-family: var(--font-mono, monospace);
        font-size: 0.82rem;
    }

    /* Buttons */
    .btn {
        padding: 4px 10px;
        border: 1px solid var(--border);
        border-radius: 4px;
        background: var(--surface-2);
        color: var(--text-primary);
        font-family: var(--font-mono, monospace);
        font-size: 0.8rem;
        cursor: pointer;
        transition: background 0.15s;
    }
    .btn:hover { background: var(--surface-3); }
    .btn:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-sm { padding: 2px 8px; font-size: 0.78rem; }
    .btn-primary {
        background: var(--tone-info-bg);
        color: var(--tone-info-text);
        border-color: var(--tone-info-text);
    }
    .btn-primary:hover { opacity: 0.85; }

    /* Source grid */
    .source-grid, .wiki-list, .results-list {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
        gap: 10px;
    }
    .source-card, .wiki-card {
        display: flex;
        flex-direction: column;
        gap: 8px;
        padding: 14px 16px;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: 8px;
        cursor: pointer;
        transition: border-color 0.15s, background 0.15s;
        text-align: left;
        width: 100%;
        min-height: 110px;
        font-family: var(--font-mono, monospace);
    }
    .source-card:hover, .wiki-card:hover {
        border-color: var(--text-muted);
        background: var(--surface-2);
    }
    .card-top {
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .type-icon {
        font-size: 18px;
        color: var(--text-muted);
        flex-shrink: 0;
    }
    .card-title {
        font-size: 0.88rem;
        font-weight: 500;
        color: var(--text-primary);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        flex: 1;
    }
    .card-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        font-size: 0.75rem;
        color: var(--text-muted);
        flex-wrap: wrap;
    }
    .meta-url {
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        max-width: 200px;
    }
    .card-tags {
        display: flex;
        gap: 5px;
        flex-wrap: wrap;
        margin-top: 2px;
    }
    .more-tags {
        font-size: 0.72rem;
        color: var(--text-muted);
    }
    .card-snippet {
        font-size: 0.78rem;
        color: var(--text-secondary);
        line-height: 1.3;
        overflow: hidden;
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
    }

    /* Empty state */
    .empty-state {
        text-align: center;
        padding: 60px 24px;
        color: var(--text-muted);
        font-family: var(--font-mono, monospace);
    }
    .empty-state p { margin: 10px 0; }
    .muted-text { font-size: 0.8rem; opacity: 0.6; }

    /* Pagination */
    .pagination {
        display: flex;
        align-items: center;
        justify-content: center;
        gap: 12px;
        padding: 16px 0;
        font-size: 0.8rem;
        color: var(--text-secondary);
        font-family: var(--font-mono, monospace);
    }

    /* Loading */
    .loading-screen {
        display: flex; justify-content: center; align-items: center;
        min-height: 200px;
    }
    .loading-text {
        color: var(--text-muted); font-family: var(--font-mono, monospace);
        font-size: 0.85rem;
    }

    /* Detail modal */
    .detail-meta {
        display: flex;
        align-items: center;
        gap: 8px;
        margin-bottom: 8px;
        flex-wrap: wrap;
        font-size: 0.82rem;
    }
    .detail-tags {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
        margin-bottom: 12px;
    }
    .source-link {
        display: flex;
        align-items: center;
        gap: 2px;
        color: var(--blue);
        text-decoration: none;
        font-size: 0.8rem;
    }
    .source-link:hover { text-decoration: underline; }
    .detail-content {
        padding: 12px 0;
        font-size: 0.85rem;
        line-height: 1.6;
        color: var(--text-primary);
        overflow-y: auto;
        max-height: 60vh;
    }
    .detail-content :global(h1) { font-size: 1.2rem; margin: 16px 0 8px; }
    .detail-content :global(h2) { font-size: 1.05rem; margin: 14px 0 6px; }
    .detail-content :global(h3) { font-size: 0.95rem; margin: 12px 0 4px; }
    .detail-content :global(pre) {
        background: var(--surface-2);
        padding: 8px;
        border-radius: 4px;
        overflow-x: auto;
        font-size: 0.8rem;
    }
    .detail-content :global(code) {
        font-family: var(--font-mono, monospace);
        font-size: 0.82rem;
    }
    .detail-content :global(a) { color: var(--blue); }
    .detail-content :global(blockquote) {
        border-left: 3px solid var(--border);
        padding-left: 12px;
        color: var(--text-secondary);
        margin: 8px 0;
    }

    /* Ingest form */
    .ingest-form {
        display: flex;
        flex-direction: column;
        gap: 12px;
    }
    .ingest-form label {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }
    .field-label {
        font-size: 0.78rem;
        color: var(--text-secondary);
        font-family: var(--font-mono, monospace);
    }
    .ingest-form input,
    .ingest-form select,
    .ingest-form textarea {
        padding: 6px 8px;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: 4px;
        color: var(--text-primary);
        font-family: var(--font-mono, monospace);
        font-size: 0.85rem;
    }
    .ingest-form textarea {
        resize: vertical;
        min-height: 80px;
    }
    .form-row {
        display: flex;
        gap: 12px;
    }
    .form-half {
        flex: 1;
    }

    /* Meta date */
    .meta-date { font-size: 0.75rem; color: var(--text-muted); }
    .meta-sources { font-size: 0.75rem; color: var(--text-muted); }

    /* Wiki category label */
    .wiki-category {
        font-size: 0.68rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
        padding: 1px 6px;
        border: 1px solid var(--border);
        border-radius: 3px;
        flex-shrink: 0;
    }

    /* Related chips — human-readable wiki links */
    .card-related {
        display: flex;
        gap: 4px;
        flex-wrap: wrap;
    }
    .related-chip {
        display: inline-flex;
        align-items: center;
        gap: 3px;
        font-size: 0.72rem;
        color: var(--text-secondary);
        padding: 1px 6px;
        background: var(--surface-2);
        border-radius: 3px;
        font-family: var(--font-mono, monospace);
    }

    /* Wiki cross-links */
    .detail-content :global(.wiki-link) {
        color: var(--blue, #4a9eff);
        text-decoration: none;
        border-bottom: 1px dashed var(--blue, #4a9eff);
        cursor: pointer;
        transition: opacity 0.15s;
    }
    .detail-content :global(.wiki-link:hover) {
        opacity: 0.75;
    }

    /* Wiki nav section (related + sources at bottom of detail) */
    .wiki-nav-section {
        border-top: 1px solid var(--border);
        margin-top: 16px;
        padding-top: 12px;
        display: flex;
        flex-direction: column;
        gap: 10px;
    }
    .wiki-nav-group {
        display: flex;
        align-items: center;
        gap: 8px;
        flex-wrap: wrap;
    }
    .wiki-nav-label {
        font-size: 0.72rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
        font-family: var(--font-mono, monospace);
        min-width: 55px;
    }
    .wiki-nav-chips {
        display: flex;
        gap: 5px;
        flex-wrap: wrap;
    }
    .wiki-nav-chip {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        padding: 3px 10px;
        font-size: 0.76rem;
        font-family: var(--font-mono, monospace);
        color: var(--text-secondary);
        background: var(--surface-2);
        border: 1px solid var(--border);
        border-radius: 4px;
        cursor: pointer;
        transition: background 0.15s, border-color 0.15s;
    }
    .wiki-nav-chip:hover {
        background: var(--surface-3);
        border-color: var(--text-muted);
    }
    .source-chip {
        font-size: 0.72rem;
        color: var(--text-muted);
    }

    /* Related chip links on wiki cards */
    .related-chip-link {
        cursor: pointer;
        border: 1px solid transparent;
        transition: background 0.15s, border-color 0.15s;
    }
    .related-chip-link:hover {
        background: var(--surface-3);
        border-color: var(--border);
    }

    /* Page info */
    .page-info { font-family: var(--font-mono, monospace); }

    /* Graph view */
    .graph-container {
        display: flex;
        flex-direction: column;
        align-items: center;
        gap: 12px;
        padding: 12px 0;
    }
    .graph-container svg {
        width: 100%;
        max-width: 900px;
        height: auto;
    }
    .graph-controls {
        display: flex;
        align-items: center;
        gap: 16px;
        font-size: 12px;
        color: var(--text-secondary, #999);
        font-family: var(--font-mono, monospace);
    }
    .toggle-label {
        display: flex;
        align-items: center;
        gap: 6px;
        cursor: pointer;
    }
    .toggle-label input { cursor: pointer; }
    .graph-stats { opacity: 0.6; }
    .graph-legend {
        display: flex;
        gap: 16px;
        font-size: 11px;
        color: var(--text-secondary, #999);
        font-family: var(--font-mono, monospace);
    }
    .legend-item {
        display: flex;
        align-items: center;
        gap: 5px;
    }
    .legend-dot {
        width: 10px;
        height: 10px;
        border-radius: 50%;
        display: inline-block;
    }

    /* Mobile */
    @media (max-width: 640px) {
        .source-grid, .wiki-list, .results-list {
            grid-template-columns: 1fr;
        }
        .toolbar {
            flex-direction: column;
            align-items: stretch;
        }
        .search-bar { min-width: unset; }
        .form-row { flex-direction: column; gap: 8px; }
        .stats-row { gap: 6px; }
        .stat-chip-tags { gap: 3px; }
    }
</style>
