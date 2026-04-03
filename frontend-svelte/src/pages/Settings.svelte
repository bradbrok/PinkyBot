<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    async function rerunOnboarding() {
        await api('POST', '/system/onboarding-reset').catch(() => {});
        window.location.hash = '#/onboarding';
    }

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
    let skillCategory = 'general';
    let skillShared = false;
    let skillSelfAssignable = false;
    let skillToolPatterns = '';
    let skillDirective = '';
    let skillMcpConfig = '';
    let skillRequires = '';
    let skillFileTemplates = '';
    let skillDefaultConfig = '';
    let showAdvancedSkill = false;

    // Session skills
    let sessionList = [];
    let selectedSession = '';
    let sessionSkills = [];

    // Owner profile
    let ownerName = '';
    let ownerPronouns = '';
    let ownerTimezone = '';
    let ownerLanguages = '';
    let ownerCommStyle = '';
    let ownerRole = '';

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
    let uiAuthStatus = {};
    let uiPassword = '';
    let uiPasswordConfirm = '';

    // Software update
    let serverInfo = {};
    let updateInfo = null;
    let updateLoading = false;
    let updateApplying = false;

    async function loadServerInfo() {
        serverInfo = await api('GET', '/api');
    }
    async function checkForUpdates() {
        updateLoading = true;
        updateInfo = null;
        try {
            updateInfo = await api('POST', '/admin/update?dry_run=true');
        } finally {
            updateLoading = false;
        }
    }
    async function applyUpdate() {
        if (!confirm('Apply update and restart the daemon?')) return;
        updateApplying = true;
        try {
            const result = await api('POST', '/admin/update');
            if (result.restarting) {
                toast('Update applied — daemon is restarting');
                updateInfo = null;
                setTimeout(loadServerInfo, 5000);
            } else {
                toast('Already up to date');
            }
        } catch (e) {
            toast('Update failed', 'error');
        } finally {
            updateApplying = false;
        }
    }

    // API keys
    let apiKeys = {};
    let newKeyName = '';
    let newKeyValue = '';

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
        const payload = {
            name: skillName,
            description: skillDesc,
            skill_type: skillType,
            category: skillCategory,
            shared: skillShared,
            self_assignable: skillSelfAssignable,
        };
        // Parse optional JSON fields
        if (skillToolPatterns.trim()) {
            payload.tool_patterns = skillToolPatterns.split(',').map(s => s.trim()).filter(Boolean);
        }
        if (skillDirective.trim()) payload.directive = skillDirective;
        if (skillRequires.trim()) {
            payload.requires = skillRequires.split(',').map(s => s.trim()).filter(Boolean);
        }
        try {
            if (skillMcpConfig.trim()) payload.mcp_server_config = JSON.parse(skillMcpConfig);
        } catch { toast('Invalid MCP server config JSON', 'error'); return; }
        try {
            if (skillFileTemplates.trim()) payload.file_templates = JSON.parse(skillFileTemplates);
        } catch { toast('Invalid file templates JSON', 'error'); return; }
        try {
            if (skillDefaultConfig.trim()) payload.default_config = JSON.parse(skillDefaultConfig);
        } catch { toast('Invalid default config JSON', 'error'); return; }

        await api('POST', '/skills', payload);
        skillName = ''; skillDesc = ''; skillToolPatterns = ''; skillDirective = '';
        skillMcpConfig = ''; skillRequires = ''; skillFileTemplates = ''; skillDefaultConfig = '';
        skillShared = false; skillSelfAssignable = false; showAdvancedSkill = false;
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

    async function loadOwnerProfile() {
        try {
            const data = await api('GET', '/settings/owner-profile');
            ownerName = data.name || '';
            ownerPronouns = data.pronouns || '';
            ownerTimezone = data.timezone || '';
            ownerLanguages = data.languages || '';
            ownerCommStyle = data.comm_style || '';
            ownerRole = data.role || '';
        } catch { /* endpoint may not exist on older backends */ }
    }
    async function saveOwnerProfile() {
        await api('PUT', '/settings/owner-profile', {
            name: ownerName,
            pronouns: ownerPronouns,
            timezone: ownerTimezone,
            languages: ownerLanguages,
            comm_style: ownerCommStyle,
            role: ownerRole,
        });
        toast('Owner profile saved');
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

    async function loadUiAuthStatus() {
        uiAuthStatus = await api('GET', '/auth/status');
    }

    async function saveUiPassword() {
        if (!uiPassword) {
            toast('Enter a password', 'error');
            return;
        }
        if (uiPassword !== uiPasswordConfirm) {
            toast('Passwords do not match', 'error');
            return;
        }
        await api('PUT', '/auth/password', { password: uiPassword });
        uiPassword = '';
        uiPasswordConfirm = '';
        toast('UI password updated');
        loadUiAuthStatus();
    }

    async function loadApiKeys() {
        const data = await api('GET', '/system/api-keys');
        apiKeys = data.keys || {};
    }

    async function saveApiKey() {
        if (!newKeyName || !newKeyValue) { toast('Select a key and enter a value', 'error'); return; }
        await api('PUT', `/system/api-keys/${newKeyName}`, { value: newKeyValue });
        newKeyValue = '';
        toast(`${newKeyName} saved`);
        loadApiKeys();
    }

    async function deleteApiKey(name) {
        if (!confirm(`Remove ${name}?`)) return;
        await api('DELETE', `/system/api-keys/${name}`);
        toast(`${name} removed`);
        loadApiKeys();
    }

    // Active tab
    let activeTab = 'system';
    const tabs = [
        { id: 'system',  label: 'System'  },
        { id: 'access',  label: 'Access'  },
        { id: 'agents',  label: 'Agents'  },
        { id: 'account', label: 'Account' },
    ];

    onMount(() => {
        loadAuthStatus();
        loadUiAuthStatus();
        loadTimezone();
        loadPrimaryUser();
        loadAllTokens();
        loadAllApprovedUsers();
        loadHeartbeatSettings();
        loadOwnerProfile();
        loadAccountInfo();
        loadApiKeys();
        loadServerInfo();
    });
</script>

<div class="content">
    <!-- Tab Bar -->
    <div class="tab-bar">
        {#each tabs as tab}
            <button
                class="tab-btn {activeTab === tab.id ? 'active' : ''}"
                on:click={() => activeTab = tab.id}
            >{tab.label}</button>
        {/each}
    </div>
    {#if activeTab === 'access'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">UI Access</div>
            <button class="btn btn-sm" on:click={loadUiAuthStatus}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Password Source</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.password_source === 'unset' ? 'off' : 'on'}">{uiAuthStatus.password_source || 'loading'}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Session Secret</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.session_secret_configured ? 'on' : 'off'}">{uiAuthStatus.session_secret_configured ? 'Configured' : 'Missing'}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Setup</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.setup_required ? 'off' : 'on'}">{uiAuthStatus.setup_required ? 'Required' : 'Complete'}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Mode</div>
                    <div style="margin-top:0.35rem;color:var(--gray-mid);font-size:0.85rem">
                        {#if uiAuthStatus.env_override}
                            Controlled by <code>PINKY_UI_PASSWORD</code>
                        {:else}
                            Stored hash in <code>system_settings</code>
                        {/if}
                    </div>
                </div>
            </div>
            {#if !uiAuthStatus.session_secret_configured}
                <p style="margin:0 0 1rem 0;color:var(--accent)">Set <code>PINKY_SESSION_SECRET</code> and restart PinkyBot before the UI can issue login cookies.</p>
            {/if}
            {#if uiAuthStatus.env_override}
                <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">Password changes are disabled because <code>PINKY_UI_PASSWORD</code> is active.</p>
            {:else}
                <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">Change the stored password used by the browser login flow when no env override is active.</p>
                <div class="form-inline">
                    <input type="password" class="form-input" bind:value={uiPassword} placeholder="New UI password" style="max-width:280px">
                    <input type="password" class="form-input" bind:value={uiPasswordConfirm} placeholder="Confirm password" style="max-width:280px">
                    <button class="btn btn-primary" on:click={saveUiPassword}>Save Password</button>
                </div>
            {/if}
        </div>
    </div>

    {/if}

    <!-- Setup Wizard -->
    {#if activeTab === 'system'}
    <div class="section">
        <div class="section-header"><div class="section-title">Setup Wizard</div></div>
        <div style="padding:1.5rem;background:var(--gray-light);display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
            <p style="margin:0;font-size:0.85rem;color:var(--gray-mid);flex:1">Re-run the onboarding wizard to reconfigure API keys, profile, agents, and channels.</p>
            <button class="btn btn-primary" on:click={rerunOnboarding}>Run Setup Wizard</button>
        </div>
    </div>
    {/if}

    <!-- Software Update -->
    {#if activeTab === 'system'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">Software Update</div>
            <button class="btn btn-sm" on:click={loadServerInfo}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <div style="display:flex;gap:2rem;flex-wrap:wrap;margin-bottom:1.2rem">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Version</div>
                    <div style="font-size:1rem;font-weight:700;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.version || '--'}</div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Branch</div>
                    <div style="font-size:0.9rem;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.git_branch || '--'}</div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Claude Code</div>
                    <div style="font-size:0.9rem;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.claude_version || '--'}</div>
                </div>
            </div>
            <div class="form-inline" style="margin-bottom:1rem">
                <button class="btn btn-sm" on:click={checkForUpdates} disabled={updateLoading}>
                    {updateLoading ? 'Checking...' : 'Check for Updates'}
                </button>
                {#if updateInfo && !updateInfo.up_to_date}
                    <button class="btn btn-primary" on:click={applyUpdate} disabled={updateApplying}>
                        {updateApplying ? 'Updating...' : `Apply ${updateInfo.pending_commits} Update${updateInfo.pending_commits !== 1 ? 's' : ''} & Restart`}
                    </button>
                {/if}
            </div>
            {#if updateInfo}
                {#if updateInfo.up_to_date}
                    <div style="color:var(--gray-mid);font-size:0.85rem">Up to date on <strong>{updateInfo.branch}</strong>.</div>
                {:else}
                    <div style="margin-bottom:0.5rem;font-size:0.85rem">
                        <strong>{updateInfo.pending_commits}</strong> pending commit{updateInfo.pending_commits !== 1 ? 's' : ''} on <strong>{updateInfo.branch}</strong>:
                    </div>
                    <div style="background:var(--surface-inverse);color:var(--text-inverse);padding:0.6rem 0.8rem;border-radius:6px;font-family:var(--font-grotesk);font-size:0.78rem;max-height:180px;overflow-y:auto">
                        {#each updateInfo.commits as commit}
                            <div style="padding:0.15rem 0;border-bottom:1px solid rgba(255,255,255,0.05);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">{commit}</div>
                        {/each}
                    </div>
                {/if}
            {/if}
        </div>
    </div>

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
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;color:var(--gray-mid)">{defaultTimezone}</span>
            </div>
        </div>
    </div>

    <!-- Setup Required Banner -->
    {#if authStatus.setup_required}
        <div class="section" style="background:var(--accent-soft);border-radius:var(--radius-lg)">
            <div class="section-header" style="background:var(--banner-warn-bg)">
                <div class="section-title" style="color:var(--accent)">Setup Required</div>
            </div>
            <div style="padding:1.5rem;background:var(--gray-light)">
                {#if !authStatus.claude_installed}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>Claude Code CLI not found.</strong> Install it first:</p>
                    <pre style="background:var(--surface-inverse);color:var(--text-inverse);padding:0.8rem 1rem;border-radius:6px;font-size:0.85rem;margin:0 0 1rem 0;overflow-x:auto">npm install -g @anthropic-ai/claude-code</pre>
                    <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">After installing, run <code style="background:var(--surface-inverse);color:var(--text-inverse);padding:0.1rem 0.4rem;border-radius:3px">claude login</code> to authenticate.</p>
                {:else}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>Not logged in to Claude.</strong> Authenticate to start using agents:</p>
                    <div style="background:var(--surface-inverse);color:var(--text-inverse);padding:1rem;border-radius:8px;margin:0 0 1rem 0">
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

    {/if}

    <!-- Account & Costs -->
    {#if activeTab === 'account'}
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
                    <div style="font-size:1.1rem;font-weight:600;margin-top:0.2rem;font-family:var(--font-grotesk)">
                        {accountInfo.apiProvider || '--'}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Email</span>
                    <div style="font-size:0.95rem;margin-top:0.2rem;font-family:var(--font-grotesk)">
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

    <!-- API Keys -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">API Keys</div>
            <button class="btn btn-sm" on:click={loadApiKeys}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">Configure API keys for voice notes (ElevenLabs, OpenAI, Deepgram) and GIFs (Giphy). Keys are stored in the system settings database.</p>
            <div class="form-inline">
                <select class="form-select" style="max-width:220px" bind:value={newKeyName}>
                    <option value="">Select key...</option>
                    <option value="ELEVENLABS_API_KEY">ElevenLabs</option>
                    <option value="OPENAI_API_KEY">OpenAI</option>
                    <option value="DEEPGRAM_API_KEY">Deepgram</option>
                    <option value="GIPHY_API_KEY">Giphy</option>
                </select>
                <input type="password" class="form-input" bind:value={newKeyValue} placeholder="API key..." style="max-width:350px">
                <button class="btn btn-primary" on:click={saveApiKey}>Save</button>
            </div>
        </div>
        <div class="section-body">
            {#if Object.keys(apiKeys).length === 0}
                <div class="empty">Loading...</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Service</th><th>Status</th><th>Source</th><th>Preview</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each Object.entries(apiKeys) as [name, info]}
                            <tr>
                                <td class="mono">{name.replace('_API_KEY', '')}</td>
                                <td><span class="badge badge-{info.configured ? 'on' : 'off'}">{info.configured ? 'Configured' : 'Not set'}</span></td>
                                <td style="font-size:0.8rem;color:var(--gray-mid)">{info.source}</td>
                                <td class="mono" style="font-size:0.75rem">{info.preview || '--'}</td>
                                <td>
                                    {#if info.configured && info.source === 'settings'}
                                        <button class="btn btn-sm btn-danger" on:click={() => deleteApiKey(name)}>Remove</button>
                                    {/if}
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    {/if}

    <!-- Skill Catalog -->
    {#if activeTab === 'agents'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">Skill Catalog</div>
            <button class="btn btn-sm" on:click={refreshSkills}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <div class="form-inline" style="margin-bottom:0.8rem">
                <input type="text" class="form-input" bind:value={skillName} placeholder="Skill name" style="max-width:200px">
                <input type="text" class="form-input" bind:value={skillDesc} placeholder="Description" style="max-width:300px">
                <select class="form-select" bind:value={skillType} style="max-width:120px">
                    <option value="custom">custom</option>
                    <option value="mcp_tool">mcp_tool</option>
                    <option value="builtin">builtin</option>
                </select>
                <select class="form-select" bind:value={skillCategory} style="max-width:140px">
                    <option value="general">general</option>
                    <option value="core">core</option>
                    <option value="development">development</option>
                    <option value="productivity">productivity</option>
                    <option value="comms">comms</option>
                    <option value="research">research</option>
                </select>
                <button class="btn btn-primary" on:click={registerSkill}>Register</button>
            </div>
            <div class="form-inline" style="margin-bottom:0.5rem">
                <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.3rem">
                    <input type="checkbox" bind:checked={skillShared}> Shared (auto-apply to all agents)
                </label>
                <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.3rem">
                    <input type="checkbox" bind:checked={skillSelfAssignable}> Self-assignable (agents can add)
                </label>
                <button class="btn btn-sm" on:click={() => showAdvancedSkill = !showAdvancedSkill}>
                    {showAdvancedSkill ? 'Hide' : 'Show'} Advanced
                </button>
            </div>
            {#if showAdvancedSkill}
                <div style="display:flex;flex-direction:column;gap:0.6rem;margin-top:0.8rem;padding:0.8rem;border-radius:var(--radius-lg);background:var(--surface-2);background:var(--bg)">
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">Tool Patterns (comma-separated)</label>
                        <input type="text" class="form-input" bind:value={skillToolPatterns} placeholder="mcp__my-server__*, Read, Bash" style="width:100%">
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">Directive (system prompt injection)</label>
                        <textarea class="form-input" bind:value={skillDirective} placeholder="Instructions injected into agent system prompt..." rows="3" style="width:100%;resize:vertical"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">MCP Server Config (JSON)</label>
                        <textarea class="form-input" bind:value={skillMcpConfig} placeholder={'{"command": "python", "args": ["-m", "my_server"], "cwd": "/path"}'} rows="3" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">Dependencies (comma-separated skill names)</label>
                        <input type="text" class="form-input" bind:value={skillRequires} placeholder="pinky-memory, file-access" style="width:100%">
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">File Templates (JSON: path -> content)</label>
                        <textarea class="form-input" bind:value={skillFileTemplates} placeholder={'{"config/my-skill.yaml": "key: value"}'} rows="2" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">Default Config (JSON)</label>
                        <textarea class="form-input" bind:value={skillDefaultConfig} placeholder={'{"api_key": "", "max_results": 10}'} rows="2" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                </div>
            {/if}
        </div>
        <div class="section-body">
            {#if skills.length === 0}
                <div class="empty">No skills registered. Register one above or they'll be seeded on startup.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Name</th><th>Type</th><th>Category</th><th>Shared</th><th>Self-Assign</th><th>Tools</th><th>Status</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each skills as s}
                            <tr style={!s.enabled ? 'opacity:0.5' : ''}>
                                <td class="mono" style="font-weight:600">{s.name}</td>
                                <td><span class="badge badge-model">{s.skill_type}</span></td>
                                <td><span class="badge" style="background:var(--gray-mid);color:#fff">{s.category}</span></td>
                                <td><span class="badge badge-{s.shared ? 'on' : 'off'}">{s.shared ? 'Yes' : 'No'}</span></td>
                                <td><span class="badge badge-{s.self_assignable ? 'on' : 'off'}">{s.self_assignable ? 'Yes' : 'No'}</span></td>
                                <td style="font-size:0.75rem;font-family:var(--font-grotesk);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{(s.tool_patterns || []).join(', ') || '--'}</td>
                                <td><span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? 'Enabled' : 'Off'}</span></td>
                                <td>
                                    <div style="display:flex;gap:0.3rem">
                                        <button class="btn btn-sm" on:click={() => toggleSkill(s.name, !s.enabled)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                                        {#if s.category !== 'core'}
                                            <button class="btn btn-sm btn-danger" on:click={() => deleteSkill(s.name)}>Delete</button>
                                        {/if}
                                    </div>
                                </td>
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
                                    <span style="font-family:var(--font-grotesk);font-size:0.8rem">{a.schedules?.length || 0}</span>
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

    {/if}

    <!-- Primary User -->
    {#if activeTab === 'access'}
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
                <div style="margin-top:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem">
                    <span class="badge badge-on">Active</span>
                    <span style="margin-left:0.3rem">{primaryDisplayName || primaryChatId}</span>
                    <span style="color:var(--gray-mid);margin-left:0.3rem">({primaryChatId})</span>
                </div>
            {/if}
        </div>
    </div>

    {/if}

    <!-- Owner Profile -->
    {#if activeTab === 'account'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">Owner Profile</div>
            <button class="btn btn-sm" on:click={loadOwnerProfile}>Refresh</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">Your identity as the owner. Agents use this to personalize interactions.</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;margin-bottom:0.8rem">
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Name</label>
                    <input type="text" class="form-input" bind:value={ownerName} placeholder="Your name" style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Pronouns</label>
                    <input type="text" class="form-input" bind:value={ownerPronouns} placeholder="e.g. he/him" style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Timezone</label>
                    <input type="text" class="form-input" bind:value={ownerTimezone} placeholder="e.g. America/Los_Angeles" style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Role</label>
                    <input type="text" class="form-input" bind:value={ownerRole} placeholder="e.g. developer, designer" style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Languages</label>
                    <input type="text" class="form-input" bind:value={ownerLanguages} placeholder="e.g. English, Spanish" style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">Communication Style</label>
                    <input type="text" class="form-input" bind:value={ownerCommStyle} placeholder="e.g. direct, casual" style="width:100%">
                </div>
            </div>
            <button class="btn btn-primary" on:click={saveOwnerProfile}>Save Profile</button>
        </div>
    </div>

    {/if}

    <!-- All Approved Users (cross-agent) -->
    {#if activeTab === 'access'}
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
    {/if}

</div>

<style>
    .tab-bar {
        display: flex;
        gap: 0;
        border-bottom: 1px solid var(--border-color, #2a2a3a);
        margin-bottom: 1.5rem;
    }
    .tab-btn {
        padding: 0.6rem 1.25rem;
        font-size: 0.85rem;
        font-weight: 600;
        font-family: var(--font-grotesk);
        background: none;
        border: none;
        border-bottom: 2px solid transparent;
        color: var(--gray-mid);
        cursor: pointer;
        letter-spacing: 0.02em;
        transition: color 0.15s, border-color 0.15s;
        margin-bottom: -1px;
    }
    .tab-btn:hover { color: var(--text-primary); }
    .tab-btn.active {
        color: var(--accent);
        border-bottom-color: var(--accent);
    }
    .form-inline { display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap; }
    .form-row { display: flex; flex-direction: column; gap: 0.3rem; }

    @media (max-width: 900px) {
        .form-inline { flex-direction: column; align-items: stretch; }
        .form-inline :global(.form-input), .form-inline :global(.form-select) { max-width: 100% !important; width: 100% !important; }
        .form-inline :global(.btn) { width: 100%; }
    }
</style>
