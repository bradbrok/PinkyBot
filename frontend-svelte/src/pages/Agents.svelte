<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    let agentList = [];
    let agentCount = 0;
    let currentAgent = '';
    let refreshInterval;

    // Detail panel state
    let detailOpen = false;
    let detailName = '';
    let detailModel = '--';
    let detailPermission = '--';
    let detailMaxSessions = '--';
    let detailGroups = '--';
    let detailWorkingDir = '';
    let detailSoul = '';
    let detailSystemPrompt = '';
    let directives = [];
    let tokens = [];
    let files = [];
    let schedules = [];
    let agentSessions = [];
    let editingFile = '';
    let fileEditorOpen = false;
    let fileEditorName = '';
    let fileEditorContent = '';
    let newDirective = '';
    let newDirectivePriority = 0;
    let tokenPlatform = 'telegram';
    let tokenValue = '';

    // Wizard state
    let wizardOpen = false;
    let wizStep = 0;
    const wizTotalSteps = 7;
    let wizName = '';
    let wizDisplayName = '';
    let wizModel = 'opus';
    let wizMode = 'bypassPermissions';
    let wizHeart = 'worker';
    let wizRole = 'sidekick';
    let wizAutoStart = true;
    let wizHeartbeatInterval = 300;
    let wizCustomSoul = '';
    let wizTelegramToken = '';
    let wizDiscordToken = '';
    let wizSlackToken = '';

    const heartTemplates = {
        worker: `# {{NAME}}\n\n## IDENTITY\n\n- **Name:** {{NAME}}\n- **Role:** Code Worker\n- **Vibe:** Heads-down builder. Ships clean code. No fluff.\n\n## SOUL\n\n### Core Principles\n\n**Execute with precision.** You receive tasks, you ship them. Clean code, tested, documented where needed.\n\n**Don't over-engineer.** Build exactly what's needed.\n\n**Be resourceful before asking.** Read the file. Check the context. Search for it. Then ask if you're stuck.\n\n**Every PR needs tests.** No exceptions.\n\n## BOUNDARIES\n\n### Always Do\n- Write tests for every change\n- Keep changes focused and minimal\n- Report what you did clearly\n- Never push to main without review\n\n## MEMORY\n\n_This section grows over time._`,
        lead: `# {{NAME}}\n\n## IDENTITY\n\n- **Name:** {{NAME}}\n- **Role:** Team Lead\n- **Vibe:** Quality guardian. Coordinates workers. Catches bugs before they ship.\n\n## SOUL\n\n### Core Principles\n\n**Quality over speed.** You're the last line of defense.\n\n**Be genuinely helpful, not performatively helpful.**\n\n**Have opinions.** You're allowed to disagree and push back on bad ideas.\n\n## BOUNDARIES\n\n### Always Do\n- Review all PRs before merge\n- Coordinate task assignments across workers\n- Log autonomous work\n\n## MEMORY\n\n_This section grows over time._`,
        sidekick: `# {{NAME}}\n\n## IDENTITY\n\n- **Name:** {{NAME}}\n- **Role:** Personal AI Sidekick\n- **Vibe:** Helpful, opinionated, gets stuff done.\n\n## SOUL\n\n### Core Principles\n\n**Be genuinely helpful, not performatively helpful.**\n\n**Have opinions.** An assistant with no personality is just a search engine.\n\n**Be resourceful before asking.** Come back with answers, not questions.\n\n**Earn trust through competence.**\n\n## BOUNDARIES\n\n### Reversible = my call. Irreversible = ask first.\n\n## MEMORY\n\n_This section grows over time._`,
        custom: '',
    };

    function toast(msg, type = 'success') {
        toastMessage.set({ message: msg, type });
    }

    async function refreshAgents() {
        try {
            const data = await api('GET', '/agents');
            agentList = data.agents || [];
            agentCount = data.count;
        } catch (e) {
            console.error('Failed to refresh agents:', e);
        }
    }

    async function deleteAgent(name) {
        if (!confirm(`Delete agent "${name}" and all its directives/tokens?`)) return;
        await api('DELETE', `/agents/${name}`);
        toast(`${name} deleted`);
        if (currentAgent === name) closeDetail();
        refreshAgents();
    }

    async function openDetail(name) {
        currentAgent = name;
        const agent = await api('GET', `/agents/${name}`);
        detailName = agent.display_name || agent.name;
        detailModel = agent.model;
        detailPermission = agent.permission_mode || 'default';
        detailMaxSessions = agent.max_sessions;
        detailGroups = agent.groups.length ? agent.groups.join(', ') : '--';
        detailWorkingDir = agent.working_dir;
        detailSoul = agent.soul || '';
        detailSystemPrompt = agent.system_prompt || '';
        detailOpen = true;
        loadDirectives();
        loadTokens();
        loadFiles();
        loadSchedules();
        loadSessions();
    }

    function closeDetail() { currentAgent = ''; detailOpen = false; }

    async function saveSoul() { await api('PUT', `/agents/${currentAgent}`, { soul: detailSoul }); toast('Soul saved'); }
    async function saveSystemPrompt() { await api('PUT', `/agents/${currentAgent}`, { system_prompt: detailSystemPrompt }); toast('System prompt saved'); }
    async function saveWorkingDir() { if (!detailWorkingDir) { toast('Enter a path', 'error'); return; } await api('PUT', `/agents/${currentAgent}`, { working_dir: detailWorkingDir }); toast('Working directory saved'); refreshAgents(); }

    async function loadDirectives() { const data = await api('GET', `/agents/${currentAgent}/directives?active_only=false`); directives = data.directives || []; }
    async function addDirective() { if (!newDirective.trim()) { toast('Enter a directive', 'error'); return; } await api('POST', `/agents/${currentAgent}/directives`, { directive: newDirective.trim(), priority: newDirectivePriority }); newDirective = ''; toast('Directive added'); loadDirectives(); }
    async function removeDirective(id) { await api('DELETE', `/agents/${currentAgent}/directives/${id}`); toast('Directive removed'); loadDirectives(); }
    async function toggleDirective(id, active) { await api('POST', `/agents/${currentAgent}/directives/${id}/toggle?active=${active}`); loadDirectives(); }

    async function loadTokens() { const data = await api('GET', `/agents/${currentAgent}/tokens`); tokens = data.tokens || []; }
    async function setToken() { if (!tokenValue) { toast('Enter a token', 'error'); return; } await api('PUT', `/agents/${currentAgent}/tokens/${tokenPlatform}`, { token: tokenValue }); tokenValue = ''; toast(`${tokenPlatform} token set`); loadTokens(); }
    async function removeToken(platform) { await api('DELETE', `/agents/${currentAgent}/tokens/${platform}`); toast(`${platform} token removed`); loadTokens(); }

    async function loadFiles() { const data = await api('GET', `/agents/${currentAgent}/files`); files = data.exists ? (data.files || []) : []; }
    async function editFile(filename) { const data = await api('GET', `/agents/${currentAgent}/files/${filename}`); editingFile = filename; fileEditorName = filename; fileEditorContent = data.content; fileEditorOpen = true; }
    function closeFileEditor() { fileEditorOpen = false; editingFile = ''; }
    async function saveFile() { await api('PUT', `/agents/${currentAgent}/files/${editingFile}`, { content: fileEditorContent }); toast(`${editingFile} saved`); loadFiles(); }
    async function syncClaudeMd() {
        const agent = await api('GET', `/agents/${currentAgent}`);
        const dir = await api('GET', `/agents/${currentAgent}/directives`);
        let content = agent.soul || '';
        if (agent.system_prompt) content += (content ? '\n\n' : '') + agent.system_prompt;
        if (dir.count > 0) { content += '\n\n## Active Directives\n'; content += dir.directives.map(d => `- ${d.directive}`).join('\n'); }
        await api('PUT', `/agents/${currentAgent}/files/CLAUDE.md`, { content });
        toast('CLAUDE.md synced'); loadFiles();
    }

    async function loadSchedules() { const data = await api('GET', `/agents/${currentAgent}/schedules?enabled_only=false`); schedules = data.schedules || []; }
    async function addSchedule() { const name = prompt('Schedule name:'); if (!name) return; const cron = prompt('Cron expression:'); if (!cron) return; const promptText = prompt('Wake prompt:', `Scheduled wake: ${name}`); await api('POST', `/agents/${currentAgent}/schedules`, { name, cron, prompt: promptText || `Scheduled wake: ${name}` }); toast(`Schedule "${name}" added`); loadSchedules(); }
    async function toggleSchedule(id, enabled) { await api('POST', `/agents/${currentAgent}/schedules/${id}/toggle?enabled=${enabled}`); loadSchedules(); }
    async function removeSchedule(id) { if (!confirm('Remove this schedule?')) return; await api('DELETE', `/agents/${currentAgent}/schedules/${id}`); toast('Schedule removed'); loadSchedules(); }

    async function loadSessions() { const data = await api('GET', `/agents/${currentAgent}/sessions`); agentSessions = data.sessions || []; }

    // Wizard
    function openWizard() { wizStep = 0; wizName = ''; wizDisplayName = ''; wizModel = 'opus'; wizMode = 'bypassPermissions'; wizHeart = 'worker'; wizRole = 'sidekick'; wizAutoStart = true; wizHeartbeatInterval = 300; wizCustomSoul = ''; wizTelegramToken = ''; wizDiscordToken = ''; wizSlackToken = ''; wizardOpen = true; }
    function closeWizard() { wizardOpen = false; }

    function wizardPrev() { if (wizStep > 0) wizStep--; }
    async function wizardNext() {
        if (wizStep === 0) {
            if (!wizName.trim()) { toast('Give your agent a name', 'error'); return; }
            if (!/^[a-z0-9_-]+$/.test(wizName)) { toast('Lowercase letters, numbers, hyphens only', 'error'); return; }
        }
        if (wizStep < wizTotalSteps - 1) { wizStep++; return; }
        // Summon!
        let soul = heartTemplates[wizHeart] || '';
        if (wizHeart === 'custom') soul = wizCustomSoul;
        soul = soul.replace(/\{\{NAME\}\}/g, wizDisplayName || wizName);
        await api('POST', '/agents', { name: wizName, display_name: wizDisplayName, model: wizModel, permission_mode: wizMode, soul, role: wizRole, auto_start: wizAutoStart, heartbeat_interval: wizHeartbeatInterval });
        if (wizTelegramToken) await api('PUT', `/agents/${wizName}/tokens/telegram`, { token: wizTelegramToken });
        if (wizDiscordToken) await api('PUT', `/agents/${wizName}/tokens/discord`, { token: wizDiscordToken });
        if (wizSlackToken) await api('PUT', `/agents/${wizName}/tokens/slack`, { token: wizSlackToken });
        const sessionType = wizAutoStart ? 'main' : 'chat';
        await api('POST', `/agents/${wizName}/sessions`, { session_type: sessionType });
        closeWizard();
        toast(`${wizDisplayName || wizName} has been summoned`);
        refreshAgents();
    }

    $: wizSummaryPlatforms = [wizTelegramToken && 'Telegram', wizDiscordToken && 'Discord', wizSlackToken && 'Slack'].filter(Boolean);

    onMount(() => { refreshAgents(); refreshInterval = setInterval(refreshAgents, 15000); });
    onDestroy(() => { clearInterval(refreshInterval); });
</script>

<div class="content">
    <!-- Agent Cards -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">Agents <span style="font-weight:400;color:var(--gray-mid)">({agentCount})</span></div>
            <button class="btn btn-primary" on:click={openWizard}>+ New Agent</button>
        </div>
        <div class="section-body">
            {#if agentList.length === 0}
                <div class="empty">No agents registered. Create one above.</div>
            {:else}
                <div class="agent-grid">
                    {#each agentList as a}
                        <div class="agent-card">
                            <div class="agent-name">{a.display_name || a.name}</div>
                            <div class="agent-meta">
                                {#if a.role}<span class="badge" style="background:var(--black);color:var(--yellow)">{a.role}</span>{/if}
                                <span class="badge badge-model">{a.model}</span>
                                <span class="badge badge-{a.enabled ? 'on' : 'off'}">{a.enabled ? 'Active' : 'Disabled'}</span>
                                {#if a.auto_start}<span class="badge" style="background:#dcfce7;color:#166534">Auto-Start</span>{/if}
                                <span class="badge" style="background:#e2e8f0;color:var(--gray-dark)">{a.permission_mode === 'bypassPermissions' ? 'YOLO' : a.permission_mode || 'default'}</span>
                                {#each a.groups as g}<span class="badge badge-group">{g}</span>{/each}
                            </div>
                            <div class="agent-desc">{a.soul || a.system_prompt || 'No soul configured'}</div>
                            <div class="agent-stats">
                                <span>max {a.max_sessions} sessions</span>
                                {#if a.heartbeat_interval}<span>heartbeat {a.heartbeat_interval}s</span>{/if}
                                <span>created {timeAgo(a.created_at)}</span>
                            </div>
                            <div class="agent-actions">
                                <button class="btn btn-sm btn-primary" on:click={() => openDetail(a.name)}>Configure</button>
                                <button class="btn btn-sm btn-danger" on:click={() => deleteAgent(a.name)}>Delete</button>
                            </div>
                        </div>
                    {/each}
                </div>
            {/if}
        </div>
    </div>

    <!-- Agent Detail Panel -->
    {#if detailOpen}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Agent: {detailName}</div>
                <button class="btn" on:click={closeDetail}>Close</button>
            </div>
            <div class="detail-grid">
                <div>
                    <div class="detail-field"><div class="detail-label">Model</div><div class="detail-value">{detailModel}</div></div>
                    <div class="detail-field"><div class="detail-label">Permission Mode</div><div class="detail-value">{detailPermission}</div></div>
                    <div class="detail-field"><div class="detail-label">Max Sessions</div><div class="detail-value">{detailMaxSessions}</div></div>
                    <div class="detail-field"><div class="detail-label">Groups</div><div class="detail-value">{detailGroups}</div></div>
                    <div class="detail-field">
                        <div class="detail-label">Working Dir</div>
                        <div style="display:flex;gap:0.3rem;align-items:center">
                            <input type="text" class="form-input" bind:value={detailWorkingDir} style="font-size:0.8rem;flex:1">
                            <button class="btn btn-sm" on:click={saveWorkingDir}>Save</button>
                        </div>
                    </div>
                </div>
                <div>
                    <div class="detail-field">
                        <div class="detail-label">Soul</div>
                        <textarea class="form-input" bind:value={detailSoul} rows="4" placeholder="CLAUDE.md content..."></textarea>
                        <button class="btn btn-sm" style="margin-top:0.3rem" on:click={saveSoul}>Save Soul</button>
                    </div>
                    <div class="detail-field">
                        <div class="detail-label">System Prompt</div>
                        <textarea class="form-input" bind:value={detailSystemPrompt} rows="3" placeholder="Base system prompt..."></textarea>
                        <button class="btn btn-sm" style="margin-top:0.3rem" on:click={saveSystemPrompt}>Save Prompt</button>
                    </div>
                </div>
            </div>

            <!-- Directives -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem">
                    <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Directives</span>
                </div>
                <div style="display:flex;gap:0.5rem;align-items:center">
                    <input type="text" class="form-input" bind:value={newDirective} placeholder="Add directive..." style="flex:1">
                    <input type="number" class="form-input" bind:value={newDirectivePriority} placeholder="Priority" style="width:80px">
                    <button class="btn btn-primary" on:click={addDirective}>Add</button>
                </div>
            </div>
            <div>
                {#if directives.length === 0}
                    <div class="empty">No directives. Add one above.</div>
                {:else}
                    {#each directives as d}
                        <div class="directive-item" class:directive-inactive={!d.active}>
                            <span class="directive-priority">{d.priority}</span>
                            <span class="directive-text">{d.directive}</span>
                            <button class="btn btn-sm" on:click={() => toggleDirective(d.id, !d.active)}>{d.active ? 'Disable' : 'Enable'}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => removeDirective(d.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Tokens -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Bot Tokens</span>
                <div style="display:flex;gap:0.5rem;align-items:center;margin-top:0.5rem;flex-wrap:wrap">
                    <select class="form-select" bind:value={tokenPlatform}>
                        <option value="telegram">Telegram</option>
                        <option value="discord">Discord</option>
                        <option value="slack">Slack</option>
                    </select>
                    <input type="password" class="form-input" bind:value={tokenValue} placeholder="Bot token..." style="flex:1;min-width:120px">
                    <button class="btn btn-primary" on:click={setToken}>Set</button>
                </div>
            </div>
            <div>
                {#if tokens.length === 0}
                    <div class="empty">No bot tokens configured.</div>
                {:else}
                    {#each tokens as t}
                        <div class="token-item">
                            <span class="badge badge-model">{t.platform}</span>
                            <span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? 'Set' : 'Missing'}</span>
                            <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? 'Enabled' : 'Disabled'}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm btn-danger" on:click={() => removeToken(t.platform)}>Remove</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Heart Files -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Heart Files</span>
                <button class="btn btn-sm" on:click={syncClaudeMd}>Sync CLAUDE.md</button>
            </div>
            <div>
                {#each files as f}
                    <div class="token-item">
                        <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:{f.is_claude_md ? '700' : '400'}">{f.is_claude_md ? '* ' : ''}{f.name}</span>
                        <span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{(f.size / 1024).toFixed(1)}K</span>
                        <span style="flex:1"></span>
                        <button class="btn btn-sm" on:click={() => editFile(f.name)}>Edit</button>
                    </div>
                {/each}
            </div>

            <!-- File Editor -->
            {#if fileEditorOpen}
                <div style="border-top:1px solid #e2e8f0">
                    <div style="padding:0.8rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                        <span style="font-family:var(--font-mono);font-size:0.75rem;font-weight:700">{fileEditorName}</span>
                        <div style="display:flex;gap:0.3rem">
                            <button class="btn btn-sm btn-primary" on:click={saveFile}>Save</button>
                            <button class="btn btn-sm" on:click={closeFileEditor}>Close</button>
                        </div>
                    </div>
                    <textarea class="form-input" bind:value={fileEditorContent} rows="12" style="margin:0;border:none;border-top:1px solid #e2e8f0;width:100%;font-size:0.8rem"></textarea>
                </div>
            {/if}

            <!-- Schedules -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Wake Schedules</span>
                <button class="btn btn-sm btn-primary" on:click={addSchedule}>+ Schedule</button>
            </div>
            <div>
                {#if schedules.length === 0}
                    <div class="empty" style="padding:0.8rem 1.5rem;font-size:0.8rem">No schedules.</div>
                {:else}
                    {#each schedules as s}
                        <div class="token-item" style={!s.enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{s.name || 'unnamed'}</span>
                            <span style="font-family:var(--font-mono);font-size:0.75rem;color:var(--gray-mid)">{s.cron}</span>
                            <span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? 'Active' : 'Off'}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => toggleSchedule(s.id, !s.enabled)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => removeSchedule(s.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Sessions -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Active Sessions</span>
            </div>
            <div>
                {#if agentSessions.length === 0}
                    <div class="empty">No active sessions.</div>
                {:else}
                    {#each agentSessions as s}
                        {@const sType = s.session_type || 'chat'}
                        {@const typeStyle = sType === 'main' ? 'background:var(--yellow);color:var(--black)' : sType === 'worker' ? 'background:#e2e8f0;color:var(--gray-dark)' : 'background:#dbeafe;color:#1e40af'}
                        <div class="token-item">
                            <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{s.id}</span>
                            <span class="badge" style={typeStyle}>{sType}</span>
                            <span class="badge badge-{s.state === 'idle' ? 'on' : s.state === 'running' ? 'model' : 'off'}">{s.state}</span>
                            <span style="font-family:var(--font-mono);font-size:0.75rem;color:var(--gray-mid)">{s.context_used_pct}% ctx</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => window.location.hash = `/chat#${s.id}`}>Chat</button>
                        </div>
                    {/each}
                {/if}
            </div>
        </div>
    {/if}
</div>

<!-- Wizard -->
{#if wizardOpen}
    <div class="wizard-overlay">
        <div class="wizard">
            <div class="wizard-header">
                <div class="wizard-title">NEW AGENT<span class="y">.</span></div>
                <div class="wizard-sub">yer a wizard, agent</div>
            </div>
            <div class="wizard-progress">
                {#each Array(wizTotalSteps) as _, i}
                    <div class="wizard-step-dot" class:active={i === wizStep} class:done={i < wizStep}></div>
                {/each}
            </div>
            <div class="wizard-body">
                {#if wizStep === 0}
                    <div class="wizard-label">Identity</div>
                    <div class="wizard-hint">Short, lowercase, no spaces.</div>
                    <input type="text" class="wizard-input" bind:value={wizName} placeholder="e.g. oleg, leo, rex">
                    <div class="wizard-label" style="margin-top:0.5rem">Display Name</div>
                    <input type="text" class="wizard-input" bind:value={wizDisplayName} placeholder="e.g. Oleg the Magnificent">
                {:else if wizStep === 1}
                    <div class="wizard-label">Brain</div>
                    <div class="wizard-hint">Pick the thinking engine.</div>
                    <div class="wizard-options">
                        {#each [['opus','OPUS','Maximum intelligence.'],['sonnet','SONNET','Fast + smart. Daily driver.'],['haiku','HAIKU','Lightning fast. Simple tasks.']] as [val, title, desc]}
                            <div class="wizard-option" class:selected={wizModel === val} on:click={() => wizModel = val}>
                                <div class="wizard-option-title">{title}</div>
                                <div class="wizard-option-desc">{desc}</div>
                            </div>
                        {/each}
                    </div>
                {:else if wizStep === 2}
                    <div class="wizard-label">Permission Mode</div>
                    <div class="wizard-options">
                        <div class="wizard-option" class:selected={wizMode === 'auto'} on:click={() => wizMode = 'auto'}>
                            <div class="wizard-option-title">AUTO</div><div class="wizard-option-desc">Smart guardrails. Recommended.</div>
                        </div>
                        <div class="wizard-option" class:selected={wizMode === 'bypassPermissions'} on:click={() => wizMode = 'bypassPermissions'}>
                            <div class="wizard-option-title">YOLO</div><div class="wizard-option-desc">No permission checks. Full send.</div>
                        </div>
                    </div>
                {:else if wizStep === 3}
                    <div class="wizard-label">Heart Config</div>
                    <div class="wizard-hearts">
                        {#each [['worker','>_','Worker','Heads-down coder.'],['lead','[*]','Team Lead','Reviews code, coordinates.'],['sidekick','~*~','Sidekick','Personal assistant.'],['custom','{?}','Custom','Write your own.']] as [val, icon, title, desc]}
                            <div class="wizard-heart" class:selected={wizHeart === val} on:click={() => wizHeart = val}>
                                <div class="wizard-heart-icon">{icon}</div>
                                <div class="wizard-heart-name">{title}</div>
                                <div class="wizard-heart-desc">{desc}</div>
                            </div>
                        {/each}
                    </div>
                    {#if wizHeart === 'custom'}
                        <textarea class="wizard-input" bind:value={wizCustomSoul} rows="5" placeholder="Write your agent's soul..."></textarea>
                    {/if}
                {:else if wizStep === 4}
                    <div class="wizard-label">Role & Autonomy</div>
                    <div class="wizard-options">
                        {#each [['sidekick','SIDEKICK','Always-on companion.'],['lead','LEAD','Coordinates workers.'],['worker','WORKER','Task executor.'],['specialist','SPECIALIST','Domain expert.']] as [val, title, desc]}
                            <div class="wizard-option" class:selected={wizRole === val} on:click={() => { wizRole = val; wizAutoStart = (val === 'sidekick' || val === 'lead'); }}>
                                <div class="wizard-option-title">{title}</div><div class="wizard-option-desc">{desc}</div>
                            </div>
                        {/each}
                    </div>
                    <div style="margin-top:1.5rem;display:flex;gap:2rem;flex-wrap:wrap">
                        <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-mono);font-size:0.8rem;cursor:pointer">
                            <input type="checkbox" bind:checked={wizAutoStart}> Auto-start main session
                        </label>
                        <div style="display:flex;align-items:center;gap:0.5rem">
                            <span style="font-family:var(--font-mono);font-size:0.8rem">Heartbeat:</span>
                            <input type="number" class="wizard-input" bind:value={wizHeartbeatInterval} min="0" step="60" style="width:80px">
                            <span style="font-size:0.7rem;color:var(--gray-mid)">sec</span>
                        </div>
                    </div>
                {:else if wizStep === 5}
                    <div class="wizard-label">Outreach</div>
                    <div class="wizard-hint">Connect to the outside world. All optional.</div>
                    <div style="margin-bottom:1.5rem"><span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;color:var(--yellow)">TELEGRAM</span>
                        <input type="password" class="wizard-input" bind:value={wizTelegramToken} placeholder="Bot token..."></div>
                    <div style="margin-bottom:1.5rem"><span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;color:var(--yellow)">DISCORD</span>
                        <input type="password" class="wizard-input" bind:value={wizDiscordToken} placeholder="Discord bot token..."></div>
                    <div><span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;color:var(--yellow)">SLACK</span>
                        <input type="password" class="wizard-input" bind:value={wizSlackToken} placeholder="xoxb-..."></div>
                {:else if wizStep === 6}
                    <div class="wizard-label">Ready to Deploy</div>
                    <div class="wizard-summary">
                        Name: <span class="val">{wizName || '(unnamed)'}</span><br>
                        Display: <span class="val">{wizDisplayName || wizName}</span><br>
                        Brain: <span class="val">{wizModel.toUpperCase()}</span><br>
                        Mode: <span class="val">{wizMode === 'bypassPermissions' ? 'YOLO' : wizMode.toUpperCase()}</span><br>
                        Heart: <span class="val">{wizHeart.toUpperCase()}</span><br>
                        Role: <span class="val">{wizRole.toUpperCase()}</span><br>
                        Auto-Start: <span class="val">{wizAutoStart ? 'Yes' : 'No'}</span><br>
                        Heartbeat: <span class="val">{wizHeartbeatInterval ? wizHeartbeatInterval + 's' : 'Disabled'}</span><br>
                        Outreach: <span class="val">{wizSummaryPlatforms.length ? wizSummaryPlatforms.join(', ') : 'None (local only)'}</span>
                    </div>
                {/if}
            </div>
            <div class="wizard-footer">
                <button class="wizard-btn" on:click={wizardPrev} style="visibility:{wizStep === 0 ? 'hidden' : 'visible'}">Back</button>
                <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Cancel</button>
                <button class="wizard-btn wizard-btn-primary" on:click={wizardNext}>{wizStep === wizTotalSteps - 1 ? 'Summon' : 'Next'}</button>
            </div>
        </div>
    </div>
{/if}

<style>
    .agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 0; }
    .agent-card { border: var(--border); margin: -1.5px; padding: 1.5rem; }
    .agent-card:hover { background: #fefce8; }
    .agent-name { font-family: var(--font-mono); font-size: 1.1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .agent-meta { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.8rem; }
    .agent-desc { font-size: 0.85rem; color: var(--gray-mid); margin-bottom: 0.8rem; max-height: 40px; overflow: hidden; }
    .agent-stats { display: flex; gap: 1.5rem; font-family: var(--font-mono); font-size: 0.7rem; color: var(--gray-mid); margin-bottom: 0.8rem; }
    .agent-actions { display: flex; gap: 0.3rem; flex-wrap: wrap; }

    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; padding: 1.5rem; }
    .detail-field { margin-bottom: 1rem; }
    .detail-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); margin-bottom: 0.3rem; }
    .detail-value { font-family: var(--font-mono); font-size: 0.85rem; }

    .directive-item { display: flex; align-items: center; gap: 0.8rem; padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }
    .directive-item:last-child { border-bottom: none; }
    .directive-priority { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; background: var(--yellow); padding: 0.1rem 0.4rem; min-width: 24px; text-align: center; }
    .directive-text { flex: 1; font-size: 0.88rem; }
    .directive-inactive { opacity: 0.5; text-decoration: line-through; }

    .token-item { display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem; border-bottom: 1px solid #e2e8f0; }
    .token-item:last-child { border-bottom: none; }

    .wizard-overlay { position: fixed; inset: 0; background: rgba(30,41,59,0.85); z-index: 999; display: flex; align-items: center; justify-content: center; }
    .wizard { background: var(--black); color: var(--white); border: var(--border); max-width: 600px; width: 95%; max-height: 90vh; overflow-y: auto; }
    .wizard-header { padding: 2rem 2rem 1rem; }
    .wizard-title { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 700; }
    .wizard-title .y { color: var(--yellow); }
    .wizard-sub { font-family: var(--font-mono); font-size: 0.75rem; color: var(--gray-mid); margin-top: 0.3rem; }
    .wizard-progress { display: flex; gap: 0; padding: 0 2rem; margin-bottom: 1.5rem; }
    .wizard-step-dot { flex: 1; height: 4px; background: var(--gray-dark); }
    .wizard-step-dot.active { background: var(--yellow); }
    .wizard-step-dot.done { background: var(--green); }
    .wizard-body { padding: 0 2rem 2rem; }
    .wizard-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--yellow); margin-bottom: 0.5rem; }
    .wizard-hint { font-size: 0.8rem; color: var(--gray-mid); margin-bottom: 1rem; }
    .wizard-input { font-family: var(--font-mono); font-size: 1rem; padding: 0.8rem 1rem; border: 2px solid var(--gray-dark); background: transparent; color: var(--white); width: 100%; margin-bottom: 1rem; }
    .wizard-input:focus { outline: none; border-color: var(--yellow); }
    .wizard-options { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-option { padding: 1rem; border: 2px solid var(--gray-dark); cursor: pointer; text-align: center; transition: all 0.15s; }
    .wizard-option:hover { border-color: var(--yellow); }
    .wizard-option.selected { border-color: var(--yellow); background: rgba(255,230,0,0.1); }
    .wizard-option-title { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; margin-bottom: 0.2rem; }
    .wizard-option-desc { font-size: 0.75rem; color: var(--gray-mid); }
    .wizard-option.selected .wizard-option-desc { color: var(--yellow); }
    .wizard-hearts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-heart { padding: 1.2rem; border: 2px solid var(--gray-dark); cursor: pointer; transition: all 0.15s; }
    .wizard-heart:hover { border-color: var(--yellow); }
    .wizard-heart.selected { border-color: var(--yellow); background: rgba(255,230,0,0.1); }
    .wizard-heart-icon { font-size: 1.5rem; margin-bottom: 0.3rem; }
    .wizard-heart-name { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; }
    .wizard-heart-desc { font-size: 0.7rem; color: var(--gray-mid); margin-top: 0.2rem; }
    .wizard-heart.selected .wizard-heart-desc { color: var(--yellow); }
    .wizard-footer { display: flex; justify-content: space-between; padding: 1.5rem 2rem; border-top: 2px solid var(--gray-dark); }
    .wizard-btn { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; padding: 0.6rem 1.5rem; border: 2px solid var(--gray-dark); background: transparent; color: var(--white); cursor: pointer; text-transform: uppercase; }
    .wizard-btn:hover { border-color: var(--yellow); color: var(--yellow); }
    .wizard-btn-primary { background: var(--yellow); color: var(--black); border-color: var(--yellow); }
    .wizard-btn-primary:hover { background: var(--white); }
    .wizard-summary { font-family: var(--font-mono); font-size: 0.85rem; line-height: 2; }
    .wizard-summary :global(.val) { color: var(--yellow); font-weight: 700; }
    .val { color: var(--yellow); font-weight: 700; }

    @media (max-width: 900px) {
        .agent-grid { grid-template-columns: 1fr; }
        .detail-grid { grid-template-columns: 1fr; padding: 1rem; }
        .detail-value { word-break: break-all; }
    }
</style>
