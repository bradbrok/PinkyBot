<script>
    import { onMount } from 'svelte';
    import { _ } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';
    import { SUPPORTED_LOCALES } from '../lib/i18n.js';

    async function rerunOnboarding() {
        await api('POST', '/system/onboarding-reset').catch((e) => { toast('Failed to reset onboarding', 'error'); });
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
    let ownerLocale = '';

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
    let channelSwitching = false;

    async function switchChannel(newChannel) {
        if (channelSwitching) return;
        if (!confirm(`Switch to ${newChannel} channel? This will change which branch updates pull from.`)) return;
        channelSwitching = true;
        try {
            await api('POST', `/admin/channel?channel=${newChannel}`);
            serverInfo = { ...serverInfo, channel: newChannel };
            toast(`Switched to ${newChannel} channel`);
            // Auto-check for updates on the new channel
            await checkForUpdates();
        } catch (e) {
            toast('Failed to switch channel', 'error');
        } finally {
            channelSwitching = false;
        }
    }

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
        if (!confirm($_('settings.confirm_apply_update'))) return;
        updateApplying = true;
        try {
            const result = await api('POST', '/admin/update');
            if (result.restarting) {
                toast($_('settings.toast_update_applied'));
                updateInfo = null;
                setTimeout(loadServerInfo, 5000);
            } else {
                toast($_('settings.toast_up_to_date'));
            }
        } catch (e) {
            toast($_('settings.toast_update_failed'), 'error');
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
        toast($_('settings.toast_skill_registered'));
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
        toast($_('settings.toast_primary_user_set'));
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
            ownerLocale = data.locale || '';
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
            locale: ownerLocale,
        });
        if (ownerLocale) {
            const { setLocale } = await import('../lib/i18n.js');
            await setLocale(ownerLocale);
        }
        toast($_('settings.toast_owner_profile_saved'));
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
        toast($_('settings.toast_ui_password_updated'));
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

    // Calendar
    let calendarTab = 'caldav';
    let calendarStatus = null;
    let calendarUrl = '';
    let calendarUsername = '';
    let calendarPassword = '';
    let calendarSaving = false;
    let calendarTesting = false;
    let calendarTestResult = null;
    let calendarAgents = [];

    // Google Calendar OAuth
    let googleStatus = null;
    let googleClientId = '';
    let googleClientSecret = '';
    let googleConnecting = false;

    async function loadGoogleStatus() {
        try {
            googleStatus = await api('GET', '/calendar/google/status');
        } catch { googleStatus = { configured: false, connected: false }; }
    }

    async function saveGoogleCredentials() {
        if (!googleClientId || !googleClientSecret) { toast('Client ID and secret required', 'error'); return; }
        await api('PUT', '/calendar/google/credentials', { client_id: googleClientId, client_secret: googleClientSecret });
        toast('Google credentials saved');
        googleClientSecret = '';
        await loadGoogleStatus();
    }

    async function startGoogleOAuth() {
        googleConnecting = true;
        try {
            const { auth_url, session } = await api('GET', '/calendar/google/auth-url');
            const popup = window.open(auth_url, 'pinkybot-google-oauth', 'width=600,height=700');

            // Listen for postMessage from proxy callback
            const handler = async (event) => {
                if (event.data?.type !== 'pinkybot-oauth') return;
                window.removeEventListener('message', handler);
                // Fetch tokens from proxy via local API
                try {
                    await api('GET', `/calendar/google/fetch-token?session=${event.data.session}`);
                    toast('Google Calendar connected!');
                    await loadGoogleStatus();
                } catch (e) {
                    toast('Failed to retrieve tokens', 'error');
                } finally {
                    googleConnecting = false;
                    if (popup && !popup.closed) popup.close();
                }
            };
            window.addEventListener('message', handler);

            // Timeout after 5 min
            setTimeout(() => {
                window.removeEventListener('message', handler);
                googleConnecting = false;
            }, 300000);
        } catch (e) {
            toast('Failed to start OAuth', 'error');
            googleConnecting = false;
        }
    }

    async function disconnectGoogle() {
        if (!confirm('Disconnect Google Calendar?')) return;
        await api('DELETE', '/calendar/google/disconnect');
        toast('Google Calendar disconnected');
        await loadGoogleStatus();
    }

    async function loadCalendarStatus() {
        try {
            calendarStatus = await api('GET', '/calendar/status');
        } catch { calendarStatus = { configured: false }; }
        await loadGoogleStatus();
    }

    async function loadCalendarAgentStatuses() {
        if (!heartbeatSettings.length) return;
        calendarAgents = await Promise.all(heartbeatSettings.map(async (a) => {
            try {
                const s = await api('GET', `/agents/${a.name}/calendar/status`);
                return { name: a.name, display_name: a.display_name, enabled: s.enabled };
            } catch {
                return { name: a.name, display_name: a.display_name, enabled: false };
            }
        }));
    }

    async function saveCalendarConfig() {
        if (!calendarUrl || !calendarUsername) { toast('URL and username required', 'error'); return; }
        calendarSaving = true;
        try {
            await api('PUT', '/calendar/config', {
                caldav_url: calendarUrl,
                caldav_username: calendarUsername,
                caldav_password: calendarPassword,
            });
            toast('Calendar config saved');
            calendarPassword = '';
            await loadCalendarStatus();
        } finally { calendarSaving = false; }
    }

    async function testCalendarConnection() {
        calendarTesting = true;
        calendarTestResult = null;
        try {
            calendarTestResult = await api('POST', '/calendar/test');
        } catch (e) {
            calendarTestResult = { ok: false, error: String(e) };
        } finally { calendarTesting = false; }
    }

    async function deleteCalendarConfig() {
        if (!confirm('Remove calendar credentials?')) return;
        await api('DELETE', '/calendar/config');
        calendarStatus = { configured: false };
        calendarUrl = ''; calendarUsername = ''; calendarPassword = '';
        calendarAgents = [];
        toast('Calendar config removed');
    }

    async function toggleAgentCalendar(agentName, enable) {
        await api('POST', `/agents/${agentName}/calendar/${enable ? 'enable' : 'disable'}`);
        toast(`Calendar ${enable ? 'enabled' : 'disabled'} for ${agentName}`);
        loadCalendarAgentStatuses();
    }

    // Active tab
    let activeTab = 'system';
    const tabs = [
        { id: 'system'    },
        { id: 'access'    },
        { id: 'agents'    },
        { id: 'providers' },
        { id: 'account'   },
    ];

    // Global providers
    let providers = [];
    let providerFormVisible = false;
    let editingProvider = null; // null = adding new, object = editing existing
    let provFormName = '';
    let provFormPreset = 'custom';
    let provFormUrl = '';
    let provFormKey = '';
    let provFormModel = '';

    const PROVIDER_PRESETS = [
        { id: 'ollama',    label: 'Ollama (local)',      url: 'http://localhost:11434', key: 'ollama', model: '' },
        { id: 'openrouter',label: 'OpenRouter',          url: 'https://openrouter.ai/api', key: '', model: 'anthropic/claude-sonnet-4-5' },
        { id: 'deepseek',  label: 'DeepSeek',            url: 'https://api.deepseek.com/anthropic', key: '', model: 'deepseek-chat' },
        { id: 'zai',       label: 'Z.ai (GLM)',          url: 'https://api.z.ai/api/anthropic', key: '', model: 'glm-5.1' },
        { id: 'custom',    label: 'Custom',              url: '', key: '', model: '' },
    ];

    function applyProvFormPreset(presetId) {
        provFormPreset = presetId;
        const p = PROVIDER_PRESETS.find(x => x.id === presetId);
        if (!p || presetId === 'custom') return;
        provFormUrl = p.url;
        provFormKey = p.key;
        provFormModel = p.model;
    }

    function detectProvFormPreset(url) {
        if (!url) return 'custom';
        if (url === 'http://localhost:11434') return 'ollama';
        if (url === 'https://api.z.ai/api/anthropic') return 'zai';
        if (url === 'https://openrouter.ai/api') return 'openrouter';
        if (url === 'https://api.deepseek.com/anthropic') return 'deepseek';
        return 'custom';
    }

    async function loadProviders() {
        providers = await api('GET', '/providers').catch(() => []);
    }

    function openAddProvider() {
        editingProvider = null;
        provFormName = '';
        provFormPreset = 'custom';
        provFormUrl = '';
        provFormKey = '';
        provFormModel = '';
        providerFormVisible = true;
    }

    function openEditProvider(p) {
        editingProvider = p;
        provFormName = p.name;
        provFormUrl = p.provider_url;
        provFormKey = p.provider_key;
        provFormModel = p.provider_model;
        provFormPreset = p.preset || detectProvFormPreset(p.provider_url);
        providerFormVisible = true;
    }

    function cancelProviderForm() {
        providerFormVisible = false;
        editingProvider = null;
    }

    async function saveProvider() {
        if (!provFormName.trim()) { toast('Name is required', 'error'); return; }
        if (!provFormUrl.trim()) { toast('URL is required', 'error'); return; }
        const body = {
            name: provFormName.trim(),
            preset: provFormPreset,
            provider_url: provFormUrl.trim(),
            provider_key: provFormKey.trim(),
            provider_model: provFormModel.trim(),
        };
        try {
            if (editingProvider) {
                await api('PUT', `/providers/${editingProvider.id}`, body);
                toast('Provider updated');
            } else {
                await api('POST', '/providers', body);
                toast('Provider created');
            }
            providerFormVisible = false;
            editingProvider = null;
            await loadProviders();
        } catch (e) {
            toast(e.message || 'Save failed', 'error');
        }
    }

    async function deleteProvider(p) {
        if (!confirm(`Delete provider "${p.name}"? Agents using it will fall back to Anthropic defaults.`)) return;
        await api('DELETE', `/providers/${p.id}`);
        toast('Provider deleted');
        await loadProviders();
    }

    onMount(() => {
        loadAuthStatus();
        loadUiAuthStatus();
        loadTimezone();
        loadPrimaryUser();
        loadAllTokens();
        loadAllApprovedUsers();
        loadHeartbeatSettings().then(loadCalendarAgentStatuses);
        loadOwnerProfile();
        loadAccountInfo();
        loadApiKeys();
        loadServerInfo();
        loadCalendarStatus();
        loadProviders();
    });
</script>

<div class="content">
    <!-- Tab Bar -->
    <div class="tab-bar">
        {#each tabs as tab}
            <button
                class="tab-btn {activeTab === tab.id ? 'active' : ''}"
                on:click={() => activeTab = tab.id}
            >{$_(`settings.tab_${tab.id}`)}</button>
        {/each}
    </div>
    {#if activeTab === 'access'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.ui_access')}</div>
            <button class="btn btn-sm" on:click={loadUiAuthStatus}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <div style="display:flex;gap:1rem;flex-wrap:wrap;margin-bottom:1rem">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.ui_password_source')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.password_source === 'unset' ? 'off' : 'on'}">{uiAuthStatus.password_source || 'loading'}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.ui_session_secret')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.session_secret_configured ? 'on' : 'off'}">{uiAuthStatus.session_secret_configured ? $_('settings.ui_configured') : $_('settings.ui_missing')}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.ui_setup_label')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{uiAuthStatus.setup_required ? 'off' : 'on'}">{uiAuthStatus.setup_required ? $_('settings.ui_required') : $_('settings.ui_complete')}</span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.ui_mode_label')}</div>
                    <div style="margin-top:0.35rem;color:var(--gray-mid);font-size:0.85rem">
                        {#if uiAuthStatus.env_override}
                            {$_('settings.ui_controlled_by_env')} <code>PINKY_UI_PASSWORD</code>
                        {:else}
                            {$_('settings.ui_stored_hash')} <code>system_settings</code>
                        {/if}
                    </div>
                </div>
            </div>
            {#if !uiAuthStatus.session_secret_configured}
                <p style="margin:0 0 1rem 0;color:var(--accent)">{@html $_('settings.ui_session_secret_warn', { values: { env_var: '<code>PINKY_SESSION_SECRET</code>' } })}</p>
            {/if}
            {#if uiAuthStatus.env_override}
                <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">{@html $_('settings.ui_env_override_note', { values: { env_var: '<code>PINKY_UI_PASSWORD</code>' } })}</p>
            {:else}
                <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.ui_change_password_desc')}</p>
                <div class="form-inline">
                    <input type="password" class="form-input" bind:value={uiPassword} placeholder={$_('settings.ui_new_password')} style="max-width:280px">
                    <input type="password" class="form-input" bind:value={uiPasswordConfirm} placeholder={$_('settings.ui_confirm_password')} style="max-width:280px">
                    <button class="btn btn-primary" on:click={saveUiPassword}>{$_('settings.ui_save_password')}</button>
                </div>
            {/if}
        </div>
    </div>

    {/if}

    <!-- Setup Wizard (moved to access tab) -->

    <!-- Software Update -->
    {#if activeTab === 'system'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.software_update')}</div>
            <button class="btn btn-sm" on:click={loadServerInfo}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <div style="display:flex;gap:2rem;flex-wrap:wrap;margin-bottom:1.2rem">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.version_label')}</div>
                    <div style="font-size:1rem;font-weight:700;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.version || '--'}</div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.branch_label')}</div>
                    <div style="font-size:0.9rem;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.git_branch || '--'}</div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.claude_code_label')}</div>
                    <div style="font-size:0.9rem;margin-top:0.25rem;font-family:var(--font-grotesk)">{serverInfo.claude_version || '--'}</div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">Release Channel</div>
                    <div style="display:flex;gap:0.4rem;margin-top:0.35rem">
                        <button
                            class="btn btn-sm"
                            style={serverInfo.channel === 'stable' ? 'background:var(--accent);color:white;border-color:var(--accent)' : ''}
                            on:click={() => switchChannel('stable')}
                            disabled={channelSwitching || serverInfo.channel === 'stable'}
                        >stable</button>
                        <button
                            class="btn btn-sm"
                            style={serverInfo.channel === 'beta' ? 'background:var(--accent);color:white;border-color:var(--accent)' : ''}
                            on:click={() => switchChannel('beta')}
                            disabled={channelSwitching || serverInfo.channel === 'beta'}
                        >beta</button>
                    </div>
                </div>
            </div>
            <div class="form-inline" style="margin-bottom:1rem">
                <button class="btn btn-sm" on:click={checkForUpdates} disabled={updateLoading}>
                    {updateLoading ? $_('settings.checking') : $_('settings.check_updates')}
                </button>
                {#if updateInfo && !updateInfo.up_to_date}
                    <button class="btn btn-primary" on:click={applyUpdate} disabled={updateApplying}>
                        {updateApplying ? $_('settings.updating') : $_('settings.apply_updates', { values: { count: updateInfo.pending_commits, plural: updateInfo.pending_commits !== 1 ? 's' : '' } })}
                    </button>
                {/if}
            </div>
            {#if updateInfo}
                {#if updateInfo.up_to_date}
                    <div style="color:var(--gray-mid);font-size:0.85rem">{$_('settings.up_to_date', { values: { branch: updateInfo.branch } })}</div>
                {:else}
                    <div style="margin-bottom:0.5rem;font-size:0.85rem">
                        {$_('settings.pending_commits', { values: { count: updateInfo.pending_commits, plural: updateInfo.pending_commits !== 1 ? 's' : '', branch: updateInfo.branch } })}
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
        <div class="section-header"><div class="section-title">{$_('settings.default_timezone')}</div></div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.timezone_desc')}</p>
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
                <div class="section-title" style="color:var(--accent)">{$_('settings.setup_required')}</div>
            </div>
            <div style="padding:1.5rem;background:var(--gray-light)">
                {#if !authStatus.claude_installed}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>{$_('settings.claude_not_found')}</strong> {$_('settings.claude_not_found_desc')}</p>
                    <pre style="background:var(--surface-inverse);color:var(--text-inverse);padding:0.8rem 1rem;border-radius:6px;font-size:0.85rem;margin:0 0 1rem 0;overflow-x:auto">npm install -g @anthropic-ai/claude-code</pre>
                    <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">{@html $_('settings.claude_install_hint')}</p>
                {:else}
                    <p style="margin:0 0 1rem 0;font-size:0.95rem"><strong>{$_('settings.claude_not_logged_in')}</strong> {$_('settings.claude_not_logged_in_desc')}</p>
                    <div style="background:var(--surface-inverse);color:var(--text-inverse);padding:1rem;border-radius:8px;margin:0 0 1rem 0">
                        <p style="margin:0 0 0.5rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.option_login_account')}</p>
                        <pre style="background:#0a0a12;padding:0.5rem 1rem;border-radius:6px;font-size:0.85rem;margin:0 0 1rem 0">claude login</pre>
                        <p style="margin:0 0 0.5rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.option_login_api_key')}</p>
                        <pre style="background:#0a0a12;padding:0.5rem 1rem;border-radius:6px;font-size:0.85rem;margin:0">export ANTHROPIC_API_KEY=sk-ant-...</pre>
                    </div>
                    <p style="margin:0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.after_auth_restart')}</p>
                {/if}
            </div>
        </div>
    {/if}

    {/if}

    {#if activeTab === 'account'}
    <!-- API Keys -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.api_keys')}</div>
            <button class="btn btn-sm" on:click={loadApiKeys}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.api_keys_desc')}</p>
            <div class="form-inline">
                <select class="form-select" style="max-width:220px" bind:value={newKeyName}>
                    <option value="">{$_('settings.select_key')}</option>
                    <option value="ELEVENLABS_API_KEY">ElevenLabs</option>
                    <option value="OPENAI_API_KEY">OpenAI</option>
                    <option value="DEEPGRAM_API_KEY">Deepgram</option>
                    <option value="GIPHY_API_KEY">Giphy</option>
                </select>
                <input type="password" class="form-input" bind:value={newKeyValue} placeholder={$_('settings.api_key_placeholder')} style="max-width:350px">
                <button class="btn btn-primary" on:click={saveApiKey}>{$_('settings.save_api_key')}</button>
            </div>
        </div>
        <div class="section-body">
            {#if Object.keys(apiKeys).length === 0}
                <div class="empty">{$_('settings.no_api_keys')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('settings.service_col')}</th><th>{$_('settings.status_col')}</th><th>{$_('settings.source_col')}</th><th>{$_('settings.preview_col')}</th><th>{$_('settings.actions_col')}</th></tr></thead>
                    <tbody>
                        {#each Object.entries(apiKeys) as [name, info]}
                            <tr>
                                <td class="mono">{name.replace('_API_KEY', '')}</td>
                                <td><span class="badge badge-{info.configured ? 'on' : 'off'}">{info.configured ? $_('settings.configured') : $_('settings.not_set')}</span></td>
                                <td style="font-size:0.8rem;color:var(--gray-mid)">{info.source}</td>
                                <td class="mono" style="font-size:0.75rem">{info.preview || '--'}</td>
                                <td>
                                    {#if info.configured && info.source === 'settings'}
                                        <button class="btn btn-sm btn-danger" on:click={() => deleteApiKey(name)}>{$_('settings.remove')}</button>
                                    {/if}
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    </div>

    <!-- Calendar -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.calendar')}</div>
            <button class="btn btn-sm" on:click={() => { loadCalendarStatus(); loadCalendarAgentStatuses(); }}>{$_('common.refresh')}</button>
        </div>

        <!-- Inner tab bar -->
        <div style="padding:0.8rem 1.2rem 0;background:var(--surface-2);display:flex;gap:0.3rem;border-bottom:1px solid var(--border)">
            <button class="tab-btn {calendarTab === 'caldav' ? 'active' : ''}" on:click={() => calendarTab = 'caldav'}>
                {$_('settings.cal_icloud_tab')}
                {#if calendarStatus?.caldav?.configured}<span class="cal-dot cal-dot-on"></span>{/if}
            </button>
            <button class="tab-btn {calendarTab === 'google' ? 'active' : ''}" on:click={() => calendarTab = 'google'}>
                {$_('settings.cal_google_tab')}
                {#if googleStatus?.connected}<span class="cal-dot cal-dot-on"></span>{/if}
            </button>
            <button class="tab-btn {calendarTab === 'agents' ? 'active' : ''}" on:click={() => calendarTab = 'agents'}>
                {$_('settings.cal_agents_tab')}
            </button>
        </div>

        <!-- iCloud tab -->
        {#if calendarTab === 'caldav'}
        <div style="padding:1.5rem;background:var(--surface-2)">
            <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1.2rem;align-items:center">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.cal_status_label')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{calendarStatus?.caldav?.configured ? 'on' : 'off'}">
                            {calendarStatus?.caldav?.configured ? $_('settings.cal_connected') : $_('settings.cal_not_connected')}
                        </span>
                    </div>
                </div>
                {#if calendarStatus?.caldav?.configured}
                    <div>
                        <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.cal_apple_id_label')}</div>
                        <div style="margin-top:0.35rem;font-family:var(--font-grotesk);font-size:0.85rem">{calendarStatus.caldav.caldav_username || '--'}</div>
                    </div>
                {/if}
            </div>
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.cal_icloud_desc')}</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;margin-bottom:0.8rem">
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.cal_apple_id_field')}</label>
                    <input type="text" class="form-input" bind:value={calendarUsername}
                        placeholder={calendarStatus?.caldav?.configured ? '(saved)' : 'you@icloud.com'}
                        style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.cal_app_password_field')}</label>
                    <input type="password" class="form-input" bind:value={calendarPassword}
                        placeholder={calendarStatus?.caldav?.configured ? '(saved — enter to update)' : 'xxxx-xxxx-xxxx-xxxx'}
                        style="width:100%">
                </div>
            </div>
            <div class="form-inline">
                <button class="btn btn-primary" on:click={() => { calendarUrl = 'https://caldav.icloud.com/'; saveCalendarConfig(); }} disabled={calendarSaving}>
                    {calendarSaving ? $_('settings.cal_saving') : $_('settings.cal_save')}
                </button>
                {#if calendarStatus?.caldav?.configured}
                    <button class="btn btn-sm" on:click={testCalendarConnection} disabled={calendarTesting}>
                        {calendarTesting ? $_('settings.cal_testing') : $_('settings.cal_test')}
                    </button>
                    <button class="btn btn-sm btn-danger" on:click={deleteCalendarConfig}>{$_('settings.cal_remove')}</button>
                {/if}
            </div>
            {#if calendarTestResult}
                <div style="margin-top:0.8rem;padding:0.6rem 0.8rem;border-radius:6px;font-size:0.85rem;background:{calendarTestResult.ok ? 'var(--success-soft,#d4f5e1)' : 'var(--danger-soft,#fde8e8)'}">
                    {#if calendarTestResult.ok}
                        <strong>{$_('settings.cal_connected_msg', { values: { count: calendarTestResult.count, plural: calendarTestResult.count !== 1 ? 's' : '' } })}</strong>
                        {calendarTestResult.calendars?.join(', ')}
                    {:else}
                        <strong>{$_('settings.cal_failed_msg')}</strong> {calendarTestResult.error}
                    {/if}
                </div>
            {/if}
        </div>
        {/if}

        <!-- Google tab -->
        {#if calendarTab === 'google'}
        <div style="padding:1.5rem;background:var(--surface-2)">
            <div style="display:flex;gap:1.5rem;flex-wrap:wrap;margin-bottom:1rem;align-items:center">
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.cal_google_credentials')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{googleStatus?.configured ? 'on' : 'off'}">
                            {googleStatus?.configured ? $_('settings.cal_google_cred_set') : $_('settings.cal_google_cred_not_set')}
                        </span>
                    </div>
                </div>
                <div>
                    <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.cal_status_label')}</div>
                    <div style="margin-top:0.35rem">
                        <span class="badge badge-{googleStatus?.connected ? 'on' : 'off'}">
                            {googleStatus?.connected ? $_('settings.cal_connected') : $_('settings.cal_not_connected')}
                        </span>
                    </div>
                </div>
                {#if googleStatus?.connected && googleStatus?.email}
                    <div>
                        <div style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.cal_google_account')}</div>
                        <div style="margin-top:0.35rem;font-family:var(--font-grotesk);font-size:0.85rem">{googleStatus.email}</div>
                    </div>
                {/if}
            </div>

            {#if googleStatus?.connected}
                <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">
                    {$_('settings.cal_google_connected_desc')}
                </p>
                <button class="btn btn-sm btn-danger" on:click={disconnectGoogle}>{$_('settings.cal_google_disconnect')}</button>
            {:else if googleStatus?.configured}
                <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">
                    {$_('settings.cal_google_pending_desc')}
                </p>
                <div class="form-inline">
                    <button class="btn btn-primary" on:click={startGoogleOAuth} disabled={googleConnecting}>
                        {googleConnecting ? $_('settings.cal_google_waiting') : $_('settings.cal_google_connect')}
                    </button>
                    <button class="btn btn-sm btn-danger" on:click={() => { googleClientId=''; googleClientSecret=''; googleStatus = { configured: false, connected: false }; api('DELETE', '/calendar/google/disconnect'); }}>
                        {$_('settings.cal_google_remove_cred')}
                    </button>
                </div>
            {:else}
                <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">
                    {$_('settings.cal_google_one_click_desc')}
                </p>
                <div class="form-inline" style="margin-bottom:1.2rem">
                    <button class="btn btn-primary" on:click={startGoogleOAuth} disabled={googleConnecting}>
                        {googleConnecting ? $_('settings.cal_google_waiting') : $_('settings.cal_google_connect')}
                    </button>
                </div>
                <details style="margin-top:0.5rem">
                    <summary style="font-size:0.8rem;color:var(--gray-mid);cursor:pointer;user-select:none">
                        {$_('settings.cal_google_own_cred')}
                    </summary>
                    <div style="margin-top:0.8rem">
                        <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">
                            <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener"
                                style="color:var(--accent)">{$_('settings.cal_google_create_oauth')}</a>
                            {$_('settings.cal_google_in_console')} <code style="font-size:0.8em">http://localhost:8888/calendar/google/callback</code>).
                        </p>
                        <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;margin-bottom:0.8rem">
                            <div>
                                <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.cal_google_client_id')}</label>
                                <input type="text" class="form-input" bind:value={googleClientId}
                                    placeholder="…apps.googleusercontent.com" style="width:100%">
                            </div>
                            <div>
                                <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.cal_google_client_secret')}</label>
                                <input type="password" class="form-input" bind:value={googleClientSecret}
                                    placeholder="GOCSPX-…" style="width:100%">
                            </div>
                        </div>
                        <button class="btn btn-primary" on:click={saveGoogleCredentials}>{$_('settings.cal_google_save_cred')}</button>
                    </div>
                </details>
            {/if}
        </div>
        {/if}

        <!-- Agents tab -->
        {#if calendarTab === 'agents'}
        {#if calendarAgents.length > 0}
        <div class="section-body">
            <table class="data-table" style="margin:0">
                <thead><tr><th>{$_('settings.cal_agent_col')}</th><th>{$_('settings.cal_calendar_col')}</th><th>{$_('settings.cal_actions_col')}</th></tr></thead>
                <tbody>
                    {#each calendarAgents as a}
                        <tr>
                            <td class="mono">{a.display_name || a.name}</td>
                            <td><span class="badge badge-{a.enabled ? 'on' : 'off'}">{a.enabled ? $_('settings.cal_enabled') : $_('settings.cal_disabled')}</span></td>
                            <td>
                                <button class="btn btn-sm" on:click={() => toggleAgentCalendar(a.name, !a.enabled)}>
                                    {a.enabled ? $_('settings.cal_disable_btn') : $_('settings.cal_enable_btn')}
                                </button>
                            </td>
                        </tr>
                    {/each}
                </tbody>
            </table>
        </div>
        {:else}
        <div style="padding:1.5rem;background:var(--surface-2)">
            <div class="empty">{$_('settings.cal_no_agents')}</div>
        </div>
        {/if}
        {/if}
    </div>

    {/if}

    <!-- Skill Catalog -->
    {#if activeTab === 'agents'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.skill_catalog')}</div>
            <button class="btn btn-sm" on:click={refreshSkills}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0">
            <div class="form-inline" style="margin-bottom:0.8rem">
                <input type="text" class="form-input" bind:value={skillName} placeholder={$_('settings.skill_name_placeholder')} style="max-width:200px">
                <input type="text" class="form-input" bind:value={skillDesc} placeholder={$_('settings.skill_desc_placeholder')} style="max-width:300px">
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
                <button class="btn btn-primary" on:click={registerSkill}>{$_('settings.skill_register')}</button>
            </div>
            <div class="form-inline" style="margin-bottom:0.5rem">
                <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.3rem">
                    <input type="checkbox" bind:checked={skillShared}> {$_('settings.skill_shared_label')}
                </label>
                <label style="font-size:0.8rem;display:flex;align-items:center;gap:0.3rem">
                    <input type="checkbox" bind:checked={skillSelfAssignable}> {$_('settings.skill_self_assignable_label')}
                </label>
                <button class="btn btn-sm" on:click={() => showAdvancedSkill = !showAdvancedSkill}>
                    {showAdvancedSkill ? $_('settings.skill_hide_advanced') : $_('settings.skill_show_advanced')}
                </button>
            </div>
            {#if showAdvancedSkill}
                <div style="display:flex;flex-direction:column;gap:0.6rem;margin-top:0.8rem;padding:0.8rem;border-radius:var(--radius-lg);background:var(--surface-2);background:var(--bg)">
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_tool_patterns_label')}</label>
                        <input type="text" class="form-input" bind:value={skillToolPatterns} placeholder="mcp__my-server__*, Read, Bash" style="width:100%">
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_directive_label')}</label>
                        <textarea class="form-input" bind:value={skillDirective} placeholder={$_('settings.skill_directive_placeholder')} rows="3" style="width:100%;resize:vertical"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_mcp_label')}</label>
                        <textarea class="form-input" bind:value={skillMcpConfig} placeholder={'{"command": "python", "args": ["-m", "my_server"], "cwd": "/path"}'} rows="3" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_deps_label')}</label>
                        <input type="text" class="form-input" bind:value={skillRequires} placeholder="pinky-memory, file-access" style="width:100%">
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_file_templates_label')}</label>
                        <textarea class="form-input" bind:value={skillFileTemplates} placeholder={'{"config/my-skill.yaml": "key: value"}'} rows="2" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                    <div>
                        <label style="font-size:0.75rem;font-weight:600;display:block;margin-bottom:0.2rem">{$_('settings.skill_default_config_label')}</label>
                        <textarea class="form-input" bind:value={skillDefaultConfig} placeholder={'{"api_key": "", "max_results": 10}'} rows="2" style="width:100%;resize:vertical;font-family:var(--font-grotesk);font-size:0.8rem"></textarea>
                    </div>
                </div>
            {/if}
        </div>
        <div class="section-body">
            {#if skills.length === 0}
                <div class="empty">{$_('settings.skill_no_skills')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('settings.skill_name_col')}</th><th>{$_('settings.skill_type_col')}</th><th>{$_('settings.skill_category_col')}</th><th>{$_('settings.skill_shared_col')}</th><th>{$_('settings.skill_self_assign_col')}</th><th>{$_('settings.skill_tools_col')}</th><th>{$_('settings.skill_status_col')}</th><th>{$_('settings.skill_actions_col')}</th></tr></thead>
                    <tbody>
                        {#each skills as s}
                            <tr style={!s.enabled ? 'opacity:0.5' : ''}>
                                <td class="mono" style="font-weight:600">{s.name}</td>
                                <td><span class="badge badge-model">{s.skill_type}</span></td>
                                <td><span class="badge" style="background:var(--gray-mid);color:#fff">{s.category}</span></td>
                                <td><span class="badge badge-{s.shared ? 'on' : 'off'}">{s.shared ? $_('settings.skill_yes') : $_('settings.skill_no')}</span></td>
                                <td><span class="badge badge-{s.self_assignable ? 'on' : 'off'}">{s.self_assignable ? $_('settings.skill_yes') : $_('settings.skill_no')}</span></td>
                                <td style="font-size:0.75rem;font-family:var(--font-grotesk);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{(s.tool_patterns || []).join(', ') || '--'}</td>
                                <td><span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? $_('settings.skill_enabled') : $_('settings.skill_off')}</span></td>
                                <td>
                                    <div style="display:flex;gap:0.3rem">
                                        <button class="btn btn-sm" on:click={() => toggleSkill(s.name, !s.enabled)}>{s.enabled ? $_('common.disable') : $_('common.enable')}</button>
                                        {#if s.category !== 'core'}
                                            <button class="btn btn-sm btn-danger" on:click={() => deleteSkill(s.name)}>{$_('common.delete')}</button>
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
            <div class="section-title">{$_('settings.heartbeat_wake')}</div>
            <button class="btn btn-sm" on:click={loadHeartbeatSettings}>{$_('common.refresh')}</button>
        </div>
        <div class="section-body">
            {#if heartbeatSettings.length === 0}
                <div class="empty">{$_('settings.hb_no_agents')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('settings.hb_agent_col')}</th><th>{$_('settings.hb_wake_col')}</th><th>{$_('settings.hb_clock_col')}</th><th>{$_('settings.hb_sleep_col')}</th><th>{$_('settings.hb_heartbeat_col')}</th><th>{$_('settings.hb_schedules_col')}</th><th>{$_('settings.hb_actions_col')}</th></tr></thead>
                    <tbody>
                        {#each heartbeatSettings as a}
                            <tr>
                                <td class="mono">{a.display_name}</td>
                                <td>
                                    <span class="badge badge-{a.wake_interval > 0 ? 'on' : 'off'}">
                                        {a.wake_interval > 0 ? formatWakeInterval(a.wake_interval) : $_('settings.hb_disabled')}
                                    </span>
                                </td>
                                <td>
                                    <span class="badge badge-{a.clock_aligned ? 'on' : 'off'}">
                                        {a.clock_aligned ? $_('settings.hb_yes') : $_('settings.hb_no')}
                                    </span>
                                </td>
                                <td>
                                    <span class="badge badge-{a.auto_sleep_hours > 0 ? 'on' : 'off'}">
                                        {a.auto_sleep_hours > 0 ? a.auto_sleep_hours + 'h' : $_('settings.hb_disabled')}
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
                                    <button class="btn btn-sm" on:click={() => openWakeEdit(a)}>{$_('settings.hb_edit')}</button>
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
                <div class="section-title">{$_('settings.edit_wake_settings')}: {editingAgent}</div>
                <button class="btn btn-sm" on:click={() => editingAgent = null}>{$_('common.cancel')}</button>
            </div>
            <div style="padding:1.5rem;background:var(--gray-light)">
                <div class="form-inline" style="margin-bottom:1rem">
                    <div class="form-row">
                        <span class="form-label">{$_('settings.wake_interval_label')}</span>
                        <select class="form-select" bind:value={editWakeInterval}>
                            <option value={0}>{$_('settings.hb_disabled')}</option>
                            <option value={900}>15 min</option>
                            <option value={1800}>30 min</option>
                            <option value={3600}>1 hour</option>
                            <option value={7200}>2 hours</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span class="form-label">{$_('settings.clock_aligned_label')}</span>
                        <select class="form-select" bind:value={editClockAligned}>
                            <option value={true}>{$_('settings.clock_aligned_yes')}</option>
                            <option value={false}>{$_('settings.clock_aligned_no')}</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <span class="form-label">{$_('settings.auto_sleep_label')}</span>
                        <select class="form-select" bind:value={editAutoSleepHours}>
                            <option value={0}>{$_('settings.hb_disabled')}</option>
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
                    {$_('settings.wake_settings_desc')}
                </p>
                <button class="btn btn-primary" on:click={saveWakeSettings}>{$_('settings.save_wake_settings')}</button>
            </div>
        </div>
    {/if}

    {/if}

    <!-- Primary User -->
    {#if activeTab === 'access'}
    <div class="section">
        <div class="section-header"><div class="section-title">{$_('settings.primary_user')}</div></div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.primary_user_desc')}</p>
            <div class="form-inline">
                {#if allApprovedUsers.length > 0}
                    {@const uniqueUsers = [...new Map(allApprovedUsers.map(u => [u.chat_id, u])).values()]}
                    <select class="form-select" style="max-width:350px" value={primaryChatId} on:change={(e) => { const u = uniqueUsers.find(u => u.chat_id === e.target.value); primaryChatId = e.target.value; primaryDisplayName = u?.display_name || ''; }}>
                        <option value="">{$_('settings.select_user')}</option>
                        {#each uniqueUsers as u}
                            <option value={u.chat_id}>{u.display_name || u.chat_id} ({u.chat_id})</option>
                        {/each}
                    </select>
                {:else}
                    <input type="text" class="form-input" bind:value={primaryChatId} placeholder={$_('settings.chat_id_no_users')} style="max-width:200px">
                    <input type="text" class="form-input" bind:value={primaryDisplayName} placeholder={$_('settings.display_name_placeholder')} style="max-width:200px">
                {/if}
                <button class="btn btn-primary" on:click={savePrimaryUser}>{$_('settings.set_primary_user')}</button>
            </div>
            {#if primaryChatId}
                <div style="margin-top:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem">
                    <span class="badge badge-on">{$_('settings.primary_user_active')}</span>
                    <span style="margin-left:0.3rem">{primaryDisplayName || primaryChatId}</span>
                    <span style="color:var(--gray-mid);margin-left:0.3rem">({primaryChatId})</span>
                </div>
            {/if}
        </div>
    </div>

    {/if}

    <!-- Owner Profile -->
    {#if activeTab === 'system'}
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.owner_profile')}</div>
            <button class="btn btn-sm" on:click={loadOwnerProfile}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <p style="margin:0 0 0.8rem 0;font-size:0.85rem;color:var(--gray-mid)">{$_('settings.owner_profile_desc')}</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:0.8rem;margin-bottom:0.8rem">
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_name_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerName} placeholder={$_('settings.owner_name_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_pronouns_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerPronouns} placeholder={$_('settings.owner_pronouns_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_timezone_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerTimezone} placeholder={$_('settings.owner_timezone_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_role_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerRole} placeholder={$_('settings.owner_role_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_languages_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerLanguages} placeholder={$_('settings.owner_languages_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_comm_style_label')}</label>
                    <input type="text" class="form-input" bind:value={ownerCommStyle} placeholder={$_('settings.owner_comm_style_placeholder')} style="width:100%">
                </div>
                <div>
                    <label style="display:block;font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em;margin-bottom:0.3rem">{$_('settings.owner_ui_language_label')}</label>
                    <select class="form-input" bind:value={ownerLocale} style="width:100%">
                        <option value="">{$_('settings.owner_ui_language_default')}</option>
                        {#each SUPPORTED_LOCALES as loc}
                            <option value={loc.code}>{loc.label}</option>
                        {/each}
                    </select>
                </div>
            </div>
            <button class="btn btn-primary" on:click={saveOwnerProfile}>{$_('settings.save_profile')}</button>
        </div>
    </div>

    {/if}

    <!-- All Approved Users (cross-agent) -->
    {#if activeTab === 'access'}
    <div class="section">
        <div class="section-header"><div class="section-title">{$_('settings.approved_users')}</div></div>
        <div class="section-body">
            {#if allApprovedUsers.length === 0}
                <div class="empty">{$_('settings.approved_no_users')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('settings.approved_agent_col')}</th><th>{$_('settings.approved_user_col')}</th><th>{$_('settings.approved_chat_id_col')}</th><th>{$_('settings.approved_status_col')}</th><th>{$_('settings.approved_timezone_col')}</th><th>{$_('settings.approved_actions_col')}</th></tr></thead>
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
                                            <button class="btn btn-sm" on:click={async () => { await api('PUT', `/agents/${u.agent_name}/approved-users/${u.chat_id}/deny`); toast(`Denied ${u.display_name || u.chat_id} for ${u.agent_name}`); loadAllApprovedUsers(); }}>{$_('settings.deny')}</button>
                                        {:else if u.status === 'denied'}
                                            <button class="btn btn-sm btn-success" on:click={async () => { await api('POST', `/agents/${u.agent_name}/approved-users`, { chat_id: u.chat_id, display_name: u.display_name }); toast(`Approved ${u.display_name || u.chat_id} for ${u.agent_name}`); loadAllApprovedUsers(); }}>{$_('settings.approve')}</button>
                                        {/if}
                                        <button class="btn btn-sm btn-danger" on:click={async () => { if (!confirm(`Revoke ${u.display_name || u.chat_id} from ${u.agent_name}?`)) return; await api('DELETE', `/agents/${u.agent_name}/approved-users/${u.chat_id}`); toast('User revoked'); loadAllApprovedUsers(); }}>{$_('settings.revoke')}</button>
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
        <div class="section-header"><div class="section-title">{$_('settings.bot_tokens_all')}</div></div>
        <div class="section-body">
            {#if allTokens.length === 0}
                <div class="empty">{$_('settings.tokens_no_tokens')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('settings.tokens_agent_col')}</th><th>{$_('settings.tokens_platform_col')}</th><th>{$_('settings.tokens_token_col')}</th><th>{$_('settings.tokens_status_col')}</th><th>{$_('settings.tokens_actions_col')}</th></tr></thead>
                    <tbody>
                        {#each allTokens as t}
                            <tr>
                                <td class="mono">{t.agent_name}</td>
                                <td><span class="badge badge-model">{t.platform}</span></td>
                                <td><span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? $_('settings.tokens_set') : $_('settings.tokens_missing')}</span></td>
                                <td><span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? $_('settings.tokens_enabled') : $_('settings.tokens_disabled')}</span></td>
                                <td>
                                    <button class="btn btn-sm btn-danger" on:click={async () => { if (!confirm(`Remove ${t.platform} token from ${t.agent_name}?`)) return; await api('DELETE', `/agents/${t.agent_name}/tokens/${t.platform}`); toast('Token removed'); loadAllTokens(); }}>{$_('settings.tokens_remove')}</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}

            <!-- iMessage toggle -->
            <div style="margin-top:1rem;padding:0.75rem;background:var(--surface-2, var(--gray-light));border-radius:8px">
                <div style="display:flex;align-items:center;gap:0.5rem;margin-bottom:0.5rem">
                    <span class="material-symbols-outlined" style="font-size:1rem">chat_bubble</span>
                    <span style="font-weight:600;font-size:0.85rem">iMessage</span>
                    <span style="font-size:0.72rem;color:var(--text-muted, var(--gray-mid))">macOS only — sends via Messages.app</span>
                </div>
                <div style="display:flex;flex-wrap:wrap;gap:0.4rem">
                    {#each [...new Set(allTokens.map(t => t.agent_name))] as agentName}
                        {@const hasImessage = allTokens.some(t => t.agent_name === agentName && t.platform === 'imessage')}
                        <button
                            class="visibility-chip"
                            class:visible={hasImessage}
                            on:click={async () => {
                                if (hasImessage) {
                                    await api('DELETE', `/agents/${agentName}/tokens/imessage`);
                                    toast(`iMessage disabled for ${agentName}`);
                                } else {
                                    await api('PUT', `/agents/${agentName}/tokens/imessage`, { token: 'enabled', enabled: true, settings: {} });
                                    toast(`iMessage enabled for ${agentName}`);
                                }
                                loadAllTokens();
                            }}
                        >
                            <span class="material-symbols-outlined" style="font-size:0.85rem">
                                {hasImessage ? 'check_circle' : 'circle'}
                            </span>
                            {agentName}
                        </button>
                    {/each}
                    {#if allTokens.length === 0}
                        <span style="font-size:0.78rem;color:var(--text-muted, var(--gray-mid))">Add a bot token first to see agents here</span>
                    {/if}
                </div>
            </div>
        </div>
    </div>

    <!-- Setup Wizard -->
    <div class="section">
        <div class="section-header"><div class="section-title">{$_('settings.setup_wizard')}</div></div>
        <div style="padding:1.5rem;background:var(--gray-light);display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
            <p style="margin:0;font-size:0.85rem;color:var(--gray-mid);flex:1">{$_('settings.setup_wizard_desc')}</p>
            <button class="btn btn-primary" on:click={rerunOnboarding}>{$_('settings.run_setup_wizard')}</button>
        </div>
    </div>
    {/if}

    <!-- Providers Tab -->
    {#if activeTab === 'providers'}

    <!-- Anthropic Account (top of providers tab) -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('settings.anthropic_account')}</div>
            <button class="btn btn-sm" on:click={loadAccountInfo}>{$_('common.refresh')}</button>
        </div>
        <div style="padding:1.5rem;background:var(--gray-light)">
            <div style="display:flex;gap:2rem;flex-wrap:wrap;margin-bottom:1rem">
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.acct_plan_label')}</span>
                    <div style="font-size:1.3rem;font-weight:700;margin-top:0.2rem">
                        {#if accountInfo.subscriptionType}
                            <span class="badge badge-on" style="font-size:0.9rem;padding:0.3rem 0.6rem">{accountInfo.subscriptionType}</span>
                        {:else}
                            <span class="badge badge-off" style="font-size:0.9rem;padding:0.3rem 0.6rem">{$_('settings.acct_unknown')}</span>
                        {/if}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.acct_provider_label')}</span>
                    <div style="font-size:1.1rem;font-weight:600;margin-top:0.2rem;font-family:var(--font-grotesk)">
                        {accountInfo.apiProvider || '--'}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.acct_email_label')}</span>
                    <div style="font-size:0.95rem;margin-top:0.2rem;font-family:var(--font-grotesk)">
                        {accountInfo.email || '--'}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.acct_lifetime_cost_label')}</span>
                    <div style="font-size:1.3rem;font-weight:700;margin-top:0.2rem;color:{lifetimeCostUsd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">
                        ${lifetimeCostUsd.toFixed(4)}
                    </div>
                </div>
                <div>
                    <span style="font-size:0.75rem;text-transform:uppercase;color:var(--gray-mid);letter-spacing:0.05em">{$_('settings.acct_this_run_label')}</span>
                    <div style="font-size:1.1rem;font-weight:600;margin-top:0.2rem;color:{totalCostUsd > 0 ? 'var(--accent)' : 'var(--gray-mid)'}">
                        ${totalCostUsd.toFixed(4)}
                    </div>
                </div>
            </div>
            {#if lifetimeAgents.length > 0}
                <table class="data-table" style="margin:0">
                    <thead><tr><th>{$_('settings.acct_agent_col')}</th><th>{$_('settings.acct_lifetime_cost_col')}</th><th>{$_('settings.acct_turns_col')}</th><th>{$_('settings.acct_input_tokens_col')}</th><th>{$_('settings.acct_output_tokens_col')}</th></tr></thead>
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
                    <thead><tr><th>{$_('settings.acct_agent_col')}</th><th>{$_('settings.acct_sessions_col')}</th><th>{$_('settings.acct_this_run_col')}</th></tr></thead>
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

    <div class="section" style="margin-top:0.5rem">
        <div class="section-header">
            <div class="section-title">{$_('settings.global_providers')}</div>
            <button class="btn btn-sm btn-primary" on:click={openAddProvider}>+ {$_('settings.add_provider')}</button>
        </div>

        {#if providerFormVisible}
        <div style="padding:1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-bottom:0.5rem">
            <div style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase;margin-bottom:0.75rem">
                {editingProvider ? $_('settings.prov_edit_title') : $_('settings.prov_add_title')}
            </div>
            <div style="display:flex;flex-direction:column;gap:0.6rem">
                <div>
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('settings.prov_name_label')}</div>
                    <input type="text" class="form-input" bind:value={provFormName} placeholder={$_('settings.prov_name_placeholder')} style="width:100%;max-width:300px">
                </div>
                <div>
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.35rem">{$_('settings.prov_preset_label')}</div>
                    <div style="display:flex;gap:0.4rem;flex-wrap:wrap">
                        {#each PROVIDER_PRESETS as preset}
                            <button
                                class="btn btn-sm"
                                class:btn-primary={provFormPreset === preset.id}
                                style={provFormPreset !== preset.id ? 'background:var(--surface-3);color:var(--text-muted)' : ''}
                                on:click={() => applyProvFormPreset(preset.id)}
                            >{preset.label}</button>
                        {/each}
                    </div>
                </div>
                <div>
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('settings.prov_url_label')}</div>
                    <input type="text" class="form-input" bind:value={provFormUrl} placeholder={$_('settings.prov_url_placeholder')} style="width:100%;max-width:420px">
                </div>
                <div>
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('settings.prov_key_label')}</div>
                    <input type="password" class="form-input" bind:value={provFormKey} placeholder={$_('settings.prov_key_placeholder')} style="width:100%;max-width:420px">
                </div>
                <div>
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('settings.prov_model_label')}</div>
                    <input type="text" class="form-input" bind:value={provFormModel} placeholder={$_('settings.prov_model_placeholder')} style="width:100%;max-width:420px">
                </div>
                <div style="display:flex;gap:0.5rem;margin-top:0.25rem">
                    <button class="btn btn-primary" on:click={saveProvider}>{$_('common.save')}</button>
                    <button class="btn" on:click={cancelProviderForm}>{$_('common.cancel')}</button>
                </div>
            </div>
        </div>
        {/if}

        <div class="section-body">
            {#if providers.length === 0 && !providerFormVisible}
                <div class="empty">{$_('settings.prov_no_providers')}</div>
            {:else if providers.length > 0}
                <table class="data-table">
                    <thead>
                        <tr>
                            <th>{$_('settings.prov_name_col')}</th>
                            <th>{$_('settings.prov_preset_col')}</th>
                            <th>{$_('settings.prov_url_col')}</th>
                            <th>{$_('settings.prov_model_col')}</th>
                            <th>{$_('settings.prov_key_col')}</th>
                            <th>{$_('settings.prov_actions_col')}</th>
                        </tr>
                    </thead>
                    <tbody>
                        {#each providers as p}
                            <tr>
                                <td style="font-weight:600;font-family:var(--font-grotesk)">{p.name}</td>
                                <td><span class="badge badge-model">{p.preset || 'custom'}</span></td>
                                <td style="font-size:0.75rem;font-family:var(--font-grotesk);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title={p.provider_url}>{p.provider_url || '—'}</td>
                                <td style="font-size:0.75rem;font-family:var(--font-grotesk)">{p.provider_model || '—'}</td>
                                <td><span class="badge badge-{p.provider_key ? 'on' : 'off'}">{p.provider_key ? $_('settings.prov_key_set') : $_('settings.prov_key_none')}</span></td>
                                <td>
                                    <div style="display:flex;gap:0.3rem">
                                        <button class="btn btn-sm" on:click={() => openEditProvider(p)}>{$_('common.edit')}</button>
                                        <button class="btn btn-sm btn-danger" on:click={() => deleteProvider(p)}>{$_('common.delete')}</button>
                                    </div>
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
        gap: 0.4rem;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
    }
    .tab-btn {
        padding: 0.4rem 1rem;
        font-size: 0.85rem;
        font-weight: 600;
        font-family: var(--font-grotesk);
        background: none;
        border: none;
        border-radius: 4px;
        color: var(--text-primary, #111);
        cursor: pointer;
        letter-spacing: 0.02em;
        transition: background 0.12s;
    }
    .tab-btn:hover { background: rgba(0,0,0,0.06); }
    .tab-btn.active {
        background: var(--accent, #f5c842);
        color: #000;
    }
    .cal-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%; margin-left: 4px; vertical-align: middle; }
    .cal-dot-on { background: #22c55e; }
    .form-inline { display: flex; gap: 0.8rem; align-items: center; flex-wrap: wrap; }
    .form-row { display: flex; flex-direction: column; gap: 0.3rem; }

    .visibility-chip { display: flex; align-items: center; gap: 0.3rem; padding: 0.35rem 0.65rem; border-radius: 999px; font-family: var(--font-grotesk); font-size: 0.78rem; cursor: pointer; border: 1px solid var(--surface-3, #ddd); background: var(--surface-2, #f5f5f5); color: var(--text-muted, #999); transition: all 0.15s; }
    .visibility-chip.visible { background: var(--accent, #7c6af7); color: var(--accent-contrast, #fff); border-color: var(--accent, #7c6af7); }

    @media (max-width: 900px) {
        .form-inline { flex-direction: column; align-items: stretch; }
        .form-inline :global(.form-input), .form-inline :global(.form-select) { max-width: 100% !important; width: 100% !important; }
        .form-inline :global(.btn) { width: 100%; }
    }
</style>
