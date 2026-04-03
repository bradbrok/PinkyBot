<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { contextClass, formatDate } from '../lib/utils.js';

    let heroAgents = '--';
    let heroResearch = '--';
    let heroTasks = '--';
    let heroPresentations = '--';

    let sessions = [];
    let upcomingTasks = [];
    let expandedSession = null;

    let presentations = [];
    let researchTopics = [];
    let researchStatusCounts = {};

    let sysVersion = '--';
    let sysUptime = '--';
    let sysApi = '--';
    let claudeVersion = '';
    let sysScheduler = '--';
    let sysSchedulerRunning = false;
    let sysSchedules = '--';
    let sysHeartbeats = '--';

    let serverStartedAt = null;
    let refreshInterval;
    let uptimeInterval;

    let activeSprints = []; // [{project, sprint, series}]

    function burndownPoints(series, total, w, h, ideal = false) {
        if (!series.length || !total) return '';
        const n = series.length;
        return series.map((pt, i) => {
            const x = n > 1 ? (i / (n - 1)) * w : 0;
            const remaining = ideal
                ? total - (i / Math.max(n - 1, 1)) * total
                : total - pt.cumulative;
            const y = h - Math.max(0, Math.min(remaining / total, 1)) * h;
            return `${x.toFixed(1)},${y.toFixed(1)}`;
        }).join(' ');
    }

    async function loadBurndown() {
        try {
            const projectsResp = await api('GET', '/projects');
            const projects = projectsResp.projects || [];
            const sprintResps = await Promise.all(
                projects.map(p =>
                    api('GET', `/projects/${p.id}/sprints?include_completed=false`).catch(() => ({ sprints: [] }))
                )
            );
            const withActive = [];
            for (let i = 0; i < projects.length; i++) {
                const active = (sprintResps[i].sprints || []).find(s => s.status === 'active');
                if (active) withActive.push({ project: projects[i], sprint: active });
            }
            activeSprints = await Promise.all(
                withActive.map(async ({ project, sprint }) => {
                    const bd = await api('GET', `/sprints/${sprint.id}/burndown`).catch(() => ({ series: [] }));
                    return { project, sprint, series: bd.series || [] };
                })
            );
        } catch (e) {
            console.error('Burndown load error:', e);
        }
    }

    const SESSION_TYPE_LABELS = {
        main: 'main',
        worker: 'worker',
        chat: 'chat',
        streaming: 'worker',
    };

    const TASK_STATUS_BADGES = {
        pending: 'model',
        in_progress: 'running',
        blocked: 'off',
        completed: 'on',
        cancelled: 'closed',
    };

    const RESEARCH_STATUS_COLORS = {
        open: 'var(--tone-lilac-text)',
        assigned: 'var(--yellow)',
        researching: 'running',
        published: 'on',
        cancelled: 'closed',
    };

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

    function priorityRank(priority) {
        return { urgent: 0, high: 1, normal: 2, low: 3 }[priority] ?? 4;
    }

    function taskDueValue(task) {
        if (!task?.due_date) return Number.MAX_SAFE_INTEGER;
        const ts = Date.parse(task.due_date);
        return Number.isFinite(ts) ? ts : Number.MAX_SAFE_INTEGER;
    }

    function formatDueLabel(dueDate) {
        if (!dueDate) return '--';

        const due = new Date(dueDate);
        if (Number.isNaN(due.getTime())) return formatDate(dueDate);

        const now = new Date();
        const startNow = new Date(now.getFullYear(), now.getMonth(), now.getDate());
        const startDue = new Date(due.getFullYear(), due.getMonth(), due.getDate());
        const diffDays = Math.round((startDue - startNow) / 86400000);

        if (diffDays < 0) return `${formatDate(dueDate)} · overdue`;
        if (diffDays === 0) return `${formatDate(dueDate)} · today`;
        if (diffDays === 1) return `${formatDate(dueDate)} · tomorrow`;
        return `${formatDate(dueDate)} · in ${diffDays}d`;
    }

    function sessionStateBadge(state) {
        if (state === 'busy') return 'running';
        if (state === 'connected') return 'on';
        if (state === 'sleeping') return 'closed';
        return state || 'closed';
    }

    function taskStatusBadge(status) {
        return TASK_STATUS_BADGES[status] || 'model';
    }

    function researchStatusBadge(status) {
        const map = { open: 'model', assigned: 'running', researching: 'running', published: 'on', cancelled: 'closed' };
        return map[status] || 'model';
    }

    function summarizeTasks(taskCounts = {}) {
        const pending = taskCounts.pending || 0;
        const inProgress = taskCounts.in_progress || 0;
        const blocked = taskCounts.blocked || 0;
        const total = pending + inProgress + blocked;
        if (!total) return 'No active tasks';

        const parts = [];
        if (inProgress) parts.push(`${inProgress} in progress`);
        if (pending) parts.push(`${pending} pending`);
        if (blocked) parts.push(`${blocked} blocked`);
        return parts.join(' · ');
    }

    function normalizeLegacySession(agent, session, health = {}) {
        const usage = session.usage || {};
        const taskCounts = session.session_type === 'main' ? (health.tasks || {}) : {};

        return {
            id: session.id,
            display_name: agent.display_name || agent.name,
            agent_name: agent.name,
            label: session.id.replace(`${agent.name}-`, '') || session.session_type || 'session',
            model: session.model || agent.model || 'default',
            session_type: session.session_type || 'chat',
            state: session.state || 'idle',
            connected: session.state !== 'closed',
            context_used_pct: session.context_used_pct || 0,
            pending_responses: 0,
            message_count: session.message_count || 0,
            turns: usage.total_turns || 0,
            total_cost_usd: usage.total_cost_usd || 0,
            reconnects: 0,
            errors: session.state === 'error' ? 1 : 0,
            auto_restarts: 0,
            task_counts: taskCounts,
            task_summary: summarizeTasks(taskCounts),
            usage,
            source: 'legacy',
        };
    }

    function normalizeStreamingSession(agent, streamingSession, health = {}) {
        const stats = streamingSession.stats || {};
        const isMain = streamingSession.label === 'main';
        const taskCounts = isMain ? (health.tasks || {}) : {};
        const contextPct = isMain ? (health.session?.context_used_pct ?? 0) : 0;
        const state = streamingSession.connected
            ? ((stats.pending_responses || 0) > 0 ? 'busy' : 'connected')
            : 'sleeping';

        return {
            id: `${agent.name}-${streamingSession.label}`,
            display_name: agent.display_name || agent.name,
            agent_name: agent.name,
            label: streamingSession.label,
            model: agent.model || 'default',
            session_type: isMain ? 'main' : 'streaming',
            state,
            connected: streamingSession.connected,
            context_used_pct: contextPct,
            pending_responses: stats.pending_responses || 0,
            message_count: (stats.messages_sent || 0) + (stats.turns || 0),
            turns: stats.turns || 0,
            total_cost_usd: stats.cost_usd || 0,
            reconnects: stats.reconnects || 0,
            errors: stats.errors || 0,
            auto_restarts: stats.auto_restarts || 0,
            task_counts: taskCounts,
            task_summary: summarizeTasks(taskCounts),
            usage: {
                total_queries: stats.messages_sent || 0,
                total_turns: stats.turns || 0,
                total_cost_usd: stats.cost_usd || 0,
            },
            source: 'streaming',
        };
    }

    async function loadSessions(agentList) {
        const diagnostics = await Promise.all(agentList.map(async (agent) => {
            const [streamingData, legacyData, healthData] = await Promise.all([
                api('GET', `/agents/${agent.name}/streaming-sessions`).catch(() => ({ sessions: [] })),
                api('GET', `/agents/${agent.name}/sessions`).catch(() => ({ sessions: [] })),
                api('GET', `/agents/${agent.name}/health`).catch(() => ({})),
            ]);

            return {
                agent,
                streaming: streamingData.sessions || [],
                legacy: legacyData.sessions || [],
                health: healthData || {},
            };
        }));

        const rows = [];
        const seenIds = new Set();

        for (const item of diagnostics) {
            for (const streamingSession of item.streaming) {
                const row = normalizeStreamingSession(item.agent, streamingSession, item.health);
                seenIds.add(row.id);
                rows.push(row);
            }

            for (const legacySession of item.legacy) {
                if (seenIds.has(legacySession.id)) continue;
                rows.push(normalizeLegacySession(item.agent, legacySession, item.health));
            }
        }

        rows.sort((a, b) => {
            const aMain = a.session_type === 'main' ? 0 : 1;
            const bMain = b.session_type === 'main' ? 0 : 1;
            if (aMain !== bMain) return aMain - bMain;
            if (a.connected !== b.connected) return a.connected ? -1 : 1;
            if (b.pending_responses !== a.pending_responses) return b.pending_responses - a.pending_responses;
            if (b.message_count !== a.message_count) return b.message_count - a.message_count;
            return a.display_name.localeCompare(b.display_name);
        });

        return rows;
    }

    function copyShareLink(shareToken) {
        const url = `${window.location.origin}/p/${shareToken}`;
        navigator.clipboard.writeText(url).catch(() => {});
    }

    async function refresh() {
        try {
            const [root, agentsData, convos, schedulerStatus, heartbeats, tasksData, schedulesData, presentationsData, researchData] = await Promise.all([
                api('GET', '/api'),
                api('GET', '/agents?enabled_only=true'),
                api('GET', '/conversations'),
                api('GET', '/scheduler/status'),
                api('GET', '/heartbeats'),
                api('GET', '/tasks?include_completed=false&limit=12'),
                api('GET', '/schedules?enabled_only=true').catch(() => ({ schedules: [] })),
                api('GET', '/presentations?limit=4').catch(() => ({ presentations: [], count: 0 })),
                api('GET', '/research').catch(() => ({ topics: [], count: 0 })),
            ]);

            const enabledAgents = agentsData.agents || [];
            sessions = await loadSessions(enabledAgents);
            const openTasks = tasksData.tasks || [];

            // Merge tasks + cron jobs into upcoming feed
            const taskItems = openTasks.map(t => ({ ...t, _type: 'task' }));
            const cronItems = (schedulesData.schedules || []).map(s => ({
                _type: 'cron',
                id: s.id,
                title: s.name,
                assigned_agent: s.agent_name,
                status: 'scheduled',
                priority: '--',
                due_date: s.next_run ? new Date(s.next_run * 1000).toISOString() : null,
                tags: [s.cron],
            }));

            upcomingTasks = [...taskItems, ...cronItems]
                .sort((a, b) => {
                    const aDue = taskDueValue(a);
                    const bDue = taskDueValue(b);
                    if (aDue !== bDue) return aDue - bDue;
                    const byPriority = priorityRank(a.priority) - priorityRank(b.priority);
                    if (byPriority !== 0) return byPriority;
                    return (a.created_at || 0) - (b.created_at || 0);
                })
                .slice(0, 8);

            // Presentations
            presentations = presentationsData.presentations || [];
            const presTotal = presentationsData.count || presentations.length;

            // Research
            const allTopics = researchData.topics || [];
            const statusCounts = {};
            for (const t of allTopics) {
                statusCounts[t.status] = (statusCounts[t.status] || 0) + 1;
            }
            researchStatusCounts = statusCounts;
            researchTopics = allTopics.slice(0, 4);
            const activeResearchCount = allTopics.filter(t => t.status !== 'published' && t.status !== 'cancelled').length;

            // Hero stats
            heroAgents = enabledAgents.length;
            heroResearch = activeResearchCount;
            heroTasks = openTasks.length;
            heroPresentations = presTotal;

            sysVersion = root.version;
            claudeVersion = root.claude_version || '';
            serverStartedAt = root.started_at;
            sysUptime = formatUptime();
            sysApi = window.location.origin;
            sysSchedulerRunning = schedulerStatus.running;
            sysScheduler = schedulerStatus.running ? 'Running' : 'Stopped';
            sysSchedules = `${schedulerStatus.enabled_schedules} active / ${schedulerStatus.total_schedules} total`;
            const aliveCount = (heartbeats.heartbeats || []).filter(h => h.status === 'alive').length;
            const totalHb = (heartbeats.heartbeats || []).length;
            sysHeartbeats = `${aliveCount} alive / ${totalHb} tracked`;
        } catch (e) {
            console.error('Dashboard refresh error:', e);
        }
    }

    onMount(() => {
        refresh();
        loadBurndown();
        refreshInterval = setInterval(refresh, 10000);
        setInterval(loadBurndown, 30000);
        uptimeInterval = setInterval(() => { sysUptime = formatUptime(); }, 1000);
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        clearInterval(uptimeInterval);
    });
</script>

<div class="content">
    <!-- Hero -->
    <div class="hero">
        <div class="hero-title">PINKY<span class="y">.</span></div>
        <div class="hero-sub">Personal AI Companion Framework — Powered by Claude Code{claudeVersion ? ` ${claudeVersion.split(' ')[0]}` : ''}</div>
        <div class="hero-stats">
            <div>
                <div class="hero-stat-value">{heroAgents}</div>
                <div class="hero-stat-label">Agents</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroResearch}</div>
                <div class="hero-stat-label">Active Research</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroTasks}</div>
                <div class="hero-stat-label">Open Tasks</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroPresentations}</div>
                <div class="hero-stat-label">Presentations</div>
            </div>
        </div>
    </div>

    <!-- Quick Nav -->
    <div class="nav-grid">
        <a href="#/chat" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">chat</span></span>
            <div class="nav-card-title">Chat</div>
        </a>
        <a href="#/fleet" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">smart_toy</span></span>
            <div class="nav-card-title">Fleet</div>
        </a>
        <a href="#/settings" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">settings</span></span>
            <div class="nav-card-title">Settings</div>
        </a>
        <a href="/docs" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">api</span></span>
            <div class="nav-card-title">API Docs</div>
        </a>
        <a href="#/tasks" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">task_alt</span></span>
            <div class="nav-card-title">Tasks</div>
        </a>
        <a href="#/presentations" class="nav-card">
            <span class="nav-card-icon-wrap"><span class="material-symbols-outlined nav-card-icon">present_to_all</span></span>
            <div class="nav-card-title">Presentations</div>
        </a>
    </div>

    <!-- Sessions + Tasks -->
    <div class="grid-2">
        <!-- Active Sessions -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Active Sessions</div>
                <a href="#/fleet" style="font-family:var(--font-grotesk);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View All &rarr;</a>
            </div>
            <div class="section-body">
                {#if sessions.length === 0}
                    <div class="empty">No active sessions</div>
                {:else}
                    <table class="data-table">
                        <thead>
                            <tr><th>Agent</th><th>Session</th><th>State</th><th>Work</th><th>Context</th></tr>
                        </thead>
                        <tbody>
                            {#each sessions as s}
                                {@const sType = SESSION_TYPE_LABELS[s.session_type] || s.session_type || 'chat'}
                                <tr class="clickable" on:click={() => expandedSession = expandedSession === s.id ? null : s.id}>
                                    <td>
                                        <div class="session-agent">{s.display_name}</div>
                                        <div class="session-sub mono">@{s.agent_name}</div>
                                    </td>
                                    <td>
                                        <span class="expand-icon">{expandedSession === s.id ? '▼' : '▶'}</span>
                                        <span class="badge badge-{sType}">{sType}</span>
                                        <span class="mono" style="font-size:0.75rem">{s.label}</span>
                                        <div class="session-sub mono">{s.model || 'default'}</div>
                                    </td>
                                    <td>
                                        <span class="badge badge-{sessionStateBadge(s.state)}">{s.state}</span>
                                        {#if s.pending_responses > 0}
                                            <div class="session-sub mono">{s.pending_responses} waiting</div>
                                        {:else if s.connected}
                                            <div class="session-sub mono">live</div>
                                        {:else}
                                            <div class="session-sub mono">disconnected</div>
                                        {/if}
                                    </td>
                                    <td>
                                        <div class="session-work">{s.task_summary}</div>
                                        <div class="session-sub mono">{s.message_count} msg{(s.message_count || 0) === 1 ? '' : 's'} · {s.turns || 0} turn{(s.turns || 0) === 1 ? '' : 's'}</div>
                                    </td>
                                    <td>
                                        <div style="display:flex;align-items:center;gap:0.5rem">
                                            <div class="context-bar" style="width:60px">
                                                <div class="context-fill {contextClass(s.context_used_pct)}" style="width:{Math.min(s.context_used_pct, 100)}%"></div>
                                            </div>
                                            <span class="mono" style="font-size:0.75rem">{s.context_used_pct}%</span>
                                        </div>
                                        <div class="session-sub mono">{s.total_cost_usd ? '$' + s.total_cost_usd.toFixed(4) : '$0.0000'}</div>
                                    </td>
                                </tr>
                                {#if expandedSession === s.id}
                                    <tr class="usage-row">
                                        <td colspan="5">
                                            <div class="usage-panel">
                                                <div class="usage-grid">
                                                    <div>
                                                        <div class="usage-label">Agent</div>
                                                        <div class="usage-value">{s.display_name}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Session</div>
                                                        <div class="usage-value">{s.label}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Messages</div>
                                                        <div class="usage-value">{s.message_count || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Turns</div>
                                                        <div class="usage-value">{s.turns || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Pending</div>
                                                        <div class="usage-value">{s.pending_responses || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Cost</div>
                                                        <div class="usage-value">{s.total_cost_usd ? '$' + s.total_cost_usd.toFixed(4) : '$0.0000'}</div>
                                                    </div>
                                                </div>
                                                <div class="usage-grid" style="margin-top:0.8rem">
                                                    <div>
                                                        <div class="usage-label">Active Tasks</div>
                                                        <div class="usage-value">{(s.task_counts.pending || 0) + (s.task_counts.in_progress || 0) + (s.task_counts.blocked || 0)}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">In Progress</div>
                                                        <div class="usage-value">{s.task_counts.in_progress || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Pending</div>
                                                        <div class="usage-value">{s.task_counts.pending || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Blocked</div>
                                                        <div class="usage-value">{s.task_counts.blocked || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Reconnects</div>
                                                        <div class="usage-value">{s.reconnects || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Errors</div>
                                                        <div class="usage-value">{s.errors || 0}</div>
                                                    </div>
                                                </div>
                                            </div>
                                        </td>
                                    </tr>
                                {/if}
                            {/each}
                        </tbody>
                    </table>
                {/if}
            </div>
        </div>

        <!-- Upcoming Tasks + Cron Jobs -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Upcoming</div>
                <a href="#/tasks" style="font-family:var(--font-grotesk);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View All &rarr;</a>
            </div>
            <div class="section-body">
                {#if upcomingTasks.length === 0}
                    <div class="empty">No open tasks or scheduled jobs</div>
                {:else}
                    <table class="data-table">
                        <thead>
                            <tr><th>Item</th><th>Type</th><th>Status</th><th>Agent</th><th>Next / Due</th></tr>
                        </thead>
                        <tbody>
                            {#each upcomingTasks as item}
                                <tr>
                                    <td>
                                        <div class="task-title">{item._type === 'task' ? `#${item.id} ` : ''}{item.title}</div>
                                        <div class="session-sub mono">{item.tags?.length ? item.tags.join(', ') : ''}</div>
                                    </td>
                                    <td><span class="badge" style="font-size:0.62rem;background:{item._type === 'cron' ? 'var(--tone-lilac-bg)' : 'var(--surface-2)'};color:{item._type === 'cron' ? 'var(--tone-lilac-text)' : 'var(--text-secondary)'}">{item._type}</span></td>
                                    <td><span class="badge badge-{item._type === 'cron' ? 'on' : taskStatusBadge(item.status)}">{item.status}</span></td>
                                    <td class="mono" style="font-size:0.75rem">{item.assigned_agent || 'unassigned'}</td>
                                    <td class="mono" style="font-size:0.75rem">{formatDueLabel(item.due_date)}</td>
                                </tr>
                            {/each}
                        </tbody>
                    </table>
                {/if}
            </div>
        </div>
    </div>

    <!-- Sprint Burndown -->
    {#if activeSprints.length > 0}
    <div class="section" style="margin-bottom:1.5rem">
        <div class="section-header">
            <div class="section-title">Sprint Burndown</div>
            <a href="#/tasks" style="font-family:var(--font-grotesk);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View Sprints &rarr;</a>
        </div>
        <div class="burndown-grid">
            {#each activeSprints as { project, sprint, series }}
                {@const total = series.length > 0 ? series[0].total : 0}
                {@const done = series.length > 0 ? series[series.length - 1].cumulative : 0}
                {@const pct = total > 0 ? Math.round((done / total) * 100) : 0}
                {@const daysLeft = sprint.end_date ? Math.ceil((new Date(sprint.end_date + 'T00:00:00') - new Date()) / 86400000) : null}
                <div class="burndown-card">
                    <div class="burndown-header">
                        <div>
                            <div class="burndown-name">{sprint.name}</div>
                            <div class="burndown-project">{project.name}</div>
                        </div>
                        <div class="burndown-meta">
                            <span class="burndown-pct">{pct}%</span>
                            {#if daysLeft !== null}
                                <span class="burndown-days" class:burndown-overdue={daysLeft < 0}>{daysLeft < 0 ? `${Math.abs(daysLeft)}d over` : `${daysLeft}d left`}</span>
                            {/if}
                        </div>
                    </div>
                    {#if series.length > 1 && total > 0}
                        <svg width="100%" viewBox="0 0 300 72" preserveAspectRatio="none" class="burndown-svg">
                            <polyline points={burndownPoints(series, total, 300, 72, true)} fill="none" stroke="var(--text-muted)" stroke-width="1" stroke-dasharray="4,3" opacity="0.5" />
                            <polyline points={burndownPoints(series, total, 300, 72, false)} fill="none" stroke="var(--yellow)" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round" />
                        </svg>
                    {:else}
                        <div class="burndown-empty">Data appears as tasks complete</div>
                    {/if}
                    <div class="burndown-footer">{done}/{total} tasks &middot; {sprint.start_date || '?'} &ndash; {sprint.end_date || '?'}</div>
                </div>
            {/each}
        </div>
    </div>
    {/if}

    <!-- Presentations + Research -->
    <div class="grid-2">
        <!-- Recent Presentations -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Recent Presentations</div>
                <a href="#/presentations" style="font-family:var(--font-grotesk);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View All &rarr;</a>
            </div>
            <div class="section-body">
                {#if presentations.length === 0}
                    <div class="empty">No presentations yet — agents can create them via MCP</div>
                {:else}
                    <div class="pres-list">
                        {#each presentations as p}
                            <div class="pres-card">
                                <div class="pres-main">
                                    <div class="pres-title">{p.title}</div>
                                    <div class="pres-meta">
                                        <span class="badge badge-model" style="font-size:0.6rem">v{p.current_version}</span>
                                        <span class="mono" style="font-size:0.7rem;color:var(--text-muted)">{p.created_by}</span>
                                    </div>
                                </div>
                                {#if p.share_token}
                                    <button
                                        class="copy-btn"
                                        title="Copy share link"
                                        on:click={() => copyShareLink(p.share_token)}
                                    >
                                        <span class="material-symbols-outlined" style="font-size:16px">link</span>
                                    </button>
                                {/if}
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        </div>

        <!-- Research Pipeline -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Research Pipeline</div>
                <a href="#/research-ui" style="font-family:var(--font-grotesk);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View All &rarr;</a>
            </div>
            <div class="section-body">
                {#if researchTopics.length === 0}
                    <div class="empty">No research topics</div>
                {:else}
                    <!-- Status breakdown -->
                    {#if Object.keys(researchStatusCounts).length > 0}
                        <div class="research-status-bar">
                            {#each Object.entries(researchStatusCounts) as [status, count]}
                                <div class="research-status-pill">
                                    <span class="badge badge-{researchStatusBadge(status)}" style="font-size:0.6rem">{status}</span>
                                    <span class="mono" style="font-size:0.75rem;font-weight:700">{count}</span>
                                </div>
                            {/each}
                        </div>
                    {/if}
                    <!-- Recent topics -->
                    <div class="research-list">
                        {#each researchTopics as t}
                            <div class="research-item">
                                <div class="research-item-main">
                                    <div class="research-title">{t.title}</div>
                                    <div class="research-meta">
                                        <span class="badge badge-{researchStatusBadge(t.status)}" style="font-size:0.6rem">{t.status}</span>
                                        {#if t.assigned_agent}
                                            <span class="mono" style="font-size:0.7rem;color:var(--text-muted)">{t.assigned_agent}</span>
                                        {/if}
                                    </div>
                                </div>
                            </div>
                        {/each}
                    </div>
                {/if}
            </div>
        </div>
    </div>

    <!-- System footer -->
    <div class="sys-footer">
        <span class="mono">v{sysVersion}</span>
        <span class="sys-dot">&middot;</span>
        <span class="mono">{sysUptime} uptime</span>
        <span class="sys-dot">&middot;</span>
        <span class="mono" style="color:{sysSchedulerRunning ? 'var(--green)' : 'var(--red)'}">{sysScheduler}</span>
        <span class="sys-dot">&middot;</span>
        <span class="mono">{sysHeartbeats}</span>
    </div>
</div>

<style>
    /* Hero — black bg, massive gold title */
    .hero {
        background: var(--surface-inverse);
        color: var(--text-inverse);
        padding: 3rem;
        margin-bottom: 1.5rem;
        border-radius: var(--radius-lg);
        position: relative;
        overflow: hidden;
    }
    .hero-title {
        font-family: var(--font-grotesk);
        font-size: 4rem;
        font-weight: 900;
        margin-bottom: 0.3rem;
        letter-spacing: -0.03em;
        color: var(--yellow);
    }
    .hero-title .y { color: var(--yellow); }
    .hero-sub {
        font-family: var(--font-body);
        font-size: 0.9rem;
        color: var(--text-subtle);
        margin-bottom: 2rem;
    }
    .hero-stats { display: flex; gap: 3rem; }
    .hero-stat-value {
        font-family: var(--font-grotesk);
        font-size: 3rem;
        font-weight: 700;
        color: var(--yellow);
        line-height: 1;
    }
    .hero-stat-label {
        font-family: var(--font-grotesk);
        font-size: 0.65rem;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--text-subtle);
        margin-top: 0.3rem;
    }

    /* Bento nav grid */
    .nav-grid {
        display: grid;
        grid-template-columns: repeat(6, 1fr);
        gap: 1rem;
        margin-bottom: 1.5rem;
    }
    .nav-card {
        padding: 1.5rem;
        cursor: pointer;
        text-decoration: none;
        color: var(--text-primary);
        background: var(--surface-1);
        border-radius: var(--radius-lg);
        transition: all 0.15s;
    }
    .nav-card:hover {
        background: var(--surface-2);
        transform: translateY(-2px);
    }
    .nav-card-icon-wrap {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        width: 44px;
        height: 44px;
        background: #000;
        border-radius: 10px;
        margin-bottom: 0.8rem;
    }
    .nav-card-icon {
        font-size: 24px;
        color: var(--yellow);
    }
    .nav-card-title {
        font-family: var(--font-grotesk);
        font-size: 0.9rem;
        font-weight: 700;
        text-transform: uppercase;
        margin-bottom: 0.3rem;
    }
    .nav-card-desc { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 1.5rem; margin-bottom: 1.5rem; }

    /* Presentations panel */
    .pres-list { display: flex; flex-direction: column; gap: 0.5rem; padding: 0.5rem 0; }
    .pres-card {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 0.6rem 0.75rem;
        background: var(--surface-2);
        border-radius: var(--radius);
    }
    .pres-main { flex: 1; min-width: 0; }
    .pres-title { font-size: 0.82rem; font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .pres-meta { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.2rem; }
    .copy-btn {
        flex-shrink: 0;
        background: none;
        border: none;
        cursor: pointer;
        color: var(--text-muted);
        padding: 0.25rem;
        border-radius: var(--radius);
        display: flex;
        align-items: center;
        transition: color 0.1s;
    }
    .copy-btn:hover { color: var(--yellow); }

    /* Research panel */
    .research-status-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 0.5rem;
        padding: 0.5rem 0 0.75rem;
        border-bottom: 1px solid var(--border);
        margin-bottom: 0.5rem;
    }
    .research-status-pill { display: flex; align-items: center; gap: 0.3rem; }
    .research-list { display: flex; flex-direction: column; gap: 0.4rem; padding: 0.25rem 0; }
    .research-item { padding: 0.5rem 0.75rem; background: var(--surface-2); border-radius: var(--radius); }
    .research-item-main { display: flex; flex-direction: column; gap: 0.2rem; }
    .research-title { font-size: 0.8rem; font-weight: 600; line-height: 1.3; }
    .research-meta { display: flex; align-items: center; gap: 0.4rem; margin-top: 0.15rem; }

    /* System footer */
    .sys-footer {
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

    /* Table helpers */
    .clickable { cursor: pointer; }
    .clickable:hover td { background: var(--hover-accent) !important; }
    .expand-icon { font-size: 0.6rem; margin-right: 0.3rem; color: var(--text-muted); }
    .session-agent { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; }
    .session-work { font-size: 0.8rem; }
    .session-sub { margin-top: 0.2rem; font-size: 0.68rem; color: var(--text-muted); }
    .task-title { font-size: 0.82rem; font-weight: 600; }
    .usage-row td { padding: 0 !important; }
    .usage-panel {
        background: var(--surface-2);
        padding: 1rem 1.5rem;
        border-radius: 0 0 var(--radius-lg) var(--radius-lg);
    }
    .usage-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.8rem; }
    .usage-label {
        font-family: var(--font-grotesk);
        font-size: 0.6rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: var(--text-muted);
        margin-bottom: 0.15rem;
    }
    .usage-value { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; }

    /* Sprint Burndown */
    .burndown-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 1rem; padding: 1rem 1.5rem 1.5rem; }
    .burndown-card { background: var(--surface-2); border-radius: var(--radius-lg); padding: 1rem; }
    .burndown-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 0.75rem; }
    .burndown-name { font-family: var(--font-grotesk); font-size: 0.88rem; font-weight: 700; }
    .burndown-project { font-family: var(--font-grotesk); font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); margin-top: 0.15rem; }
    .burndown-meta { text-align: right; }
    .burndown-pct { font-family: var(--font-grotesk); font-size: 1.4rem; font-weight: 700; color: var(--yellow); display: block; line-height: 1; }
    .burndown-days { font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--text-muted); }
    .burndown-overdue { color: var(--red) !important; }
    .burndown-svg { display: block; border-radius: var(--radius); background: var(--surface-3); }
    .burndown-empty { font-size: 0.72rem; color: var(--text-muted); font-style: italic; padding: 0.4rem 0; }
    .burndown-footer { font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--text-muted); margin-top: 0.5rem; }

    @media (max-width: 1100px) {
        .nav-grid { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 900px) {
        .nav-grid { grid-template-columns: repeat(2, 1fr); }
        .grid-2 { grid-template-columns: 1fr; }
        .hero { padding: 1.5rem; }
        .hero-title { font-size: 2.5rem; }
        .hero-stats { flex-wrap: wrap; gap: 1.5rem; }
        .hero-stat-value { font-size: 2rem; }
    }
    @media (max-width: 480px) {
        .nav-grid { grid-template-columns: 1fr 1fr; }
        .nav-card { padding: 1rem; }
        .nav-card-icon { font-size: 24px; margin-bottom: 0.4rem; }
        .hero-stats { gap: 1rem; }
        .usage-grid { grid-template-columns: repeat(3, 1fr); }
    }
</style>
