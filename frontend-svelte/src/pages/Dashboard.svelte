<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { contextClass } from '../lib/utils.js';

    let heroSessions = '--';
    let heroSkills = '--';
    let heroPlatforms = '--';
    let heroConversations = '--';

    let sessions = [];
    let platforms = [];
    let skills = [];
    let expandedSession = null;

    let sysVersion = '--';
    let sysUptime = '--';
    let sysApi = '--';
    let sysScheduler = '--';
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

    async function refresh() {
        try {
            const [root, sessData, skillsData, platData, convos, schedulerStatus, heartbeats] = await Promise.all([
                api('GET', '/api'),
                api('GET', '/sessions'),
                api('GET', '/skills'),
                api('GET', '/outreach/platforms'),
                api('GET', '/conversations'),
                api('GET', '/scheduler/status'),
                api('GET', '/heartbeats'),
            ]);

            heroSessions = sessData.length;
            heroSkills = skillsData.count;
            heroPlatforms = platData.count;
            heroConversations = convos.count;

            sessions = sessData.slice(0, 5);
            platforms = platData.platforms || [];
            skills = skillsData.skills || [];

            sysVersion = root.version;
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
        refreshInterval = setInterval(refresh, 10000);
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
        <div class="hero-sub">Personal AI Companion Framework -- Powered by Claude Code</div>
        <div class="hero-stats">
            <div>
                <div class="hero-stat-value">{heroSessions}</div>
                <div class="hero-stat-label">Sessions</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroSkills}</div>
                <div class="hero-stat-label">Skills</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroPlatforms}</div>
                <div class="hero-stat-label">Platforms</div>
            </div>
            <div>
                <div class="hero-stat-value">{heroConversations}</div>
                <div class="hero-stat-label">Conversations</div>
            </div>
        </div>
    </div>

    <!-- Quick Nav -->
    <div class="nav-grid">
        <a href="#/chat" class="nav-card">
            <div class="nav-card-icon">&gt;_</div>
            <div class="nav-card-title">Chat</div>
            <div class="nav-card-desc">Interactive sessions with Claude Code</div>
        </a>
        <a href="#/fleet" class="nav-card">
            <div class="nav-card-icon">&lt;/&gt;</div>
            <div class="nav-card-title">Fleet</div>
            <div class="nav-card-desc">Manage sessions, groups, agent comms</div>
        </a>
        <a href="#/settings" class="nav-card">
            <div class="nav-card-icon">{"{ }"}</div>
            <div class="nav-card-title">Settings</div>
            <div class="nav-card-desc">Skills, platforms, configuration</div>
        </a>
        <a href="/docs" class="nav-card">
            <div class="nav-card-icon">#</div>
            <div class="nav-card-title">API Docs</div>
            <div class="nav-card-desc">Auto-generated OpenAPI reference</div>
        </a>
    </div>

    <!-- Sessions + Platforms -->
    <div class="grid-2">
        <!-- Active Sessions -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Active Sessions</div>
                <a href="#/fleet" style="font-family:var(--font-mono);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">View All &rarr;</a>
            </div>
            <div class="section-body">
                {#if sessions.length === 0}
                    <div class="empty">No active sessions</div>
                {:else}
                    <table class="data-table">
                        <thead>
                            <tr><th>Session</th><th>Model</th><th>State</th><th>Context</th><th>Cost</th></tr>
                        </thead>
                        <tbody>
                            {#each sessions as s}
                                {@const sType = s.session_type || 'chat'}
                                {@const typeColor = sType === 'main' ? 'background:#FFE600;color:#1E293B' : sType === 'worker' ? 'background:#e2e8f0;color:#334155' : 'background:#dbeafe;color:#1e40af'}
                                {@const u = s.usage || {}}
                                <tr class="clickable" on:click={() => expandedSession = expandedSession === s.id ? null : s.id}>
                                    <td class="mono">
                                        <span class="expand-icon">{expandedSession === s.id ? '▼' : '▶'}</span>
                                        {s.id}
                                    </td>
                                    <td>
                                        <span class="badge" style={typeColor}>{sType}</span>
                                        <span class="mono" style="font-size:0.75rem">{s.model || 'default'}</span>
                                    </td>
                                    <td><span class="badge badge-{s.state}">{s.state}</span></td>
                                    <td>
                                        <div style="display:flex;align-items:center;gap:0.5rem">
                                            <div class="context-bar" style="width:60px">
                                                <div class="context-fill {contextClass(s.context_used_pct)}" style="width:{Math.min(s.context_used_pct, 100)}%"></div>
                                            </div>
                                            <span class="mono" style="font-size:0.75rem">{s.context_used_pct}%</span>
                                        </div>
                                    </td>
                                    <td class="mono" style="font-size:0.75rem">{u.total_cost_usd ? '$' + u.total_cost_usd.toFixed(4) : '--'}</td>
                                </tr>
                                {#if expandedSession === s.id}
                                    <tr class="usage-row">
                                        <td colspan="5">
                                            <div class="usage-panel">
                                                <div class="usage-grid">
                                                    <div>
                                                        <div class="usage-label">Queries</div>
                                                        <div class="usage-value">{u.total_queries || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Turns</div>
                                                        <div class="usage-value">{u.total_turns || 0}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Total Cost</div>
                                                        <div class="usage-value">{u.total_cost_usd ? '$' + u.total_cost_usd.toFixed(4) : '$0'}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Duration</div>
                                                        <div class="usage-value">{u.total_duration_ms ? (u.total_duration_ms / 1000).toFixed(1) + 's' : '--'}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">API Duration</div>
                                                        <div class="usage-value">{u.total_api_duration_ms ? (u.total_api_duration_ms / 1000).toFixed(1) + 's' : '--'}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Stop Reason</div>
                                                        <div class="usage-value">{u.last_stop_reason || '--'}</div>
                                                    </div>
                                                </div>
                                                <div class="usage-grid" style="margin-top:0.8rem">
                                                    <div>
                                                        <div class="usage-label">Input Tokens</div>
                                                        <div class="usage-value">{(u.input_tokens || 0).toLocaleString()}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Output Tokens</div>
                                                        <div class="usage-value">{(u.output_tokens || 0).toLocaleString()}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Cache Read</div>
                                                        <div class="usage-value">{(u.cache_read_tokens || 0).toLocaleString()}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Cache Write</div>
                                                        <div class="usage-value">{(u.cache_write_tokens || 0).toLocaleString()}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Total Tokens</div>
                                                        <div class="usage-value">{((u.input_tokens || 0) + (u.output_tokens || 0)).toLocaleString()}</div>
                                                    </div>
                                                    <div>
                                                        <div class="usage-label">Messages</div>
                                                        <div class="usage-value">{s.message_count}</div>
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

        <!-- Outreach Platforms -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Outreach Platforms</div>
                <a href="#/settings" style="font-family:var(--font-mono);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">Configure &rarr;</a>
            </div>
            <div class="section-body">
                {#if platforms.length === 0}
                    <div class="empty">No platforms configured</div>
                {:else}
                    <table class="data-table">
                        <thead>
                            <tr><th>Platform</th><th>Status</th><th>Token</th></tr>
                        </thead>
                        <tbody>
                            {#each platforms as p}
                                <tr>
                                    <td class="mono">{p.platform}</td>
                                    <td><span class="badge badge-{p.enabled ? 'on' : 'off'}">{p.enabled ? 'Active' : 'Disabled'}</span></td>
                                    <td><span class="badge badge-{p.token_set ? 'on' : 'off'}">{p.token_set ? 'Set' : 'Missing'}</span></td>
                                </tr>
                            {/each}
                        </tbody>
                    </table>
                {/if}
            </div>
        </div>
    </div>

    <!-- Skills -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Registered Skills</div>
            <a href="#/settings" style="font-family:var(--font-mono);font-size:0.7rem;text-transform:uppercase;color:var(--gray-mid);text-decoration:none">Manage &rarr;</a>
        </div>
        <div class="section-body">
            {#if skills.length === 0}
                <div class="empty">No skills registered</div>
            {:else}
                <table class="data-table">
                    <thead>
                        <tr><th>Name</th><th>Type</th><th>Version</th><th>Status</th><th>Description</th></tr>
                    </thead>
                    <tbody>
                        {#each skills as s}
                            <tr>
                                <td class="mono">{s.name}</td>
                                <td class="mono" style="font-size:0.75rem">{s.skill_type}</td>
                                <td class="mono" style="font-size:0.75rem">{s.version}</td>
                                <td><span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? 'Enabled' : 'Disabled'}</span></td>
                                <td style="color:var(--gray-mid);font-size:0.85rem">{s.description || '--'}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- System Info -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">System</div>
        </div>
        <div class="section-body" style="padding: 1.5rem;">
            <div class="sys-grid">
                <div>
                    <div class="sys-label">Version</div>
                    <div class="mono">{sysVersion}</div>
                </div>
                <div>
                    <div class="sys-label">Uptime</div>
                    <div class="mono">{sysUptime}</div>
                </div>
                <div>
                    <div class="sys-label">API</div>
                    <div class="mono">{sysApi}</div>
                </div>
            </div>
            <div class="sys-grid" style="margin-top:1rem">
                <div>
                    <div class="sys-label">Scheduler</div>
                    <div class="mono" style="color:{sysSchedulerRunning ? '#22c55e' : '#ef4444'}">{sysScheduler}</div>
                </div>
                <div>
                    <div class="sys-label">Schedules</div>
                    <div class="mono">{sysSchedules}</div>
                </div>
                <div>
                    <div class="sys-label">Heartbeats</div>
                    <div class="mono">{sysHeartbeats}</div>
                </div>
            </div>
        </div>
    </div>
</div>

<style>
    .hero {
        background: var(--black);
        color: var(--white);
        padding: 3rem;
        margin-bottom: 2rem;
        border: var(--border);
    }
    .hero-title { font-family: var(--font-mono); font-size: 2.5rem; font-weight: 700; margin-bottom: 0.5rem; }
    .hero-title .y { color: var(--yellow); }
    .hero-sub { font-family: var(--font-mono); font-size: 0.85rem; color: var(--gray-mid); margin-bottom: 2rem; }
    .hero-stats { display: flex; gap: 3rem; }
    .hero-stat-value { font-family: var(--font-mono); font-size: 2.5rem; font-weight: 700; color: var(--yellow); }
    .hero-stat-label { font-family: var(--font-mono); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--gray-mid); }

    .nav-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; margin-bottom: 2rem; }
    .nav-card { padding: 2rem 1.5rem; border: var(--border); margin: -1.5px; cursor: pointer; text-decoration: none; color: var(--black); background: var(--white); transition: background 0.15s; }
    .nav-card:hover { background: var(--yellow); }
    .nav-card-icon { font-family: var(--font-mono); font-size: 1.8rem; margin-bottom: 0.8rem; }
    .nav-card-title { font-family: var(--font-mono); font-size: 0.9rem; font-weight: 700; text-transform: uppercase; margin-bottom: 0.3rem; }
    .nav-card-desc { font-size: 0.8rem; color: var(--gray-mid); }
    .nav-card:hover .nav-card-desc { color: var(--gray-dark); }

    .grid-2 { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; margin-bottom: 2rem; }

    .sys-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; }
    .sys-label { font-family: var(--font-mono); font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--gray-mid); margin-bottom: 0.3rem; }

    .clickable { cursor: pointer; }
    .clickable:hover { background: var(--gray-light); }
    .expand-icon { font-size: 0.6rem; margin-right: 0.3rem; color: var(--gray-mid); }
    .usage-row td { padding: 0 !important; border-bottom: 2px solid var(--black); }
    .usage-panel { background: var(--gray-light); padding: 1rem 1.5rem; }
    .usage-grid { display: grid; grid-template-columns: repeat(6, 1fr); gap: 0.8rem; }
    .usage-label { font-family: var(--font-mono); font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.08em; color: var(--gray-mid); margin-bottom: 0.15rem; }
    .usage-value { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; }

    @media (max-width: 900px) {
        .nav-grid { grid-template-columns: repeat(2, 1fr); }
        .grid-2 { grid-template-columns: 1fr; }
        .hero { padding: 1.5rem; }
        .hero-title { font-size: 1.8rem; }
        .hero-stats { flex-wrap: wrap; gap: 1.5rem; }
        .hero-stat-value { font-size: 1.8rem; }
    }
    @media (max-width: 480px) {
        .nav-grid { grid-template-columns: 1fr 1fr; }
        .nav-card { padding: 1.2rem 1rem; }
        .nav-card-icon { font-size: 1.3rem; margin-bottom: 0.4rem; }
        .hero-stats { gap: 1rem; }
        .usage-grid { grid-template-columns: repeat(3, 1fr); }
    }
</style>
