<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { buildSoul } from '../lib/soulTemplates.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let step = 0;
    const totalSteps = 6;
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
    let agentModel = 'opus';
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
            if (step === 1) {
                authStatus = await api('GET', '/system/auth');
                apiKeys = await api('GET', '/system/api-keys');
                optionalKeyNames.forEach(k => {
                    if (!newKeyValues[k]) newKeyValues[k] = '';
                });
            } else if (step === 2) {
                const [profile, tzResp] = await Promise.all([
                    api('GET', '/settings/owner-profile').catch(() => ({})),
                    api('GET', '/system/timezone').catch(() => ({ timezone: 'America/Los_Angeles' })),
                ]);
                ownerName = profile.name || '';
                ownerPronouns = profile.pronouns || '';
                ownerTimezone = tzResp.timezone || 'America/Los_Angeles';
                ownerLanguages = profile.languages || '';
                ownerCommStyle = profile.comm_style || '';
            } else if (step === 3) {
                existingAgents = await api('GET', '/agents').catch(() => []);
                if (!Array.isArray(existingAgents)) existingAgents = [];
            } else if (step === 4) {
                const platforms = await api('GET', '/outreach/platforms').catch(() => []);
                configuredPlatforms = Array.isArray(platforms) ? platforms.filter(p => p.enabled) : [];
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

    // Step 2: Save profile
    async function saveProfile() {
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
            toast('Profile & timezone saved');
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
            const soul = buildSoul(agentHeart, {
                name: agentName,
                displayName: agentDisplayName || agentName,
                model: agentModel,
                mode: 'bypassPermissions',
                role,
                autoStart: true,
                heartbeatInterval: 300,
                hasTelegram: false,
                hasDiscord: false,
                hasSlack: false,
            });
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

    // Step 5: Complete onboarding
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
</script>

<div class="onboarding-page">
    <div class="onboarding-card">
        <div class="wizard-header">
            <div class="wizard-title">SETUP<span class="y">.</span></div>
            <div class="wizard-sub">
                {#if step === 0}welcome to pinky
                {:else if step === 1}authentication
                {:else if step === 2}who are you?
                {:else if step === 3}create your first agent
                {:else if step === 4}connect a channel
                {:else}you're all set
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
                <!-- Welcome -->
                <div class="welcome-hero">
                    <div class="welcome-icon">
                        <span class="material-symbols-outlined" style="font-size:3rem;color:var(--yellow)">neurology</span>
                    </div>
                    <p class="welcome-text">
                        Pinky is your personal AI companion framework. This wizard will get you up and running with the essentials:
                    </p>
                    <div class="checklist">
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> Claude authentication</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> Your profile & timezone</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> Your first AI agent</div>
                        <div class="checklist-item"><span class="material-symbols-outlined ci">check_circle</span> Messaging channel (optional)</div>
                    </div>
                </div>

            {:else if step === 1}
                <!-- Auth & API Keys -->
                <div class="wizard-label">Claude Authentication</div>
                <div class="auth-status-card" class:ok={authStatus.logged_in || authStatus.has_api_key} class:warn={!authStatus.logged_in && !authStatus.has_api_key}>
                    <span class="material-symbols-outlined" style="font-size:1.2rem">{authStatus.logged_in || authStatus.has_api_key ? 'check_circle' : 'warning'}</span>
                    <div>
                        {#if authStatus.logged_in}
                            <strong>Authenticated</strong> via {authStatus.auth_method || 'Claude login'}
                            {#if authStatus.email}<br><span style="font-size:0.8rem;color:var(--text-muted)">{authStatus.email}</span>{/if}
                        {:else if authStatus.has_api_key}
                            <strong>API Key</strong> configured
                        {:else}
                            <strong>Not authenticated.</strong> Run one of these in your terminal:
                        {/if}
                    </div>
                    <button class="wizard-btn" style="margin-left:auto;font-size:0.7rem;padding:0.4rem 0.8rem" on:click={refreshAuth} disabled={loading}>Refresh</button>
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

                <div class="wizard-label" style="margin-top:1.5rem">Optional API Keys</div>
                <div class="wizard-hint">These enable additional features like voice, images, etc.</div>
                {#each optionalKeyNames as keyName}
                    <div class="api-key-row">
                        <span class="api-key-name">{keyLabel(keyName)}</span>
                        {#if apiKeys[keyName]?.set}
                            <span class="badge-ok">Set</span>
                        {:else}
                            <input type="password" class="wizard-input" style="margin-bottom:0;flex:1" bind:value={newKeyValues[keyName]} placeholder="Paste key...">
                            <button class="wizard-btn" style="font-size:0.7rem;padding:0.4rem 0.8rem" on:click={() => saveApiKey(keyName)} disabled={!newKeyValues[keyName]}>Save</button>
                        {/if}
                    </div>
                {/each}

            {:else if step === 2}
                <!-- Owner Profile -->
                <div class="wizard-label">Display Name</div>
                <input type="text" class="wizard-input" bind:value={ownerName} placeholder="e.g. Brad">

                <div class="wizard-label">Pronouns <span style="color:var(--text-muted);font-weight:400;text-transform:none">(optional)</span></div>
                <input type="text" class="wizard-input" bind:value={ownerPronouns} placeholder="e.g. he/him, she/her, they/them">

                <div class="wizard-label">Timezone</div>
                <select class="wizard-input" bind:value={ownerTimezone}>
                    {#each commonTimezones as tz}
                        <option value={tz}>{tz}</option>
                    {/each}
                </select>

                <div class="wizard-label">Languages <span style="color:var(--text-muted);font-weight:400;text-transform:none">(optional)</span></div>
                <input type="text" class="wizard-input" bind:value={ownerLanguages} placeholder="e.g. English, Spanish">

                <div class="wizard-label">Communication Style <span style="color:var(--text-muted);font-weight:400;text-transform:none">(optional)</span></div>
                <input type="text" class="wizard-input" bind:value={ownerCommStyle} placeholder="e.g. direct, casual, concise">

                <button class="wizard-btn wizard-btn-primary" style="margin-top:0.5rem" on:click={saveProfile} disabled={loading}>
                    {loading ? 'Saving...' : 'Save Profile'}
                </button>

            {:else if step === 3}
                <!-- Create Agent -->
                {#if existingAgents.length > 0 && !agentCreated}
                    <div class="wizard-hint">
                        You already have {existingAgents.length} agent{existingAgents.length > 1 ? 's' : ''}: <strong>{existingAgents.map(a => a.display_name || a.name).join(', ')}</strong>. Create another or skip.
                    </div>
                {/if}

                <div class="wizard-label">Name</div>
                <div class="wizard-hint">What your agent goes by.</div>
                <input type="text" class="wizard-input" bind:value={agentDisplayName} on:input={() => { agentName = agentDisplayName.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]/g, ''); }} placeholder="e.g. Oleg, Rex, Barsik" disabled={agentCreated}>
                {#if agentDisplayName}<div class="wizard-id-preview">ID: {agentName}</div>{/if}

                <div class="wizard-label">Brain</div>
                <div class="wizard-hint">Pick the thinking engine.</div>
                <div class="wizard-options">
                    {#each [['opus','OPUS','Maximum intelligence.'],['sonnet','SONNET','Fast + smart. Daily driver.'],['haiku','HAIKU','Lightning fast. Simple tasks.']] as [val, title, desc]}
                        <div class="wizard-option" class:selected={agentModel === val} on:click={() => { if (!agentCreated) agentModel = val; }}>
                            <div class="wizard-option-title">{title}</div>
                            <div class="wizard-option-desc">{desc}</div>
                        </div>
                    {/each}
                </div>

                <div class="wizard-label">Heart</div>
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
                        <strong>{agentDisplayName || agentName}</strong> created and running.
                    </div>
                {:else}
                    <button class="wizard-btn wizard-btn-primary" on:click={createAgent} disabled={loading || !agentDisplayName.trim()}>
                        {loading ? 'Creating...' : 'Create Agent'}
                    </button>
                {/if}

            {:else if step === 4}
                <!-- Connect Channel -->
                <div class="wizard-hint">Connect a messaging platform so your agent can reach you. All optional.</div>

                {#if configuredPlatforms.length > 0}
                    <div class="wizard-label">Already Configured</div>
                    <div style="margin-bottom:1rem">
                        {#each configuredPlatforms as p}
                            <span class="badge-ok" style="margin-right:0.5rem">{platformLabels[p.platform] || p.platform}</span>
                        {/each}
                    </div>
                {/if}

                <div class="wizard-label">Platform</div>
                <div class="wizard-options" style="grid-template-columns:1fr 1fr 1fr">
                    {#each [['telegram','TELEGRAM','BotFather token'],['discord','DISCORD','Bot token'],['slack','SLACK','xoxb- token']] as [val, title, desc]}
                        <div class="wizard-option" class:selected={selectedPlatform === val} on:click={() => { selectedPlatform = val; platformConfigured = false; platformTestResult = null; platformToken = ''; }}>
                            <div class="wizard-option-title">{title}</div>
                            <div class="wizard-option-desc">{desc}</div>
                        </div>
                    {/each}
                </div>

                <div class="wizard-label">{platformLabels[selectedPlatform]} Bot Token</div>
                <input type="password" class="wizard-input" bind:value={platformToken} placeholder={selectedPlatform === 'slack' ? 'xoxb-...' : 'Paste bot token...'} disabled={platformConfigured}>

                {#if platformConfigured}
                    <div class="auth-status-card" class:ok={platformTestResult === 'success'} class:warn={platformTestResult === 'failed'} class:neutral={!platformTestResult}>
                        <span class="material-symbols-outlined" style="font-size:1.2rem">{platformTestResult === 'success' ? 'check_circle' : platformTestResult === 'failed' ? 'error' : 'link'}</span>
                        <span>{platformTestResult === 'success' ? 'Connected!' : platformTestResult === 'failed' ? 'Connection failed. Check token.' : 'Token saved. Test the connection.'}</span>
                        <button class="wizard-btn" style="margin-left:auto;font-size:0.7rem;padding:0.4rem 0.8rem" on:click={testPlatform} disabled={platformTesting}>
                            {platformTesting ? 'Testing...' : 'Test'}
                        </button>
                    </div>
                {:else}
                    <button class="wizard-btn wizard-btn-primary" on:click={configurePlatform} disabled={loading || !platformToken.trim()}>
                        {loading ? 'Configuring...' : 'Configure'}
                    </button>
                {/if}

            {:else if step === 5}
                <!-- Done -->
                <div class="summary-grid">
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{authStatus.logged_in || authStatus.has_api_key ? 'var(--green)' : 'var(--orange)'}">
                            {authStatus.logged_in || authStatus.has_api_key ? 'check_circle' : 'warning'}
                        </span>
                        <div>
                            <div class="summary-label">Claude Auth</div>
                            <div class="summary-value">{authStatus.logged_in ? 'Logged in' : authStatus.has_api_key ? 'API Key' : 'Not configured'}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{ownerName ? 'var(--green)' : 'var(--text-muted)'}">
                            {ownerName ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">Owner</div>
                            <div class="summary-value">{ownerName || 'Skipped'}{ownerTimezone !== 'UTC' ? ` (${ownerTimezone})` : ''}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{existingAgents.length > 0 ? 'var(--green)' : 'var(--text-muted)'}">
                            {existingAgents.length > 0 ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">Agent</div>
                            <div class="summary-value">{existingAgents.length > 0 ? existingAgents.map(a => a.display_name || a.name).join(', ') : 'None yet'}</div>
                        </div>
                    </div>
                    <div class="summary-item">
                        <span class="material-symbols-outlined" style="font-size:1.3rem;color:{configuredPlatforms.length > 0 || platformConfigured ? 'var(--green)' : 'var(--text-muted)'}">
                            {configuredPlatforms.length > 0 || platformConfigured ? 'check_circle' : 'radio_button_unchecked'}
                        </span>
                        <div>
                            <div class="summary-label">Channel</div>
                            <div class="summary-value">{configuredPlatforms.length > 0 ? configuredPlatforms.map(p => platformLabels[p.platform] || p.platform).join(', ') : platformConfigured ? platformLabels[selectedPlatform] : 'Local only'}</div>
                        </div>
                    </div>
                </div>
            {/if}

            {#if error}
                <div class="wizard-error">{error}</div>
            {/if}
        </div>

        <div class="wizard-footer">
            <button class="wizard-btn" on:click={prev} style="visibility:{step === 0 ? 'hidden' : 'visible'}">Back</button>
            {#if step > 0 && step < totalSteps - 1}
                <button class="wizard-btn" on:click={next} style="color:var(--text-muted);font-size:0.7rem">Skip</button>
            {:else}
                <div></div>
            {/if}
            {#if step === totalSteps - 1}
                <button class="wizard-btn wizard-btn-primary" on:click={completeOnboarding} disabled={loading}>
                    {loading ? 'Finishing...' : 'Go to Dashboard'}
                </button>
            {:else if step === 0}
                <button class="wizard-btn wizard-btn-primary" on:click={next}>Let's Go</button>
            {:else}
                <button class="wizard-btn wizard-btn-primary" on:click={next}>Next</button>
            {/if}
        </div>
    </div>
</div>

<style>
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

    @media (max-width: 600px) {
        .wizard-options { grid-template-columns: 1fr; }
        .wizard-hearts { grid-template-columns: 1fr; }
        .api-key-row { flex-wrap: wrap; }
    }
</style>
