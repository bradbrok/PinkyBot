<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    // Timezone
    let defaultTimezone = '';
    const commonTimezones = [
        'America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York',
        'America/Anchorage', 'Pacific/Honolulu', 'America/Phoenix',
        'America/Toronto', 'America/Vancouver', 'America/Sao_Paulo',
        'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Moscow',
        'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
        'Australia/Sydney', 'Pacific/Auckland', 'UTC',
    ];

    // Primary user
    let primaryChatId = '';
    let primaryDisplayName = '';

    // Cross-agent data
    let allTokens = [];
    let allApprovedUsers = [];

    // Platforms
    let platforms = [];
    let platformSelect = 'telegram';
    let platformToken = '';
    let settingsPlatform = '';
    let settingsJson = '';
    let settingsOpen = false;

    // Skills
    let skills = [];
    let skillName = '';
    let skillDesc = '';
    let skillType = 'custom';

    // Session skills
    let sessionList = [];
    let selectedSession = '';
    let sessionSkills = [];

    // Heartbeat/Wake settings
    let heartbeatSettings = [];
    let editingAgent = null;
    let editWakeInterval = 1800;
    let editClockAligned = true;
    let editAutoSleepHours = 8;

    // Account info & costs
    let accountInfo = {};
    let totalCostUsd = 0;
    let agentCosts = [];

    // Auth status
    let authStatus = {};

    function typeClass(type) {
        if (type === 'mcp_tool') return 'mcp';
        if (type === 'builtin') return 'builtin';
        return 'custom';
    }

    async function refreshPlatforms() {
        const data = await api('GET', '/outreach/platforms');
        platforms = data.platforms || [];
    }

    async function configurePlatform() {
        if (!platformToken) { toast('Enter a token', 'error'); return; }
        await api('PUT', `/outreach/platforms/${platformSelect}`, { token: platformToken, enabled: true });
        platformToken = '';
        toast(`${platformSelect} configured`);
        refreshPlatforms();
    }

    async function testPlatform() {
        const result = await api('POST', `/outreach/platforms/${platformSelect}/test`);
        if (result.success) toast(`${platformSelect} connected! ${result.bot_username ? '@' + result.bot_username : ''}`);
        else toast(`${platformSelect} failed: ${result.error}`, 'error');
    }

    async function togglePlatform(platform, enable) {
        await api('POST', `/outreach/platforms/${platform}/${enable ? 'enable' : 'disable'}`);
        toast(`${platform} ${enable ? 'enabled' : 'disabled'}`);
        refreshPlatforms();
    }

    async function deletePlatform(platform) {
        if (!confirm(`Delete ${platform}?`)) return;
        await api('DELETE', `/outreach/platforms/${platform}`);
        toast(`${platform} deleted`);
        refreshPlatforms();
    }

    async function editPlatformSettings(platform) {
        settingsPlatform = platform;
        const config = await api('GET', `/outreach/platforms/${platform}`);
        settingsJson = JSON.stringify(config.settings || {}, null, 2);
        settingsOpen = true;
    }

    async function savePlatformSettings() {
        try {
            const settings = JSON.parse(settingsJson);
            await api('PUT', `/outreach/platforms/${settingsPlatform}`, { settings });
            toast('Settings saved');
            settingsOpen = false;
            refreshPlatforms();
        } catch (e) { toast('Invalid JSON', 'error'); }
    }

    // Skills
    async function refreshSkills() {
        const data = await api('GET', '/skills');
        skills = data.skills || [];
    }

    async function registerSkill() {
        if (!skillName.trim()) { toast('Enter a skill name', 'error'); return; }
        await api('POST', '/skills', { name: skillName, description: skillDesc, skill_type: skillType });
        skillName = ''; skillDesc = '';
        toast(`Skill registered`);
        refreshSkills();
    }

    async function toggleSkill(name, enable) {
        await api('POST', `/skills/${name}/${enable ? 'enable' : 'disable'}`);
        toast(`${name} ${enable ? 'enabled' : 'disabled'}`);
        refreshSkills();
    }

    async function deleteSkill(name) {
        if (!confirm(`Delete "${name}"?`)) return;
        await api('DELETE', `/skills/${name}`);
        toast(`${name} deleted`);
        refreshSkills();
    }

    // Session Skills
    async function refreshSessions() {
        const sessions = await api('GET', '/sessions');
        sessionList = sessions;
    }

    async function loadSessionSkills() {
        if (!selectedSession) { toast('Select a session first', 'error'); return; }
        const data = await api('GET', `/sessions/${selectedSession}/skills`);
        sessionSkills = data.skills || [];
    }

    async function setSessionSkill(skillName, enabled) {
        await api('PUT', `/sessions/${selectedSession}/skills/${skillName}`, { enabled });
        toast(`${skillName} ${enabled ? 'enabled' : 'disabled'} for ${selectedSession}`);
        loadSessionSkills();
    }

    async function clearSessionSkill(skillName) {
        await api('DELETE', `/sessions/${selectedSession}/skills/${skillName}`);
        toast(`Override cleared`);
        loadSessionSkills();
    }

    async function loadTimezone() {
        const data = await api('GET', '/system/timezone');
        defaultTimezone = data.timezone || 'UTC';
    }
    async function saveTimezone() {
        await api('PUT', `/system/timezone?timezone=${encodeURIComponent(defaultTimezone)}`);
        toast(`Timezone set to ${defaultTimezone}`);
    }

    async function loadPrimaryUser() {
        const data = await api('GET', '/system/primary-user');
        primaryChatId = data.chat_id || '';
        primaryDisplayName = data.display_name || '';
    }
    async function savePrimaryUser() {
        if (!primaryChatId.trim()) { toast('Enter a chat ID', 'error'); return; }
        await api('PUT', `/system/primary-user?chat_id=${encodeURIComponent(primaryChatId.trim())}&display_name=${encodeURIComponent(primaryDisplayName.trim())}`);
        toast('Primary user set — auto-approved across all agents');
        loadPrimaryUser();
        loadAllApprovedUsers();
    }
    async function loadAllTokens() {
        const data = await api('GET', '/system/all-tokens');
        allTokens = data.tokens || [];
    }
    async function loadAllApprovedUsers() {
        const data = await api('GET', '/system/all-approved-users');
        allApprovedUsers = data.users || [];
    }

    async function loadHeartbeatSettings() {
        const data = await api('GET', '/settings/heartbeat');
        heartbeatSettings = data.agents || [];
    }

    function formatWakeInterval(seconds) {
        if (!seconds || seconds <= 0) return 'Disabled';
        if (seconds >= 3600) return (seconds / 3600) + 'h';
        return (seconds / 60) + 'm';
    }

    function openWakeEdit(agent) {
        editingAgent = agent.name;
        editWakeInterval = agent.wake_interval || 0;
        editClockAligned = agent.clock_aligned !== false;
        editAutoSleepHours = agent.auto_sleep_hours ?? 8;
    }

    async function saveWakeSettings() {
        const name = editingAgent;
        await api('PUT', `/agents/${editingAgent}`, {
            wake_interval: editWakeInterval,
            clock_aligned: editClockAligned,
            auto_sleep_hours: editAutoSleepHours,
        });
        editingAgent = null;
        toast(`Wake settings saved for ${name}`);
        loadHeartbeatSettings();
    }

    // Lifetime costs
    let lifetimeCostUsd = 0;
    let lifetimeAgents = [];

    async function loadAccountInfo() {
        const data = await api('GET', '/settings/account');
        accountInfo = data.account || {};
        totalCostUsd = data.run_cost_usd || 0;
        agentCosts = data.run_agents || [];
        lifetimeCostUsd = data.lifetime_cost_usd || 0;
        lifetimeAgents = data.lifetime_agents || [];
    }

    async function loadAuthStatus() {
        authStatus = await api('GET', '/system/auth');
    }

    onMount(() => {
        loadAuthStatus();
        loadTimezone();
        loadPrimaryUser();
        loadAllTokens();
        loadAllApprovedUsers();
        refreshPlatforms();
        refreshSkills();
        refreshSessions();
        loadHeartbeatSettings();
        loadAccountInfo();
    });
</script>

<div class="content" style="max-width:1200px">
    <!-- Default Timezone -->
    <div class="section">
        <div class="section-header"><div class="section-title">Default Timezone</div></div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">Used for all message timestamps unless a user has a per-user timezone set.</p>
            <div class="form-inline">
                <select class="form-select" style="max-width:300px" bind:value={defaultTimezone} on:change={saveTimezone}>
                    {#each commonTimezones as tz}
                        <option value={tz}>{tz}</option>
                    {/each}
                </select>
                <span style="font-family:var(--font-mono);font-size:0.8rem;color:var(--gray-mid)">{defaultTimezone}</span>
            </div>
        </div>
    </div>

    <!-- Setup Required Banner -->
    {#if authStatus.setup_required}
        <div class="section" style="border:2px solid var(--yellow)">
            <div class="section-header" style="background:rgba(234,179,8,0.1)">
                <div class="section-title" style="color:var(--yellow)">Setup Required</div>
            </div>
            <div style="padding:1.5rem;background:var(--gray-light)">
                {#if !authStatus.claude_installed}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>Claude Code CLI not found.</strong> Install it first:</p>
                    <pre style="background:var(--gray-dark);padding:0.8rem 1rem;border-radius:6px;font-size:0.85rem;margin:0 0 1rem 0;overflow-x:auto">npm install -g @anthropic-ai/claude-code</pre>
                    <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">After installing, run <code style="background:var(--gray-dark);padding:0.1rem 0.4rem;border-radius:3px">claude login</code> to authenticate.</p>
                {:else}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>Not logged in to Claude.</strong> Authenticate to start using agents:</p>
                    <div style="background:var(--gray-dark);padding:1rem;border-radius:8px;margin:0 0 1rem 0">
                        <p style="margin:0 0 0.5rem 0;font-size:0.85rem;color:var(--gray-mid)">Option 1 — Log in with your Anthropic account (Max/Pro plan):</p>
                        <pre style="background:#0a0a12;padding:0.5rem 1rem;border-radius:6px;font-size:0.85rem;margin:0 0 1rem 0">claude login</pre>
                        <p style="margin:0 0 0.5rem 0;font-size:0.85rem;color:var(--gray-mid)">Option 2 — Use an API key:</p>
                        <pre style="background:#0a0a12;padding:0.5rem 1rem;border-radius:6px;font-size:0.85rem;margin:0">export ANTHROPIC_API_KEY=sk-ant-...</pre>
                    </div>
                    <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">After authenticating, restart PinkyBot to pick up the credentials.</p>
                {/if}
            </div>
        </div>
    {/if}

    <!-- Account & Costs -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Account & Costs</div>
            <button class="btn btn-sm" on:click={loadAccountInfo}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <div style="display:flex;gap:2rem;flex-wrap:wrap;margin-bottom:1rem">
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Plan</span>
                    <div style="font-size:1.3rem;font-weight:700;margin-top:0.2rem">
                        {#if accountInfo.subscriptionType}
                            <span class="badge badge-on" style="font-size:0.9rem;padding:0.3rem 0.6rem">{accountInfo.subscriptionType}</span>
                        {:else}
                            <span class="badge badge-off" style="font-size:0.9rem;padding:0.3rem 0.6rem">Unknown</span>
                        {/if}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Provider</span>
                    <div style="font-size:1.1rem;font-weight:600;margin-top:0.2rem;font-family:var(--font-mono)">
                        {accountInfo.apiProvider || '--'}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Email</span>
                    <div style="font-size:0.95rem;margin-top:0.2rem;font-family:var(--font-mono)">
                        {accountInfo.email || '--'}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Lifetime Cost</span>
                    <div style="font-size:1.3rem;font-weight:700;margin-top:0.2rem;color:{lifetimeCostUsd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">
                        ${lifetimeCostUsd.toFixed(4)}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">This Run</span>
                    <div style="font-size:1.1rem;font-weight:600;margin-top:0.2rem;color:{totalCostUsd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">
                        ${totalCostUsd.toFixed(4)}
                    </div>
                </div>
            </div>
            {#if lifetimeAgents.length > 0}
                <table class="data-table" style="margin:0">
                    <thead><tr><th>Agent</th><th>Lifetime Cost</th><th>Turns</th><th>Input Tokens</th><th>Output Tokens</th></tr></thead>
                    <tbody>
                        {#each lifetimeAgents as ac}
                            <tr>
                                <td class="mono">{ac.agent_name}</td>
                                <td class="mono" style="color:{ac.total_cost_usd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">${ac.total_cost_usd.toFixed(4)}</td>
                                <td class="mono">{ac.total_turns?.toLocaleString() || 0}</td>
                                <td class="mono" style="font-size:0.8rem">{ac.total_input_tokens?.toLocaleString() || 0}</td>
                                <td class="mono" style="font-size:0.8rem">{ac.total_output_tokens?.toLocaleString() || 0}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {:else if agentCosts.length > 0}
                <table class="data-table" style="margin:0">
                    <thead><tr><th>Agent</th><th>Sessions</th><th>This Run</th></tr></thead>
                    <tbody>
                        {#each agentCosts as ac}
                            <tr>
                                <td class="mono">{ac.name}</td>
                                <td>{ac.sessions}</td>
                                <td class="mono" style="color:{ac.cost_usd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">${ac.cost_usd.toFixed(4)}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Heartbeat & Wake Settings -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Heartbeat & Wake Settings</div>
            <button class="btn btn-sm" on:click={loadHeartbeatSettings}>Refresh</button>
        </div>
        <div class="section-body">
            {#if heartbeatSettings.length === 0}
                <div class="empty">No agents found.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Agent</th><th>Wake Interval</th><th>Clock Aligned</th><th>Auto Sleep</th><th>Heartbeat</th><th>Schedules</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each heartbeatSettings as a}
                            <tr>
                                <td class="mono">{a.display_name}</td>
                                <td>
                                    <span class="badge badge-{a.wake_interval > 0 ? 'on' : 'off'}">
                                        {formatWakeInterval(a.wake_interval)}
                                    </span>
                                </td>
                                <td>
                                    <span class="badge badge-{a.clock_aligned ? 'on' : 'off'}">
                                        {a.clock_aligned ? 'Yes' : 'No'}
                                    </span>
                                </td>
                                <td>
                                    <span class="badge badge-{a.auto_sleep_hours > 0 ? 'on' : 'off'}">
                                        {a.auto_sleep_hours > 0 ? a.auto_sleep_hours + 'h' : 'Off'}
                                    </span>
                                </td>
                                <td>
                                    {#if a.latest_heartbeat}
                                        <span class="badge badge-{a.latest_heartbeat.status === 'alive' ? 'on' : a.latest_heartbeat.status === 'stale' ? 'model' : 'off'}">
                                            {a.latest_heartbeat.status}
                                        </span>
                                        <span style="font-size:0.7rem;color:var(--gray-mid);margin-left:0.3rem">{timeAgo(a.latest_heartbeat.timestamp)}</span>
                                    {:else}
                                        <span style="color:var(--gray-mid);font-size:0.8rem">--</span>
                                    {/if}
                                </td>
                                <td>
                                    <span style="font-family:var(--font-mono);font-size:0.8rem">{a.schedules?.length || 0}</span>
                                </td>
                                <td>
                                    <button class="btn btn-sm" on:click={() => openWakeEdit(a)}>Edit</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Wake Settings Editor -->
    {#if editingAgent}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Edit Wake Settings: {editingAgent}</div>
                <button class="btn btn-sm" on:click={() => editingAgent = null}>Cancel</button>
            </div>
            <div style="padding:1.5rem;background:var(--gray-light)">
                <div class="form-inline" style="margin-bottom:1rem">
                    <div class="form-row">
                        <span class="form-label">Wake Interval</span>
                        <select class="form-select" bind:value={editWakeInterval}>
                            <option value={0}>Disabled</option>
                            <option value={900}>15 min</option>
                            <option value={1800}>30 min</option>
                            <option value={3600}>1 hour</option>
                            <option value={7200}>2 hours</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span class="form-label">Clock Aligned</span>
                        <select class="form-select" bind:value={editClockAligned}>
                            <option value={true}>Yes (wake at :00, :30, etc.)</option>
                            <option value={false}>No (interval from last activity)</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span class="form-label">Auto Sleep (hours idle)</span>
                        <select class="form-select" bind:value={editAutoSleepHours}>
                            <option value={0}>Disabled</option>
                            <option value={2}>2 hours</option>
                            <option value={4}>4 hours</option>
                            <option value={6}>6 hours</option>
                            <option value={8}>8 hours</option>
                            <option value={12}>12 hours</option>
                            <option value={24}>24 hours</option>
                        </select>
                    </div>
                </div>
                <p style="margin:0 0 1rem 0;font-size:0.8rem;color:var(--gray-mid)">
                    Clock-aligned wakes fire at wall-clock boundaries (e.g., 1:00, 1:30, 2:00 for 30m).
                    Auto-sleep puts the agent to sleep after the specified idle period. Cron schedules still wake agents during sleep.
                </p>
                <button class="btn btn-primary" on:click={saveWakeSettings}>Save</button>
            </div>
        </div>
    {/if}

    <!-- Primary User -->
    <div class="section">
        <div class="section-header"><div class="section-title">Primary User</div></div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">The primary user is auto-approved across all agents and all outreach channels.</p>
            <div class="form-inline">
                {#if allApprovedUsers.length > 0}
                    {@const uniqueUsers = [...new Map(allApprovedUsers.map(u => [u.chat_id, u])).values()]}
                    <select class="form-select" style="max-width:350px" value={primaryChatId} on:change={(e) => { const u = uniqueUsers.find(u => u.chat_id === e.target.value); primaryChatId = e.target.value; primaryDisplayName = u?.display_name || ''; }}>
                        <option value="">Select user...</option>
                        {#each uniqueUsers as u}
                            <option value={u.chat_id}>{u.display_name || u.chat_id} ({u.chat_id})</option>
                        {/each}
                    </select>
                {:else}
                    <input type="text" class="form-input" bind:value={primaryChatId} placeholder="Chat ID (no approved users yet)" style="max-width:200px">
                    <input type="text" class="form-input" bind:value={primaryDisplayName} placeholder="Display name" style="max-width:200px">
                {/if}
                <button class="btn btn-primary" on:click={savePrimaryUser}>Set Primary User</button>
            </div>
            {#if primaryChatId}
                <div style="margin-top:0.5rem;font-family:var(--font-mono);font-size:0.8rem">
                    <span class="badge badge-on">Active</span>
                    <span style="margin-left:0.3rem">{primaryDisplayName || primaryChatId}</span>
                    <span style="color:var(--gray-mid);margin-left:0.3rem">({primaryChatId})</span>
                </div>
            {/if}
        </div>
    </div>

    <!-- All Approved Users (cross-agent) -->
    <div class="section">
        <div class="section-header"><div class="section-title">Approved Users (All Agents)</div></div>
        <div class="section-body">
            {#if allApprovedUsers.length === 0}
                <div class="empty">No approved users across any agent.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Agent</th><th>User</th><th>Chat ID</th><th>Status</th><th>Timezone</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each allApprovedUsers as u}
                            <tr>
                                <td class="mono">{u.agent_name}</td>
                                <td class="mono">{u.display_name || '--'}</td>
                                <td class="mono" style="font-size:0.75rem">{u.chat_id}</td>
                                <td><span class="badge badge-{u.status === 'approved' ? 'on' : u.status === 'denied' ? 'off' : 'model'}">{u.status}</span></td>
                                <td class="mono" style="font-size:0.75rem">{u.timezone || '--'}</td>
                                <td>
                                    <div style="display:flex;gap:0.3rem">
                                        {#if u.status === 'approved'}
                                            <button class="btn btn-sm" on:click={async () => { await api('PUT', `/agents/${u.agent_name}/approved-users/${u.chat_id}/deny`); toast(`Denied ${u.display_name || u.chat_id} for ${u.agent_name}`); loadAllApprovedUsers(); }}>Deny</button>
                                        {:else if u.status === 'denied'}
                                            <button class="btn btn-sm btn-success" on:click={async () => { await api('POST', `/agents/${u.agent_name}/approved-users`, { chat_id: u.chat_id, display_name: u.display_name }); toast(`Approved ${u.display_name || u.chat_id} for ${u.agent_name}`); loadAllApprovedUsers(); }}>Approve</button>
                                        {/if}
                                        <button class="btn btn-sm btn-danger" on:click={async () => { if (!confirm(`Revoke ${u.display_name || u.chat_id} from ${u.agent_name}?`)) return; await api('DELETE', `/agents/${u.agent_name}/approved-users/${u.chat_id}`); toast('User revoked'); loadAllApprovedUsers(); }}>Revoke</button>
                                    </div>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- All Bot Tokens (cross-agent) -->
    <div class="section">
        <div class="section-header"><div class="section-title">Bot Tokens (All Agents)</div></div>
        <div class="section-body">
            {#if allTokens.length === 0}
                <div class="empty">No bot tokens configured across any agent.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Agent</th><th>Platform</th><th>Token</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each allTokens as t}
                            <tr>
                                <td class="mono">{t.agent_name}</td>
                                <td><span class="badge badge-model">{t.platform}</span></td>
                                <td><span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? 'Set' : 'Missing'}</span></td>
                                <td><span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? 'Enabled' : 'Disabled'}</span></td>
                                <td>
                                    <button class="btn btn-sm btn-danger" on:click={async () => { if (!confirm(`Remove ${t.platform} token from ${t.agent_name}?`)) return; await api('DELETE', `/agents/${t.agent_name}/tokens/${t.platform}`); toast('Token removed'); loadAllTokens(); }}>Remove</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Outreach Platforms -->
    <div class="section">
        <div class="section-header"><div class="section-title">Outreach Platforms</div></div>
        <div style="padding:1.5rem;border-bottom:var(--border);background:var(--gray-light)">
            <div class="form-inline">
                <select class="form-select" bind:value={platformSelect}>
                    <option value="telegram">Telegram</option><option value="discord">Discord</option><option value="slack">Slack</option>
                </select>
                <input type="password" class="form-input" bind:value={platformToken} placeholder="Bot token..." style="max-width:400px">
                <div style="display:flex;gap:0.5rem">
                    <button class="btn btn-primary" on:click={configurePlatform}>Save</button>
                    <button class="btn btn-success" on:click={testPlatform}>Test</button>
                </div>
            </div>
        </div>
        <div class="section-body">
            {#if platforms.length === 0}
                <div class="empty">No platforms configured.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Platform</th><th>Status</th><th>Token</th><th>Settings</th><th>Updated</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each platforms as p}
                            <tr>
                                <td class="mono">{p.platform}</td>
                                <td><span class="badge badge-{p.enabled ? 'on' : 'off'}">{p.enabled ? 'Active' : 'Disabled'}</span></td>
                                <td><span class="badge badge-{p.token_set ? 'on' : 'off'}">{p.token_set ? 'Set' : 'Missing'}</span></td>
                                <td><button class="btn btn-sm" on:click={() => editPlatformSettings(p.platform)}>Settings</button></td>
                                <td class="mono" style="font-size:0.75rem">{timeAgo(p.updated_at)}</td>
                                <td>
                                    <div style="display:flex;gap:0.3rem">
                                        <button class="btn btn-sm {p.enabled ? 'btn-danger' : 'btn-success'}" on:click={() => togglePlatform(p.platform, !p.enabled)}>{p.enabled ? 'Disable' : 'Enable'}</button>
                                        <button class="btn btn-sm btn-danger" on:click={() => deletePlatform(p.platform)}>Delete</button>
                                    </div>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Platform Settings -->
    {#if settingsOpen}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Platform Settings: {settingsPlatform}</div>
                <button class="btn" on:click={() => settingsOpen = false}>Close</button>
            </div>
            <div class="section-body" style="padding:1.5rem">
                <div class="form-row">
                    <span class="form-label">Settings JSON</span>
                    <textarea class="form-input" bind:value={settingsJson} rows="6" style="font-size:0.8rem"></textarea>
                </div>
                <div style="text-align:right">
                    <button class="btn btn-primary" on:click={savePlatformSettings}>Save Settings</button>
                </div>
            </div>
        </div>
    {/if}

    <!-- Skills -->
    <div class="section">
        <div class="section-header"><div class="section-title">Skills / Plugins</div></div>
        <div style="padding:1.5rem;border-bottom:var(--border);background:var(--gray-light)">
            <div class="form-inline">
                <input type="text" class="form-input" bind:value={skillName} placeholder="Skill name" style="max-width:200px">
                <input type="text" class="form-input" bind:value={skillDesc} placeholder="Description" style="max-width:300px">
                <select class="form-select" bind:value={skillType}>
                    <option value="custom">Custom</option><option value="mcp_tool">MCP Tool</option><option value="builtin">Built-in</option>
                </select>
                <button class="btn btn-primary" on:click={registerSkill}>Register</button>
            </div>
        </div>
        <div class="section-body">
            {#if skills.length === 0}
                <div class="empty">No skills registered.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Name</th><th>Type</th><th>Version</th><th>Status</th><th>Description</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each skills as s}
                            <tr>
                                <td class="mono">{s.name}</td>
                                <td><span class="badge badge-{typeClass(s.skill_type)}">{s.skill_type}</span></td>
                                <td class="mono" style="font-size:0.75rem">{s.version}</td>
                                <td><span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? 'Enabled' : 'Disabled'}</span></td>
                                <td style="color:var(--gray-mid);font-size:0.85rem">{s.description || '--'}</td>
                                <td>
                                    <button class="btn btn-sm {s.enabled ? 'btn-danger' : 'btn-success'}" on:click={() => toggleSkill(s.name, !s.enabled)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                                    <button class="btn btn-sm btn-danger" on:click={() => deleteSkill(s.name)}>Delete</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Session Skill Overrides -->
    <div class="section">
        <div class="section-header"><div class="section-title">Session Skill Overrides</div></div>
        <div style="padding:1.5rem;border-bottom:var(--border);background:var(--gray-light)">
            <div style="display:flex;gap:0.8rem;align-items:center;flex-wrap:wrap">
                <select class="form-select" bind:value={selectedSession}>
                    <option value="">Select session...</option>
                    {#each sessionList as s}<option value={s.id}>{s.id} ({s.model || 'default'})</option>{/each}
                </select>
                <button class="btn" on:click={loadSessionSkills}>Load Skills</button>
            </div>
        </div>
        <div class="section-body">
            {#if sessionSkills.length === 0}
                <div class="empty">Select a session above to view skill overrides</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Skill</th><th>Global</th><th>Override</th><th>Effective</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each sessionSkills as s}
                            <tr>
                                <td class="mono">{s.name}</td>
                                <td><span class="badge badge-{s.global_enabled ? 'on' : 'off'}">{s.global_enabled ? 'On' : 'Off'}</span></td>
                                <td>
                                    {#if s.session_override !== null}
                                        <span class="badge badge-{s.session_override ? 'on' : 'off'}">{s.session_override ? 'On' : 'Off'}</span>
                                    {:else}
                                        <span style="color:var(--gray-mid);font-family:var(--font-mono);font-size:0.75rem">--</span>
                                    {/if}
                                </td>
                                <td><span class="badge badge-{s.effective_enabled ? 'on' : 'off'}">{s.effective_enabled ? 'On' : 'Off'}</span></td>
                                <td>
                                    <button class="btn btn-sm btn-success" on:click={() => setSessionSkill(s.name, true)}>On</button>
                                    <button class="btn btn-sm btn-danger" on:click={() => setSessionSkill(s.name, false)}>Off</button>
                                    <button class="btn btn-sm" on:click={() => clearSessionSkill(s.name)}>Reset</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>
</div>

<style>
    .form-inline { display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap; }
    .form-row { display: flex; flex-direction: column; gap: 0.3rem; }

    @media (max-width: 900px) {
        .form-inline { flex-direction: column; align-items: stretch; }
        .form-inline :global(.form-input), .form-inline :global(.form-select) { max-width: 100% !important; width: 100% !important; }
        .form-inline :global(.btn) { width: 100%; }
    }
</style>
