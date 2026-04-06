<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { timeAgo } from '../lib/utils.js';

    let loading = true;
    let agents = [];
    let activityEvents = [];
    let upcomingSchedules = [];

    let sysVersion = '--';
    let sysUptime = '--';
    let claudeVersion = '';
    let sysSchedulerRunning = false;
    let sysSchedules = '--';
    let sysHeartbeats = '--';
    let serverStartedAt = null;

    let refreshInterval;
    let uptimeInterval;

    function formatUptime() {
        if (!serverStartedAt) return '--';
        const diff = Math.floor(Date.now() / 1000 - serverStartedAt);
        if (diff < 0) return '--';
        const d = Math.floor(diff / 86400);
        const h = Math.floor((diff % 86400) / 3600);
        const m = Math.floor((diff % 3600) / 60);
        const s = diff % 60;
        if (d > 0) return `${d}d ${h}h ${m}m`;
        if (h > 0) return `${h}h ${m}m`;
        if (m > 0) return `${m}m ${s}s`;
        return `${s}s`;
    }

    function statusColor(status) {
        if (status === 'online') return 'var(--green)';
        if (status === 'idle') return 'var(--yellow)';
        return 'var(--text-muted)';
    }

    function statusLabel(status) {
        if (status === 'online') return 'working';
        if (status === 'idle') return 'idle';
        return 'offline';
    }

    function contextBarColor(pct, nudge) {
        if (!nudge) nudge = 80;
        const ratio = pct / nudge;
        if (ratio >= 1) return 'var(--red)';
        if (ratio >= 0.75) return 'var(--yellow)';
        return 'var(--green)';
    }

    async function sleepAgent(name, event) {
        event.preventDefault();
        event.stopPropagation();
        try {
            await api('POST', `/agents/${name}/sleep`);
            refresh();
        } catch (e) {
            console.error('Sleep failed:', e);
        }
    }

    function summarizeTasks(taskCounts = {}) {
        const pending = taskCounts.pending || 0;
        const inProgress = taskCounts.in_progress || 0;
        const blocked = taskCounts.blocked || 0;
        const total = pending + inProgress + blocked;
        if (!total) return null;
        const parts = [];
        if (inProgress) parts.push(`${inProgress} in progress`);
        if (pending) parts.push(`${pending} pending`);
        if (blocked) parts.push(`${blocked} blocked`);
        return parts.join(' · ');
    }

    function formatNextRun(ts) {
        if (!ts) return '--';
        const d = new Date(ts * 1000);
        const now = new Date();
        const diffMs = d - now;
        if (diffMs < 0) return 'overdue';
        const diffMin = Math.floor(diffMs / 60000);
        if (diffMin < 60) return `in ${diffMin}m`;
        const diffH = Math.floor(diffMin / 60);
        if (diffH < 24) return `in ${diffH}h ${diffMin % 60}m`;
        const diffD = Math.floor(diffH / 24);
        return `in ${diffD}d ${diffH % 24}h`;
    }

    async function refresh() {
        try {
            const [root, agentsData, schedulerStatus, heartbeats, activityData, schedulesData] = await Promise.all([
                api('GET', '/api'),
                api('GET', '/agents?enabled_only=true'),
                api('GET', '/scheduler/status'),
                api('GET', '/heartbeats'),
                api('GET', '/activity?limit=20').catch(() => ({ events: [] })),
                api('GET', '/schedules?enabled_only=true').catch(() => ({ schedules: [] })),
            ]);

            const enabledAgents = agentsData.agents || [];

            // Load health + streaming sessions + tasks for each agent
            const agentDetails = await Promise.all(enabledAgents.map(async (agent) => {
                const [healthData, streamingData, tasksData] = await Promise.all([
                    api('GET', `/agents/${agent.name}/health`).catch(() => ({})),
                    api('GET', `/agents/${agent.name}/streaming-sessions`).catch(() => ({ sessions: [] })),
                    api('GET', `/tasks?assigned_agent=${agent.name}&include_completed=false&limit=5`).catch(() => ({ tasks: [] })),
                ]);

                const health = healthData || {};
                const streamingSessions = streamingData.sessions || [];
                const mainSession = streamingSessions.find(s => s.label === 'main');
                const stats = mainSession?.stats || {};
                const agentTasks = (tasksData.tasks || []);

                const contextPct = health.session?.context_used_pct ?? 0;
                const nudgePct = agent.restart_threshold_pct || 80;
                const cost = stats.cost_usd || health.cost?.session_cost_usd || 0;
                const turns = stats.turns || 0;
                const messages = (stats.messages_sent || 0) + turns;
                const taskSummary = summarizeTasks(health.tasks || {});

                // Determine status — streaming session is the source of truth
                let status;
                if (mainSession?.connected && (stats.pending_responses || 0) > 0) {
                    status = 'online'; // actively processing
                } else if (mainSession?.connected) {
                    status = 'idle'; // connected but not processing
                } else {
                    status = 'offline'; // no active session
                }

                return {
                    name: agent.name,
                    display_name: agent.display_name || agent.name,
                    model: agent.model || 'default',
                    role: agent.role || '',
                    status,
                    connected: mainSession?.connected || false,
                    pending: stats.pending_responses || 0,
                    contextPct,
                    nudgePct,
                    cost,
                    turns,
                    messages,
                    taskSummary,
                    tasks: agentTasks,
                    reconnects: stats.reconnects || 0,
                    errors: stats.errors || 0,
                    autoRestarts: stats.auto_restarts || 0,
                    workerCount: streamingSessions.filter(s => s.label !== 'main' && s.connected).length,
                    recommendation: health.recommendation || null,
                };
            }));

            // Sort: online first, then idle, then offline
            const statusOrder = { online: 0, idle: 1, offline: 2, unknown: 3 };
            agentDetails.sort((a, b) => (statusOrder[a.status] ?? 3) - (statusOrder[b.status] ?? 3));
            agents = agentDetails;

            activityEvents = activityData.events || [];

            // Schedules — sort by next_run
            upcomingSchedules = (schedulesData.schedules || [])
                .filter(s => s.next_run)
                .sort((a, b) => a.next_run - b.next_run)
                .slice(0, 10);

            sysVersion = root.version;
            claudeVersion = root.claude_version || '';
            serverStartedAt = root.started_at;
            sysUptime = formatUptime();
            sysSchedulerRunning = schedulerStatus.running;
            sysSchedules = `${schedulerStatus.enabled_schedules} active / ${schedulerStatus.total_schedules} total`;
            const aliveCount = (heartbeats.heartbeats || []).filter(h => h.status === 'alive').length;
            const totalHb = (heartbeats.heartbeats || []).length;
            sysHeartbeats = `${aliveCount} alive / ${totalHb} tracked`;
            loading = false;
        } catch (e) {
            console.error('Dashboard refresh error:', e);
            loading = false;
        }
    }

    onMount(() => {
        refresh();
        refreshInterval = setInterval(refresh, 10000);
        uptimeInterval = setInterval(() => { sysUptime = formatUptime(); }, 1000);
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        clearInterval(uptimeInterval);
    });
</script>

{#if loading}
<div class="loading-screen">
    <div class="loading-logo">PINKY<span class="loading-dot">.</span></div>
    <div class="loading-bar"><div class="loading-fill"></div></div>
</div>
{:else}
<div class="content">
    <!-- Agent Status Cards -->
    <div class="section agents-section">
        <div class="section-header">
            <div class="section-title">Agents</div>
            <span class="agent-count mono">{agents.length} registered</span>
        </div>
        <div class="agent-grid">
            {#if agents.length === 0}
                <div class="empty">No agents online</div>
            {:else}
                {#each agents as agent}
                    <a href="#/chat/{agent.name}" class="agent-card">
                        <!-- Status indicator + name -->
                        <div class="agent-header">
                            <div class="agent-dot" style="background:{statusColor(agent.status)}"></div>
                            <div class="agent-name">{agent.display_name}</div>
                            <span class="agent-status-tag" style="color:{statusColor(agent.status)}">{statusLabel(agent.status)}</span>
                        </div>

                        <!-- Work summary -->
                        <div class="agent-work">
                            {#if agent.taskSummary}
                                <span class="agent-task-text">{agent.taskSummary}</span>
                            {:else if agent.connected}
                                <span class="agent-idle-text">Listening</span>
                            {:else}
                                <span class="agent-offline-text">No active session</span>
                            {/if}
                        </div>

                        <!-- Task list (hover reveal) -->
                        {#if agent.tasks.length > 0}
                            <div class="agent-task-list">
                                {#each agent.tasks as task}
                                    <div class="agent-task-row">
                                        <span class="task-status-dot task-{task.status}"></span>
                                        <span class="task-title">#{task.id} {task.title}</span>
                                    </div>
                                {/each}
                            </div>
                        {/if}

                        <!-- Context bar — scaled to nudge threshold -->
                        <div class="agent-stats">
                            <div class="agent-stat">
                                <div class="stat-bar-wrap" title="Restart nudge at {agent.nudgePct}%">
                                    <div class="stat-bar" style="width:{Math.min(agent.contextPct / agent.nudgePct * 100, 100)}%;background:{contextBarColor(agent.contextPct, agent.nudgePct)}"></div>
                                </div>
                                <span class="stat-val" style="color:{contextBarColor(agent.contextPct, agent.nudgePct)}">{agent.contextPct}%</span>
                            </div>
                            <div class="agent-meta">
                                <span>{agent.messages} msg</span>
                                <span class="meta-dot">·</span>
                                <span>{agent.turns} turns</span>
                                <span class="meta-dot">·</span>
                                <span>${agent.cost.toFixed(2)}</span>
                                {#if agent.workerCount > 0}
                                    <span class="meta-dot">·</span>
                                    <span class="worker-tag">+{agent.workerCount} worker{agent.workerCount > 1 ? 's' : ''}</span>
                                {/if}
                            </div>
                        </div>

                        <!-- Footer: model + actions -->
                        <div class="agent-footer">
                            <span class="agent-model">{agent.model}</span>
                            <div class="agent-footer-right">
                                {#if agent.errors > 0}
                                    <span class="agent-errors">{agent.errors} err</span>
                                {/if}
                                {#if agent.connected}
                                    <button class="btn-sleep" on:click={(e) => sleepAgent(agent.name, e)} title="Put to sleep">
                                        <span class="material-symbols-outlined" style="font-size:14px">dark_mode</span>
                                    </button>
                                {/if}
                            </div>
                        </div>
                    </a>
                {/each}
            {/if}
        </div>
    </div>

    <!-- Schedules + Activity side by side -->
    <div class="grid-2">
        <!-- Upcoming Schedules -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Schedules</div>
                <span class="agent-count mono">{upcomingSchedules.length} upcoming</span>
            </div>
            <div class="section-body feed">
                {#if upcomingSchedules.length === 0}
                    <div class="empty">No upcoming schedules</div>
                {:else}
                    {#each upcomingSchedules as sched}
                        <div class="feed-row">
                            <span class="feed-icon" style="color:var(--tone-lilac-text)">⏱</span>
                            <span class="feed-agent">{sched.agent_name}</span>
                            <span class="feed-title" title={sched.name}>{sched.name}</span>
                            <span class="sched-cron mono">{sched.cron}</span>
                            <span class="feed-time">{formatNextRun(sched.next_run)}</span>
                        </div>
                    {/each}
                {/if}
            </div>
        </div>

        <!-- Activity Feed -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Activity</div>
                <span class="agent-count mono">{activityEvents.length} events</span>
            </div>
            <div class="section-body feed">
                {#if activityEvents.length === 0}
                    <div class="empty">No recent activity</div>
                {:else}
                    {#each activityEvents as ev}
                        {@const iconMap = { task_created: '⊕', task_completed: '✓', research_published: '◎', presentation_created: '▣', agent_status: '●', agent_idle: '○', agent_working: '●', agent_offline: '○' }}
                        {@const colorMap = { task_completed: 'var(--green)', research_published: 'var(--yellow)', task_created: 'var(--text-secondary)', presentation_created: 'var(--tone-lilac-text)', agent_status: 'var(--text-muted)', agent_working: 'var(--green)', agent_idle: 'var(--yellow)', agent_offline: 'var(--text-muted)' }}
                        <div class="feed-row">
                            <span class="feed-icon" style="color:{colorMap[ev.event_type] || 'var(--text-muted)'}">{iconMap[ev.event_type] || '●'}</span>
                            <span class="feed-agent">{ev.agent_name}</span>
                            <span class="feed-title">{ev.title}</span>
                            <span class="feed-time">{timeAgo(ev.created_at)}</span>
                        </div>
                    {/each}
                {/if}
            </div>
        </div>
    </div>

    <!-- System Strip -->
    <div class="sys-strip">
        <span class="mono">v{sysVersion}{claudeVersion ? ` · ${claudeVersion.split(' ')[0]}` : ''}</span>
        <span class="sys-dot">·</span>
        <span class="mono">{sysUptime} uptime</span>
        <span class="sys-dot">·</span>
        <span class="mono" style="color:{sysSchedulerRunning ? 'var(--green)' : 'var(--red)'}">{sysSchedulerRunning ? 'scheduler on' : 'scheduler off'}</span>
        <span class="sys-dot">·</span>
        <span class="mono">{sysSchedules}</span>
        <span class="sys-dot">·</span>
        <span class="mono">{sysHeartbeats}</span>
    </div>
</div>
{/if}

<style>
    /* Loading screen */
    .loading-screen {
        display: flex;
        flex-direction: column;
        align-items: center;
        justify-content: center;
        min-height: 60vh;
        gap: 1.5rem;
    }
    .loading-logo {
        font-family: var(--font-grotesk);
        font-size: 2.5rem;
        font-weight: 900;
        letter-spacing: -0.02em;
        color: var(--yellow);
    }
    .loading-dot { color: var(--text-muted); }
    .loading-bar {
        width: 120px;
        height: 3px;
        background: var(--surface-3);
        border-radius: 2px;
        overflow: hidden;
    }
    .loading-fill {
        height: 100%;
        width: 40%;
        background: var(--yellow);
        border-radius: 2px;
        animation: loading-slide 1s ease-in-out infinite;
    }
    @keyframes loading-slide {
        0% { transform: translateX(-100%); }
        100% { transform: translateX(350%); }
    }
    /* Agents section — override overflow:hidden so task popup isn't clipped */
    .agents-section { overflow: visible; }
    /* Agent grid */
    .agent-grid {
        display: grid;
        grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
        gap: 1rem;
        padding: 1rem 1.5rem 1.5rem;
        overflow: visible;
    }
    .agent-card {
        display: flex;
        flex-direction: column;
        gap: 0.6rem;
        padding: 1rem 1.2rem;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        text-decoration: none;
        color: var(--text-primary);
        transition: all 0.15s;
        cursor: pointer;
        position: relative;
    }
    .agent-card:hover {
        background: var(--surface-2);
        border-color: var(--text-muted);
        transform: translateY(-1px);
        z-index: 20;
    }

    .agent-header {
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    .agent-dot {
        width: 8px;
        height: 8px;
        border-radius: 50%;
        flex-shrink: 0;
    }
    .agent-name {
        font-family: var(--font-grotesk);
        font-size: 0.95rem;
        font-weight: 700;
        flex: 1;
    }
    .agent-status-tag {
        font-family: var(--font-grotesk);
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        font-weight: 600;
    }

    .agent-work {
        font-size: 0.78rem;
        color: var(--text-secondary);
        min-height: 1.2em;
    }
    .agent-working { color: var(--green); font-weight: 600; }
    .agent-task-text { color: var(--text-secondary); }
    .agent-idle-text { color: var(--text-muted); font-style: italic; }
    .agent-offline-text { color: var(--text-muted); font-style: italic; }

    /* Task list — floating popup on hover */
    .agent-task-list {
        position: absolute;
        top: 100%;
        left: 0;
        right: 0;
        z-index: 10;
        display: flex; flex-direction: column; gap: 0.25rem;
        padding: 0.6rem 0.8rem;
        margin-top: 4px;
        background: var(--surface-1);
        border: 1px solid var(--border);
        border-radius: var(--radius-lg);
        box-shadow: 0 4px 16px rgba(0,0,0,0.4);
        opacity: 0;
        pointer-events: none;
        transform: translateY(-4px);
        transition: opacity 0.15s ease, transform 0.15s ease;
    }
    .agent-card:hover .agent-task-list {
        opacity: 1;
        pointer-events: auto;
        transform: translateY(0);
    }
    .agent-task-row { display: flex; align-items: center; gap: 0.4rem; }
    .task-status-dot { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; }
    .task-in_progress { background: var(--green); }
    .task-pending { background: var(--yellow); }
    .task-blocked { background: var(--red); }
    .task-title {
        font-size: 0.68rem;
        color: var(--text-muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }

    .agent-stats {
        display: flex;
        flex-direction: column;
        gap: 0.35rem;
    }
    .agent-stat {
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    .stat-bar-wrap {
        flex: 1;
        height: 4px;
        background: var(--surface-3);
        border-radius: 2px;
        overflow: hidden;
        position: relative;
    }
    .stat-bar {
        height: 100%;
        border-radius: 2px;
        transition: width 0.3s;
    }
    .stat-val {
        font-family: var(--font-grotesk);
        font-size: 0.7rem;
        font-weight: 600;
        min-width: 2.5rem;
        text-align: right;
    }
    .agent-meta {
        display: flex;
        align-items: center;
        gap: 0.3rem;
        font-family: var(--font-body);
        font-size: 0.68rem;
        color: var(--text-muted);
    }
    .meta-dot { color: var(--border); }
    .worker-tag { color: var(--tone-lilac-text); font-weight: 600; }

    .agent-footer {
        display: flex;
        align-items: center;
        justify-content: space-between;
    }
    .agent-footer-right {
        display: flex;
        align-items: center;
        gap: 0.5rem;
    }
    .btn-sleep {
        background: none;
        border: none;
        cursor: pointer;
        color: var(--text-muted);
        padding: 0.15rem;
        border-radius: var(--radius);
        display: flex;
        align-items: center;
        transition: color 0.1s;
    }
    .btn-sleep:hover { color: var(--yellow); }
    .agent-model {
        font-family: var(--font-grotesk);
        font-size: 0.6rem;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--text-muted);
        background: var(--surface-2);
        padding: 0.15rem 0.4rem;
        border-radius: var(--radius);
    }
    .agent-errors {
        font-family: var(--font-grotesk);
        font-size: 0.6rem;
        color: var(--red);
        font-weight: 600;
    }
    .agent-count {
        font-size: 0.7rem;
        color: var(--text-muted);
    }

    /* Two-column layout */
    .grid-2 {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 1.5rem;
        margin-bottom: 0.5rem;
    }

    /* Feed rows (shared by activity + schedules) */
    .feed { padding: 0; }
    .feed-row {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 0.5rem 1rem;
        font-size: 0.82rem;
        border-bottom: 1px solid var(--surface-2);
    }
    .feed-row:last-child { border-bottom: none; }
    .feed-icon { font-size: 0.75rem; width: 1.2rem; text-align: center; flex-shrink: 0; }
    .feed-agent {
        font-family: var(--font-grotesk);
        font-size: 0.72rem;
        font-weight: 700;
        color: var(--text-primary);
        min-width: 70px;
        flex-shrink: 0;
    }
    .feed-title {
        flex: 1;
        color: var(--text-secondary);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .feed-time {
        font-family: var(--font-body);
        font-size: 0.65rem;
        color: var(--text-muted);
        flex-shrink: 0;
    }
    .sched-cron {
        font-size: 0.62rem;
        color: var(--text-muted);
        background: var(--surface-2);
        padding: 0.1rem 0.35rem;
        border-radius: var(--radius);
        flex-shrink: 0;
    }

    /* System strip */
    .sys-strip {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        padding: 0.75rem 0 0.5rem;
        font-size: 0.75rem;
        color: var(--text-muted);
        border-top: 1px solid var(--border);
        margin-top: 0.5rem;
    }
    .sys-dot { color: var(--border); }

    @media (max-width: 900px) {
        .grid-2 { grid-template-columns: 1fr; }
    }
    @media (max-width: 640px) {
        .agent-grid { grid-template-columns: 1fr; }
        /* On mobile, show tasks inline (no hover popup) */
        .agent-task-list {
            position: static;
            opacity: 1;
            pointer-events: auto;
            transform: none;
            box-shadow: none;
            border: none;
            margin-top: 0;
            padding: 0;
            background: transparent;
        }
    }
</style>
