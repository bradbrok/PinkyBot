<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';

    let loading = true;
    let overview = null;
    let agentsList = null;
    let categories = null;
    let hourly = null;
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
        if (!ms) return '--';
        if (ms < 1000) return ms + 'ms';
        return (ms / 1000).toFixed(1) + 's';
    }

    function formatDelta(pct) {
        if (pct === null || pct === undefined) return '';
        const sign = pct > 0 ? '+' : '';
        return `${sign}${pct}%`;
    }

    function deltaClass(pct, inverted = false) {
        if (pct === null || pct === undefined) return 'delta-neutral';
        // For cost, positive = bad (inverted). For tokens/hours, positive = neutral.
        if (inverted) {
            return pct > 0 ? 'delta-negative' : pct < 0 ? 'delta-positive' : 'delta-neutral';
        }
        return 'delta-neutral';
    }

    function maxTokensInTrend(trend) {
        if (!trend || !trend.length) return 1;
        return Math.max(1, ...trend.map(t => (t.input_tokens || 0) + (t.output_tokens || 0) + (t.cached_input_tokens || 0)));
    }

    function maxSessionsInTrend(trend) {
        if (!trend || !trend.length) return 1;
        return Math.max(1, ...trend.map(t => t.sessions_count || 0));
    }

    function barHeight(tokens, maxVal) {
        return Math.max(2, (tokens / maxVal) * 100);
    }

    function shortDate(bucket) {
        if (!bucket) return '';
        const d = new Date(bucket + 'T00:00:00');
        return d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    }

    function agentTotalTokens(agent) {
        return (agent.input_tokens || 0) + (agent.output_tokens || 0) + (agent.cached_input_tokens || 0);
    }

    function maxAgentTokens(agents) {
        if (!agents || !agents.length) return 1;
        return Math.max(1, ...agents.map(agentTotalTokens));
    }

    function maxAgentCost(agents) {
        if (!agents || !agents.length) return 1;
        return Math.max(0.01, ...agents.map(a => a.cost_usd || 0));
    }

    function tokenPct(agent, maxTok) {
        const total = agentTotalTokens(agent);
        return (total / maxTok) * 100;
    }

    const CATEGORY_COLORS = {
        programming: 'var(--accent)',
        research: '#6aa3d9',
        thinking: '#b39ddb',
        messaging: 'var(--green)',
        testing: '#ff9800',
        delegation: '#e57373',
        other: 'var(--text-muted)',
    };

    const CATEGORY_LABELS = {
        programming: 'Programming',
        research: 'Research',
        thinking: 'Thinking',
        messaging: 'Messaging',
        testing: 'Testing',
        delegation: 'Delegation',
        other: 'Other',
    };

    function formatHour(h) {
        if (h === 0) return '12a';
        if (h < 12) return h + 'a';
        if (h === 12) return '12p';
        return (h - 12) + 'p';
    }

    async function refresh() {
        try {
            const tz = 'America/Los_Angeles';  // Owner-local timezone for consistent semantics
            const [ov, ag, cat, hr] = await Promise.all([
                api('GET', `/analytics/overview?range=${range}`),
                api('GET', `/analytics/agents?range=${range}`),
                api('GET', `/analytics/categories?range=${range}`),
                api('GET', `/analytics/hourly?range=${range}&tz=${encodeURIComponent(tz)}`),
            ]);
            overview = ov;
            agentsList = ag;
            categories = cat;
            hourly = hr;

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
        <!-- Hero metrics with deltas and sparklines -->
        <div class="hero-metrics">
            <div class="metric-card">
                <div class="metric-label">Total Cost</div>
                <div class="metric-value cost">{formatCost(overview.totals.cost_usd)}</div>
                {#if overview.deltas?.cost_usd != null}
                    <div class="metric-delta {deltaClass(overview.deltas.cost_usd, true)}">{formatDelta(overview.deltas.cost_usd)}</div>
                {/if}
                {#if overview.trend?.length > 1}
                    <div class="sparkline">
                        {#each overview.trend as day}
                            {@const maxCost = Math.max(0.01, ...overview.trend.map(t => t.cost_usd || 0))}
                            <div class="spark-bar cost" style="height: {Math.max(2, ((day.cost_usd || 0) / maxCost) * 100)}%" title="{shortDate(day.bucket)}: {formatCost(day.cost_usd)}"></div>
                        {/each}
                    </div>
                {/if}
            </div>
            <div class="metric-card">
                <div class="metric-label">Active Hours</div>
                <div class="metric-value">{formatHours(overview.totals.active_hours)}</div>
                {#if overview.deltas?.active_hours != null}
                    <div class="metric-delta delta-neutral">{formatDelta(overview.deltas.active_hours)}</div>
                {/if}
            </div>
            <div class="metric-card">
                <div class="metric-label">Sessions</div>
                <div class="metric-value">{overview.totals.sessions_count || 0}</div>
                {#if overview.deltas?.sessions_count != null}
                    <div class="metric-delta delta-neutral">{formatDelta(overview.deltas.sessions_count)}</div>
                {/if}
                {#if overview.trend?.length > 1}
                    <div class="sparkline">
                        {#each overview.trend as day}
                            {@const maxSess = maxSessionsInTrend(overview.trend)}
                            <div class="spark-bar sessions" style="height: {Math.max(2, ((day.sessions_count || 0) / maxSess) * 100)}%" title="{shortDate(day.bucket)}: {day.sessions_count || 0} sessions"></div>
                        {/each}
                    </div>
                {/if}
            </div>
            <div class="metric-card">
                <div class="metric-label">Total Tokens</div>
                <div class="metric-value">{formatTokens((overview.totals.input_tokens || 0) + (overview.totals.output_tokens || 0) + (overview.totals.cached_input_tokens || 0))}</div>
                {#if overview.deltas?.total_tokens != null}
                    <div class="metric-delta delta-neutral">{formatDelta(overview.deltas.total_tokens)}</div>
                {/if}
            </div>
        </div>

        <!-- Token trend chart -->
        {#if overview.trend && overview.trend.length > 0}
            <div class="section">
                <h2>Token Usage</h2>
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

        <!-- Usage by category -->
        {#if categories && categories.categories && categories.categories.length > 0}
            {@const maxCatTokens = Math.max(1, ...categories.categories.map(c => (c.input_tokens || 0) + (c.output_tokens || 0) + (c.cached_input_tokens || 0)))}
            <div class="section">
                <h2>Usage by Category</h2>
                <div class="category-list">
                    {#each categories.categories as cat}
                        {@const totalTok = (cat.input_tokens || 0) + (cat.output_tokens || 0) + (cat.cached_input_tokens || 0)}
                        {@const pct = (totalTok / maxCatTokens) * 100}
                        <div class="category-row">
                            <span class="cat-label" style="color: {CATEGORY_COLORS[cat.category] || 'var(--text-muted)'}">
                                {CATEGORY_LABELS[cat.category] || cat.category}
                            </span>
                            <div class="cat-bar-bg">
                                <div class="cat-bar-fill" style="width: {pct}%; background: {CATEGORY_COLORS[cat.category] || 'var(--text-muted)'}"></div>
                            </div>
                            <span class="cat-tokens">{formatTokens(totalTok)}</span>
                            <span class="cat-turns">{cat.turns} turns</span>
                        </div>
                    {/each}
                </div>
            </div>
        {/if}

        <!-- Hourly activity -->
        {#if hourly && hourly.hours}
            {@const maxHourTokens = Math.max(1, ...hourly.hours.map(h => Math.max(h.total_tokens || 0, h.historical_avg || 0)))}
            <div class="section">
                <h2>Activity by Hour</h2>
                <div class="hourly-chart">
                    {#each hourly.hours as h}
                        <div class="hourly-bar-wrapper" title="{formatHour(h.hour)}: {formatTokens(h.total_tokens)} tokens (avg: {formatTokens(h.historical_avg)})">
                            <div class="hourly-bar-container">
                                <div class="hourly-bar current" style="height: {Math.max(1, (h.total_tokens / maxHourTokens) * 100)}%"></div>
                                {#if h.historical_avg > 0}
                                    <div class="hourly-avg-line" style="bottom: {(h.historical_avg / maxHourTokens) * 100}%"></div>
                                {/if}
                            </div>
                            <div class="hourly-label">{h.hour % 6 === 0 ? formatHour(h.hour) : ''}</div>
                        </div>
                    {/each}
                </div>
                <div class="trend-legend">
                    <span class="legend-item"><span class="legend-dot" style="background: var(--accent)"></span> Current</span>
                    <span class="legend-item"><span class="legend-line"></span> 90-day avg (active days)</span>
                </div>
            </div>
        {/if}

        <!-- Agent leaderboard — visual bars instead of table -->
        {#if agentsList && agentsList.agents && agentsList.agents.length > 0}
            <div class="section">
                <h2>Agents</h2>
                <div class="agent-leaderboard">
                    {#each agentsList.agents as agent}
                        {@const maxTok = maxAgentTokens(agentsList.agents)}
                        {@const maxCst = maxAgentCost(agentsList.agents)}
                        {@const total = agentTotalTokens(agent)}
                        {@const barPct = tokenPct(agent, maxTok)}
                        {@const inputPct = total > 0 ? (agent.input_tokens / total) * barPct : 0}
                        {@const outputPct = total > 0 ? (agent.output_tokens / total) * barPct : 0}
                        {@const cachedPct = total > 0 ? (agent.cached_input_tokens / total) * barPct : 0}
                        <button
                            class="agent-card"
                            class:selected={selectedAgent === agent.agent_name}
                            on:click={() => selectAgent(agent.agent_name)}
                        >
                            <div class="agent-card-header">
                                <span class="agent-name">{agent.agent_name}</span>
                                <span class="agent-cost">{formatCost(agent.cost_usd)}</span>
                            </div>
                            <div class="agent-bar-row">
                                <div class="agent-token-bar">
                                    <div class="bar-segment input" style="width: {inputPct}%"></div>
                                    <div class="bar-segment output" style="width: {outputPct}%"></div>
                                    <div class="bar-segment cached" style="width: {cachedPct}%"></div>
                                </div>
                                <span class="agent-tokens-label">{formatTokens(total)}</span>
                            </div>
                            <div class="agent-card-stats">
                                <span class="stat">{formatHours(agent.active_hours)} active</span>
                                <span class="stat">{agent.sessions_count} sessions</span>
                                <span class="stat">{agent.turns_count} turns</span>
                            </div>
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
                    <div class="sessions-chips">
                        {#each agentDetail.sessions as session}
                            <div class="session-chip" class:active={!session.ended_at} title="{session.session_id}">
                                <span class="chip-model">{session.model || '?'}</span>
                                <span class="chip-time">{new Date(session.started_at).toLocaleString([], { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' })}</span>
                                <span class="chip-status" class:is-active={!session.ended_at}>{session.ended_at ? 'ended' : 'live'}</span>
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
        grid-template-columns: repeat(4, 1fr);
        gap: 0.75rem;
        margin-bottom: 1.5rem;
    }

    .metric-card {
        background: var(--bg-secondary);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 1rem;
        display: flex;
        flex-direction: column;
        gap: 0.2rem;
    }

    .metric-label {
        font-family: var(--font-mono);
        font-size: 0.7rem;
        color: var(--text-muted);
        text-transform: uppercase;
        letter-spacing: 0.05em;
    }

    .metric-value {
        font-family: var(--font-mono);
        font-size: 1.5rem;
        font-weight: 700;
        color: var(--text);
        line-height: 1.2;
    }

    .metric-value.cost {
        color: var(--green);
    }

    .metric-delta {
        font-family: var(--font-mono);
        font-size: 0.7rem;
        font-weight: 600;
    }

    .delta-neutral { color: var(--text-muted); }
    .delta-positive { color: var(--green); }
    .delta-negative { color: #e55; }

    /* Sparklines inside hero cards */
    .sparkline {
        display: flex;
        align-items: flex-end;
        gap: 1px;
        height: 24px;
        margin-top: 0.35rem;
    }

    .spark-bar {
        flex: 1;
        border-radius: 1px;
        min-height: 1px;
        transition: height 0.3s ease;
    }

    .spark-bar.cost { background: var(--green); opacity: 0.6; }
    .spark-bar.sessions { background: var(--accent); opacity: 0.6; }

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
        height: 140px;
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
    .trend-bar.cached { background: var(--text-muted); opacity: 0.4; }

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
    .legend-dot.cached { background: var(--text-muted); opacity: 0.4; }

    /* Agent leaderboard — cards with bars */
    .agent-leaderboard {
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .agent-card {
        display: flex;
        flex-direction: column;
        gap: 6px;
        padding: 0.75rem 1rem;
        background: transparent;
        border: 1px solid var(--border);
        border-radius: 6px;
        cursor: pointer;
        transition: all 0.15s;
        width: 100%;
        text-align: left;
        color: var(--text);
        font-family: var(--font-mono);
    }

    .agent-card:hover {
        background: var(--bg-hover);
        border-color: var(--text-muted);
    }

    .agent-card.selected {
        border-color: var(--accent);
        border-left: 3px solid var(--accent);
    }

    .agent-card-header {
        display: flex;
        justify-content: space-between;
        align-items: baseline;
    }

    .agent-name {
        font-size: 0.9rem;
        font-weight: 700;
    }

    .agent-cost {
        font-size: 0.85rem;
        color: var(--green);
        font-weight: 600;
    }

    .agent-bar-row {
        display: flex;
        align-items: center;
        gap: 0.75rem;
    }

    .agent-token-bar {
        flex: 1;
        height: 10px;
        background: var(--border);
        border-radius: 5px;
        overflow: hidden;
        display: flex;
    }

    .bar-segment {
        height: 100%;
        transition: width 0.3s ease;
    }

    .bar-segment.input { background: var(--accent); }
    .bar-segment.output { background: var(--green); }
    .bar-segment.cached { background: var(--text-muted); opacity: 0.5; }

    .agent-tokens-label {
        font-size: 0.75rem;
        color: var(--text-muted);
        min-width: 50px;
        text-align: right;
    }

    .agent-card-stats {
        display: flex;
        gap: 1rem;
    }

    .stat {
        font-size: 0.7rem;
        color: var(--text-muted);
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

    /* Session chips */
    .sessions-chips {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
    }

    .session-chip {
        display: flex;
        align-items: center;
        gap: 6px;
        padding: 0.35rem 0.6rem;
        background: var(--bg);
        border: 1px solid var(--border);
        border-radius: 4px;
        font-family: var(--font-mono);
        font-size: 0.7rem;
        color: var(--text-muted);
    }

    .session-chip.active {
        border-color: var(--green);
    }

    .chip-model {
        color: var(--text);
        font-weight: 600;
    }

    .chip-status {
        font-size: 0.6rem;
        text-transform: uppercase;
        opacity: 0.7;
    }

    .chip-status.is-active {
        color: var(--green);
        opacity: 1;
    }

    /* Category breakdown */
    .category-list {
        display: flex;
        flex-direction: column;
        gap: 6px;
    }

    .category-row {
        display: grid;
        grid-template-columns: 100px 1fr 60px 70px;
        gap: 0.5rem;
        align-items: center;
        font-family: var(--font-mono);
        font-size: 0.8rem;
    }

    .cat-label {
        font-weight: 600;
        white-space: nowrap;
    }

    .cat-bar-bg {
        height: 8px;
        background: var(--border);
        border-radius: 4px;
        overflow: hidden;
    }

    .cat-bar-fill {
        height: 100%;
        border-radius: 4px;
        transition: width 0.3s ease;
    }

    .cat-tokens {
        text-align: right;
        color: var(--text);
        font-weight: 600;
    }

    .cat-turns {
        text-align: right;
        color: var(--text-muted);
        font-size: 0.7rem;
    }

    /* Hourly activity chart */
    .hourly-chart {
        display: flex;
        align-items: flex-end;
        gap: 2px;
        height: 120px;
        padding: 0.5rem 0;
    }

    .hourly-bar-wrapper {
        flex: 1;
        display: flex;
        flex-direction: column;
        align-items: center;
        height: 100%;
        min-width: 0;
    }

    .hourly-bar-container {
        flex: 1;
        width: 100%;
        position: relative;
        display: flex;
        align-items: flex-end;
    }

    .hourly-bar.current {
        width: 100%;
        background: var(--accent);
        border-radius: 2px 2px 0 0;
        min-height: 0;
        transition: height 0.3s ease;
        opacity: 0.7;
    }

    .hourly-avg-line {
        position: absolute;
        left: -1px;
        right: -1px;
        height: 2px;
        background: var(--green);
        border-radius: 1px;
    }

    .hourly-label {
        font-family: var(--font-mono);
        font-size: 0.55rem;
        color: var(--text-muted);
        margin-top: 4px;
        height: 12px;
    }

    .legend-line {
        width: 16px;
        height: 2px;
        background: var(--green);
        border-radius: 1px;
        display: inline-block;
    }

    /* Responsive */
    @media (max-width: 768px) {
        .analytics-page {
            padding: 1rem;
        }

        .hero-metrics {
            grid-template-columns: repeat(2, 1fr);
        }

        .tool-row {
            grid-template-columns: 100px 1fr 40px;
        }

        .tool-duration {
            display: none;
        }

        .agent-card-stats {
            flex-wrap: wrap;
            gap: 0.5rem;
        }

        .category-row {
            grid-template-columns: 80px 1fr 50px;
        }

        .cat-turns {
            display: none;
        }
    }
</style>
