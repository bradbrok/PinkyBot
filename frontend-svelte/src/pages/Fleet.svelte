<script>
    import { onMount, onDestroy } from 'svelte';
    import Modal from '../components/Modal.svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo, contextClass } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let statAgents = '--'; let statAgentsSub = '';
    let statSessions = '--'; let statSessionsSub = '';
    let statMessages = '--'; let statGroups = '--';
    let statConversations = '--'; let statScheduler = '--';
    let statSchedulerRunning = false; let statSchedulerSub = '';

    let agentBlocks = [];
    let orphanSessions = [];
    let groups = [];
    let conversations = [];
    let activityEvents = [];
    let activeTasks = [];
    let researchTopics = [];
    let researchStats = {};
    let feedFilter = 'all';
    $: unifiedFeed = (() => {
        const tasks = activeTasks.map(t => ({ ...t, _type: 'task' }));
        const research = researchTopics.map(r => ({ ...r, _type: 'research' }));
        const combined = [...tasks, ...research].sort((a, b) => {
            const ta = new Date(a.updated_at || a.created_at).getTime();
            const tb = new Date(b.updated_at || b.created_at).getTime();
            return tb - ta;
        });
        if (feedFilter === 'all') return combined;
        if (feedFilter === 'active') return combined.filter(i =>
            i.status === 'pending' || i.status === 'in_progress' || i.status === 'blocked' || i.status === 'assigned'
        );
        if (feedFilter === 'completed') return combined.filter(i =>
            i.status === 'completed' || i.status === 'published'
        );
        return combined;
    })();
    let commsMessages = [];
    let inboxSummaries = [];
    let openAgents = new Set();
    let refreshInterval;
    let activityInterval;
    let workingStatusInterval;
    async function refreshWorkingStatus() {
        try {
            const data = await api('GET', '/agents');
            const statusMap = {};
            for (const a of data.agents || []) statusMap[a.name] = a.working_status || 'idle';
            agentBlocks = agentBlocks.map(b => ({
                ...b,
                agent: { ...b.agent, working_status: statusMap[b.agent.name] ?? b.agent.working_status },
            }));
        } catch {}
    }

    // Modals
    let groupModalOpen = false; let groupName = ''; let groupMembers = '';
    let searchQuery = ''; let searchResults = []; let searchOpen = false;

    function toggleAgent(name) {
        if (openAgents.has(name)) openAgents.delete(name);
        else openAgents.add(name);
        openAgents = openAgents; // trigger reactivity
    }

    function expandAll() { agentBlocks.forEach(a => openAgents.add(a.agent.name)); openAgents = openAgents; }
    function collapseAll() { openAgents.clear(); openAgents = openAgents; }

    function normalizeStreamingSession(agentName, ss) {
        const stats = ss.stats || {};
        const state = ss.connected
            ? ((stats.pending_responses || 0) > 0 ? 'busy' : 'connected')
            : 'sleeping';
        return {
            id: `${agentName}-${ss.label}`,
            agent_name: agentName,
            label: ss.label,
            model: 'default',
            session_type: ss.label === 'main' ? 'main' : 'streaming',
            state,
            context_used_pct: 0,
            message_count: (stats.messages_sent || 0) + (stats.turns || 0),
            source: 'streaming',
        };
    }

    async function refreshAll() {
        try {
            const [root, agentsData, sessions, groupsData, convos, heartbeats, schedulerStatus, taskStats] = await Promise.all([
                api('GET', '/api'), api('GET', '/agents'), api('GET', '/sessions'),
                api('GET', '/groups'), api('GET', '/conversations'),
                api('GET', '/heartbeats'), api('GET', '/scheduler/status'),
                api('GET', '/tasks/stats').catch(() => ({ by_agent: {} })),
            ]);
            const taskCountByAgent = taskStats.by_agent || {};

            const hbMap = {};
            (heartbeats.heartbeats || []).forEach(h => { hbMap[h.agent_name] = h; });

            // Build hierarchy from legacy sessions
            const agentNames = new Set(agentsData.agents.map(a => a.name));
            const agentSessionsMap = {};
            const orphans = [];
            const seenIds = new Set();
            for (const s of sessions) {
                seenIds.add(s.id);
                const owner = s.agent_name || '';
                if (owner && agentNames.has(owner)) {
                    if (!agentSessionsMap[owner]) agentSessionsMap[owner] = [];
                    agentSessionsMap[owner].push(s);
                } else {
                    let matched = false;
                    for (const aName of agentNames) {
                        if (s.id.startsWith(aName + '-') || s.id === aName) {
                            if (!agentSessionsMap[aName]) agentSessionsMap[aName] = [];
                            agentSessionsMap[aName].push(s);
                            matched = true; break;
                        }
                    }
                    if (!matched) orphans.push(s);
                }
            }

            // Fetch streaming sessions per agent and merge
            const streamingResults = await Promise.all(
                agentsData.agents.map(a =>
                    api('GET', `/agents/${a.name}/streaming-sessions`).catch(() => ({ sessions: [] }))
                )
            );
            let totalStreamingSessions = 0;
            agentsData.agents.forEach((agent, i) => {
                const streamingSessions = streamingResults[i].sessions || [];
                for (const ss of streamingSessions) {
                    const normalized = normalizeStreamingSession(agent.name, ss);
                    if (seenIds.has(normalized.id)) continue;
                    seenIds.add(normalized.id);
                    if (!agentSessionsMap[agent.name]) agentSessionsMap[agent.name] = [];
                    agentSessionsMap[agent.name].push(normalized);
                    totalStreamingSessions++;
                }
            });

            const allSessions = sessions.length + totalStreamingSessions;
            statAgents = agentsData.count;
            const enabledAgents = agentsData.agents.filter(a => a.enabled).length;
            statAgentsSub = `${enabledAgents} active`;
            statSessions = allSessions;
            const running = sessions.filter(s => s.state === 'running').length + totalStreamingSessions;
            statSessionsSub = running ? `${running} running` : 'all idle';
            statMessages = sessions.reduce((a, s) => a + s.message_count, 0);
            statGroups = groupsData.groups.length;
            statConversations = convos.count;
            statSchedulerRunning = schedulerStatus.running;
            statScheduler = schedulerStatus.running ? '●' : '○';
            statSchedulerSub = `${schedulerStatus.enabled_schedules} schedule${schedulerStatus.enabled_schedules !== 1 ? 's' : ''}`;

            const tokenPromises = agentsData.agents.map(a => api('GET', `/agents/${a.name}/tokens`));
            const allTokens = await Promise.all(tokenPromises);

            agentBlocks = agentsData.agents.map((agent, i) => ({
                agent,
                sessions: agentSessionsMap[agent.name] || [],
                tokens: allTokens[i].tokens || [],
                hb: hbMap[agent.name],
                activeTasks: taskCountByAgent[agent.name] || 0,
            }));
            orphanSessions = orphans;
            groups = groupsData.groups || [];
            conversations = convos.conversations || [];
        } catch (e) { console.error('Fleet refresh error:', e); }
    }

    async function refreshActivity() {
        try {
            const [feed, taskData, resData, topicsData] = await Promise.all([
                api('GET', '/activity?limit=30'),
                api('GET', '/tasks?include_completed=true&limit=30'),
                api('GET', '/research/stats').catch(() => ({})),
                api('GET', '/research?limit=30').catch(() => ({})),
            ]);
            activityEvents = feed.events || [];
            activeTasks = taskData.tasks || [];
            researchStats = resData || {};
            researchTopics = topicsData.topics || [];
        } catch (e) { console.error('Activity error:', e); }
    }

    async function refreshComms() {
        try {
            const [msgData, inboxData] = await Promise.all([
                api('GET', '/comms/messages?limit=50'),
                api('GET', '/comms/inboxes'),
            ]);
            commsMessages = msgData.messages || [];
            inboxSummaries = inboxData.inboxes || [];
        } catch (e) { console.error('Comms error:', e); }
    }

    // Actions
    async function restartSession(id) { await api('POST', `/sessions/${id}/restart`); refreshAll(); }
    function openChat(id) { window.location.hash = `/chat#${id}`; }

    function openGroupModal() { groupName = ''; groupMembers = ''; groupModalOpen = true; }
    async function submitGroup() {
        const members = groupMembers.split(',').map(m => m.trim()).filter(Boolean);
        if (!groupName.trim()) { toast('Group name required', 'error'); return; }
        if (!members.length) { toast('Add at least one member', 'error'); return; }
        groupModalOpen = false;
        await api('POST', '/groups', { name: groupName, members });
        toast(`Group "${groupName}" created`);
        refreshAll();
    }

    async function searchConversations() {
        if (!searchQuery.trim()) return;
        const results = await api('GET', `/conversations/search?q=${encodeURIComponent(searchQuery)}`);
        searchResults = results.results || [];
        searchOpen = true;
    }

    onMount(() => {
        refreshAll(); refreshComms(); refreshActivity();
        refreshInterval = setInterval(() => { refreshAll(); refreshComms(); }, 10000);
        activityInterval = setInterval(refreshActivity, 3000);
        workingStatusInterval = setInterval(refreshWorkingStatus, 5000);
    });
    onDestroy(() => { clearInterval(refreshInterval); clearInterval(activityInterval); clearInterval(workingStatusInterval); });
</script>

<div class="content">
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-label">Agents</div><div class="stat-value">{statAgents}</div><div class="stat-sub">{statAgentsSub}</div></div>
        <div class="stat-card"><div class="stat-label">Sessions</div><div class="stat-value">{statSessions}</div><div class="stat-sub">{statSessionsSub}</div></div>
        <div class="stat-card"><div class="stat-label">Messages</div><div class="stat-value">{statMessages}</div></div>
        <div class="stat-card"><div class="stat-label">Groups</div><div class="stat-value">{statGroups}</div></div>
        <div class="stat-card"><div class="stat-label">Conversations</div><div class="stat-value">{statConversations}</div></div>
        <div class="stat-card"><div class="stat-label">Scheduler</div><div class="stat-value" style="color:{statSchedulerRunning ? 'var(--green)' : 'var(--red)'}">{statScheduler}</div><div class="stat-sub">{statSchedulerSub}</div></div>
    </div>

    <!-- Fleet -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Fleet</div>
            <div class="inline-spread">
                <a href="#/agents" class="btn btn-primary">+ New Agent</a>
                <button class="btn" on:click={expandAll}>Expand All</button>
                <button class="btn" on:click={collapseAll}>Collapse</button>
            </div>
        </div>
        <div class="section-body">
            {#if agentBlocks.length === 0}
                <div class="empty">No agents registered. <a href="#/agents">Create one</a>.</div>
            {:else}
                {#each agentBlocks as block}
                    {@const hbStatus = block.hb ? block.hb.status : 'unknown'}
                    {@const isOpen = openAgents.has(block.agent.name)}
                    <div class="agent-block">
                        <div class="agent-header" on:click={() => toggleAgent(block.agent.name)}>
                            <span class="agent-toggle">{isOpen ? '▼' : '▶'}</span>
                            <span class="heartbeat-dot {hbStatus}"></span>
                            <span class="working-dot" class:working={block.agent.working_status === 'working'} title={block.agent.working_status === 'working' ? 'Working' : 'Idle'}></span>
                            <span class="agent-name-label">{block.agent.display_name || block.agent.name}</span>
                            <div class="agent-badges">
                                {#if block.agent.role}<span class="badge badge-role">{block.agent.role}</span>{/if}
                                <span class="badge badge-model">{block.agent.model}</span>
                                <span class="badge badge-{block.agent.enabled ? 'on' : 'off'}">{block.agent.enabled ? 'Active' : 'Off'}</span>
                                {#if block.agent.auto_start}<span class="badge badge-autostart">Auto-Start</span>{/if}
                                {#each block.tokens.filter(t => t.token_set && t.enabled) as t}<span class="badge badge-platform">{t.platform}</span>{/each}
                                {#each block.agent.groups as g}<span class="badge badge-group">{g}</span>{/each}
                            </div>
                            {#if block.activeTasks > 0}
                                <span class="agent-task-count" title="Active tasks assigned">{block.activeTasks} task{block.activeTasks !== 1 ? 's' : ''}</span>
                            {/if}
                            <span class="agent-session-count">{block.sessions.length} session{block.sessions.length !== 1 ? 's' : ''}</span>
                            <div class="agent-actions-header" on:click|stopPropagation>
                                <a href="#/agents" class="btn btn-sm">Config</a>
                            </div>
                        </div>
                        {#if isOpen}
                            <div class="agent-body open">
                                {#if block.activeTasks > 0}
                                    <div class="agent-tasks-row">
                                        <span class="agent-tasks-label">Active Tasks</span>
                                        <span class="agent-tasks-pill">{block.activeTasks}</span>
                                        <a href="#/tasks" class="btn btn-sm" style="margin-left:auto">View Board &rarr;</a>
                                    </div>
                                {/if}
                                {#each block.tokens as t}
                                    <div class="outreach-row">
                                        <span class="outreach-icon">[{t.platform.substring(0,2).toUpperCase()}]</span>
                                        <span class="badge badge-platform">{t.platform}</span>
                                        <span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? 'Connected' : 'No Token'}</span>
                                        <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? 'Active' : 'Disabled'}</span>
                                    </div>
                                {/each}
                                {#if block.sessions.length === 0}
                                    <div class="empty" style="padding:1rem">No active sessions.</div>
                                {:else}
                                    {#each block.sessions as s}
                                        {@const sType = s.session_type || 'chat'}
                                        <div class="session-row">
                                            <span class="session-id">{s.id}</span>
                                            <div class="session-meta">
                                                <span class="badge badge-{sType}">{sType}</span>
                                                <span class="badge badge-{s.state}">{s.state}</span>
                                                <span class="badge badge-model" style="font-size:0.6rem">{s.model || 'default'}</span>
                                                <div style="display:flex;align-items:center;gap:0.3rem">
                                                    <div class="context-bar" style="width:60px">
                                                        <div class="context-fill {contextClass(s.context_used_pct)}" style="width:{Math.min(s.context_used_pct, 100)}%"></div>
                                                    </div>
                                                    <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--gray-mid)">{s.context_used_pct}%</span>
                                                </div>
                                                <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--gray-mid)">{s.message_count} msgs</span>
                                            </div>
                                            <div class="session-actions">
                                                <button class="btn btn-sm" on:click={() => openChat(s.id)}>Chat</button>
                                                <button class="btn btn-sm" on:click={() => restartSession(s.id)}>Restart</button>
                                            </div>
                                        </div>
                                    {/each}
                                {/if}
                            </div>
                        {/if}
                    </div>
                {/each}
            {/if}
        </div>
    </div>

    <!-- Activity Feed -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Live Activity</div>
            <button class="btn btn-sm" on:click={refreshActivity}>Refresh</button>
        </div>
        <div class="section-body">
            <!-- Filter bar -->
            <div style="display:flex;align-items:center;gap:0.5rem;padding:0.5rem 0.8rem;border-bottom:1px solid var(--surface-2)">
                <span style="font-size:0.7rem;color:var(--gray-mid)">{unifiedFeed.length} items</span>
                <div style="display:flex;gap:0.2rem;margin-left:auto">
                    {#each ['all', 'active', 'completed'] as f}
                        <button class="btn btn-sm" style="padding:0.1rem 0.5rem;font-size:0.65rem;{feedFilter === f ? 'background:var(--surface-inverse);color:var(--accent)' : ''}" on:click={() => feedFilter = f}>{f}</button>
                    {/each}
                </div>
            </div>
            <!-- Unified feed -->
            {#if unifiedFeed.length === 0}
                <div class="empty">No activity yet.</div>
            {:else}
                {#each unifiedFeed as item}
                    {@const isTask = item._type === 'task'}
                    {@const statusBg = item.status === 'completed' || item.status === 'published' ? 'var(--tone-success-bg)' : item.status === 'in_progress' || item.status === 'assigned' ? 'var(--tone-info-bg)' : item.status === 'blocked' ? 'var(--tone-error-bg)' : 'var(--surface-2)'}
                    {@const statusFg = item.status === 'completed' || item.status === 'published' ? 'var(--tone-success-text)' : item.status === 'in_progress' || item.status === 'assigned' ? 'var(--tone-info-text)' : item.status === 'blocked' ? 'var(--tone-error-text)' : 'var(--text-secondary)'}
                    <div class="activity-item">
                        <span class="feed-type-tag {item._type}">{item._type}</span>
                        <span class="badge" style="font-size:0.62rem;background:{statusBg};color:{statusFg}">{item.status}</span>
                        <span class="activity-agent">{item.assigned_agent || '--'}</span>
                        <span class="activity-detail" style="flex:1">{item.title}</span>
                        <span class="activity-time">{timeAgo(item.updated_at || item.created_at)}</span>
                    </div>
                {/each}
            {/if}
        </div>
    </div>

    <!-- Communications -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Agent Communications</div>
            <button class="btn" on:click={refreshComms}>Refresh</button>
        </div>
        <div class="section-body">
            {#if inboxSummaries.length > 0}
                <div style="display:flex;flex-wrap:wrap;gap:0.5rem;padding:0.5rem 0.8rem;border-bottom:1px solid var(--surface-2)">
                    {#each inboxSummaries as inbox}
                        <span class="badge" style="background:var(--tone-warning-bg);color:var(--tone-warning-text)">{inbox.agent}: {inbox.unread} unread</span>
                    {/each}
                </div>
            {/if}
            {#if commsMessages.length === 0}
                <div class="empty">No agent messages yet</div>
            {:else}
                {#each commsMessages as m}
                    <div class="msg-item">
                        <span class="msg-from">{m.from}</span><span class="msg-arrow">&rarr;</span><span class="msg-to">{m.to}</span>
                        <span class="msg-type" class:broadcast={m.type === 'broadcast'} class:group={m.type === 'group'}>{m.type}</span>
                        <span class="msg-status" style="font-size:0.7rem;color:{m.read ? 'var(--gray-mid)' : 'var(--tone-warning-text)'}">{m.read ? 'read' : 'unread'}</span>
                        <span class="msg-content">{m.content.substring(0, 100)}{m.content.length > 100 ? '...' : ''}</span>
                        <span class="msg-time">{timeAgo(m.timestamp)}</span>
                    </div>
                {/each}
            {/if}
        </div>
    </div>

    <!-- Groups -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Groups</div>
            <button class="btn btn-primary" on:click={openGroupModal}>+ New Group</button>
        </div>
        <div class="section-body" style="padding:1rem">
            {#if groups.length === 0}
                <div class="empty">No groups created</div>
            {:else}
                {#each groups as g}
                    <div class="group-card"><span>{g.name}</span><span class="group-count">{g.members}</span></div>
                {/each}
            {/if}
        </div>
    </div>

    <!-- Conversations -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Conversation Store</div>
            <div style="display:flex;gap:0.5rem;align-items:center">
                <input type="text" bind:value={searchQuery} placeholder="Search..." style="font-family:var(--font-body);font-size:0.8rem;padding:0.3rem 0.6rem;border:none;background:var(--surface-2);border-radius:var(--radius-lg);width:200px" on:keydown={e => { if (e.key === 'Enter') searchConversations(); }}>
                <button class="btn" on:click={searchConversations}>Search</button>
            </div>
        </div>
        <div class="section-body">
            {#if conversations.length === 0}
                <div class="empty">No conversations stored</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Session</th><th>Messages</th><th>Platform</th><th>Last Active</th></tr></thead>
                    <tbody>
                        {#each conversations as c}
                            <tr>
                                <td style="font-family:var(--font-body);font-size:0.8rem">{c.session_id}</td>
                                <td>{c.message_count}</td>
                                <td>{c.platform || '--'}</td>
                                <td style="font-family:var(--font-body);font-size:0.75rem">{timeAgo(c.last_message_at)}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Search Results -->
    {#if searchOpen}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Search: "{searchQuery}" ({searchResults.length})</div>
                <button class="btn" on:click={() => searchOpen = false}>Close</button>
            </div>
            <div class="section-body">
                {#if searchResults.length === 0}
                    <div class="empty">No results</div>
                {:else}
                    {#each searchResults as r}
                        <div class="msg-item">
                            <span class="msg-from">{r.role}</span>
                            <span style="font-family:var(--font-body);font-size:0.75rem;color:var(--gray-mid);min-width:120px">{r.session_id}</span>
                            <span class="msg-content">{r.content.substring(0, 200)}{r.content.length > 200 ? '...' : ''}</span>
                            <span class="msg-time">{timeAgo(r.timestamp)}</span>
                        </div>
                    {/each}
                {/if}
            </div>
        </div>
    {/if}
</div>

<Modal bind:show={groupModalOpen} title="New Group" width="500px">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">Group Name</label><input type="text" class="form-input w-full" bind:value={groupName} placeholder="e.g. core-team"></div>
        <div class="form-row"><label class="form-label">Member Session IDs</label><input type="text" class="form-input w-full" bind:value={groupMembers} placeholder="Comma-separated"></div>
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => groupModalOpen = false}>Cancel</button>
        <button class="btn btn-primary" on:click={submitGroup}>Create Group</button>
    </div>
</Modal>

<style>
    .agent-block { background: var(--surface-1); border-radius: var(--radius-lg); margin-bottom: 0.5rem; }
    .agent-block:last-child { margin-bottom: 0; }
    .agent-header { display: flex; align-items: center; gap: 1rem; padding: 1rem 1.5rem; background: var(--surface-2); border-radius: var(--radius-lg); cursor: pointer; }
    .agent-header:hover { background: var(--hover-soft); }
    .agent-toggle { font-family: var(--font-grotesk); font-size: 0.8rem; color: var(--text-muted); width: 1.5rem; }
    .agent-name-label { font-family: var(--font-grotesk); font-size: 1rem; font-weight: 700; }
    .agent-badges { display: flex; gap: 0.4rem; flex-wrap: wrap; }
    .agent-task-count { font-family: var(--font-grotesk); font-size: 0.68rem; font-weight: 700; background: var(--tone-warning-bg); color: var(--tone-warning-text); padding: 0.15rem 0.5rem; border-radius: var(--radius-lg); }
    .agent-session-count { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-left: auto; }
    .agent-tasks-row { display: flex; align-items: center; gap: 0.8rem; padding: 0.5rem 1.5rem 0.5rem 3.5rem; background: var(--tone-warning-bg); }
    .agent-tasks-label { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; color: var(--tone-warning-text); text-transform: uppercase; letter-spacing: 0.05em; }
    .agent-tasks-pill { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; color: var(--tone-warning-text); }
    .agent-actions-header { display: flex; gap: 0.3rem; }

    .session-row { display: flex; align-items: center; gap: 1rem; padding: 0.7rem 1.5rem 0.7rem 3.5rem; background: var(--surface-1); }
    .session-row:last-child { border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
    .session-row:hover { background: var(--hover-accent); }
    .session-id { font-family: var(--font-body); font-size: 0.8rem; font-weight: 700; min-width: 180px; }
    .session-meta { display: flex; gap: 0.5rem; align-items: center; flex: 1; flex-wrap: wrap; }
    .session-actions { display: flex; gap: 0.3rem; }

    .outreach-row { display: flex; align-items: center; gap: 0.8rem; padding: 0.5rem 1.5rem 0.5rem 3.5rem; background: var(--surface-2); }
    .outreach-icon { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); }

    .msg-item { padding: 0.8rem 1rem; background: var(--surface-1); display: flex; gap: 1rem; align-items: flex-start; }
    .msg-item:nth-child(even) { background: var(--surface-2); }
    .msg-item:hover { background: var(--hover-accent); }
    .msg-from { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; min-width: 120px; }
    .msg-arrow { color: var(--text-muted); font-family: var(--font-body); font-size: 0.8rem; }
    .msg-to { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); min-width: 120px; }
    .msg-content { flex: 1; font-size: 0.88rem; }
    .msg-time { font-family: var(--font-body); font-size: 0.65rem; color: var(--text-muted); }
    .msg-type { font-family: var(--font-grotesk); font-size: 0.6rem; padding: 0.1rem 0.3rem; background: var(--tone-neutral-bg); color: var(--tone-neutral-text); text-transform: uppercase; border-radius: var(--radius-lg); }
    .msg-type.broadcast { background: var(--tone-info-bg); color: var(--tone-info-text); }
    .msg-type.group { background: var(--tone-lilac-bg); color: var(--tone-lilac-text); }

    .group-card { display: inline-flex; align-items: center; gap: 0.5rem; padding: 0.5rem 1rem; background: var(--surface-2); border: none; border-radius: var(--radius-lg); margin: 0.5rem; font-family: var(--font-grotesk); font-size: 0.8rem; }
    .group-count { background: var(--yellow); padding: 0.1rem 0.4rem; font-weight: 700; font-size: 0.7rem; border-radius: var(--radius-lg); }

    .activity-item { display: flex; align-items: center; gap: 0.6rem; padding: 0.4rem 1rem; background: var(--surface-1); font-size: 0.8rem; }
    .activity-item:nth-child(even) { background: var(--surface-2); }
    .activity-item:hover { background: var(--hover-accent); }
    .activity-time { font-family: var(--font-body); font-size: 0.6rem; color: var(--text-muted); min-width: 55px; }
    .activity-event { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.1rem 0.3rem; text-transform: uppercase; min-width: 80px; }
    .activity-agent { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; min-width: 70px; }
    .activity-detail { flex: 1; font-family: var(--font-body); font-size: 0.7rem; color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .feed-type-tag { font-family: var(--font-grotesk); font-size: 0.55rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; padding: 0.15rem 0.4rem; border-radius: var(--radius-lg); min-width: 58px; text-align: center; }
    .feed-type-tag.task { background: var(--tone-neutral-bg); color: var(--tone-neutral-text); }
    .feed-type-tag.research { background: var(--tone-lilac-bg); color: var(--tone-lilac-text); }

    .working-dot { width: 6px; height: 6px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; transition: background 0.3s; }
    .working-dot.working { background: var(--green); box-shadow: 0 0 6px rgba(74,222,128,0.6); animation: working-pulse 1.5s ease-in-out infinite; }
    @keyframes working-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }

    @media (max-width: 900px) {
        .stats-bar { grid-template-columns: repeat(2, 1fr); }
        .agent-header { flex-wrap: wrap; }
        .session-row { flex-wrap: wrap; padding-left: 1.5rem; }
        .session-id { min-width: 100%; }
    }
</style>
