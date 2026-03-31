<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';
    import { buildSoul } from '../lib/soulTemplates.js';

    let agentList = [];
    let agentCount = 0;
    let retiredList = [];
    let retiredCount = 0;
    let currentAgent = '';
    let refreshInterval;

    // Retire modal state
    let retireModalOpen = false;
    let pendingRetireAgent = '';
    let retireConfirmInput = '';

    // Detail panel state
    let detailOpen = false;
    let detailName = '';
    let detailModel = '--';
    let detailPermission = '--';
    let detailMaxSessions = '--';
    let detailGroups = '--';
    let detailWorkingDir = '';
    let detailSoul = '';
    let detailUsers = '';
    let detailBoundaries = '';
    let directives = [];
    let tokens = [];
    let files = [];
    let schedules = [];
    let agentSessions = [];
    let streamingSessions = [];
    let channelSessions = {};
    let editingFile = '';
    let fileEditorOpen = false;
    let fileEditorName = '';
    let fileEditorContent = '';
    let newDirective = '';
    let newDirectivePriority = 0;
    let tokenPlatform = 'telegram';
    let tokenValue = '';

    // Voice config state
    let voiceReply = false;
    let ttsProvider = 'openai';
    let ttsVoice = '';
    let ttsModel = '';
    let transcribeProvider = 'openai';
    let voiceDirty = false;

    // Cron modal state
    let cronModalOpen = false;
    let cronName = '';
    let cronExpression = '';
    let cronPrompt = '';

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

    // Soul templates are in src/lib/soulTemplates.js — buildSoul() handles all heart types.

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
        try {
            const retired = await api('GET', '/agents/retired');
            retiredList = retired.agents || [];
            retiredCount = retired.count;
        } catch (e) {
            retiredList = [];
            retiredCount = 0;
        }
    }

    function openRetireModal(name) {
        pendingRetireAgent = name;
        retireConfirmInput = '';
        retireModalOpen = true;
    }

    function closeRetireModal() {
        retireModalOpen = false;
        pendingRetireAgent = '';
        retireConfirmInput = '';
    }

    async function confirmRetire() {
        if (!pendingRetireAgent || retireConfirmInput !== pendingRetireAgent) return;
        const name = pendingRetireAgent;
        closeRetireModal();
        await api('DELETE', `/agents/${name}`);
        toast(`${name} retired`);
        if (currentAgent === name) closeDetail();
        refreshAgents();
    }

    async function restoreAgent(name) {
        await api('POST', `/agents/${name}/restore`);
        toast(`${name} restored`);
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
        detailUsers = agent.users || '';
        detailBoundaries = agent.boundaries || '';
        const vc = agent.voice_config || {};
        voiceReply = vc.voice_reply || false;
        ttsProvider = vc.tts_provider || 'openai';
        ttsVoice = vc.tts_voice || '';
        ttsModel = vc.tts_model || '';
        transcribeProvider = vc.transcribe_provider || 'openai';
        voiceDirty = false;
        detailOpen = true;
        loadDirectives();
        loadTokens();
        loadFiles();
        loadSchedules();
        loadSessions();
        loadStreamingSessions();
        loadChannelSessions();
        loadApprovedUsers();
        loadPendingMessages();
        loadGroupChats();
    }

    function closeDetail() { currentAgent = ''; detailOpen = false; }

    async function saveSoul() { await api('PUT', `/agents/${currentAgent}`, { soul: detailSoul }); toast('Soul saved'); }
    async function saveUsers() { await api('PUT', `/agents/${currentAgent}`, { users: detailUsers }); toast('Users saved'); }
    async function saveBoundaries() { await api('PUT', `/agents/${currentAgent}`, { boundaries: detailBoundaries }); toast('Boundaries saved'); }
    async function saveVoiceConfig() {
        const vc = { voice_reply: voiceReply, tts_provider: ttsProvider, tts_voice: ttsVoice, tts_model: ttsModel, transcribe_provider: transcribeProvider };
        await api('PUT', `/agents/${currentAgent}`, { voice_config: vc });
        voiceDirty = false;
        toast('Voice config saved');
    }
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
    function closeCronModal() { cronModalOpen = false; cronName = ''; cronExpression = ''; cronPrompt = ''; }
    async function submitCronJob() {
        if (!cronName || !cronExpression) return;
        await api('POST', `/agents/${currentAgent}/schedules`, { name: cronName, cron: cronExpression, prompt: cronPrompt || `Scheduled wake: ${cronName}` });
        toast(`Cron job "${cronName}" added`);
        closeCronModal();
        loadSchedules();
    }
    async function toggleSchedule(id, enabled) { await api('POST', `/agents/${currentAgent}/schedules/${id}/toggle?enabled=${enabled}`); loadSchedules(); }
    async function removeSchedule(id) { if (!confirm('Remove this schedule?')) return; await api('DELETE', `/agents/${currentAgent}/schedules/${id}`); toast('Schedule removed'); loadSchedules(); }

    async function loadSessions() { const data = await api('GET', `/agents/${currentAgent}/sessions`); agentSessions = data.sessions || []; }

    async function loadStreamingSessions() {
        const data = await api('GET', `/agents/${currentAgent}/streaming-sessions`);
        streamingSessions = data.sessions || [];
    }
    async function loadChannelSessions() {
        const data = await api('GET', `/agents/${currentAgent}/channel-sessions`);
        channelSessions = {};
        for (const m of (data.mappings || [])) {
            channelSessions[m.chat_id] = m.session_label;
        }
    }
    async function setChannelSession(chatId, label) {
        await api('PUT', `/agents/${currentAgent}/channel-sessions/${encodeURIComponent(chatId)}?session_label=${encodeURIComponent(label)}`);
        toast(`Channel assigned to ${label}`);
        loadChannelSessions();
    }
    async function createStreamingSession() {
        const label = prompt('Session label (e.g. "group-chat"):');
        if (!label || !label.trim()) return;
        try {
            await api('POST', `/agents/${currentAgent}/streaming-sessions?label=${encodeURIComponent(label.trim())}`);
            toast(`Streaming session "${label.trim()}" created`);
            loadStreamingSessions();
        } catch (e) { toast(e.message || 'Failed to create session', 'error'); }
    }
    async function deleteStreamingSession(label) {
        if (!confirm(`Delete streaming session "${label}"?`)) return;
        await api('DELETE', `/agents/${currentAgent}/streaming-sessions/${encodeURIComponent(label)}`);
        toast(`Streaming session "${label}" deleted`);
        loadStreamingSessions();
    }

    // Approved users
    let approvedUsers = [];
    let newUserChatId = '';
    let newUserName = '';
    async function loadApprovedUsers() { const data = await api('GET', `/agents/${currentAgent}/approved-users`); approvedUsers = data.users || []; }
    async function approveUser() {
        if (!newUserChatId.trim()) { toast('Enter a chat ID', 'error'); return; }
        await api('POST', `/agents/${currentAgent}/approved-users`, { chat_id: newUserChatId.trim(), display_name: newUserName.trim() });
        toast(`User ${newUserName || newUserChatId} approved`);
        newUserChatId = ''; newUserName = '';
        loadApprovedUsers();
    }
    async function denyUser(chatId) { await api('PUT', `/agents/${currentAgent}/approved-users/${chatId}/deny`); toast('User denied'); loadApprovedUsers(); loadPendingMessages(); }
    async function revokeUser(chatId) { await api('DELETE', `/agents/${currentAgent}/approved-users/${chatId}`); toast('User revoked'); loadApprovedUsers(); }

    // Pending messages (broker)
    let pendingMessages = {};
    let pendingUserCount = 0;
    let pendingTotalCount = 0;
    async function loadPendingMessages() {
        const data = await api('GET', `/agents/${currentAgent}/pending-messages`);
        pendingMessages = data.by_sender || {};
        pendingUserCount = data.pending_users || 0;
        pendingTotalCount = data.total_messages || 0;
    }
    async function approveAndDeliver(chatId, displayName) {
        await api('POST', `/agents/${currentAgent}/approved-users`, { chat_id: chatId, display_name: displayName || '' });
        toast(`User ${displayName || chatId} approved — delivering messages`);
        loadApprovedUsers();
        loadPendingMessages();
    }
    async function denyPendingUser(chatId) {
        await api('PUT', `/agents/${currentAgent}/approved-users/${chatId}/deny`);
        await api('DELETE', `/agents/${currentAgent}/pending-messages/${chatId}`);
        toast('User denied, messages discarded');
        loadApprovedUsers();
        loadPendingMessages();
    }

    // Group chats (broker)
    let groupChats = [];
    async function loadGroupChats() {
        const data = await api('GET', `/agents/${currentAgent}/group-chats`);
        groupChats = data.group_chats || [];
    }
    async function setGroupAlias(chatId, alias) {
        await api('PUT', `/agents/${currentAgent}/group-chats/${chatId}?alias=${encodeURIComponent(alias)}`);
        toast('Alias updated');
        loadGroupChats();
    }
    async function deactivateGroup(chatId) {
        await api('DELETE', `/agents/${currentAgent}/group-chats/${chatId}`);
        toast('Group deactivated');
        loadGroupChats();
    }

    // Wizard
    let wizPronouns = '';
    function openWizard() { wizStep = 0; wizName = ''; wizDisplayName = ''; wizPronouns = ''; wizModel = 'opus'; wizMode = 'bypassPermissions'; wizHeart = 'worker'; wizRole = 'sidekick'; wizAutoStart = true; wizHeartbeatInterval = 300; wizCustomSoul = ''; wizTelegramToken = ''; wizDiscordToken = ''; wizSlackToken = ''; wizardOpen = true; }
    function closeWizard() { wizardOpen = false; }

    function wizardPrev() { if (wizStep > 0) wizStep--; }
    async function wizardNext() {
        if (wizStep === 0) {
            if (!wizName.trim()) { toast('Give your agent a name', 'error'); return; }
            if (!/^[a-z0-9_-]+$/.test(wizName)) { toast('Lowercase letters, numbers, hyphens only', 'error'); return; }
        }
        if (wizStep < wizTotalSteps - 1) { wizStep++; return; }
        // Summon!
        let soul = buildSoul(wizHeart, {
            name: wizName,
            displayName: wizDisplayName,
            pronouns: wizPronouns,
            model: wizModel,
            mode: wizMode,
            role: wizRole,
            autoStart: wizAutoStart,
            heartbeatInterval: wizHeartbeatInterval,
            customSoul: wizCustomSoul,
            hasTelegram: !!wizTelegramToken,
            hasDiscord: !!wizDiscordToken,
            hasSlack: !!wizSlackToken,
        });
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
                                {#if a.role}<span class="badge" style="background:var(--surface-inverse);color:var(--accent)">{a.role}</span>{/if}
                                <span class="badge badge-model">{a.model}</span>
                                <span class="badge badge-{a.enabled ? 'on' : 'off'}">{a.enabled ? 'Active' : 'Disabled'}</span>
                                {#if a.auto_start}<span class="badge" style="background:#dcfce7;color:#166534">Auto-Start</span>{/if}
                                <span class="badge" style="background:var(--tone-neutral-bg);color:var(--tone-neutral-text)">{a.permission_mode === 'bypassPermissions' ? 'YOLO' : a.permission_mode || 'default'}</span>
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
                                <button class="btn-danger-text" on:click={() => openRetireModal(a.name)}>retire</button>
                            </div>
                        </div>
                    {/each}
                </div>
            {/if}
        </div>
    </div>

    <!-- Retired Agents -->
    {#if retiredCount > 0}
        <div class="section">
            <div class="section-header">
                <div class="section-title" style="color:var(--gray-mid)">Retired <span style="font-weight:400">({retiredCount})</span></div>
            </div>
            <div class="section-body">
                <div class="agent-grid">
                    {#each retiredList as a}
                        <div class="agent-card" style="opacity:0.6;border-style:dashed">
                            <div class="agent-name">{a.display_name || a.name}</div>
                            <div class="agent-meta">
                                <span class="badge" style="background:var(--tone-error-bg);color:var(--tone-error-text)">Retired</span>
                                <span class="badge badge-model">{a.model}</span>
                                {#if a.role}<span class="badge" style="background:var(--gray-light);color:var(--gray-dark)">{a.role}</span>{/if}
                            </div>
                            <div class="agent-stats">
                                <span>retired {timeAgo(a.retired_at)}</span>
                                <span>created {timeAgo(a.created_at)}</span>
                            </div>
                            <div class="agent-actions">
                                <button class="btn btn-sm btn-success" on:click={() => restoreAgent(a.name)}>Restore</button>
                            </div>
                        </div>
                    {/each}
                </div>
            </div>
        </div>
    {/if}

    <!-- Retire Confirmation Modal -->
    {#if retireModalOpen}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <div class="delete-modal-overlay active" on:click|self={closeRetireModal}>
            <div class="delete-modal">
                <h3>Retire Agent</h3>
                <p>This will retire <strong style="color:var(--red)">{pendingRetireAgent}</strong> and disable all its sessions. The agent's data will be preserved and can be restored later.</p>
                <p>Type the agent name to confirm:</p>
                <input type="text" bind:value={retireConfirmInput} autocomplete="off" spellcheck="false" placeholder="agent name">
                <div class="modal-actions">
                    <button class="btn btn-sm" on:click={closeRetireModal}>Cancel</button>
                    <button class="btn btn-sm btn-confirm-delete" class:ready={retireConfirmInput === pendingRetireAgent} disabled={retireConfirmInput !== pendingRetireAgent} on:click={confirmRetire}>Retire</button>
                </div>
            </div>
        </div>
    {/if}

    <!-- Cron Job Modal -->
    {#if cronModalOpen}
        <!-- svelte-ignore a11y-click-events-have-key-events -->
        <div class="delete-modal-overlay active" on:click|self={closeCronModal}>
            <div class="delete-modal">
                <h3>New Cron Job</h3>
                <p style="font-size:0.8rem;color:var(--gray-dark);margin-bottom:0.5rem">Schedule a recurring task for this agent.</p>
                <label class="cron-label">Name</label>
                <input type="text" bind:value={cronName} placeholder="e.g. morning_check" autocomplete="off">
                <label class="cron-label">Cron Expression</label>
                <input type="text" bind:value={cronExpression} placeholder="e.g. 0 8 * * *">
                <p style="font-size:0.7rem;color:var(--gray-mid);margin-top:-0.5rem;margin-bottom:0.8rem">min hour day month weekday — <a href="https://crontab.guru" target="_blank" style="color:var(--gray-dark)">crontab.guru</a></p>
                <label class="cron-label">Prompt</label>
                <textarea bind:value={cronPrompt} placeholder="Message sent to the agent when this job fires..." rows="3" style="width:100%;padding:0.5rem;border:var(--border);font-family:var(--font-mono);font-size:0.8rem;margin-bottom:1rem;resize:vertical"></textarea>
                <div class="modal-actions">
                    <button class="btn btn-sm" on:click={closeCronModal}>Cancel</button>
                    <button class="btn btn-sm btn-primary" disabled={!cronName || !cronExpression} on:click={submitCronJob}>Create</button>
                </div>
            </div>
        </div>
    {/if}

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
                        <textarea class="form-input" bind:value={detailSoul} rows="4" placeholder="Core identity, personality, purpose..."></textarea>
                        <button class="btn btn-sm" style="margin-top:0.3rem" on:click={saveSoul}>Save Soul</button>
                    </div>
                    <div class="detail-field">
                        <div class="detail-label">Users</div>
                        <textarea class="form-input" bind:value={detailUsers} rows="3" placeholder="Who this agent serves, user profiles..."></textarea>
                        <button class="btn btn-sm" style="margin-top:0.3rem" on:click={saveUsers}>Save Users</button>
                    </div>
                    <div class="detail-field">
                        <div class="detail-label">Boundaries</div>
                        <textarea class="form-input" bind:value={detailBoundaries} rows="3" placeholder="Rules, constraints, what to avoid..."></textarea>
                        <button class="btn btn-sm" style="margin-top:0.3rem" on:click={saveBoundaries}>Save Boundaries</button>
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

            <!-- Voice Config -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <div style="display:flex;justify-content:space-between;align-items:center">
                    <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Voice</span>
                    {#if voiceDirty}<button class="btn btn-sm btn-primary" on:click={saveVoiceConfig}>Save</button>{/if}
                </div>
                <div style="margin-top:0.8rem;display:flex;flex-direction:column;gap:0.8rem">
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-mono);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={voiceReply} on:change={() => voiceDirty = true}> Auto-reply to voice messages with TTS
                    </label>
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-mono);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">TTS Provider</div>
                            <select class="form-select" bind:value={ttsProvider} on:change={() => voiceDirty = true} style="width:100%">
                                <option value="openai">OpenAI</option>
                                <option value="elevenlabs">ElevenLabs</option>
                                <option value="deepgram">Deepgram</option>
                            </select>
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-mono);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Voice</div>
                            <input type="text" class="form-input" bind:value={ttsVoice} on:input={() => voiceDirty = true} placeholder={ttsProvider === 'openai' ? 'alloy, nova, shimmer...' : ttsProvider === 'elevenlabs' ? 'Voice ID' : 'aura-asteria-en'} style="width:100%">
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-mono);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Model <span style="font-weight:400;text-transform:none">(optional)</span></div>
                            <input type="text" class="form-input" bind:value={ttsModel} on:input={() => voiceDirty = true} placeholder={ttsProvider === 'openai' ? 'tts-1, tts-1-hd' : ttsProvider === 'elevenlabs' ? 'eleven_multilingual_v2' : ''} style="width:100%">
                        </div>
                    </div>
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="min-width:140px">
                            <div style="font-family:var(--font-mono);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Transcription Provider</div>
                            <select class="form-select" bind:value={transcribeProvider} on:change={() => voiceDirty = true}>
                                <option value="openai">OpenAI Whisper</option>
                                <option value="deepgram">Deepgram Nova</option>
                            </select>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Approved Users -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Approved Users</span>
                <div style="display:flex;gap:0.5rem;align-items:center;margin-top:0.5rem;flex-wrap:wrap">
                    <input type="text" class="form-input" bind:value={newUserChatId} placeholder="Chat ID" style="width:130px">
                    <input type="text" class="form-input" bind:value={newUserName} placeholder="Display name (optional)" style="flex:1;min-width:120px">
                    <button class="btn btn-primary" on:click={approveUser}>Approve</button>
                </div>
            </div>
            <div>
                {#if approvedUsers.length === 0}
                    <div class="empty">No approved users.</div>
                {:else}
                    {#each approvedUsers as u}
                        <div class="token-item">
                            <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{u.display_name || u.chat_id}</span>
                            {#if u.display_name}<span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{u.chat_id}</span>{/if}
                            <span class="badge badge-{u.status === 'approved' ? 'on' : u.status === 'denied' ? 'off' : 'model'}">{u.status}</span>
                            {#if u.timezone}<span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{u.timezone}</span>{/if}
                            {#if streamingSessions.length > 1}
                            <select style="font-family:var(--font-mono);font-size:0.75rem;padding:0.15rem 0.3rem;border:var(--border);border-radius:4px;background:var(--white)"
                                value={channelSessions[u.chat_id] || 'main'}
                                on:change={(e) => setChannelSession(u.chat_id, e.target.value)}>
                                <option value="main">main</option>
                                {#each streamingSessions.filter(s => s.label !== 'main') as ss}
                                    <option value={ss.label}>{ss.label}</option>
                                {/each}
                            </select>
                            {/if}
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => { const tz = prompt('Timezone (IANA):', u.timezone || 'America/Los_Angeles'); if (tz !== null) { api('PUT', `/agents/${currentAgent}/approved-users/${u.chat_id}/timezone?timezone=${encodeURIComponent(tz)}`).then(() => { toast('Timezone set'); loadApprovedUsers(); }); } }}>TZ</button>
                            {#if u.status === 'denied'}
                                <button class="btn btn-sm btn-success" on:click={() => { api('POST', `/agents/${currentAgent}/approved-users`, { chat_id: u.chat_id, display_name: u.display_name }).then(() => { toast('User approved'); loadApprovedUsers(); }); }}>Approve</button>
                            {:else}
                                <button class="btn btn-sm" on:click={() => denyUser(u.chat_id)}>Deny</button>
                            {/if}
                            <button class="btn btn-sm btn-danger" on:click={() => revokeUser(u.chat_id)}>Revoke</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Pending Approvals -->
            {#if pendingUserCount > 0}
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--pending-bg)">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Pending Approvals</span>
                <span class="badge badge-model" style="margin-left:0.5rem">{pendingUserCount}</span>
            </div>
            <div>
                {#each Object.entries(pendingMessages) as [chatId, msgs]}
                    <div class="token-item" style="flex-direction:column;align-items:flex-start;gap:0.5rem">
                        <div style="display:flex;width:100%;align-items:center;gap:0.5rem">
                            <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{msgs[0]?.sender_name || chatId}</span>
                            <span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{chatId}</span>
                            <span class="badge badge-model">{msgs.length} msg{msgs.length > 1 ? 's' : ''}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm btn-success" on:click={() => approveAndDeliver(chatId, msgs[0]?.sender_name)}>Approve</button>
                            <button class="btn btn-sm btn-danger" on:click={() => denyPendingUser(chatId)}>Deny</button>
                        </div>
                        <div style="font-family:var(--font-mono);font-size:0.75rem;color:var(--gray-mid);padding-left:0.5rem;max-height:3rem;overflow:hidden">
                            {msgs[0]?.content?.slice(0, 150)}{msgs[0]?.content?.length > 150 ? '...' : ''}
                        </div>
                    </div>
                {/each}
            </div>
            {/if}

            <!-- Group Chats -->
            {#if groupChats.length > 0}
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light)">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Group Chats</span>
                <span class="badge" style="margin-left:0.5rem">{groupChats.length}</span>
            </div>
            <div>
                {#each groupChats as gc}
                    <div class="token-item">
                        <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{gc.alias || gc.chat_title || gc.chat_id}</span>
                        {#if gc.alias}<span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_title}</span>{/if}
                        <span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_type}</span>
                        {#if gc.member_count > 0}<span style="font-family:var(--font-mono);font-size:0.7rem;color:var(--gray-mid)">{gc.member_count} members</span>{/if}
                        <select style="font-family:var(--font-mono);font-size:0.75rem;padding:0.15rem 0.3rem;border:var(--border);border-radius:4px;background:var(--white)"
                            value={channelSessions[gc.chat_id] || 'main'}
                            on:change={(e) => setChannelSession(gc.chat_id, e.target.value)}>
                            <option value="main">main</option>
                            {#each streamingSessions.filter(s => s.label !== 'main') as ss}
                                <option value={ss.label}>{ss.label}</option>
                            {/each}
                        </select>
                        <span style="flex:1"></span>
                        <button class="btn btn-sm" on:click={() => { const alias = prompt('Set alias:', gc.alias || ''); if (alias !== null) setGroupAlias(gc.chat_id, alias); }}>Alias</button>
                        <button class="btn btn-sm btn-danger" on:click={() => deactivateGroup(gc.chat_id)}>Leave</button>
                    </div>
                {/each}
            </div>
            {/if}

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
                <div style="border-top:1px solid var(--row-divider)">
                    <div style="padding:0.8rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                        <span style="font-family:var(--font-mono);font-size:0.75rem;font-weight:700">{fileEditorName}</span>
                        <div style="display:flex;gap:0.3rem">
                            <button class="btn btn-sm btn-primary" on:click={saveFile}>Save</button>
                            <button class="btn btn-sm" on:click={closeFileEditor}>Close</button>
                        </div>
                    </div>
                    <textarea class="form-input" bind:value={fileEditorContent} rows="12" style="margin:0;border:none;border-top:1px solid var(--row-divider);width:100%;font-size:0.8rem"></textarea>
                </div>
            {/if}

            <!-- Schedules -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Cron Jobs</span>
                <button class="btn btn-sm btn-primary" on:click={() => cronModalOpen = true}>+ Cron Job</button>
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

            <!-- Streaming Sessions -->
            <div style="border-top:var(--border);padding:1rem 1.5rem;background:var(--gray-light);display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700;text-transform:uppercase">Streaming Sessions</span>
                <button class="btn btn-sm btn-primary" on:click={createStreamingSession}>+ Session</button>
            </div>
            <div>
                {#if streamingSessions.length === 0}
                    <div class="empty">No streaming sessions.</div>
                {:else}
                    {#each streamingSessions as ss}
                        <div class="token-item">
                            <span style="font-family:var(--font-mono);font-size:0.8rem;font-weight:700">{ss.label}</span>
                            <span class="badge badge-{ss.connected ? 'on' : 'off'}">{ss.connected ? 'connected' : 'disconnected'}</span>
                            {#if ss.stats?.pending_responses > 0}<span class="badge" style="background:#fef3c7;color:#92400e">{ss.stats.pending_responses} pending</span>{/if}
                            <span style="flex:1"></span>
                            {#if ss.label !== 'main'}<button class="btn btn-sm btn-danger" on:click={() => deleteStreamingSession(ss.label)}>X</button>{/if}
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
                        {@const typeStyle = sType === 'main' ? 'background:var(--accent);color:var(--accent-contrast)' : sType === 'worker' ? 'background:var(--tone-neutral-bg);color:var(--tone-neutral-text)' : 'background:var(--tone-info-bg);color:var(--tone-info-text)'}
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
                    <div class="wizard-label">Name</div>
                    <div class="wizard-hint">What your agent goes by.</div>
                    <input type="text" class="wizard-input" bind:value={wizDisplayName} on:input={() => { wizName = wizDisplayName.toLowerCase().replace(/\s+/g, '-').replace(/[^a-z0-9_-]/g, ''); }} placeholder="e.g. Oleg, Rex, Barsik">
                    {#if wizDisplayName}<div class="wizard-id-preview">ID: {wizName}</div>{/if}
                    <div class="wizard-label" style="margin-top:0.5rem">Pronouns <span style="color:var(--gray-mid);font-weight:400;text-transform:none">(optional)</span></div>
                    <input type="text" class="wizard-input" bind:value={wizPronouns} placeholder="e.g. he/him, she/her, they/them">
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
    .agent-card { border: var(--border); margin: -1.5px; padding: 1.5rem; background: var(--surface-1); }
    .agent-card:hover { background: var(--hover-accent); }
    .agent-name { font-family: var(--font-mono); font-size: 1.1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .agent-meta { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.8rem; }
    .agent-desc { font-size: 0.85rem; color: var(--gray-mid); margin-bottom: 0.8rem; max-height: 40px; overflow: hidden; }
    .agent-stats { display: flex; gap: 1.5rem; font-family: var(--font-mono); font-size: 0.7rem; color: var(--gray-mid); margin-bottom: 0.8rem; }
    .agent-actions { display: flex; gap: 0.3rem; flex-wrap: wrap; }

    .detail-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; padding: 1.5rem; }
    .detail-field { margin-bottom: 1rem; }
    .detail-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); margin-bottom: 0.3rem; }
    .detail-value { font-family: var(--font-mono); font-size: 0.85rem; }

    .directive-item { display: flex; align-items: center; gap: 0.8rem; padding: 0.6rem 1rem; border-bottom: 1px solid var(--row-divider); }
    .directive-item:last-child { border-bottom: none; }
    .directive-priority { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; background: var(--yellow); padding: 0.1rem 0.4rem; min-width: 24px; text-align: center; }
    .directive-text { flex: 1; font-size: 0.88rem; }
    .directive-inactive { opacity: 0.5; text-decoration: line-through; }

    .token-item { display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem; border-bottom: 1px solid var(--row-divider); }
    .token-item:last-child { border-bottom: none; }

    .wizard-overlay { position: fixed; inset: 0; background: var(--overlay-scrim); z-index: 999; display: flex; align-items: center; justify-content: center; }
    .wizard { background: var(--surface-inverse); color: var(--text-inverse); border: var(--border); max-width: 600px; width: 95%; max-height: 90vh; overflow-y: auto; }
    .wizard-header { padding: 2rem 2rem 1rem; }
    .wizard-title { font-family: var(--font-mono); font-size: 1.5rem; font-weight: 700; }
    .wizard-title .y { color: var(--yellow); }
    .wizard-sub { font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); margin-top: 0.3rem; }
    .wizard-progress { display: flex; gap: 0; padding: 0 2rem; margin-bottom: 1.5rem; }
    .wizard-step-dot { flex: 1; height: 4px; background: var(--text-muted); }
    .wizard-step-dot.active { background: var(--yellow); }
    .wizard-step-dot.done { background: var(--green); }
    .wizard-body { padding: 0 2rem 2rem; }
    .wizard-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--yellow); margin-bottom: 0.5rem; }
    .wizard-hint { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; }
    .wizard-id-preview { font-family: var(--font-mono); font-size: 0.7rem; color: var(--text-muted); margin-top: -0.7rem; margin-bottom: 0.8rem; }
    .wizard-input { font-family: var(--font-mono); font-size: 1rem; padding: 0.8rem 1rem; border: 2px solid var(--border-color); background: rgba(255,255,255,0.04); color: var(--text-inverse); width: 100%; margin-bottom: 1rem; }
    .wizard-input:focus { outline: none; border-color: var(--accent); }
    .wizard-options { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-option { padding: 1rem; border: 2px solid var(--border-color); cursor: pointer; text-align: center; transition: all 0.15s; }
    .wizard-option:hover { border-color: var(--accent); }
    .wizard-option.selected { border-color: var(--accent); background: var(--accent-soft); }
    .wizard-option-title { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; margin-bottom: 0.2rem; }
    .wizard-option-desc { font-size: 0.75rem; color: var(--text-muted); }
    .wizard-option.selected .wizard-option-desc { color: var(--accent); }
    .wizard-hearts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-heart { padding: 1.2rem; border: 2px solid var(--border-color); cursor: pointer; transition: all 0.15s; }
    .wizard-heart:hover { border-color: var(--accent); }
    .wizard-heart.selected { border-color: var(--accent); background: var(--accent-soft); }
    .wizard-heart-icon { font-size: 1.5rem; margin-bottom: 0.3rem; }
    .wizard-heart-name { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; }
    .wizard-heart-desc { font-size: 0.7rem; color: var(--text-muted); margin-top: 0.2rem; }
    .wizard-heart.selected .wizard-heart-desc { color: var(--accent); }
    .wizard-footer { display: flex; justify-content: space-between; padding: 1.5rem 2rem; border-top: 2px solid var(--border-color); }
    .wizard-btn { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; padding: 0.6rem 1.5rem; border: 2px solid var(--border-color); background: transparent; color: var(--text-inverse); cursor: pointer; text-transform: uppercase; }
    .wizard-btn:hover { border-color: var(--accent); color: var(--accent); }
    .wizard-btn-primary { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }
    .wizard-btn-primary:hover { background: var(--surface-1); }
    .wizard-summary { font-family: var(--font-mono); font-size: 0.85rem; line-height: 2; }
    .wizard-summary :global(.val) { color: var(--accent); font-weight: 700; }
    .val { color: var(--yellow); font-weight: 700; }

    .btn-danger-text { background: none; border: none; color: var(--text-muted); font-size: 0.6rem; cursor: pointer; padding: 0.2rem 0.4rem; font-family: var(--font-mono); text-transform: uppercase; letter-spacing: 0.05em; }
    .btn-danger-text:hover { color: var(--red); }

    .delete-modal-overlay { position: fixed; inset: 0; background: var(--overlay-scrim); z-index: 1000; display: flex; align-items: center; justify-content: center; }
    .delete-modal { background: var(--surface-1); border: var(--border); padding: 1.5rem; max-width: 400px; width: 90%; }
    .delete-modal h3 { font-family: var(--font-mono); font-size: 0.9rem; margin-bottom: 0.75rem; text-transform: uppercase; }
    .delete-modal p { font-size: 0.8rem; color: var(--text-secondary); margin-bottom: 1rem; line-height: 1.4; }
    .delete-modal input { width: 100%; padding: 0.5rem; border: var(--border); font-family: var(--font-mono); font-size: 0.8rem; margin-bottom: 1rem; background: var(--input-bg); color: var(--text-primary); }
    .modal-actions { display: flex; gap: 0.5rem; justify-content: flex-end; }
    .cron-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); margin-bottom: 0.3rem; display: block; }
    .btn-confirm-delete { background: var(--gray-light); color: var(--gray-mid); border: 2px solid var(--gray-light); cursor: not-allowed; }
    .btn-confirm-delete.ready { background: var(--red); color: var(--white); border-color: var(--red); cursor: pointer; }

    @media (max-width: 900px) {
        .agent-grid { grid-template-columns: 1fr; }
        .detail-grid { grid-template-columns: 1fr; padding: 1rem; }
        .detail-value { word-break: break-all; }
    }
</style>
