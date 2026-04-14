<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';

    let loading = true;
    let overview = null;
    let agentsList = null;
    let selectedAgent = null;
    let agentDetail = null;
    let range = '7d';
    let refreshInterval;

    const RANGES = [
        { value: 'today', label: 'Today' },
        { value: '7d', label: '7 Days' },
        { value: '30d', label: '30 Days' },
    ];

    function formatTokens(n) {
        if (!n && n !== 0) return '0';
        if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M';
        if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K';
        return n.toString();
    }

    function formatCost(usd) {
        if (!usd && usd !== 0) return '$0.00';
        return '$' + usd.toFixed(2);
    }

    function formatHours(h) {
        if (!h && h !== 0) return '0h';
        if (h < 1) return Math.round(h * 60) + 'm';
        return h.toFixed(1) + 'h';
    }

    function formatDuration(ms) {
        if (!ms) return '—';
        if (ms < 1000) return ms + 'ms';
        return (ms / 1000).toFixed(1) + 's';
    }

    function maxTokensInTrend(trend) {
        if (!trend || !trend.length) return 1;
        return Math.max(1, ...trend.map(t => (t.input_tokens || 0) + (t.output_tokens || 0) + (t.cached_input_tokens || 0)));
    }

    function barHeight(tokens, maxVal) {
        return Math.max(2, (tokens / maxVal) * 100);
    }

    function shortDate(bucket) {
        if (!bucket) return '';
        const d = new Date(bucket + 'T00:00:00');
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }

    async function refresh() {
        try {
            const [ov, ag] = await Promise.all([
                api('GET', `/analytics/overview?range=${range}`),
                api('GET', `/analytics/agents?range=${range}`),
            ]);
            overview = ov;
            agentsList = ag;

            if (selectedAgent) {
                agentDetail = await api('GET', `/analytics/agents/${selectedAgent}?range=${range}`);
            }
        } catch (e) {
            console.error('Analytics fetch error:', e);
            toast('Failed to load analytics', 'error');
        } finally {
            loading = false;
        }
    }

    async function selectAgent(name) {
        if (selectedAgent === name) {
            selectedAgent = null;
            agentDetail = null;
            return;
        }
        selectedAgent = name;
        try {
            agentDetail = await api('GET', `/analytics/agents/${name}?range=${range}`);
        } catch (e) {
            toast(`Failed to load ${name} details`, 'error');
        }
    }

    function changeRange(newRange) {
        range = newRange;
        loading = true;
        selectedAgent = null;
        agentDetail = null;
        refresh();
    }

    onMount(() => {
        refresh();
        refreshInterval = setInterval(refresh, 60_000);
    });

    onDestroy(() => {
        if (refreshInterval) clearInterval(refreshInterval);
    });
</script>

<div class="analytics-page">
    <div class="page-header">
        <h1>Analytics</h1>
        <div class="range-selector">
            {#each RANGES as r}
                <button
                    class="range-btn"
                    class:active={range === r.value}
                    on:click={() => changeRange(r.value)}
                >{r.label}</button>
            {/each}
        </div>
    </div>

    {#if loading}
        <div class="loading">Loading analytics...</div>
    {:else if overview}
        <!-- Hero metrics -->
        <div class="hero-metrics">
            <div class="metric-card">
                <div class="metric-label">Total Cost</div>
                <div class="metric-value cost">{formatCost(overview.totals.cost_usd)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Active Hours</div>
                <div class="metric-value">{formatHours(overview.totals.active_hours)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Input Tokens</div>
                <div class="metric-value">{formatTokens(overview.totals.input_tokens)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Output Tokens</div>
                <div class="metric-value">{formatTokens(overview.totals.output_tokens)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Cached Tokens</div>
                <div class="metric-value">{formatTokens(overview.totals.cached_input_tokens)}</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Agents</div>
                <div class="metric-value">{overview.totals.agent_count}</div>
            </div>
        </div>

        <!-- Token trend chart -->
        {#if overview.trend && overview.trend.length > 0}
            <div class="section">
                <h2>Token Usage Trend</h2>
                <div class="trend-chart">
                    {#each overview.trend as day}
                        {@const total = (day.input_tokens || 0) + (day.output_tokens || 0) + (day.cached_input_tokens || 0)}
                        {@const maxVal = maxTokensInTrend(overview.trend)}
                        <div class="trend-bar-wrapper" title="{shortDate(day.bucket)}: {formatTokens(total)} tokens">
                            <div class="trend-bar-stack">
                                <div class="trend-bar input" style="height: {barHeight(day.input_tokens, maxVal)}%"></div>
                                <div class="trend-bar output" style="height: {barHeight(day.output_tokens, maxVal)}%"></div>
                                <div class="trend-bar cached" style="height: {barHeight(day.cached_input_tokens, maxVal)}%"></div>
                            </div>
                            <div class="trend-label">{shortDate(day.bucket)}</div>
                        </div>
                    {/each}
                </div>
                <div class="trend-legend">
                    <span class="legend-item"><span class="legend-dot input"></span> Input</span>
                    <span class="legend-item"><span class="legend-dot output"></span> Output</span>
                    <span class="legend-item"><span class="legend-dot cached"></span> Cached</span>
                </div>
            </div>
        {/if}

        <!-- Agent leaderboard -->
        {#if agentsList && agentsList.agents && agentsList.agents.length > 0}
            <div class="section">
                <h2>Agents</h2>
                <div class="agent-table">
                    <div class="agent-row header">
                        <span class="col-name">Agent</span>
                        <span class="col-num">Active</span>
                        <span class="col-num">Sessions</span>
                        <span class="col-num">Turns</span>
                        <span class="col-num">Input</span>
                        <span class="col-num">Output</span>
                        <span class="col-num">Cached</span>
                        <span class="col-num">Cost</span>
                    </div>
                    {#each agentsList.agents as agent}
                        <button
                            class="agent-row"
                            class:selected={selectedAgent === agent.agent_name}
                            on:click={() => selectAgent(agent.agent_name)}
                        >
                            <span class="col-name">{agent.agent_name}</span>
                            <span class="col-num">{formatHours(agent.active_hours)}</span>
                            <span class="col-num">{agent.sessions_count}</span>
                            <span class="col-num">{agent.turns_count}</span>
                            <span class="col-num">{formatTokens(agent.input_tokens)}</span>
                            <span class="col-num">{formatTokens(agent.output_tokens)}</span>
                            <span class="col-num">{formatTokens(agent.cached_input_tokens)}</span>
                            <span class="col-num">{formatCost(agent.cost_usd)}</span>
                        </button>
                    {/each}
                </div>
            </div>
        {:else}
            <div class="section">
                <div class="empty-state">No analytics data yet. Data will appear as agents run.</div>
            </div>
        {/if}

        <!-- Agent detail panel -->
        {#if agentDetail}
            <div class="section agent-detail">
                <h2>{agentDetail.agent_name}</h2>

                <div class="detail-metrics">
                    <div class="detail-metric">
                        <span class="dm-label">Active</span>
                        <span class="dm-value">{formatHours(agentDetail.totals.active_hours)}</span>
                    </div>
                    <div class="detail-metric">
                        <span class="dm-label">Sessions</span>
                        <span class="dm-value">{agentDetail.totals.sessions_count}</span>
                    </div>
                    <div class="detail-metric">
                        <span class="dm-label">Turns</span>
                        <span class="dm-value">{agentDetail.totals.turns_count}</span>
                    </div>
                    <div class="detail-metric">
                        <span class="dm-label">Input</span>
                        <span class="dm-value">{formatTokens(agentDetail.totals.input_tokens)}</span>
                    </div>
                    <div class="detail-metric">
                        <span class="dm-label">Output</span>
                        <span class="dm-value">{formatTokens(agentDetail.totals.output_tokens)}</span>
                    </div>
                    <div class="detail-metric">
                        <span class="dm-label">Cost</span>
                        <span class="dm-value cost">{formatCost(agentDetail.totals.cost_usd)}</span>
                    </div>
                </div>

                <!-- Agent token trend -->
                {#if agentDetail.trend && agentDetail.trend.length > 0}
                    <h3>Token Trend</h3>
                    <div class="trend-chart small">
                        {#each agentDetail.trend as day}
                            {@const total = (day.input_tokens || 0) + (day.output_tokens || 0) + (day.cached_input_tokens || 0)}
                            {@const maxVal = maxTokensInTrend(agentDetail.trend)}
                            <div class="trend-bar-wrapper" title="{shortDate(day.bucket)}: {formatTokens(total)}">
                                <div class="trend-bar-stack">
                                    <div class="trend-bar input" style="height: {barHeight(day.input_tokens, maxVal)}%"></div>
                                    <div class="trend-bar output" style="height: {barHeight(day.output_tokens, maxVal)}%"></div>
                                    <div class="trend-bar cached" style="height: {barHeight(day.cached_input_tokens, maxVal)}%"></div>
                                </div>
                                <div class="trend-label">{shortDate(day.bucket)}</div>
                            </div>
                        {/each}
                    </div>
                {/if}

                <!-- Tool usage -->
                {#if agentDetail.tools && agentDetail.tools.length > 0}
                    <h3>Tool Usage</h3>
                    <div class="tool-list">
                        {#each agentDetail.tools as tool}
                            {@const maxCalls = Math.max(1, ...agentDetail.tools.map(t => t.calls))}
                            <div class="tool-row">
                                <span class="tool-name">{tool.tool_name}</span>
                                <div class="tool-bar-bg">
                                    <div class="tool-bar-fill" style="width: {(tool.calls / maxCalls) * 100}%"></div>
                                </div>
                                <span class="tool-count">{tool.calls}</span>
                                <span class="tool-duration">{formatDuration(tool.total_duration_ms)}</span>
                            </div>
                        {/each}
                    </div>
                {/if}

                <!-- Recent sessions -->
                {#if agentDetail.sessions && agentDetail.sessions.length > 0}
                    <h3>Recent Sessions</h3>
                    <div class="sessions-list">
                        {#each agentDetail.sessions as session}
                            <div class="session-row">
                                <span class="session-id" title={session.session_id}>{session.session_id.slice(0, 8)}...</span>
                                <span class="session-model">{session.model}</span>
                                <span class="session-time">{new Date(session.started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
                                <span class="session-status">{session.ended_at ? 'ended' : 'active'}</span>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        {/if}
    {/if}
</div>

<style>
    .analytics-page {
        max-width: 1100px;
        margin: 0 auto;
        padding: 1.5rem;
    }

    .page-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
        gap: 0.75rem;
    }

    h1 {
        font-family: var(--font-mono);
        font-size: 1.5rem;
        margin: 0;
        color: var(--text);
    }

    h2 {
        font-family: var(--font-mono);
        font-size: 1.1rem;
        margin: 0 0 1rem 0;
        color: var(--text);
    }

    h3 {
        font-family: var(--font-mono);
        font-size: 0.9rem;
        margin: 1.25rem 0 0.75rem 0;
        color: var(--text-muted);
    }

    .range-selector {
        display: flex;
        gap: 0.25rem;
        background: var(--bg-secondary);
        border-radius: 6px;
        padding: 2px;
    }

    .range-btn {
        padding: 0.35rem 0.75rem;
        border: none;
        background: transparent;
        color: var(--text-muted);
        font-family: var(--font-mono);
        font-size: 0.8rem;
        cursor: pointer;
        border-radius: 4px;
        transition: all 0.15s;
    }

    .range-btn:hover {
        color: var(--text);
    }

    .range-btn.active {
        background: var(--accent);
        color: var(--bg);
    }

    .loading {
        text-align: center;
        padding: 3rem 1rem;
        color: var(--text-muted);
        font-family: var(--font-mono);
        font-size: 0.9rem;
    }

    .empty-state {
        text-align: center;
        padding: 2rem 1rem;
        color: var(--text-muted);
        font-family: var(--font-mono);
        font-size: 0.85rem;
    }

    /* Hero metrics */
    .hero-metrics {
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 0.75rem;
        margin-bottom: 1.5rem;
    }

    .metric-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1rem;
    }

    .metric-label {
        font-family: var(--font-mono);
        font-size: 0.7rem;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 0.35rem;
    }

    .metric-value {
        font-family: var(--font-mono);
        font-size: 1.4rem;
        font-weight: 700;
        color: var(--text);
    }

    .metric-value.cost {
        color: var(--green);
    }

    /* Sections */
    .section {
        background: var(--bg-secondary);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1.25rem;
        margin-bottom: 1rem;
    }

    /* Trend chart */
    .trend-chart {
        display: flex;
        align-items: flex-end;
        gap: 3px;
        height: 120px;
        padding: 0.5rem 0;
    }

    .trend-chart.small {
        height: 80px;
    }

    .trend-bar-wrapper {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 100%;
        min-width: 0;
    }

    .trend-bar-stack {
        flex: 1;
        width: 100%;
        display: flex;
        flex-direction: column;
        justify-content: flex-end;
        gap: 1px;
    }

    .trend-bar {
        width: 100%;
        border-radius: 2px 2px 0 0;
        min-height: 0;
        transition: height 0.3s ease;
    }

    .trend-bar.input { background: var(--accent); }
    .trend-bar.output { background: var(--green); }
    .trend-bar.cached { background: var(--text-muted); opacity: 0.5; }

    .trend-label {
        font-family: var(--font-mono);
        font-size: 0.55rem;
        color: var(--text-muted);
        margin-top: 4px;
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
        max-width: 100%;
    }

    .trend-legend {
        display: flex;
        gap: 1rem;
        margin-top: 0.5rem;
        justify-content: center;
    }

    .legend-item {
        font-family: var(--font-mono);
        font-size: 0.7rem;
        color: var(--text-muted);
        display: flex;
        align-items: center;
        gap: 4px;
    }

    .legend-dot {
        width: 8px;
        height: 8px;
        border-radius: 2px;
        display: inline-block;
    }

    .legend-dot.input { background: var(--accent); }
    .legend-dot.output { background: var(--green); }
    .legend-dot.cached { background: var(--text-muted); opacity: 0.5; }

    /* Agent table */
    .agent-table {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .agent-row {
        display: grid;
        grid-template-columns: 1.5fr repeat(7, 1fr);
        gap: 0.5rem;
        padding: 0.6rem 0.75rem;
        font-family: var(--font-mono);
        font-size: 0.8rem;
        border: none;
        background: transparent;
        color: var(--text);
        cursor: pointer;
        text-align: left;
        border-radius: 4px;
        width: 100%;
        transition: background 0.1s;
    }

    .agent-row:hover {
        background: var(--bg-hover);
    }

    .agent-row.selected {
        background: var(--bg-hover);
        border-left: 2px solid var(--accent);
    }

    .agent-row.header {
        color: var(--text-muted);
        font-size: 0.7rem;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        cursor: default;
    }

    .agent-row.header:hover {
        background: transparent;
    }

    .col-name {
        font-weight: 600;
    }

    .col-num {
        text-align: right;
    }

    /* Agent detail panel */
    .agent-detail {
        border-left: 3px solid var(--accent);
    }

    .detail-metrics {
        display: flex;
        flex-wrap: wrap;
        gap: 1.5rem;
    }

    .detail-metric {
        display: flex;
        flex-direction: column;
    }

    .dm-label {
        font-family: var(--font-mono);
        font-size: 0.65rem;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    .dm-value {
        font-family: var(--font-mono);
        font-size: 1.2rem;
        font-weight: 700;
        color: var(--text);
    }

    .dm-value.cost {
        color: var(--green);
    }

    /* Tool usage */
    .tool-list {
        display: flex;
        flex-direction: column;
        gap: 4px;
    }

    .tool-row {
        display: grid;
        grid-template-columns: 140px 1fr 50px 60px;
        gap: 0.5rem;
        align-items: center;
        font-family: var(--font-mono);
        font-size: 0.75rem;
    }

    .tool-name {
        color: var(--text);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .tool-bar-bg {
        height: 6px;
        background: var(--border);
        border-radius: 3px;
        overflow: hidden;
    }

    .tool-bar-fill {
        height: 100%;
        background: var(--accent);
        border-radius: 3px;
        transition: width 0.3s ease;
    }

    .tool-count {
        text-align: right;
        color: var(--text-muted);
    }

    .tool-duration {
        text-align: right;
        color: var(--text-muted);
        font-size: 0.7rem;
    }

    /* Sessions list */
    .sessions-list {
        display: flex;
        flex-direction: column;
        gap: 2px;
    }

    .session-row {
        display: grid;
        grid-template-columns: 100px 1fr auto auto;
        gap: 0.75rem;
        padding: 0.4rem 0;
        font-family: var(--font-mono);
        font-size: 0.75rem;
        color: var(--text-muted);
        border-bottom: 1px solid var(--border);
    }

    .session-row:last-child {
        border-bottom: none;
    }

    .session-id {
        color: var(--text);
    }

    .session-model {
        font-size: 0.7rem;
    }

    .session-status {
        font-size: 0.7rem;
        text-transform: uppercase;
    }

    /* Responsive */
    @media (max-width: 768px) {
        .analytics-page {
            padding: 1rem;
        }

        .hero-metrics {
            grid-template-columns: repeat(2, 1fr);
        }

        .agent-row {
            grid-template-columns: 1.5fr repeat(3, 1fr);
            font-size: 0.75rem;
        }

        .agent-row .col-num:nth-child(n+6) {
            display: none;
        }

        .agent-row.header .col-num:nth-child(n+6) {
            display: none;
        }

        .tool-row {
            grid-template-columns: 100px 1fr 40px;
        }

        .tool-duration {
            display: none;
        }
    }
</style>
