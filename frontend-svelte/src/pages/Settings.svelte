<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

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

    onMount(() => {
        refreshPlatforms();
        refreshSkills();
        refreshSessions();
    });
</script>

<div class="content" style="max-width:1200px">
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
