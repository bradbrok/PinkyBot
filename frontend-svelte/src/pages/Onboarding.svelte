<script>
    import { onMount, onDestroy } from 'svelte';
    import { _, locale as currentLocale } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    // Soul templates now served from backend API
    import { SUPPORTED_LOCALES, setLocale } from '../lib/i18n.js';

    let step = 0;
    const totalSteps = 7;

    // Step 0: Language selection
    let selectedLocale = $currentLocale || 'en';
    async function applyLocale(code) {
        selectedLocale = code;
        await setLocale(code);
    }
    let loading = false;
    let error = '';

    // Step 1: Auth & API keys
    let authStatus = {};
    let apiKeys = {};
    const optionalKeyNames = ['ELEVENLABS_API_KEY', 'OPENAI_API_KEY', 'DEEPGRAM_API_KEY', 'GIPHY_API_KEY'];
    let newKeyValues = {};

    // Step 2: Owner profile & preferences
    let ownerName = '';
    let ownerPronouns = '';
    let ownerTimezone = 'America/Los_Angeles';
    let ownerLanguages = '';
    let ownerCommStyle = '';
    const commonTimezones = [
        'America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York',
        'America/Anchorage', 'Pacific/Honolulu', 'America/Phoenix',
        'America/Toronto', 'America/Vancouver', 'America/Sao_Paulo',
        'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Moscow',
        'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
        'Australia/Sydney', 'Pacific/Auckland', 'UTC',
    ];

    // Step 3: Create agent
    let agentName = '';
    let agentDisplayName = '';
    let agentModel = 'claude-sonnet-4-6';
    let agentHeart = 'sidekick';
    let agentCreated = false;
    let createdAgentName = '';
    let existingAgents = [];

    // Step 4: Channel
    let selectedPlatform = 'telegram';
    let platformToken = '';
    let platformConfigured = false;
    let platformTestResult = null;
    let platformTesting = false;
    let configuredPlatforms = [];

    // Load data for current step
    async function loadStepData() {
        error = '';
        try {
            if (step === 2) {
                authStatus = await api('GET', '/system/auth');
                apiKeys = await api('GET', '/system/api-keys');
                optionalKeyNames.forEach(k => {
                    if (!newKeyValues[k]) newKeyValues[k] = '';
                });
            } else if (step === 3) {
                const [profile, tzResp] = await Promise.all([
                    api('GET', '/settings/owner-profile').catch(() => ({})),
                    api('GET', '/system/timezone').catch(() => ({ timezone: 'America/Los_Angeles' })),
                ]);
                ownerName = profile.name || '';
                ownerPronouns = profile.pronouns || '';
                ownerTimezone = tzResp.timezone || 'America/Los_Angeles';
                ownerLanguages = profile.languages || '';
                ownerCommStyle = profile.comm_style || '';
            } else if (step === 4) {
                existingAgents = await api('GET', '/agents').catch(() => []);
                if (!Array.isArray(existingAgents)) existingAgents = [];
            } else if (step === 5) {
                const platforms = await api('GET', '/outreach/platforms').catch(() => []);
                configuredPlatforms = Array.isArray(platforms) ? platforms.filter(p => p.enabled) : [];
                // Start polling for pending TG users when entering channel step
                startApprovalPolling();
            } else if (step === 6) {
                stopApprovalPolling();
                // Reload data for summary
                const [authResp, agentsResp, platformsResp, profileResp] = await Promise.all([
                    api('GET', '/system/auth').catch(() => ({})),
                    api('GET', '/agents').catch(() => []),
                    api('GET', '/outreach/platforms').catch(() => []),
                    api('GET', '/settings/owner-profile').catch(() => ({})),
                ]);
                authStatus = authResp;
                existingAgents = Array.isArray(agentsResp) ? agentsResp : [];
                configuredPlatforms = Array.isArray(platformsResp) ? platformsResp.filter(p => p.enabled) : [];
                ownerName = profileResp.name || ownerName;
            } else {
                stopApprovalPolling();
            }
        } catch (e) {
            error = e.message || 'Failed to load data';
        }
    }

    // Step 1: Refresh auth
    async function refreshAuth() {
        loading = true;
        try {
            authStatus = await api('GET', '/system/auth');
            toast('Auth status refreshed');
        } catch (e) {
            toast(e.message || 'Failed to refresh', 'error');
        }
        loading = false;
    }

    // Step 1: Save optional API key
    async function saveApiKey(keyName) {
        const val = newKeyValues[keyName];
        if (!val?.trim()) return;
        try {
            await api('PUT', `/system/api-keys/${keyName}`, { value: val.trim() });
            apiKeys = await api('GET', '/system/api-keys');
            newKeyValues[keyName] = '';
            toast(`${keyName} saved`);
        } catch (e) {
            toast(e.message || 'Failed to save key', 'error');
        }
    }

    // Step 2: Save profile (and advance)
    async function saveProfile(advance = true) {
        loading = true;
        try {
            await Promise.all([
                api('PUT', '/settings/owner-profile', {
                    name: ownerName,
                    pronouns: ownerPronouns,
                    languages: ownerLanguages,
                    comm_style: ownerCommStyle,
                }),
                api('PUT', '/system/timezone', { timezone: ownerTimezone }),
            ]);
            toast('Profile saved');
            if (advance) next();
        } catch (e) {
            toast(e.message || 'Failed to save', 'error');
        }
        loading = false;
    }

    // Step 3: Create agent
    async function createAgent() {
        if (!agentName.trim()) { toast('Give your agent a name', 'error'); return; }
        if (!/^[a-z0-9_-]+$/.test(agentName)) { toast('Lowercase letters, numbers, hyphens only', 'error'); return; }
        loading = true;
        try {
            const role = agentHeart === 'custom' ? 'sidekick' : agentHeart;
            const soulResp = await api('POST', '/soul-templates/render', {
                type: agentHeart,
                name: agentDisplayName || agentName,
                model: agentModel,
                mode: 'bypassPermissions',
                heartbeat_interval: 300,
            });
            const soul = soulResp.soul;
            await api('POST', '/agents', {
                name: agentName,
                display_name: agentDisplayName || agentName,
                model: agentModel,
                permission_mode: 'bypassPermissions',
                soul,
                role,
                auto_start: true,
                heartbeat_interval: 300,
            });
            await api('POST', `/agents/${agentName}/streaming-sessions?label=main`);
            createdAgentName = agentName;
            agentCreated = true;
            existingAgents = await api('GET', '/agents').catch(() => []);
            toast(`${agentDisplayName || agentName} created`);
            // Auto-advance after brief pause so user sees the success
            setTimeout(() => next(), 800);
        } catch (e) {
            toast(e.message || 'Failed to create agent', 'error');
        }
        loading = false;
    }

    // Step 4: Configure platform
    async function configurePlatform() {
        if (!platformToken.trim()) { toast('Enter a bot token', 'error'); return; }
        loading = true;
        try {
            await api('PUT', `/outreach/platforms/${selectedPlatform}`, {
                token: platformToken.trim(),
                enabled: true,
            });
            // Also set on the created agent if we have one
            const target = createdAgentName || (existingAgents.length ? existingAgents[0].name : '');
            if (target) {
                await api('PUT', `/agents/${target}/tokens/${selectedPlatform}`, {
                    token: platformToken.trim(),
                    enabled: true,
                });
            }
            platformConfigured = true;
            toast(`${selectedPlatform} configured`);
        } catch (e) {
            toast(e.message || 'Failed to configure', 'error');
        }
        loading = false;
    }

    // Step 4: Test connection
    async function testPlatform() {
        platformTesting = true;
        platformTestResult = null;
        try {
            const result = await api('POST', `/outreach/platforms/${selectedPlatform}/test`);
            platformTestResult = result.success !== false ? 'success' : 'failed';
            toast(platformTestResult === 'success' ? 'Connection successful' : 'Connection failed', platformTestResult === 'success' ? 'success' : 'error');
        } catch (e) {
            platformTestResult = 'failed';
            toast(e.message || 'Test failed', 'error');
        }
        platformTesting = false;
    }

    // Step 5: TG user approval polling
    let pendingUsers = [];
    let approvedUsers = [];
    let approvalPollInterval = null;
    let approvalLoading = {};

    async function pollPendingUsers() {
        const target = createdAgentName || (existingAgents.length ? existingAgents[0].name : '');
        if (!target) return;
        try {
            const [pendingResp, approvedResp] = await Promise.all([
                api('GET', `/agents/${target}/pending-messages`).catch(() => ({ by_sender: {} })),
                api('GET', `/agents/${target}/approved-users`).catch(() => ({ users: [] })),
            ]);
            // Extract unique pending senders
            const senders = Object.entries(pendingResp.by_sender || {}).map(([chatId, msgs]) => ({
                chat_id: chatId,
                display_name: msgs[0]?.sender_name || chatId,
                message_count: msgs.length,
                last_message: msgs[msgs.length - 1]?.text || '',
            }));
            pendingUsers = senders;
            approvedUsers = (approvedResp.users || []).filter(u => u.status === 'approved');
        } catch (e) {
            console.error('Poll pending users failed:', e);
        }
    }

    async function approveUser(chatId, displayName) {
        const target = createdAgentName || (existingAgents.length ? existingAgents[0].name : '');
        if (!target) return;
        approvalLoading[chatId] = true;
        approvalLoading = approvalLoading; // trigger reactivity
        try {
            await api('POST', `/agents/${target}/approved-users`, {
                chat_id: chatId,
                display_name: displayName,
                approved_by: 'onboarding',
            });
            toast(`${displayName} approved`);
            await pollPendingUsers();
        } catch (e) {
            toast(e.message || 'Failed to approve', 'error');
        }
        approvalLoading[chatId] = false;
        approvalLoading = approvalLoading;
    }

    function startApprovalPolling() {
        pollPendingUsers();
        if (approvalPollInterval) clearInterval(approvalPollInterval);
        approvalPollInterval = setInterval(pollPendingUsers, 4000);
    }

    function stopApprovalPolling() {
        if (approvalPollInterval) { clearInterval(approvalPollInterval); approvalPollInterval = null; }
    }

    // Step 6: Complete onboarding
    async function completeOnboarding() {
        loading = true;
        try {
            await api('POST', '/system/onboarding-complete');
            window.location.hash = '#/';
        } catch (e) {
            toast(e.message || 'Failed to complete', 'error');
        }
        loading = false;
    }

    // Navigation
    function prev() { if (step > 0) { step--; loadStepData(); } }
    function next() {
        if (step < totalSteps - 1) { step++; loadStepData(); }
    }

    function keyLabel(k) {
        return k.replace('_API_KEY', '').replace(/_/g, ' ');
    }

    $: platformLabels = { telegram: 'Telegram', discord: 'Discord', slack: 'Slack' };

    onMount(() => { loadStepData(); });
    onDestroy(() => { stopApprovalPolling(); });
</script>

<div class="onboarding-page">
    <div class="onboarding-card">
        <div class="wizard-header">
            <div class="wizard-title">SETUP<span class="y">.</span></div>
            <div class="wizard-sub">
                {#if step === 0}{$_('onboarding.lang_sub')}
                {:else if step === 1}{$_('onboarding.step0_sub')}
                {:else if step === 2}{$_('onboarding.step1_sub')}
                {:else if step === 3}{$_('onboarding.step2_sub')}
                {:else if step === 4}{$_('onboarding.step3_sub')}
                {:else if step === 5}{$_('onboarding.step4_sub')}
                {:else}{$_('onboarding.step5_sub')}
                {/if}
            </div>
        </div>

        <div class="wizard-progress">
            {#each Array(totalSteps) as _, i}
                <div class="wizard-step-dot" class:active={i === step} class:done={i < step}></div>
            {/each}
        </div>

        <div class="wizard-body">
            {#if step === 0}
                <!-- Language Selection -->
                <div class="lang-grid">
                    {#each SUPPORTED_LOCALES as loc}
                        <button
                            class="lang-tile"
                            class:selected={selectedLocale === loc.code}
                            on:click={() => applyLocale(loc.code)}
                        >
                            <span class="lang-code">{loc.code.toUpperCase()}</span>
                            <span class="lang-name">{loc.label}</span>
                        </button>
                    {/each}
                </div>

            {:else if step === 1}
                <!-- Welcome -->
                <div class="welcome-hero">
                    <div class="welcome-icon">
                        <span class="material-symbols-outlined" style="font-size:3rem;color:var(--yellow)">neurology</span>
                    </div>
                    <p class="welcome-text">
                        {$_('onboarding.welcome_text')}
                    </p>
                    <div class="checklist">
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> {$_('onboarding.checklist_auth')}</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> {$_('onboarding.checklist_profile')}</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> {$_('onboarding.checklist_agent')}</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> {$_('onboarding.checklist_channel')}</div>
                    </div>
                </div>

            {:else if step === 2}
                <!-- Auth & API Keys -->
                <div class="wizard-label">{$_('onboarding.claude_auth')}</div>
                <div class="auth-status-card" class:ok={authStatus.logged_in || authStatus.has_api_key} class:warn={!authStatus.logged_in && !authStatus.has_api_key}>
                    <span class="material-symbols-outlined" style="font-size:1.2rem">{authStatus.logged_in || authStatus.has_api_key ? 'check_circle' : 'warning'}</span>
                    <div>
                        {#if authStatus.logged_in}
                            <strong>{$_('onboarding.auth_authenticated')}</strong> via {authStatus.auth_method || 'Claude login'}
                            {#if authStatus.email}<br><span style="font-size:0.8rem;color:var(--text-muted)">{authStatus.email}</span>{/if}
                        {:else if authStatus.has_api_key}
                            <strong>{$_('onboarding.auth_api_key')}</strong> {$_('onboarding.auth_configured')}
                        {:else}
                            <strong>{$_('onboarding.auth_not_authenticated')}</strong> {$_('onboarding.auth_run_terminal')}
                        {/if}
                    </div>
                    <button class="wizard-btn" style="margin-left:auto;font-size:0.7rem;padding:0.4rem 0.8rem" on:click={refreshAuth} disabled={loading}>{$_('common.refresh')}</button>
                </div>

                {#if !authStatus.logged_in && !authStatus.has_api_key}
                    <div class="terminal-block">
                        <div class="terminal-label">Option 1 — Claude login (Max/Pro):</div>
                        <pre>claude login</pre>
                        <div class="terminal-label" style="margin-top:0.8rem">Option 2 — API key:</div>
                        <pre>export ANTHROPIC_API_KEY=sk-ant-...</pre>
                    </div>
                    <div class="wizard-hint" style="margin-top:0.5rem">After authenticating, click Refresh above. You can also continue and set this up later.</div>
                {/if}

                <div class="wizard-label" style="margin-top:1.5rem">{$_('onboarding.optional_api_keys')}</div>
                <div class="wizard-hint">{$_('onboarding.optional_api_keys_hint')}</div>
                {#each optionalKeyNames as keyName}
                    <div class="api-key-row">
                        <span class="api-key-name">{keyLabel(keyName)}</span>
                        {#if apiKeys.keys?.[keyName]?.configured}
                            <span class="badge-ok">{$_('onboarding.key_set')}</span>
                        {:else}
                            <input type="password" class="wizard-input" style="margin-bottom:0;flex:1" bind:value={newKeyValues[keyName]} placeholder={$_('onboarding.paste_key')}>
                            <button class="wizard-btn" style="font-size:0.7rem;padding:0.4rem 0.8rem" on:click={() => saveApiKey(keyName)} disabled={!newKeyValues[keyName]}>{$_('common.save')}</button>
                        {/if}
                    </div>
                {/each}

            {:else if step === 3}
                <!-- Owner Profile -->
                <div class="wizard-label">{$_('onboarding.display_name')}</div>
                <input type="text" class="wizard-input" bind:value={ownerName} placeholder="e.g. Brad">

                <div class="wizard-label">{$_('onboarding.pronouns')} <span style="color:var(--text-muted);font-weight:400;text-transform:none">({$_('common.optional')})</span></div>
                <input type="text" class="wizard-input" bind:value={ownerPronouns} placeholder="e.g. he/him, she/her, they/them">

                <div class="wizard-label">{$_('onboarding.timezone')}</div>
                <select class="wizard-input" bind:value={ownerTimezone}>
                    {#each commonTimezones as tz}
                        <option value={tz}>{tz}</option>
                    {/each}
                </select>

                <div class="wizard-label">{$_('onboarding.languages')} <span style="color:var(--text-muted);font-weight:400;text-transform:none">({$_('common.optional')})</span></div>
                <input type="text" class="wizard-input" bind:value={ownerLanguages} placeholder="e.g. English, Spanish">

                <div class="wizard-label">{$_('onboarding.comm_style')} <span style="color:var(--text-muted);font-weight:400;text-transform:none">({$_('common.optional')})</span></div>
                <input type="text" class="wizard-input" bind:value={ownerCommStyle} placeholder="e.g. direct, casual, concise">

                <button class="wizard-btn wizard-btn-primary" style="margin-top:0.5rem" on:click={() => saveProfile(true)} disabled={loading}>
                    {loading ? $_('common.saving') : $_('common.next') + ' →'}
                </button>

            {:else if step === 4}
                <!-- Create Agent -->
                {#if existingAgents.length > 0 && !agentCreated}
                    <div class="wizard-hint">
                        You already have {existingAgents.length} agent{existingAgents.length > 1 ? 's' : ''}: <strong>{existingAgents.map(a => a.display_name || a.name).join(', ')}</strong>. Create another or skip.
                    </div>
                {/if}

                <div class="wizard-label">{$_('tasks.name')}</div>
                <div class="wizard-hint">{$_('onboarding.agent_name_hint')}</div>
                <input type="text" class="wizard-input" bind:value={agentDisplayName} on:input={() => { agentName = agentDisplayName.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]/g, ''); }} placeholder="e.g. Oleg, Rex, Barsik" disabled={agentCreated}>
                {#if agentDisplayName}<div class="wizard-id-preview">ID: {agentName}</div>{/if}

                <div class="wizard-label">{$_('onboarding.brain')}</div>
                <div class="wizard-hint">{$_('onboarding.brain_hint')}</div>
                <div class="wizard-options">
                    {#each [['claude-sonnet-4-6','SONNET 4.6','Fast + smart. Best daily driver. (1M context)'],['claude-opus-4-6','OPUS 4.6','Maximum intelligence. (1M context)'],['claude-haiku-4-5-20251001','HAIKU 4.5','Lightning fast. Simple tasks.']] as [val, title, desc]}
                        <div class="wizard-option" class:selected={agentModel === val} on:click={() => { if (!agentCreated) agentModel = val; }}>
                            <div class="wizard-option-title">{title}</div>
                            <div class="wizard-option-desc">{desc}</div>
                        </div>
                    {/each}
                </div>

                <div class="wizard-label">{$_('onboarding.heart')}</div>
                <div class="wizard-hearts">
                    {#each [['sidekick','ᓚᘏᗢ','Sidekick','Personal assistant.'],['worker','>_','Worker','Heads-down coder.'],['lead','[*]','Team Lead','Reviews code, coordinates.'],['custom','{?}','Custom','Write your own.']] as [val, icon, title, desc]}
                        <div class="wizard-heart" class:selected={agentHeart === val} on:click={() => { if (!agentCreated) agentHeart = val; }}>
                            <div class="wizard-heart-icon">{icon}</div>
                            <div class="wizard-heart-name">{title}</div>
                            <div class="wizard-heart-desc">{desc}</div>
                        </div>
                    {/each}
                </div>

                {#if agentCreated}
                    <div class="auth-status-card ok">
                        <span class="material-symbols-outlined" style="font-size:1.2rem">check_circle</span>
                        <strong>{agentDisplayName || agentName}</strong> {$_('onboarding.agent_created')}
                    </div>
                {:else}
                    <button class="wizard-btn wizard-btn-primary" on:click={createAgent} disabled={loading || !agentDisplayName.trim()}>
                        {loading ? $_('tasks.creating') : $_('onboarding.create_agent')}
                    </button>
                {/if}

            {:else if step === 5}
                <!-- Connect Channel -->
                <div class="wizard-hint">{$_('onboarding.channel_hint')}</div>

                {#if configuredPlatforms.length > 0}
                    <div class="wizard-label">{$_('onboarding.already_configured')}</div>
                    <div style="margin-bottom:1rem">
                        {#each configuredPlatforms as p}
                            <span class="badge-ok" style="margin-right:0.5rem">{platformLabels[p.platform] || p.platform}</span>
                        {/each}
                    </div>
                {/if}

                <div class="wizard-label">{$_('onboarding.platform')}</div>
                <div class="wizard-options" style="grid-template-columns:1fr 1fr 1fr">
                    {#each [['telegram','TELEGRAM','BotFather token'],['discord','DISCORD','Bot token'],['slack','SLACK','xoxb- token']] as [val, title, desc]}
                        <div class="wizard-option" class:selected={selectedPlatform === val} on:click={() => { selectedPlatform = val; platformConfigured = false; platformTestResult = null; platformToken = ''; }}>
                            <div class="wizard-option-title">{title}</div>
                            <div class="wizard-option-desc">{desc}</div>
                        </div>
                    {/each}
                </div>

                <div class="wizard-label">{platformLabels[selectedPlatform]} Bot Token</div>
                {#if selectedPlatform === 'telegram'}
                    <div class="wizard-hint">Get a token from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> on Telegram → /newbot → copy the token.</div>
                {:else if selectedPlatform === 'discord'}
                    <div class="wizard-hint">Create a bot at <a href="https://discord.com/developers/applications" target="_blank" rel="noopener">discord.com/developers</a> → Bot → Reset Token → copy it.</div>
                {:else if selectedPlatform === 'slack'}
                    <div class="wizard-hint">Create a Slack app at <a href="https://api.slack.com/apps" target="_blank" rel="noopener">api.slack.com/apps</a> → OAuth & Permissions → Bot User OAuth Token (xoxb-).</div>
                {/if}
                <input type="password" class="wizard-input" bind:value={platformToken} placeholder={selectedPlatform === 'slack' ? 'xoxb-...' : 'Paste bot token...'} disabled={platformConfigured}>

                {#if platformConfigured}
                    <div class="auth-status-card" class:ok={platformTestResult === 'success'} class:warn={platformTestResult === 'failed'} class:neutral={!platformTestResult}>
                        <span class="material-symbols-outlined" style="font-size:1.2rem">{platformTestResult === 'success' ? 'check_circle' : platformTestResult === 'failed' ? 'error' : 'link'}</span>
                        <span>{platformTestResult === 'success' ? $_('onboarding.connected') : platformTestResult === 'failed' ? $_('onboarding.connection_failed') : $_('onboarding.token_saved')}</span>
                        <button class="wizard-btn" style="margin-left:auto;font-size:0.7rem;padding:0.4rem 0.8rem" on:click={testPlatform} disabled={platformTesting}>
                            {platformTesting ? $_('onboarding.testing') : $_('onboarding.test')}
                        </button>
                    </div>
                {:else}
                    <button class="wizard-btn wizard-btn-primary" on:click={configurePlatform} disabled={loading || !platformToken.trim()}>
                        {loading ? $_('onboarding.configuring') : $_('onboarding.configure')}
                    </button>
                {/if}

                <!-- Approved Users -->
                {#if approvedUsers.length > 0}
                    <div class="wizard-label" style="margin-top:1.5rem">APPROVED USERS</div>
                    {#each approvedUsers as user}
                        <div class="approval-row approved">
                            <span class="material-symbols-outlined" style="font-size:1rem;color:var(--green)">check_circle</span>
                            <span class="approval-name">{user.display_name || user.chat_id}</span>
                            <span class="approval-id">{user.chat_id}</span>
                        </div>
                    {/each}
                {/if}

                <!-- Pending Users -->
                {#if pendingUsers.length > 0}
                    <div class="wizard-label" style="margin-top:1.5rem">PENDING APPROVAL</div>
                    <div class="wizard-hint">These users messaged your bot. Approve to let them chat with your agent.</div>
                    {#each pendingUsers as user}
                        <div class="approval-row pending">
                            <div class="approval-info">
                                <span class="approval-name">{user.display_name}</span>
                                <span class="approval-id">{user.chat_id}</span>
                                {#if user.last_message}
                                    <span class="approval-preview">"{user.last_message.slice(0, 60)}{user.last_message.length > 60 ? '...' : ''}"</span>
                                {/if}
                            </div>
                            <button class="wizard-btn wizard-btn-primary" style="font-size:0.7rem;padding:0.4rem 0.8rem;margin-left:auto" on:click={() => approveUser(user.chat_id, user.display_name)} disabled={approvalLoading[user.chat_id]}>
                                {approvalLoading[user.chat_id] ? 'Approving...' : 'Approve'}
                            </button>
                        </div>
                    {/each}
                {:else if platformConfigured || configuredPlatforms.length > 0}
                    <div class="wizard-label" style="margin-top:1.5rem">WAITING FOR USERS</div>
                    <div class="wizard-hint">Send a message to your bot on Telegram to see it here. Polling every 4s...</div>
                    <div class="waiting-indicator">
                        <span class="material-symbols-outlined pulse-icon">radio_button_checked</span>
                        <span>Listening for messages...</span>
                    </div>
                {/if}

            {:else if step === 6}
                <!-- Done -->
                <div class="summary-grid">
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{authStatus.logged_in || authStatus.has_api_key ? 'var(--green)' : 'var(--orange)'}">
                            {authStatus.logged_in || authStatus.has_api_key ? 'check_circle' : 'warning'}
                        </span>
                        <div>
                            <div class="summary-label">{$_('onboarding.claude_auth')}</div>
                            <div class="summary-value">{authStatus.logged_in ? $_('onboarding.logged_in') : authStatus.has_api_key ? $_('onboarding.auth_api_key') : $_('onboarding.not_configured')}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{ownerName ? 'var(--green)' : 'var(--text-muted)'}">
                            {ownerName ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">{$_('onboarding.owner')}</div>
                            <div class="summary-value">{ownerName || $_('onboarding.skipped')}{ownerTimezone !== 'UTC' ? ` (${ownerTimezone})` : ''}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{existingAgents.length > 0 ? 'var(--green)' : 'var(--text-muted)'}">
                            {existingAgents.length > 0 ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">{$_('dashboard.col_agent')}</div>
                            <div class="summary-value">{existingAgents.length > 0 ? existingAgents.map(a => a.display_name || a.name).join(', ') : $_('onboarding.none_yet')}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{configuredPlatforms.length > 0 || platformConfigured ? 'var(--green)' : 'var(--text-muted)'}">
                            {configuredPlatforms.length > 0 || platformConfigured ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">{$_('onboarding.channel')}</div>
                            <div class="summary-value">{configuredPlatforms.length > 0 ? configuredPlatforms.map(p => platformLabels[p.platform] || p.platform).join(', ') : platformConfigured ? platformLabels[selectedPlatform] : $_('onboarding.local_only')}</div>
                        </div>
                    </div>
                </div>
            {/if}

            {#if error}
                <div class="wizard-error">{error}</div>
            {/if}
        </div>

        <div class="wizard-footer">
            <button class="wizard-btn" on:click={prev} style="visibility:{step === 0 ? 'hidden' : 'visible'}">{$_('common.back')}</button>
            {#if step > 0 && step < totalSteps - 1}
                <button class="wizard-btn" on:click={next} style="color:var(--text-muted);font-size:0.7rem">{$_('onboarding.skip')}</button>
            {:else}
                <div></div>
            {/if}
            {#if step === totalSteps - 1}
                <button class="wizard-btn wizard-btn-primary" on:click={completeOnboarding} disabled={loading}>
                    {loading ? $_('onboarding.finishing') : $_('onboarding.go_to_dashboard')}
                </button>
            {:else if step === 0}
                <button class="wizard-btn wizard-btn-primary" on:click={next}>{$_('onboarding.lets_go')}</button>
            {:else}
                <button class="wizard-btn wizard-btn-primary" on:click={next}>{$_('common.next')}</button>
            {/if}
        </div>
    </div>
</div>

<style>
    .lang-grid {
        display: grid;
        grid-template-columns: 1fr 1fr;
        gap: 0.6rem;
        margin-bottom: 1rem;
    }
    .lang-tile {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        padding: 0.75rem 1rem;
        background: var(--gray-light);
        border: 1.5px solid transparent;
        border-radius: 6px;
        cursor: pointer;
        text-align: left;
        transition: border-color 0.15s, background 0.15s;
        font-family: var(--font-grotesk);
    }
    .lang-tile:hover { border-color: var(--yellow); }
    .lang-tile.selected { border-color: var(--yellow); background: var(--accent-soft); }
    .lang-code { font-size: 0.75rem; font-weight: 700; color: var(--yellow); letter-spacing: 0.05em; min-width: 2rem; }
    .lang-name { font-size: 0.9rem; color: var(--text-primary); }

    .onboarding-page {
        display: flex;
        align-items: flex-start;
        justify-content: center;
        padding: 2rem 1rem;
        min-height: 100%;
        background:
            radial-gradient(circle at top, var(--accent-soft), transparent 32%),
            var(--app-bg);
    }
    .onboarding-card {
        background: var(--surface-container-lowest);
        color: var(--text-primary);
        border-radius: var(--radius-xl);
        max-width: 700px;
        width: 100%;
        overflow: hidden;
        border: 1px solid var(--outline-variant);
        box-shadow: 0 24px 60px var(--shadow-color);
    }

    /* Reuse wizard styles from Agents.svelte */
    .wizard-header {
        padding: 2rem 2rem 1rem;
        background: linear-gradient(180deg, var(--accent-soft), transparent 70%);
    }
    .wizard-title { font-family: var(--font-grotesk); font-size: 1.5rem; font-weight: 700; }
    .wizard-title .y { color: var(--yellow); }
    .wizard-sub { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-subtle); margin-top: 0.3rem; }
    .wizard-progress { display: flex; gap: 0.2rem; padding: 0 2rem; margin-bottom: 1.5rem; }
    .wizard-step-dot { flex: 1; height: 4px; background: var(--surface-3); border-radius: 2px; transition: background 0.2s; }
    .wizard-step-dot.active { background: var(--yellow); }
    .wizard-step-dot.done { background: var(--green); }
    .wizard-body { padding: 0 2rem 2rem; }
    .wizard-label { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--yellow); margin-bottom: 0.5rem; }
    .wizard-hint { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; }
    .wizard-id-preview { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-top: -0.7rem; margin-bottom: 0.8rem; }
    .wizard-input {
        font-family: var(--font-grotesk);
        font-size: 1rem;
        padding: 0.8rem 1rem;
        border: none;
        background: var(--input-bg);
        color: var(--text-primary);
        width: 100%;
        margin-bottom: 1rem;
        border-radius: var(--radius-lg);
        box-sizing: border-box;
    }
    .wizard-input::placeholder { color: var(--text-subtle); }
    .wizard-input:focus {
        outline: 2px solid var(--accent);
        outline-offset: -2px;
        background: var(--input-focus-bg);
    }
    .wizard-input:disabled { opacity: 0.5; cursor: not-allowed; }
    select.wizard-input { appearance: auto; }
    .wizard-options { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-option {
        padding: 1rem;
        border: 1px solid var(--outline-variant);
        background: var(--surface-1);
        border-radius: var(--radius-lg);
        cursor: pointer;
        text-align: center;
        transition: all 0.15s;
    }
    .wizard-option:hover { background: var(--surface-2); }
    .wizard-option.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-option-title { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; margin-bottom: 0.2rem; }
    .wizard-option-desc { font-size: 0.75rem; color: var(--text-muted); }
    .wizard-option.selected .wizard-option-desc { color: var(--accent); }
    .wizard-hearts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-heart {
        padding: 1.2rem;
        border: 1px solid var(--outline-variant);
        background: var(--surface-1);
        border-radius: var(--radius-lg);
        cursor: pointer;
        transition: all 0.15s;
    }
    .wizard-heart:hover { background: var(--surface-2); }
    .wizard-heart.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-heart-icon { font-size: 1.5rem; margin-bottom: 0.3rem; }
    .wizard-heart-name { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; }
    .wizard-heart-desc { font-size: 0.7rem; color: var(--text-muted); margin-top: 0.2rem; }
    .wizard-heart.selected .wizard-heart-desc { color: var(--accent); }
    .wizard-footer {
        display: flex;
        justify-content: space-between;
        align-items: center;
        padding: 1.5rem 2rem;
        background: var(--surface-1);
        border-top: 1px solid var(--outline-variant);
    }
    .wizard-btn {
        font-family: var(--font-grotesk);
        font-size: 0.8rem;
        font-weight: 700;
        padding: 0.6rem 1.5rem;
        border: none;
        background: var(--surface-2);
        color: var(--text-primary);
        cursor: pointer;
        text-transform: uppercase;
        border-radius: var(--radius-lg);
    }
    .wizard-btn:hover { background: var(--surface-3); color: var(--accent); }
    .wizard-btn:disabled { opacity: 0.5; cursor: not-allowed; }
    .wizard-btn-primary { background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px var(--primary); }
    .wizard-btn-primary:hover { background: var(--primary-container); }
    .wizard-btn-primary:active { transform: scale(0.98); }
    .wizard-error { font-size: 0.8rem; color: var(--red); margin-top: 0.5rem; }

    /* Welcome step */
    .welcome-hero { text-align: center; }
    .welcome-icon { margin-bottom: 1rem; }
    .welcome-text { font-size: 0.9rem; color: var(--text-muted); line-height: 1.6; margin-bottom: 1.5rem; }
    .checklist { text-align: left; display: inline-flex; flex-direction: column; gap: 0.6rem; }
    .checklist-item { display: flex; align-items: center; gap: 0.5rem; font-family: var(--font-grotesk); font-size: 0.85rem; }
    .ci { font-size: 1rem; color: var(--yellow); }

    /* Auth status card */
    .auth-status-card {
        display: flex;
        align-items: center;
        gap: 0.8rem;
        padding: 0.8rem 1rem;
        border-radius: var(--radius-lg);
        margin-bottom: 1rem;
        font-family: var(--font-grotesk);
        font-size: 0.85rem;
        background: var(--surface-1);
    }
    .auth-status-card.neutral { background: var(--tone-neutral-bg); color: var(--tone-neutral-text); }
    .auth-status-card.ok { background: var(--tone-success-bg); color: var(--tone-success-text); }
    .auth-status-card.warn { background: var(--tone-warning-bg); color: var(--tone-warning-text); }

    /* Terminal block */
    .terminal-block {
        background: var(--code-pre-bg);
        color: var(--code-pre-text);
        padding: 1rem;
        border-radius: var(--radius-lg);
        margin-bottom: 1rem;
    }
    .terminal-label { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.3rem; }
    .terminal-block pre {
        background: color-mix(in srgb, var(--code-pre-text) 10%, transparent);
        padding: 0.5rem 1rem;
        border-radius: 4px;
        font-family: monospace;
        font-size: 0.85rem;
        margin: 0;
        color: var(--green);
        overflow-x: auto;
    }

    /* API key row */
    .api-key-row { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.5rem; font-family: var(--font-grotesk); font-size: 0.8rem; }
    .api-key-name { min-width: 90px; font-weight: 600; text-transform: uppercase; font-size: 0.7rem; }
    .badge-ok {
        font-family: var(--font-grotesk);
        font-size: 0.65rem;
        font-weight: 700;
        text-transform: uppercase;
        background: var(--tone-success-bg);
        color: var(--tone-success-text);
        padding: 0.2rem 0.5rem;
        border-radius: var(--radius-lg);
    }

    /* Summary grid */
    .summary-grid { display: flex; flex-direction: column; gap: 1rem; }
    .summary-item {
        display: flex;
        align-items: center;
        gap: 0.8rem;
        padding: 0.8rem 1rem;
        background: var(--surface-1);
        border: 1px solid var(--outline-variant);
        border-radius: var(--radius-lg);
    }
    .summary-label { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--text-muted); }
    .summary-value { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 500; }

    /* Approval rows */
    .approval-row {
        display: flex;
        align-items: center;
        gap: 0.8rem;
        padding: 0.6rem 1rem;
        background: var(--surface-1);
        border: 1px solid var(--outline-variant);
        border-radius: var(--radius-lg);
        margin-bottom: 0.5rem;
        font-family: var(--font-grotesk);
        font-size: 0.85rem;
    }
    .approval-row.pending { border-color: var(--orange); }
    .approval-row.approved { border-color: var(--green); }
    .approval-info { display: flex; flex-direction: column; gap: 0.15rem; flex: 1; }
    .approval-name { font-weight: 600; font-size: 0.85rem; }
    .approval-id { font-size: 0.7rem; color: var(--text-muted); font-family: monospace; }
    .approval-preview { font-size: 0.75rem; color: var(--text-subtle); font-style: italic; }
    .waiting-indicator {
        display: flex;
        align-items: center;
        gap: 0.6rem;
        padding: 0.8rem 1rem;
        background: var(--surface-1);
        border-radius: var(--radius-lg);
        font-family: var(--font-grotesk);
        font-size: 0.85rem;
        color: var(--text-muted);
    }
    .pulse-icon {
        font-size: 1rem;
        color: var(--yellow);
        animation: pulse 1.5s ease-in-out infinite;
    }
    @keyframes pulse {
        0%, 100% { opacity: 0.4; }
        50% { opacity: 1; }
    }

    @media (max-width: 600px) {
        .wizard-options { grid-template-columns: 1fr; }
        .wizard-hearts { grid-template-columns: 1fr; }
        .api-key-row { flex-wrap: wrap; }
    }
</style>
