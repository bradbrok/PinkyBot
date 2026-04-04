<script>
    import { onMount, onDestroy } from 'svelte';
    import Modal from '../components/Modal.svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';
    import { buildSoul } from '../lib/soulTemplates.js';

    let agentList = [];
    let agentCount = 0;
    let retiredList = [];
    let retiredCount = 0;
    let currentAgent = '';
    let mainAgent = '';
    let refreshInterval;
    let heartbeats = {};

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
    let claudeMdContent = '';
    let claudeMdOriginal = '';
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

    // Dream config state
    let dreamEnabled = false;
    let dreamSchedule = '0 3 * * *';
    let dreamTimezone = 'America/Los_Angeles';
    let dreamModel = '';
    let dreamNotify = true;
    let dreamDirty = false;

    // Model provider state
    let providerUrl = '';
    let providerKey = '';
    let providerModel = '';
    let providerPreset = 'anthropic'; // 'anthropic' | 'ollama' | 'zai' | 'openrouter' | 'deepseek' | 'custom'
    let providerRef = '';  // ID of a global provider (empty = use agent-specific config)
    let providerDirty = false;
    let globalProviders = [];

    // Agent skills state
    let agentSkills = [];
    let availableSkills = [];
    let skillCategoryFilter = '';
    let skillsPendingApply = false;
    let createSkillOpen = false;
    let newSkillMd = `---\nname: my-skill\ndescription: What this skill does and when to use it.\n---\n\n# My Skill\n\nInstructions for the agent go here.\n`;
    let showCoreSkills = false;
    let gitSkillUrl = '';
    let gitSkillLoading = false;
    $: visibleSkills = agentSkills.filter(s => showCoreSkills || s.category !== 'core');
    $: coreSkillCount = agentSkills.filter(s => s.category === 'core').length;
    $: claudeMdDirty = claudeMdContent !== claudeMdOriginal;

    // Tab navigation
    let activeTab = 'identity';
    // dirtyTabs: populated when unsaved changes exist on a tab (see #98 for full wiring)
    $: dirtyTabs = new Set([
        ...(claudeMdDirty ? ['identity'] : []),
        ...(voiceDirty ? ['behavior'] : []),
        ...(dreamDirty ? ['behavior'] : []),
        ...(providerDirty ? ['behavior'] : []),
    ]);

    const tabs = [
        { id: 'identity', label: 'Identity' },
        { id: 'connections', label: 'Connections' },
        { id: 'behavior', label: 'Behavior' },
        { id: 'automation', label: 'Automation' },
        { id: 'runtime', label: 'Runtime' },
    ];

    // MCP servers state
    let mcpServers = [];
    let mcpModalOpen = false;
    let mcpName = '';
    let mcpType = 'stdio';
    let mcpCommand = '';
    let mcpArgs = '';
    let mcpUrl = '';
    let mcpEnvPairs = [{ key: '', value: '' }];

    // Triggers state
    let triggers = [];
    let triggerModalOpen = false;
    let newTriggerType = 'webhook';
    let newTriggerName = '';
    let newTriggerUrl = '';
    let newTriggerMethod = 'GET';
    let newTriggerCondition = 'status_changed';
    let newTriggerFilePath = '';
    let newTriggerInterval = 300;
    let newTriggerPrompt = '';
    let newTriggerWebhookToken = ''; // shown once after creation
    let creatingTrigger = false;

    // Cron modal state
    let cronModalOpen = false;
    let cronName = '';
    let cronExpression = '';
    let cronPrompt = '';

    // Wizard state
    let wizardOpen = false;
    let wizStep = 0;
    const wizTotalSteps = 5;
    let wizName = '';
    let wizDisplayName = '';
    let wizModel = 'opus';
    let wizMode = 'bypassPermissions';
    let wizHeart = 'sidekick';
    let wizRole = 'sidekick';
    let wizAutoStart = true;
    let wizHeartbeatInterval = 300;
    let wizCustomSoul = '';
    let wizTelegramToken = '';
    let wizDiscordToken = '';
    let wizSlackToken = '';

    // Import (OpenClaw migration) state
    let importMode = false;
    let importStep = 1;          // 1=upload, 2=preview, 3=confirm
    /** @type {{ workspace: File|null, config: File|null, lock: File|null }} */
    let importFiles = { workspace: null, config: null, lock: null };
    let importParseId = null;
    let importPreview = null;
    let importLoading = false;
    let importTaskId = null;
    let importProgress = { total: 0, imported: 0, failed: 0, done: false };
    let importDragover = false;
    let importError = null;
    let importAgentName = null;   // final agent name returned by apply
    let importProgressInterval = null;
    let importPollAttempts = 0;
    const IMPORT_POLL_MAX = 150; // 5 min at 2s intervals

    // Soul templates are in src/lib/soulTemplates.js — buildSoul() handles all heart types.

    function toast(msg, type = 'success') {
        toastMessage.set({ message: msg, type });
    }

    function heartbeatStatus(hb) {
        if (!hb) return 'unknown';
        const age = Date.now() / 1000 - hb.timestamp;
        if (age < 600) return 'fresh';     // < 10 min
        if (age < 1800) return 'stale';    // 10-30 min
        return 'old';                       // > 30 min
    }

    // Runtime data: presence (status, last_seen) + context %
    let presenceMap = {};  // name -> { status, last_seen }
    let contextMap = {};   // name -> context_used_pct

    function stripMarkdown(text) {
        if (!text) return '';
        return text
            .replace(/^#+\s*/gm, '')          // headings
            .replace(/\*\*(.+?)\*\*/g, '$1')  // bold
            .replace(/\*(.+?)\*/g, '$1')       // italic
            .replace(/`(.+?)`/g, '$1')         // inline code
            .replace(/\[(.+?)\]\(.+?\)/g, '$1') // links
            .replace(/^[-*]\s+/gm, '')         // bullets
            .replace(/\n{2,}/g, ' ')           // collapse newlines
            .trim()
            .slice(0, 120);
    }

    async function refreshPresence() {
        try {
            const data = await api('GET', '/agents/presence');
            const m = {};
            for (const a of data.agents || []) m[a.agent] = { status: a.status, last_seen: a.last_seen };
            presenceMap = m;
        } catch {}
    }

    async function refreshContextPct(names) {
        const results = await Promise.allSettled(
            names.map(n => api('GET', `/agents/${n}/health`).then(h => ({ name: n, pct: h?.session?.context_used_pct ?? 0 })))
        );
        const m = { ...contextMap };
        for (const r of results) {
            if (r.status === 'fulfilled') m[r.value.name] = r.value.pct;
        }
        contextMap = m;
    }

    async function refreshAgents() {
        try {
            const data = await api('GET', '/agents');
            agentList = data.agents || [];
            agentCount = data.count;
            mainAgent = data.main_agent || '';
            // Kick off runtime enrichment in parallel, non-blocking
            refreshPresence();
            refreshContextPct(agentList.map(a => a.name));
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
        try {
            const hbData = await api('GET', '/heartbeats');
            const hbMap = {};
            for (const hb of (hbData.heartbeats || [])) {
                hbMap[hb.agent_name] = hb;
            }
            heartbeats = hbMap;
        } catch (e) {
            // non-critical, don't break the page
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

    async function setMainAgent(name) {
        await api('PUT', '/settings/main-agent', { agent: name });
        mainAgent = name;
        toast(`${name} is now the main agent`);
    }

    async function openDetail(name) {
        currentAgent = name;
        activeTab = 'identity';
        const agent = await api('GET', `/agents/${name}`);
        detailName = agent.display_name || agent.name;
        detailModel = agent.model;
        detailPermission = agent.permission_mode || 'default';
        detailMaxSessions = agent.max_sessions;
        detailGroups = agent.groups.length ? agent.groups.join(', ') : '--';
        detailWorkingDir = agent.working_dir;
        // Prefer CLAUDE.md on disk (agent may have edited it) over DB soul
        try {
            const file = await api('GET', `/agents/${agent.name}/files/CLAUDE.md`);
            claudeMdContent = file.content || agent.soul || '';
        } catch {
            claudeMdContent = agent.soul || '';
        }
        claudeMdOriginal = claudeMdContent;
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
        dreamEnabled = agent.dream_enabled || false;
        dreamSchedule = agent.dream_schedule || '0 3 * * *';
        dreamTimezone = agent.dream_timezone || 'America/Los_Angeles';
        dreamModel = agent.dream_model || '';
        dreamNotify = agent.dream_notify !== false;
        dreamDirty = false;
        providerUrl = agent.provider_url || '';
        providerKey = agent.provider_key || '';
        providerModel = agent.provider_model || '';
        providerRef = agent.provider_ref || '';
        if (providerUrl === 'http://localhost:11434') {
            providerPreset = 'ollama';
        } else if (providerUrl === 'https://api.z.ai/api/anthropic') {
            providerPreset = 'zai';
        } else if (providerUrl === 'https://openrouter.ai/api') {
            providerPreset = 'openrouter';
        } else if (providerUrl === 'https://api.deepseek.com/anthropic') {
            providerPreset = 'deepseek';
        } else if (providerUrl || providerKey) {
            providerPreset = 'custom';
        } else {
            providerPreset = 'anthropic';
        }
        providerDirty = false;
        globalProviders = await api('GET', '/providers').catch(() => []);
        // Clear stale ref if the referenced provider no longer exists
        if (providerRef && !globalProviders.find(p => p.id === providerRef)) providerRef = '';
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
        loadAgentSkills();
        loadMcpServers();
        loadTriggers(agent.name);
        loadGroupChats();
    }

    function closeDetail() { currentAgent = ''; detailOpen = false; }

    async function saveClaudeMd() {
        await api('PUT', `/agents/${currentAgent}/files/CLAUDE.md`, { content: claudeMdContent });
        claudeMdOriginal = claudeMdContent;
        toast('CLAUDE.md saved');
        loadFiles();
    }
    async function saveVoiceConfig() {
        const vc = { voice_reply: voiceReply, tts_provider: ttsProvider, tts_voice: ttsVoice, tts_model: ttsModel, transcribe_provider: transcribeProvider };
        await api('PUT', `/agents/${currentAgent}`, { voice_config: vc });
        voiceDirty = false;
        toast('Voice config saved');
    }
    async function saveDreamConfig() {
        await api('PUT', `/agents/${currentAgent}`, {
            dream_enabled: dreamEnabled,
            dream_schedule: dreamSchedule,
            dream_timezone: dreamTimezone,
            dream_model: dreamModel,
            dream_notify: dreamNotify,
        });
        dreamDirty = false;
        toast('Dream config saved');
    }
    function applyProviderPreset(preset) {
        providerPreset = preset;
        providerRef = ''; // switching to agent-specific config clears global ref
        if (preset === 'anthropic') {
            providerUrl = '';
            providerKey = '';
            providerModel = '';
        } else if (preset === 'ollama') {
            providerUrl = 'http://localhost:11434';
            providerKey = 'ollama';
        } else if (preset === 'zai') {
            providerUrl = 'https://api.z.ai/api/anthropic';
            providerKey = '';
            providerModel = 'glm-5.1';
        } else if (preset === 'openrouter') {
            providerUrl = 'https://openrouter.ai/api';
            providerKey = '';
            providerModel = 'anthropic/claude-sonnet-4-5';
        } else if (preset === 'deepseek') {
            providerUrl = 'https://api.deepseek.com/anthropic';
            providerKey = '';
            providerModel = 'deepseek-chat';
        }
        providerDirty = true;
    }

    function selectGlobalProvider(id) {
        providerRef = id;
        if (id) {
            // Clear agent-specific fields — they become inactive when ref is set
            providerUrl = '';
            providerKey = '';
            providerModel = '';
            providerPreset = 'anthropic';
        }
        providerDirty = true;
    }
    async function saveProvider() {
        await api('PUT', `/agents/${currentAgent}/provider`, {
            provider_url: providerUrl,
            provider_key: providerKey,
            provider_model: providerModel,
            provider_ref: providerRef,
        });
        providerDirty = false;
        toast('Provider saved — restart session to apply');
    }
    async function saveWorkingDir() { if (!detailWorkingDir) { toast('Enter a path', 'error'); return; } await api('PUT', `/agents/${currentAgent}`, { working_dir: detailWorkingDir }); toast('Working directory saved'); refreshAgents(); }

    async function loadDirectives() { const data = await api('GET', `/agents/${currentAgent}/directives?active_only=false`); directives = data.directives || []; }
    async function addDirective() { if (!newDirective.trim()) { toast('Enter a directive', 'error'); return; } await api('POST', `/agents/${currentAgent}/directives`, { directive: newDirective.trim(), priority: newDirectivePriority }); newDirective = ''; toast('Directive added'); loadDirectives(); }
    async function removeDirective(id) { await api('DELETE', `/agents/${currentAgent}/directives/${id}`); toast('Directive removed'); loadDirectives(); }
    async function toggleDirective(id, active) { await api('POST', `/agents/${currentAgent}/directives/${id}/toggle?active=${active}`); loadDirectives(); }

    async function loadTokens() { const data = await api('GET', `/agents/${currentAgent}/tokens`); tokens = data.tokens || []; }
    async function setToken() { if (!tokenValue) { toast('Enter a token', 'error'); return; } await api('PUT', `/agents/${currentAgent}/tokens/${tokenPlatform}`, { token: tokenValue }); tokenValue = ''; toast(`${tokenPlatform} token set`); loadTokens(); }
    async function removeToken(platform) { await api('DELETE', `/agents/${currentAgent}/tokens/${platform}`); toast(`${platform} token removed`); loadTokens(); }

    // MCP Servers
    async function loadMcpServers() { try { const data = await api('GET', `/agents/${currentAgent}/mcp-servers`); mcpServers = data.servers || []; } catch { mcpServers = []; } }
    function openMcpModal() { mcpName = ''; mcpType = 'stdio'; mcpCommand = ''; mcpArgs = ''; mcpUrl = ''; mcpEnvPairs = [{ key: '', value: '' }]; mcpModalOpen = true; }
    async function addMcpServer() {
        if (!mcpName.trim()) { toast('Server name required', 'error'); return; }
        const env = {};
        mcpEnvPairs.forEach(p => { if (p.key.trim()) env[p.key.trim()] = p.value; });
        const body = { name: mcpName.trim(), server_type: mcpType, env };
        if (mcpType === 'stdio') {
            body.command = mcpCommand;
            body.args = mcpArgs.trim() ? mcpArgs.split(/\s+/) : [];
        } else {
            body.url = mcpUrl;
        }
        await api('POST', `/agents/${currentAgent}/mcp-servers`, body);
        mcpModalOpen = false;
        toast(`MCP server '${mcpName}' added`);
        loadMcpServers();
    }
    async function removeMcpServer(serverName) { await api('DELETE', `/agents/${currentAgent}/mcp-servers/${serverName}`); toast(`${serverName} removed`); loadMcpServers(); }
    async function toggleMcpServer(serverName, enabled) { await api('POST', `/agents/${currentAgent}/mcp-servers/${serverName}/toggle?enabled=${enabled}`); loadMcpServers(); }

    // Triggers
    async function loadTriggers(agentName) {
        try {
            const data = await api('GET', `/agents/${agentName}/triggers`);
            triggers = data.triggers || [];
        } catch { triggers = []; }
    }

    async function deleteTrigger(id) {
        await api('DELETE', `/agents/${currentAgent}/triggers/${id}`);
        await loadTriggers(currentAgent);
        toast('Trigger deleted');
    }

    async function toggleTrigger(id, enabled) {
        await api('PUT', `/agents/${currentAgent}/triggers/${id}`, { enabled });
        await loadTriggers(currentAgent);
    }

    async function testTrigger(id) {
        await api('POST', `/agents/${currentAgent}/triggers/${id}/test`);
        toast('Trigger fired');
    }

    function openTriggerModal() {
        newTriggerType = 'webhook';
        newTriggerName = '';
        newTriggerUrl = '';
        newTriggerMethod = 'GET';
        newTriggerCondition = 'status_changed';
        newTriggerFilePath = '';
        newTriggerInterval = 300;
        newTriggerPrompt = '';
        newTriggerWebhookToken = '';
        triggerModalOpen = true;
    }

    async function createTrigger() {
        if (!newTriggerType) { toast('Select a type', 'error'); return; }
        creatingTrigger = true;
        try {
            const payload = {
                trigger_type: newTriggerType,
                name: newTriggerName.trim(),
                prompt_template: newTriggerPrompt.trim(),
                enabled: true,
            };
            if (newTriggerType === 'url') {
                payload.url = newTriggerUrl.trim();
                payload.method = newTriggerMethod;
                payload.condition = newTriggerCondition;
                payload.interval_seconds = Number(newTriggerInterval);
            }
            if (newTriggerType === 'file') {
                payload.file_path = newTriggerFilePath.trim();
                payload.interval_seconds = Number(newTriggerInterval);
            }
            const result = await api('POST', `/agents/${currentAgent}/triggers`, payload);
            newTriggerWebhookToken = result.token || '';
            if (!newTriggerWebhookToken) triggerModalOpen = false;
            await loadTriggers(currentAgent);
            toast('Trigger created');
        } catch (e) {
            toast(`Failed: ${e.message}`, 'error');
        } finally {
            creatingTrigger = false;
        }
    }

    // Agent Skills
    async function loadAgentSkills() {
        try {
            const data = await api('GET', `/agents/${currentAgent}/skills?enabled_only=false`);
            agentSkills = data.skills || [];
        } catch { agentSkills = []; }
        try {
            const params = skillCategoryFilter ? `&category=${skillCategoryFilter}` : '';
            const avail = await api('GET', `/agents/${currentAgent}/skills/available?self_assignable=false${params}`);
            availableSkills = avail.skills || [];
        } catch { availableSkills = []; }
        skillsPendingApply = false;
    }
    async function assignSkill(skillName) {
        await api('POST', `/agents/${currentAgent}/skills/${skillName}`, { assigned_by: 'user' });
        toast(`Skill '${skillName}' assigned`);
        skillsPendingApply = true;
        loadAgentSkills();
    }
    async function removeAgentSkill(skillName) {
        await api('DELETE', `/agents/${currentAgent}/skills/${skillName}`);
        toast(`Skill '${skillName}' removed`);
        skillsPendingApply = true;
        loadAgentSkills();
    }
    async function toggleAgentSkill(skillName, enable) {
        await api('POST', `/agents/${currentAgent}/skills/${skillName}/${enable ? 'enable' : 'disable'}`);
        skillsPendingApply = true;
        loadAgentSkills();
    }
    async function applySkills() {
        toast('Applying skills — session may restart...', 'info');
        const result = await api('POST', `/agents/${currentAgent}/skills/apply`);
        skillsPendingApply = false;
        if (result.session_restarted) {
            toast('Skills applied — session restarted');
        } else {
            toast('Skills applied');
        }
        loadAgentSkills();
    }
    async function installSkillFromGit() {
        if (!gitSkillUrl.trim()) { toast('Enter a git URL', 'error'); return; }
        gitSkillLoading = true;
        try {
            const result = await api('POST', '/skills/from-git', { url: gitSkillUrl, agent_name: currentAgent });
            const names = [...(result.registered || []), ...(result.updated || [])];
            toast(`Installed ${names.length} skill${names.length !== 1 ? 's' : ''}: ${names.join(', ')}`);
            gitSkillUrl = '';
            skillsPendingApply = names.length > 0;
            loadAgentSkills();
        } catch (e) { toast(e.message || 'Failed to install from git', 'error'); }
        gitSkillLoading = false;
    }
    async function createSkillFromMd() {
        if (!newSkillMd.trim()) { toast('Enter SKILL.md content', 'error'); return; }
        try {
            const result = await api('POST', '/skills/from-md', { content: newSkillMd, agent_name: currentAgent });
            toast(`Skill '${result.name}' created and assigned`);
            createSkillOpen = false;
            newSkillMd = `---\nname: my-skill\ndescription: What this skill does and when to use it.\n---\n\n# My Skill\n\nInstructions for the agent go here.\n`;
            skillsPendingApply = true;
            loadAgentSkills();
        } catch (e) { toast(e.message || 'Failed to create skill', 'error'); }
    }

    async function loadFiles() { const data = await api('GET', `/agents/${currentAgent}/files`); files = data.exists ? (data.files || []) : []; }
    async function editFile(filename) { const data = await api('GET', `/agents/${currentAgent}/files/${filename}`); editingFile = filename; fileEditorName = filename; fileEditorContent = data.content; fileEditorOpen = true; }
    function closeFileEditor() { fileEditorOpen = false; editingFile = ''; }
    async function saveFile() { await api('PUT', `/agents/${currentAgent}/files/${editingFile}`, { content: fileEditorContent }); toast(`${editingFile} saved`); loadFiles(); }

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
    function openWizard() { wizStep = -1; importMode = false; importStep = 1; importFiles = { workspace: null, config: null, lock: null }; importParseId = null; importPreview = null; importLoading = false; importTaskId = null; importProgress = { total: 0, imported: 0, failed: 0, done: false }; importDragover = false; importError = null; importAgentName = null; if (importProgressInterval) { clearInterval(importProgressInterval); importProgressInterval = null; } wizName = ''; wizDisplayName = ''; wizPronouns = ''; wizModel = 'opus'; wizMode = 'bypassPermissions'; wizHeart = 'sidekick'; wizRole = 'sidekick'; wizAutoStart = true; wizHeartbeatInterval = 300; wizCustomSoul = ''; wizTelegramToken = ''; wizDiscordToken = ''; wizSlackToken = ''; wizardOpen = true; }
    function closeWizard() { if (importProgressInterval) { clearInterval(importProgressInterval); importProgressInterval = null; } wizardOpen = false; }

    // Import (OpenClaw migration) helpers
    async function importParse() {
        if (!importFiles.workspace) { toast('Please select a workspace zip', 'error'); return; }
        importLoading = true;
        importError = null;
        try {
            const fd = new FormData();
            fd.append('workspace', importFiles.workspace);
            if (importFiles.config) fd.append('config', importFiles.config);
            if (importFiles.lock) fd.append('lock', importFiles.lock);
            const resp = await fetch('/api/migrate/openclaw/parse', { method: 'POST', body: fd, credentials: 'same-origin' });
            if (!resp.ok) { const t = await resp.text(); throw new Error(t); }
            const parsed = await resp.json();
            importParseId = parsed.parse_id || parsed.id || null;
            // Go to preview
            importLoading = true;
            importStep = 2;
            const previewResp = await fetch('/api/migrate/openclaw/preview', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify(parsed) });
            if (!previewResp.ok) { const t = await previewResp.text(); throw new Error(t); }
            importPreview = await previewResp.json();
        } catch (e) {
            importError = e.message || String(e);
            importStep = 1;
        } finally {
            importLoading = false;
        }
    }

    async function importApply() {
        if (!importPreview) return;
        importLoading = true;
        importError = null;
        try {
            const resp = await fetch('/api/migrate/openclaw/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'same-origin', body: JSON.stringify(importPreview) });
            if (!resp.ok) { const t = await resp.text(); throw new Error(t); }
            const result = await resp.json();
            importAgentName = result.agent_name;
            importTaskId = result.task_id;
            importProgress = { total: 0, imported: 0, failed: 0, done: false };
            importStep = 3;
            importLoading = false;
            // Start polling with timeout guard
            importPollAttempts = 0;
            importProgressInterval = setInterval(async () => {
                importPollAttempts++;
                if (importPollAttempts > IMPORT_POLL_MAX) {
                    clearInterval(importProgressInterval); importProgressInterval = null;
                    importError = 'Memory import timed out — check agent state in the dashboard.';
                    return;
                }
                try {
                    const sr = await fetch(`/api/migrate/openclaw/status/${importTaskId}`, { credentials: 'same-origin' });
                    if (sr.ok) {
                        const s = await sr.json();
                        importProgress = s;
                        if (s.done) { clearInterval(importProgressInterval); importProgressInterval = null; refreshAgents(); }
                    } else if (sr.status === 404) {
                        // Task expired or server restarted
                        clearInterval(importProgressInterval); importProgressInterval = null;
                        importError = 'Import task not found — the server may have restarted. Check agent state in the dashboard.';
                    }
                } catch {}
            }, 2000);
        } catch (e) {
            importError = e.message || String(e);
            importLoading = false;
        }
    }

    function importStatusBadgeStyle(status) {
        if (status === 'ok') return 'background:var(--tone-success-bg,#d1fae5);color:var(--tone-success-text,#065f46)';
        if (status === 'warn') return 'background:var(--warning-bg,#fef3c7);color:#92400e';
        return 'background:rgba(239,68,68,0.15);color:var(--red,#ef4444)';
    }
    function importStatusIcon(status) {
        if (status === 'ok') return '✅';
        if (status === 'warn') return '⚠️';
        return '❌';
    }

    function wizardPrev() { if (wizStep > -1) wizStep--; }
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
        const label = wizAutoStart ? 'main' : 'chat';
        await api('POST', `/agents/${wizName}/streaming-sessions?label=${encodeURIComponent(label)}`);
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
                        {@const agentPresence = presenceMap[a.name] || {}}
                        {@const agentStatus = a.working_status === 'working' ? 'working' : (agentPresence.status || 'unknown')}
                        {@const agentCtxPct = contextMap[a.name] ?? null}
                        <div class="agent-card">
                            <div class="agent-name">{a.display_name || a.name}</div>
                            <div class="agent-meta">
                                {#if a.name === mainAgent}<span class="badge" style="background:#fef3c7;color:#92400e">[*] Main</span>{/if}
                                {#if a.role}<span class="badge" style="background:var(--surface-inverse);color:var(--accent)">{a.role}</span>{/if}
                                <span class="badge badge-model">{a.model}</span>
                                <span class="badge badge-{a.enabled ? 'on' : 'off'}">{a.enabled ? 'Active' : 'Disabled'}</span>
                                {#if a.auto_start}<span class="badge" style="background:#dcfce7;color:#166534">Auto-Start</span>{/if}
                                <span class="badge" style="background:var(--tone-neutral-bg);color:var(--tone-neutral-text)">{a.permission_mode === 'bypassPermissions' ? 'YOLO' : a.permission_mode || 'default'}</span>
                                {#each a.groups as g}<span class="badge badge-group">{g}</span>{/each}
                            </div>
                            <div class="agent-runtime">
                                <span class="status-pill status-{agentStatus}">{agentStatus}</span>
                                {#if heartbeats[a.name]}
                                    {@const hb = heartbeats[a.name]}
                                    {@const hbAge = heartbeatStatus(hb)}
                                    <span
                                        class="hb-pulse hb-{hbAge}"
                                        title="Last heartbeat: {timeAgo(hb.timestamp * 1000)} · {hb.status} · ctx {Math.round(hb.context_pct)}%{hb.notes ? ' · ' + hb.notes : ''}"
                                    ></span>
                                {/if}
                                {#if agentCtxPct !== null && agentCtxPct > 0}
                                    <span class="ctx-bar" title="Context {agentCtxPct.toFixed(1)}% used">
                                        <span class="ctx-fill ctx-{agentCtxPct > 80 ? 'warn' : 'ok'}" style="width:{Math.min(agentCtxPct,100)}%"></span>
                                    </span>
                                    <span class="ctx-label">{agentCtxPct.toFixed(0)}%</span>
                                {/if}
                                {#if agentPresence.last_seen}
                                    {@const ls = new Date(agentPresence.last_seen * 1000)}
                                    <span class="last-active" title={ls.toLocaleString()}>
                                        {ls.getHours().toString().padStart(2,'0')}:{ls.getMinutes().toString().padStart(2,'0')} · {timeAgo(agentPresence.last_seen)}
                                    </span>
                                {/if}
                            </div>
                            <div class="agent-actions">
                                <button class="btn btn-sm btn-primary" on:click={() => openDetail(a.name)}>Configure</button>
                                {#if a.name !== mainAgent}
                                    <button class="btn btn-sm" on:click={() => setMainAgent(a.name)}>Set as Main</button>
                                {/if}
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
                        <div class="agent-card" style="opacity:0.6;background:var(--surface-2)">
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

    <Modal bind:show={retireModalOpen} title="Retire Agent" width="420px">
        <div class="modal-form">
            <p class="modal-note">This will retire <strong style="color:var(--red)">{pendingRetireAgent}</strong> and disable all its sessions. The agent data stays available for restoration.</p>
            <div class="form-row">
                <label class="form-label">Type the agent name to confirm</label>
                <input type="text" class="form-input w-full" bind:value={retireConfirmInput} autocomplete="off" spellcheck="false" placeholder="agent name">
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={closeRetireModal}>Cancel</button>
            <button class="btn btn-sm btn-confirm-delete" class:ready={retireConfirmInput === pendingRetireAgent} disabled={retireConfirmInput !== pendingRetireAgent} on:click={confirmRetire}>Retire</button>
        </div>
    </Modal>

    <Modal bind:show={cronModalOpen} title="New Cron Job" width="460px">
        <div class="modal-form">
            <p class="modal-note">Schedule a recurring task for this agent.</p>
            <div class="form-row">
                <label class="form-label">Name</label>
                <input type="text" class="form-input w-full" bind:value={cronName} placeholder="e.g. morning_check" autocomplete="off">
            </div>
            <div class="form-row">
                <label class="form-label">Cron Expression</label>
                <input type="text" class="form-input w-full" bind:value={cronExpression} placeholder="e.g. 0 8 * * *">
                <p class="modal-note" style="margin-top:0.4rem">min hour day month weekday — <a href="https://crontab.guru" target="_blank" rel="noreferrer">crontab.guru</a></p>
            </div>
            <div class="form-row">
                <label class="form-label">Prompt</label>
                <textarea class="form-input w-full" bind:value={cronPrompt} placeholder="Message sent to the agent when this job fires..." rows="3"></textarea>
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={closeCronModal}>Cancel</button>
            <button class="btn btn-sm btn-primary" disabled={!cronName || !cronExpression} on:click={submitCronJob}>Create</button>
        </div>
    </Modal>

    <Modal bind:show={mcpModalOpen} title="Add MCP Server" width="500px">
        <div class="modal-form">
            <div class="form-row">
                <label class="form-label">Server Name</label>
                <input type="text" class="form-input w-full" bind:value={mcpName} placeholder="e.g. webclaw" autocomplete="off">
            </div>
            <div class="form-row">
                <label class="form-label">Type</label>
                <select class="form-select w-full" bind:value={mcpType}>
                    <option value="stdio">stdio (command)</option>
                    <option value="http">HTTP (URL)</option>
                </select>
            </div>
            {#if mcpType === 'stdio'}
                <div class="form-row">
                    <label class="form-label">Command</label>
                    <input type="text" class="form-input w-full" bind:value={mcpCommand} placeholder="e.g. npx">
                </div>
                <div class="form-row">
                    <label class="form-label">Arguments (space-separated)</label>
                    <input type="text" class="form-input w-full" bind:value={mcpArgs} placeholder="e.g. -y @webclaw/mcp">
                </div>
            {:else}
                <div class="form-row">
                    <label class="form-label">URL</label>
                    <input type="text" class="form-input w-full" bind:value={mcpUrl} placeholder="e.g. http://localhost:8931/mcp">
                </div>
            {/if}
            <div class="form-row">
                <label class="form-label">Environment Variables</label>
                {#each mcpEnvPairs as pair, i}
                    <div class="inline-spread" style="margin-bottom:0.35rem">
                        <input type="text" class="form-input grow" bind:value={pair.key} placeholder="KEY">
                        <input type="text" class="form-input grow" bind:value={pair.value} placeholder="value">
                        {#if mcpEnvPairs.length > 1}
                            <button class="btn btn-sm" on:click={() => { mcpEnvPairs = mcpEnvPairs.filter((_, j) => j !== i); }}>X</button>
                        {/if}
                    </div>
                {/each}
                <button class="btn btn-sm" on:click={() => { mcpEnvPairs = [...mcpEnvPairs, { key: '', value: '' }]; }}>+ Env Var</button>
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={() => mcpModalOpen = false}>Cancel</button>
            <button class="btn btn-sm btn-primary" disabled={!mcpName.trim()} on:click={addMcpServer}>Add Server</button>
        </div>
    </Modal>

    <Modal bind:show={triggerModalOpen} title="New Trigger" width="480px">
        <div class="modal-form">
            {#if newTriggerWebhookToken}
                <div style="background:var(--tone-success-bg);border-radius:var(--radius-lg);padding:0.75rem 1rem;font-size:0.82rem;color:var(--tone-success-text)">
                    Webhook created. Copy your token — it won't be shown again:
                    <div style="font-family:var(--font-body);font-size:0.78rem;word-break:break-all;margin-top:0.4rem;color:var(--text-primary)">{newTriggerWebhookToken}</div>
                    <button class="btn btn-sm" style="margin-top:0.5rem" on:click={() => navigator.clipboard.writeText(newTriggerWebhookToken).then(() => toast('Copied'))}>Copy</button>
                </div>
            {:else}
                <div class="form-row">
                    <label class="form-label">Type</label>
                    <select class="form-select w-full" bind:value={newTriggerType}>
                        <option value="webhook">Webhook — receive HTTP POST</option>
                        <option value="url">URL Watcher — poll a URL</option>
                        <option value="file">File Watcher — watch a file/glob</option>
                    </select>
                </div>
                <div class="form-row">
                    <label class="form-label">Name (optional)</label>
                    <input type="text" class="form-input w-full" bind:value={newTriggerName} placeholder="e.g. Deploy webhook">
                </div>
                {#if newTriggerType === 'url'}
                    <div class="form-row">
                        <label class="form-label">URL</label>
                        <input type="url" class="form-input w-full" bind:value={newTriggerUrl} placeholder="https://api.example.com/status">
                    </div>
                    <div class="form-row">
                        <label class="form-label">Method</label>
                        <select class="form-select w-full" bind:value={newTriggerMethod}>
                            <option value="GET">GET</option>
                            <option value="POST">POST</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <label class="form-label">Condition</label>
                        <select class="form-select w-full" bind:value={newTriggerCondition}>
                            <option value="status_changed">Status changed</option>
                            <option value="status_is">Status is</option>
                            <option value="body_contains">Body contains</option>
                            <option value="json_field_equals">JSON field equals</option>
                            <option value="json_field_changed">JSON field changed</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <label class="form-label">Poll interval (seconds)</label>
                        <input type="number" class="form-input w-full" bind:value={newTriggerInterval} min="30">
                    </div>
                {/if}
                {#if newTriggerType === 'file'}
                    <div class="form-row">
                        <label class="form-label">File path or glob</label>
                        <input type="text" class="form-input w-full" bind:value={newTriggerFilePath} placeholder="/path/to/file.txt or /logs/*.log">
                    </div>
                    <div class="form-row">
                        <label class="form-label">Poll interval (seconds)</label>
                        <input type="number" class="form-input w-full" bind:value={newTriggerInterval} min="10">
                    </div>
                {/if}
                <div class="form-row">
                    <label class="form-label">Prompt template (optional)</label>
                    <textarea class="form-input w-full" bind:value={newTriggerPrompt} rows="3" placeholder="Wake message sent to agent. Use {'{{body.field}}'} for URL/webhook data."></textarea>
                </div>
            {/if}
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn" on:click={() => triggerModalOpen = false}>
                {newTriggerWebhookToken ? 'Close' : 'Cancel'}
            </button>
            {#if !newTriggerWebhookToken}
                <button class="btn btn-primary" on:click={createTrigger} disabled={creatingTrigger}>
                    {creatingTrigger ? 'Creating…' : 'Create Trigger'}
                </button>
            {/if}
        </div>
    </Modal>

    <!-- Agent Detail Panel -->
    {#if detailOpen}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Agent: {detailName} {#if currentAgent === mainAgent}<span style="font-size:0.75rem;color:#92400e;background:#fef3c7;padding:0.15rem 0.5rem;border-radius:var(--radius-lg);margin-left:0.5rem;vertical-align:middle">[*] Main Agent</span>{/if}</div>
                <button class="btn" on:click={closeDetail}>Close</button>
            </div>
            <!-- Compact metadata row -->
            <div style="padding:0.8rem 1.5rem;display:flex;flex-wrap:wrap;gap:0.8rem 1.5rem;align-items:center;background:var(--surface-2);border-radius:var(--radius-lg);font-family:var(--font-grotesk);font-size:0.8rem">
                <span><span style="color:var(--gray-mid)">Model:</span> {detailModel}</span>
                <span><span style="color:var(--gray-mid)">Perm:</span> {detailPermission}</span>
                <span><span style="color:var(--gray-mid)">Max:</span> {detailMaxSessions}</span>
                <span><span style="color:var(--gray-mid)">Groups:</span> {detailGroups}</span>
                <span style="display:flex;gap:0.3rem;align-items:center;flex:1;min-width:200px">
                    <span style="color:var(--gray-mid)">Dir:</span>
                    <input type="text" class="form-input" bind:value={detailWorkingDir} style="font-size:0.8rem;flex:1;padding:0.2rem 0.4rem">
                    <button class="btn btn-sm" on:click={saveWorkingDir}>Save</button>
                </span>
            </div>

            <!-- Tab Bar -->
            <div class="detail-tabs">
                {#each tabs as tab}
                    <button class="detail-tab" class:active={activeTab === tab.id} on:click={() => activeTab = tab.id}>
                        {tab.label}
                        {#if dirtyTabs.has(tab.id)}<span class="dirty-dot"></span>{/if}
                    </button>
                {/each}
            </div>

            {#if activeTab === 'identity'}
            <!-- CLAUDE.md Editor -->
            <div style="background:var(--surface-1);border-radius:var(--radius-lg);margin:0.5rem 0">
                <div style="padding:0.6rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0;display:flex;justify-content:space-between;align-items:center">
                    <div style="display:flex;align-items:center;gap:0.6rem">
                        <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">CLAUDE.MD</span>
                        {#if claudeMdDirty}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--accent);font-weight:700">unsaved</span>{/if}
                    </div>
                    <div style="display:flex;gap:0.3rem">
                        <button class="btn btn-sm btn-primary" on:click={saveClaudeMd} disabled={!claudeMdDirty}>Save</button>
                    </div>
                </div>
                <textarea class="form-input" bind:value={claudeMdContent} rows="20" style="margin:0;border:none;width:100%;font-family:var(--font-grotesk);font-size:0.8rem;line-height:1.5;resize:vertical;padding:0.8rem 1.5rem;background:var(--input-bg);border-radius:0 0 var(--radius-lg) var(--radius-lg)" placeholder="Agent's full CLAUDE.md — identity, boundaries, directives, everything..."></textarea>
            </div>

            <!-- Directives -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem">
                    <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Directives</span>
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

            <!-- Heart Files -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Heart Files</span>
            </div>
            <div>
                {#each files.filter(f => !f.is_claude_md) as f}
                    <div class="token-item">
                        <span style="font-family:var(--font-grotesk);font-size:0.8rem">{f.name}</span>
                        <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{(f.size / 1024).toFixed(1)}K</span>
                        <span style="flex:1"></span>
                        <button class="btn btn-sm" on:click={() => editFile(f.name)}>Edit</button>
                    </div>
                {/each}
            </div>

            <!-- File Editor -->
            {#if fileEditorOpen}
                <div style="background:var(--surface-1);border-radius:var(--radius-lg);margin-top:0.5rem">
                    <div style="padding:0.8rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg) var(--radius-lg) 0 0;display:flex;justify-content:space-between;align-items:center">
                        <span style="font-family:var(--font-grotesk);font-size:0.75rem;font-weight:700">{fileEditorName}</span>
                        <div style="display:flex;gap:0.3rem">
                            <button class="btn btn-sm btn-primary" on:click={saveFile}>Save</button>
                            <button class="btn btn-sm" on:click={closeFileEditor}>Close</button>
                        </div>
                    </div>
                    <textarea class="form-input" bind:value={fileEditorContent} rows="12" style="margin:0;border:none;width:100%;font-size:0.8rem;background:var(--input-bg);border-radius:0 0 var(--radius-lg) var(--radius-lg)"></textarea>
                </div>
            {/if}
            {/if}<!-- end identity tab -->

            {#if activeTab === 'connections'}
            <!-- Connections: Bot Tokens, Users (approved + pending), Group Chats -->

            <!-- Bot Tokens -->
            <div class="detail-section-header">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Bot Tokens</span>
            </div>
            <div style="padding:0.75rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
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

            <!-- Users (approved + pending merged) -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Users</span>
                {#if pendingUserCount > 0}<span class="badge" style="background:#fef3c7;color:#92400e;margin-left:0.5rem">{pendingUserCount} pending</span>{/if}
            </div>
            <div style="padding:0.75rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
                    <input type="text" class="form-input" bind:value={newUserChatId} placeholder="Chat ID" style="width:130px">
                    <input type="text" class="form-input" bind:value={newUserName} placeholder="Display name (optional)" style="flex:1;min-width:120px">
                    <button class="btn btn-primary" on:click={approveUser}>Approve</button>
                </div>
            </div>
            <div>
                <!-- Pending users first (with yellow badge) -->
                {#each Object.entries(pendingMessages) as [chatId, msgs]}
                    <div class="token-item" style="flex-direction:column;align-items:flex-start;gap:0.5rem">
                        <div style="display:flex;width:100%;align-items:center;gap:0.5rem">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{msgs[0]?.sender_name || chatId}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{chatId}</span>
                            <span class="badge" style="background:#fef3c7;color:#92400e">pending</span>
                            <span class="badge badge-model">{msgs.length} msg{msgs.length > 1 ? 's' : ''}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm btn-success" on:click={() => approveAndDeliver(chatId, msgs[0]?.sender_name)}>Approve</button>
                            <button class="btn btn-sm btn-danger" on:click={() => denyPendingUser(chatId)}>Deny</button>
                        </div>
                        <div style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid);padding-left:0.5rem;max-height:3rem;overflow:hidden">
                            {msgs[0]?.content?.slice(0, 150)}{msgs[0]?.content?.length > 150 ? '...' : ''}
                        </div>
                    </div>
                {/each}
                <!-- Approved users -->
                {#if approvedUsers.length === 0 && pendingUserCount === 0}
                    <div class="empty">No users.</div>
                {:else}
                    {#each approvedUsers as u}
                        <div class="token-item">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{u.display_name || u.chat_id}</span>
                            {#if u.display_name}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{u.chat_id}</span>{/if}
                            {#if u.status === 'approved'}
                                <span class="badge" style="background:#dcfce7;color:#166534">approved</span>
                            {:else if u.status === 'denied'}
                                <span class="badge badge-off">denied</span>
                            {:else}
                                <span class="badge badge-model">{u.status}</span>
                            {/if}
                            {#if u.timezone}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{u.timezone}</span>{/if}
                            {#if streamingSessions.length > 1}
                            <select style="font-family:var(--font-grotesk);font-size:0.75rem;padding:0.15rem 0.3rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg)"
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

            <!-- Group Chats -->
            {#if groupChats.length > 0}
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Group Chats</span>
                <span class="badge" style="margin-left:0.5rem">{groupChats.length}</span>
            </div>
            <div>
                {#each groupChats as gc}
                    <div class="token-item">
                        <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{gc.alias || gc.chat_title || gc.chat_id}</span>
                        {#if gc.alias}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_title}</span>{/if}
                        <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_type}</span>
                        {#if gc.member_count > 0}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{gc.member_count} members</span>{/if}
                        <select style="font-family:var(--font-grotesk);font-size:0.75rem;padding:0.15rem 0.3rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg)"
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
            {/if}<!-- end connections tab -->

            {#if activeTab === 'behavior'}
            <!-- Voice Config -->
            <div class="detail-section-header">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Voice</span>
                {#if voiceDirty}<button class="btn btn-sm btn-primary" on:click={saveVoiceConfig}>Save</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="margin-top:0;display:flex;flex-direction:column;gap:0.8rem">
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={voiceReply} on:change={() => voiceDirty = true}> Auto-reply to voice messages with TTS
                    </label>
                    {#if voiceReply}
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">TTS Provider</div>
                            <select class="form-select" bind:value={ttsProvider} on:change={() => voiceDirty = true} style="width:100%">
                                <option value="openai">OpenAI</option>
                                <option value="elevenlabs">ElevenLabs</option>
                                <option value="deepgram">Deepgram</option>
                            </select>
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Voice</div>
                            {#if ttsProvider === 'openai'}
                                <select class="form-select" bind:value={ttsVoice} on:change={() => voiceDirty = true} style="width:100%">
                                    <option value="">Default</option>
                                    <option value="alloy">Alloy</option>
                                    <option value="ash">Ash</option>
                                    <option value="coral">Coral</option>
                                    <option value="echo">Echo</option>
                                    <option value="fable">Fable</option>
                                    <option value="nova">Nova</option>
                                    <option value="onyx">Onyx</option>
                                    <option value="sage">Sage</option>
                                    <option value="shimmer">Shimmer</option>
                                </select>
                            {:else if ttsProvider === 'elevenlabs'}
                                <input type="text" class="form-input" bind:value={ttsVoice} on:input={() => voiceDirty = true} placeholder="ElevenLabs Voice ID" style="width:100%">
                            {:else}
                                <select class="form-select" bind:value={ttsVoice} on:change={() => voiceDirty = true} style="width:100%">
                                    <option value="">Default</option>
                                    <option value="aura-asteria-en">Asteria (F)</option>
                                    <option value="aura-luna-en">Luna (F)</option>
                                    <option value="aura-stella-en">Stella (F)</option>
                                    <option value="aura-athena-en">Athena (F)</option>
                                    <option value="aura-hera-en">Hera (F)</option>
                                    <option value="aura-orion-en">Orion (M)</option>
                                    <option value="aura-arcas-en">Arcas (M)</option>
                                    <option value="aura-perseus-en">Perseus (M)</option>
                                    <option value="aura-angus-en">Angus (M)</option>
                                    <option value="aura-orpheus-en">Orpheus (M)</option>
                                    <option value="aura-helios-en">Helios (M)</option>
                                    <option value="aura-zeus-en">Zeus (M)</option>
                                </select>
                            {/if}
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Model</div>
                            {#if ttsProvider === 'openai'}
                                <select class="form-select" bind:value={ttsModel} on:change={() => voiceDirty = true} style="width:100%">
                                    <option value="">Default (tts-1)</option>
                                    <option value="tts-1">TTS-1</option>
                                    <option value="tts-1-hd">TTS-1 HD</option>
                                    <option value="gpt-4o-mini-tts">GPT-4o Mini TTS</option>
                                </select>
                            {:else if ttsProvider === 'elevenlabs'}
                                <select class="form-select" bind:value={ttsModel} on:change={() => voiceDirty = true} style="width:100%">
                                    <option value="">Default (Flash v2.5)</option>
                                    <option value="eleven_v3">Eleven v3 (Best)</option>
                                    <option value="eleven_multilingual_v2">Multilingual v2</option>
                                    <option value="eleven_flash_v2_5">Flash v2.5 (Fast)</option>
                                    <option value="eleven_flash_v2">Flash v2 (EN only)</option>
                                    <option value="eleven_turbo_v2_5">Turbo v2.5</option>
                                    <option value="eleven_turbo_v2">Turbo v2</option>
                                </select>
                            {:else}
                                <input type="text" class="form-input" bind:value={ttsModel} on:input={() => voiceDirty = true} placeholder="Model ID" style="width:100%">
                            {/if}
                        </div>
                    </div>
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Transcription Provider</div>
                            <select class="form-select" bind:value={transcribeProvider} on:change={() => voiceDirty = true}>
                                <option value="openai">OpenAI Whisper</option>
                                <option value="deepgram">Deepgram Nova</option>
                            </select>
                        </div>
                    </div>
                    {/if}
                </div>
            </div>

            <!-- Dreaming Config -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Dreaming</span>
                {#if dreamDirty}<button class="btn btn-sm btn-primary" on:click={saveDreamConfig}>Save</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;flex-direction:column;gap:0.8rem">
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={dreamEnabled} on:change={() => dreamDirty = true}> Enable nightly memory consolidation
                    </label>
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={dreamNotify} on:change={() => dreamDirty = true}> Inject dream summary into morning wake context
                    </label>
                    {#if dreamEnabled}
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Schedule (cron)</div>
                            <input type="text" class="form-input" bind:value={dreamSchedule} on:input={() => dreamDirty = true} placeholder="0 3 * * *" style="width:100%">
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Timezone</div>
                            <select class="form-select" bind:value={dreamTimezone} on:change={() => dreamDirty = true} style="width:100%">
                                <option value="America/Los_Angeles">Pacific (LA)</option>
                                <option value="America/Denver">Mountain (Denver)</option>
                                <option value="America/Chicago">Central (Chicago)</option>
                                <option value="America/New_York">Eastern (NYC)</option>
                                <option value="Europe/London">London</option>
                                <option value="Europe/Berlin">Berlin</option>
                                <option value="Europe/Moscow">Moscow</option>
                                <option value="Asia/Tokyo">Tokyo</option>
                                <option value="Asia/Shanghai">Shanghai</option>
                                <option value="Australia/Sydney">Sydney</option>
                                <option value="UTC">UTC</option>
                            </select>
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">Model</div>
                            <select class="form-select" bind:value={dreamModel} on:change={() => dreamDirty = true} style="width:100%">
                                <option value="">Default (agent's model)</option>
                                <option value="opus">Opus</option>
                                <option value="sonnet">Sonnet</option>
                                <option value="haiku">Haiku</option>
                            </select>
                        </div>
                    </div>
                    {/if}
                </div>
            </div>

            <!-- Model Provider -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Model Provider</span>
                {#if providerDirty}<button class="btn btn-sm btn-primary" on:click={saveProvider}>Save</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                {#if globalProviders.length > 0}
                <div style="margin-bottom:0.75rem">
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Global Provider</div>
                    <select class="form-select" value={providerRef} on:change={(e) => selectGlobalProvider(e.target.value)} style="width:100%;max-width:320px">
                        <option value="">(none — use agent-specific config)</option>
                        {#each globalProviders as gp}
                            <option value={gp.id}>{gp.name}{gp.provider_model ? ' · ' + gp.provider_model : ''}</option>
                        {/each}
                    </select>
                </div>
                {/if}
                <div style="{providerRef ? 'opacity:0.4;pointer-events:none' : ''}">
                    <div style="display:flex;gap:0.4rem;margin-top:0;flex-wrap:wrap">
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'anthropic'} style={providerPreset !== 'anthropic' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('anthropic')}>Anthropic (default)</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'ollama'} style={providerPreset !== 'ollama' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('ollama')}>Ollama (local)</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'openrouter'} style={providerPreset !== 'openrouter' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('openrouter')}>OpenRouter</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'deepseek'} style={providerPreset !== 'deepseek' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('deepseek')}>DeepSeek</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'zai'} style={providerPreset !== 'zai' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('zai')}>Z.ai (GLM)</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'custom'} style={providerPreset !== 'custom' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => { providerPreset = 'custom'; providerRef = ''; providerDirty = true; }}>Custom</button>
                    </div>
                    {#if providerPreset === 'openrouter'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        Get your API key at <a href="https://openrouter.ai/keys" target="_blank" style="color:var(--primary)">openrouter.ai/keys</a>. Access any model — Claude, GPT, Gemini, DeepSeek, Llama, and more — from one endpoint.
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">API Key</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="sk-or-..." style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Model</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="anthropic/claude-sonnet-4-5" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">Examples: anthropic/claude-sonnet-4-5, openai/gpt-4o, google/gemini-2.0-flash, deepseek/deepseek-chat</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'deepseek'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        Get your API key at <a href="https://platform.deepseek.com/api_keys" target="_blank" style="color:var(--primary)">platform.deepseek.com</a>. ~10x cheaper than Claude Sonnet, great for coding tasks.
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">API Key</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="Your DeepSeek API key" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Model</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="deepseek-chat" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">Options: deepseek-chat (V3), deepseek-reasoner (R1)</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'zai'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        Get your API key at <a href="https://platform.z.ai" target="_blank" style="color:var(--primary)">platform.z.ai</a> → API Keys. Subscribe to the Coding Plan for GLM-5.1 and other models.
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">API Key</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="Your Z.ai API key" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Model</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="glm-5.1" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">Options: glm-5.1, glm-5, glm-4.7, glm-4.5-air</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'ollama' || providerPreset === 'custom'}
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Base URL</div>
                            <input type="text" class="form-input" bind:value={providerUrl} on:input={() => providerDirty = true} placeholder="http://localhost:11434" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">API Key</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="ollama or your key" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">Model Override</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="leave empty to use agent's model setting" style="width:100%">
                        </div>
                    </div>
                    {/if}
                </div>
            </div>
            {/if}<!-- end behavior tab -->

            {#if activeTab === 'automation'}
            <!-- Skills -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.5rem">
                    <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Skills</span>
                    <div style="display:flex;gap:0.4rem;align-items:center">
                        <a href="https://github.com/anthropics/skills" target="_blank" rel="noopener" class="btn btn-sm" style="font-size:0.7rem">Browse Community</a>
                        <button class="btn btn-sm btn-primary" on:click={() => createSkillOpen = !createSkillOpen}>+ Create</button>
                        {#if skillsPendingApply}
                            <button class="btn btn-sm" style="background:var(--accent);color:#fff" on:click={applySkills}>Apply &amp; Restart</button>
                        {/if}
                    </div>
                </div>
                <div style="display:flex;gap:0.4rem;align-items:center;margin-top:0.5rem">
                    <input type="text" bind:value={gitSkillUrl} placeholder="https://github.com/org/skill-name" style="flex:1;font-family:var(--font-grotesk);font-size:0.8rem;padding:0.35rem 0.5rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg)">
                    <button class="btn btn-sm btn-primary" on:click={installSkillFromGit} disabled={gitSkillLoading}>
                        {gitSkillLoading ? 'Cloning...' : 'Install from Git'}
                    </button>
                </div>
                {#if skillsPendingApply}
                    <div style="background:var(--warning-bg, #fff3cd);border:none;border-radius:var(--radius-lg);padding:0.5rem 0.8rem;font-size:0.75rem;margin-top:0.5rem">
                        Skill changes pending — click "Apply &amp; Restart" to activate.
                    </div>
                {/if}
            </div>
            <!-- Create Skill from SKILL.md -->
            {#if createSkillOpen}
                <div style="padding:1rem 1.5rem;background:var(--surface-1);border-radius:var(--radius-lg);margin-top:0.5rem">
                    <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.5rem">Create Skill from SKILL.md</div>
                    <p style="font-size:0.75rem;color:var(--gray-mid);margin:0 0 0.5rem 0">
                        Paste a <a href="https://agentskills.io/specification" target="_blank" rel="noopener">SKILL.md</a> below. The agent can also create skills for itself via the <code>create_skill</code> tool.
                    </p>
                    <textarea bind:value={newSkillMd} rows="12" style="width:100%;font-family:var(--font-grotesk);font-size:0.8rem;padding:0.5rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg);resize:vertical"></textarea>
                    <div style="display:flex;gap:0.5rem;margin-top:0.5rem">
                        <button class="btn btn-primary" on:click={createSkillFromMd}>Create &amp; Assign</button>
                        <button class="btn" on:click={() => createSkillOpen = false}>Cancel</button>
                    </div>
                </div>
            {/if}
            <div>
                {#if visibleSkills.length === 0 && !showCoreSkills}
                    <div style="padding:0.8rem 1.5rem;font-size:0.8rem;color:var(--gray-mid)">
                        No custom skills assigned.
                        {#if agentSkills.length > 0}
                            <button style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:0.75rem;text-decoration:underline;padding:0" on:click={() => showCoreSkills = true}>
                                Show {agentSkills.length} default skill{agentSkills.length !== 1 ? 's' : ''}
                            </button>
                        {/if}
                    </div>
                {:else}
                    {#each visibleSkills as s}
                        <div class="token-item" style={!s.effective_enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{s.name}</span>
                            <span class="badge" style="background:var(--gray-mid);color:#fff;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px">{s.category}</span>
                            {#if s.assigned_by === 'shared'}
                                <span class="badge badge-on" style="font-size:0.65rem">Shared</span>
                            {:else if s.assigned_by !== 'system'}
                                <span class="badge" style="font-size:0.65rem;background:var(--surface-3)">{s.assigned_by}</span>
                            {/if}
                            {#if s.description}
                                <span style="color:var(--gray-mid);font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px">{s.description}</span>
                            {/if}
                            <span style="flex:1"></span>
                            {#if s.category !== 'core'}
                                <button class="btn btn-sm" on:click={() => toggleAgentSkill(s.name, !s.effective_enabled)}>
                                    {s.effective_enabled ? 'Disable' : 'Enable'}
                                </button>
                            {/if}
                            {#if s.assigned_by !== 'shared' && s.category !== 'core'}
                                <button class="btn btn-sm btn-danger" on:click={() => removeAgentSkill(s.name)}>X</button>
                            {/if}
                        </div>
                    {/each}
                    {#if !showCoreSkills && coreSkillCount > 0}
                        <div style="padding:0.4rem 1.5rem">
                            <button style="background:none;border:none;color:var(--gray-mid);cursor:pointer;font-size:0.7rem;text-decoration:underline;padding:0" on:click={() => showCoreSkills = true}>
                                + {coreSkillCount} default skills (always on)
                            </button>
                        </div>
                    {:else if showCoreSkills}
                        <div style="padding:0.4rem 1.5rem">
                            <button style="background:none;border:none;color:var(--gray-mid);cursor:pointer;font-size:0.7rem;text-decoration:underline;padding:0" on:click={() => showCoreSkills = false}>
                                Hide default skills
                            </button>
                        </div>
                    {/if}
                {/if}
            </div>
            <!-- Available Skills -->
            {#if availableSkills.length > 0}
                <div style="padding:0.8rem 1.5rem;background:var(--surface-1);border-radius:var(--radius-lg);margin-top:0.5rem">
                    <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.5rem;color:var(--gray-mid)">Available to Add</div>
                    {#each availableSkills as s}
                        <div style="display:flex;align-items:center;gap:0.5rem;padding:0.3rem 0;font-size:0.8rem">
                            <span style="font-family:var(--font-grotesk);font-weight:600">{s.name}</span>
                            <span class="badge" style="background:var(--gray-mid);color:#fff;font-size:0.6rem;padding:0.1rem 0.3rem;border-radius:3px">{s.category}</span>
                            <span style="flex:1;color:var(--gray-mid);font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{s.description}</span>
                            <button class="btn btn-sm btn-primary" on:click={() => assignSkill(s.name)}>+ Add</button>
                        </div>
                    {/each}
                </div>
            {/if}

            <!-- MCP Servers -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem;display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">MCP Servers</span>
                <button class="btn btn-sm btn-primary" on:click={openMcpModal}>+ Add</button>
            </div>
            <div>
                {#if mcpServers.length === 0}
                    <div class="empty">No MCP servers configured.</div>
                {:else}
                    {#each mcpServers as srv}
                        {@const sourceStyle = srv.source === 'core' ? 'background:var(--accent);color:var(--accent-contrast)' : srv.source === 'skill' ? 'background:var(--tone-lilac-bg);color:var(--tone-lilac-text)' : srv.source === 'custom' ? 'background:var(--tone-info-bg);color:var(--tone-info-text)' : 'background:var(--surface-3)'}
                        <div class="token-item" style={srv.source === 'custom' && !srv.enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{srv.name}</span>
                            <span class="badge" style="{sourceStyle};font-size:0.6rem">{srv.source}</span>
                            <span class="badge badge-model" style="font-size:0.6rem">{srv.server_type || 'stdio'}</span>
                            {#if srv.source === 'custom'}
                                <span class="badge badge-{srv.enabled ? 'on' : 'off'}">{srv.enabled ? 'Enabled' : 'Disabled'}</span>
                            {/if}
                            <span style="flex:1"></span>
                            {#if srv.source === 'custom'}
                                <button class="btn btn-sm" on:click={() => toggleMcpServer(srv.name, !srv.enabled)}>{srv.enabled ? 'Disable' : 'Enable'}</button>
                                <button class="btn btn-sm btn-danger" on:click={() => removeMcpServer(srv.name)}>X</button>
                            {/if}
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Triggers -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem;display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Triggers</span>
                <button class="btn btn-sm btn-primary" on:click={openTriggerModal}>+ Add</button>
            </div>
            <div>
                {#if triggers.length === 0}
                    <div class="empty">No triggers configured. Add a webhook, URL watcher, or file watcher.</div>
                {:else}
                    {#each triggers as t}
                        <div class="token-item">
                            <span class="badge badge-{t.trigger_type === 'webhook' ? 'model' : t.trigger_type === 'url' ? 'running' : 'off'}">{t.trigger_type}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{t.name || t.trigger_type}</span>
                            <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? 'On' : 'Off'}</span>
                            {#if t.fire_count > 0}
                                <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--text-muted)">{t.fire_count}× fired</span>
                            {/if}
                            {#if t.last_fired_at}
                                <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--text-muted)">{timeAgo(t.last_fired_at * 1000)}</span>
                            {/if}
                            <button class="btn btn-sm" on:click={() => toggleTrigger(t.id, !t.enabled)}>{t.enabled ? 'Disable' : 'Enable'}</button>
                            <button class="btn btn-sm" on:click={() => testTrigger(t.id)}>Test</button>
                            <button class="btn btn-sm btn-danger" on:click={() => deleteTrigger(t.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Schedules / Cron Jobs -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Cron Jobs</span>
                <button class="btn btn-sm btn-primary" on:click={() => cronModalOpen = true}>+ Cron Job</button>
            </div>
            <div>
                {#if schedules.length === 0}
                    <div class="empty" style="padding:0.8rem 1.5rem;font-size:0.8rem">No schedules.</div>
                {:else}
                    {#each schedules as s}
                        <div class="token-item" style={!s.enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{s.name || 'unnamed'}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid)">{s.cron}</span>
                            <span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? 'Active' : 'Off'}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => toggleSchedule(s.id, !s.enabled)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => removeSchedule(s.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>
            {/if}<!-- end automation tab -->

            {#if activeTab === 'runtime'}
            <!-- Live Sessions (formerly Streaming Sessions) -->
            <div class="detail-section-header">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Live Sessions</span>
                <button class="btn btn-sm btn-primary" on:click={createStreamingSession}>+ Session</button>
            </div>
            <div>
                {#if streamingSessions.length === 0}
                    <div class="empty">No live sessions.</div>
                {:else}
                    {#each streamingSessions as ss}
                        <div class="token-item">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{ss.label}</span>
                            <span class="badge badge-{ss.connected ? 'on' : 'off'}">{ss.connected ? 'connected' : 'disconnected'}</span>
                            {#if ss.stats?.pending_responses > 0}<span class="badge" style="background:#fef3c7;color:#92400e">{ss.stats.pending_responses} pending</span>{/if}
                            <span style="flex:1"></span>
                            {#if ss.label !== 'main'}<button class="btn btn-sm btn-danger" on:click={() => deleteStreamingSession(ss.label)}>X</button>{/if}
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Conversations (formerly Active Sessions) -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">Conversations</span>
            </div>
            <div>
                {#if agentSessions.length === 0}
                    <div class="empty">No active sessions.</div>
                {:else}
                    {#each agentSessions as s}
                        {@const sType = s.session_type || 'chat'}
                        {@const typeStyle = sType === 'main' ? 'background:var(--accent);color:var(--accent-contrast)' : sType === 'worker' ? 'background:var(--tone-neutral-bg);color:var(--tone-neutral-text)' : 'background:var(--tone-info-bg);color:var(--tone-info-text)'}
                        <div class="token-item">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{s.id}</span>
                            <span class="badge" style={typeStyle}>{sType}</span>
                            <span class="badge badge-{s.state === 'idle' ? 'on' : s.state === 'running' ? 'model' : 'off'}">{s.state}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid)">{s.context_used_pct}% ctx</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => window.location.hash = `/chat#${s.id}`}>Chat</button>
                        </div>
                    {/each}
                {/if}
            </div>
            {/if}<!-- end runtime tab -->
        </div>
    {/if}
</div>

<!-- Wizard -->
{#if wizardOpen}
    <div class="wizard-overlay">
        <div class="wizard">
            <div class="wizard-header">
                <div class="wizard-title">{importMode ? 'IMPORT FROM OPENCLAW' : 'NEW AGENT'}<span class="y">.</span></div>
                <div class="wizard-sub">{importMode ? 'bring your agent home' : 'yer a wizard, agent'}</div>
            </div>

            {#if !importMode && wizStep === -1}
                <!-- Entry choice screen -->
                <div class="wizard-body">
                    <div class="wizard-label">How do you want to start?</div>
                    <div class="wizard-hint">Choose a path to create your new agent.</div>
                    <div class="import-entry-grid">
                        <div class="import-entry-card" on:click={() => { wizStep = 0; }}>
                            <div class="import-entry-icon">✨</div>
                            <div class="import-entry-title">Start from scratch</div>
                            <div class="import-entry-desc">Build your agent from the ground up with the standard wizard.</div>
                        </div>
                        <div class="import-entry-card import-entry-disabled">
                            <div class="import-entry-icon">📋</div>
                            <div class="import-entry-title">Use template</div>
                            <div class="import-entry-desc">Coming soon — pre-built agent templates.</div>
                        </div>
                        <div class="import-entry-card" on:click={() => { importMode = true; importStep = 1; }}>
                            <div class="import-entry-icon">🐾</div>
                            <div class="import-entry-title">Import from OpenClaw</div>
                            <div class="import-entry-desc">Migrate your existing OpenClaw agent — soul, memory, connections, and all.</div>
                        </div>
                    </div>
                </div>
                <div class="wizard-footer">
                    <span></span>
                    <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Cancel</button>
                    <span></span>
                </div>

            {:else if importMode}
                <!-- Import flow -->
                {#if importStep === 1}
                    <!-- Upload step -->
                    <div class="wizard-progress">
                        {#each ['Upload','Preview','Confirm'] as label, i}
                            <div class="wizard-step-dot" class:active={i === 0} class:done={false}></div>
                        {/each}
                    </div>
                    <div class="wizard-body">
                        <div class="wizard-label">Upload Workspace</div>
                        <div class="wizard-hint">Export your OpenClaw agent folder as a zip and drop it below.</div>

                        <!-- Drop zone -->
                        <div
                            class="import-dropzone"
                            class:dragover={importDragover}
                            on:dragover|preventDefault={() => importDragover = true}
                            on:dragleave={() => importDragover = false}
                            on:drop|preventDefault={(e) => { importDragover = false; const f = e.dataTransfer?.files?.[0]; if (f) importFiles = { ...importFiles, workspace: f }; }}
                            on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.zip'; i.onchange = (/** @type {any} */ e) => { if (e.target?.files?.[0]) importFiles = { ...importFiles, workspace: e.target.files[0] }; }; i.click(); }}
                        >
                            {#if importFiles.workspace}
                                <div class="import-dropzone-file">📦 {importFiles.workspace.name}</div>
                                <div class="import-dropzone-hint">Click to change</div>
                            {:else}
                                <div class="import-dropzone-icon">📂</div>
                                <div class="import-dropzone-label">Drop workspace zip here</div>
                                <div class="import-dropzone-hint">or click to browse</div>
                            {/if}
                        </div>

                        <div class="wizard-label" style="margin-top:1rem">openclaw.json <span style="color:var(--gray-mid);font-weight:400;text-transform:none">(optional)</span></div>
                        <div class="wizard-hint" style="margin-top:-0.5rem">Channel tokens and model config. Usually at ~/.openclaw/openclaw.json</div>
                        <div class="import-file-row">
                            <span class="import-file-name">{importFiles.config ? importFiles.config.name : 'No file selected'}</span>
                            <button class="wizard-btn" style="padding:0.4rem 0.8rem;font-size:0.7rem" on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.json'; i.onchange=(/** @type {any} */ e)=>{if(e.target?.files?.[0]) importFiles={...importFiles,config:e.target.files[0]}}; i.click(); }}>Browse</button>
                        </div>

                        <div class="wizard-label" style="margin-top:0.75rem">.clawhub/lock.json <span style="color:var(--gray-mid);font-weight:400;text-transform:none">(optional)</span></div>
                        <div class="wizard-hint" style="margin-top:-0.5rem">Installed skill list for better migration coverage.</div>
                        <div class="import-file-row">
                            <span class="import-file-name">{importFiles.lock ? importFiles.lock.name : 'No file selected'}</span>
                            <button class="wizard-btn" style="padding:0.4rem 0.8rem;font-size:0.7rem" on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.json'; i.onchange=(/** @type {any} */ e)=>{if(e.target?.files?.[0]) importFiles={...importFiles,lock:e.target.files[0]}}; i.click(); }}>Browse</button>
                        </div>

                        {#if importError}
                            <div class="import-error">{importError}</div>
                        {/if}
                    </div>
                    <div class="wizard-footer">
                        <button class="wizard-btn" on:click={() => { importMode = false; wizStep = -1; }}>Back</button>
                        <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Cancel</button>
                        <button class="wizard-btn wizard-btn-primary" on:click={importParse} disabled={importLoading || !importFiles.workspace}>
                            {importLoading ? 'Parsing...' : 'Parse'}
                        </button>
                    </div>

                {:else if importStep === 2}
                    <!-- Preview step -->
                    <div class="wizard-progress">
                        {#each ['Upload','Preview','Confirm'] as label, i}
                            <div class="wizard-step-dot" class:active={i === 1} class:done={i < 1}></div>
                        {/each}
                    </div>
                    <div class="wizard-body">
                        {#if importLoading}
                            <div class="import-loading">
                                <div class="import-spinner"></div>
                                <div class="import-loading-text">Analyzing your OpenClaw agent...</div>
                            </div>
                        {:else if importPreview}
                            {@const p = importPreview}

                            <!-- Identity section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">Identity</span>
                                    <span class="import-badge" style={importStatusBadgeStyle(p.identity?.status || 'ok')}>{importStatusIcon(p.identity?.status || 'ok')}</span>
                                </div>
                                <div class="import-field-row">
                                    <span class="import-field-key">Name</span>
                                    <span class="import-field-val">{p.identity?.name || '—'}</span>
                                </div>
                                {#if p.identity?.soul_preview}
                                    <div class="import-field-row">
                                        <span class="import-field-key">Soul</span>
                                        <span class="import-field-val import-truncate">{p.identity.soul_preview}</span>
                                    </div>
                                {/if}
                                {#if p.identity?.boundaries_preview}
                                    <div class="import-field-row">
                                        <span class="import-field-key">Boundaries</span>
                                        <span class="import-field-val import-truncate">{p.identity.boundaries_preview}</span>
                                    </div>
                                {/if}
                            </div>

                            <!-- Memory section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">Memory</span>
                                    <span class="import-count-badge">{p.memory?.count ?? 0} memories</span>
                                </div>
                                {#if p.memory_store_available === false && (p.memory?.count ?? 0) > 0}
                                    <div style="background:rgba(239,68,68,0.12);border-radius:var(--radius-lg);padding:0.5rem 0.75rem;font-size:0.75rem;color:var(--red,#ef4444);margin-bottom:0.4rem">
                                        ⚠️ Memory store unavailable — {p.memory.count} memories shown in preview but won't be imported. Ensure pinky_memory is running.
                                    </div>
                                {/if}
                                {#if p.memory?.samples && p.memory.samples.length > 0}
                                    {#each p.memory.samples.slice(0,3) as sample}
                                        <div class="import-memory-sample">{sample}</div>
                                    {/each}
                                {:else}
                                    <div class="import-empty">No memory found.</div>
                                {/if}
                            </div>

                            <!-- Connections section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">Connections</span>
                                </div>
                                {#if p.connections && p.connections.length > 0}
                                    {#each p.connections as conn}
                                        <div class="import-conn-row">
                                            <span class="import-field-key">{conn.platform}</span>
                                            <span class="import-badge" style={importStatusBadgeStyle(conn.status)} title={conn.note || ''}>
                                                {importStatusIcon(conn.status)} {conn.note ? conn.note : ''}
                                            </span>
                                        </div>
                                    {/each}
                                {:else}
                                    <div class="import-empty">No connections found.</div>
                                {/if}
                            </div>

                            <!-- Automation section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">Automation</span>
                                </div>
                                {#if p.skills && p.skills.length > 0}
                                    {#each p.skills as skill}
                                        <div class="import-conn-row">
                                            <span class="import-field-key">{skill.name}</span>
                                            <span class="import-badge" style={importStatusBadgeStyle(skill.status)} title={skill.note || ''}>
                                                {importStatusIcon(skill.status)}
                                            </span>
                                        </div>
                                    {/each}
                                {/if}
                                {#if (p.schedules_count ?? 0) > 0}
                                    <div class="import-field-row">
                                        <span class="import-field-key">Schedules</span>
                                        <span class="import-field-val">{p.schedules_count} task{p.schedules_count !== 1 ? 's' : ''}</span>
                                    </div>
                                {/if}
                                {#if (!p.skills || p.skills.length === 0) && (p.schedules_count ?? 0) === 0}
                                    <div class="import-empty">None detected.</div>
                                {/if}
                            </div>

                            <!-- Warnings summary -->
                            {#if p.warnings && p.warnings.length > 0}
                                <div class="import-warnings">
                                    <div class="import-warnings-title">⚠️ {p.warnings.length} item{p.warnings.length !== 1 ? 's' : ''} need{p.warnings.length === 1 ? 's' : ''} attention before migrating</div>
                                    {#each p.warnings as w}
                                        <div class="import-warning-item">— {w}</div>
                                    {/each}
                                </div>
                            {/if}

                            {#if importError}
                                <div class="import-error">{importError}</div>
                            {/if}
                        {/if}
                    </div>
                    <div class="wizard-footer">
                        <button class="wizard-btn" on:click={() => { importStep = 1; importPreview = null; importError = null; }}>Back</button>
                        <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Cancel</button>
                        <button class="wizard-btn wizard-btn-primary" on:click={importApply} disabled={importLoading || !importPreview}>
                            {importLoading ? 'Working...' : 'Confirm Import'}
                        </button>
                    </div>

                {:else if importStep === 3}
                    <!-- Confirm / progress step -->
                    <div class="wizard-progress">
                        {#each ['Upload','Preview','Confirm'] as label, i}
                            <div class="wizard-step-dot" class:active={i === 2} class:done={i < 2}></div>
                        {/each}
                    </div>
                    <div class="wizard-body">
                        {#if importLoading}
                            <div class="import-loading">
                                <div class="import-spinner"></div>
                                <div class="import-loading-text">Creating agent...</div>
                            </div>
                        {:else if importProgress.done}
                            <div class="import-done">
                                <div class="import-done-icon">🎉</div>
                                <div class="import-done-title">Agent {importAgentName} is ready!</div>
                                {#if importAgentName && importAgentName.endsWith('-imported')}
                                    <div class="import-done-note">Agent created as '<strong>{importAgentName}</strong>' — rename it in Settings after creation.</div>
                                {/if}
                                {#if importProgress.failed > 0}
                                    <div class="import-done-warn">⚠️ {importProgress.imported} memories imported, {importProgress.failed} failed.</div>
                                {:else}
                                    <div class="import-done-stat">{importProgress.imported} memories imported successfully.</div>
                                {/if}
                                <div class="import-done-actions">
                                    <button class="wizard-btn wizard-btn-primary" on:click={() => { closeWizard(); window.location.hash = `/agents/${importAgentName}`; }}>Configure</button>
                                    <button class="wizard-btn" on:click={() => { closeWizard(); window.location.hash = `/chat`; }}>Chat</button>
                                </div>
                            </div>
                        {:else}
                            <!-- Memory import in progress -->
                            {#if true}
                                {@const pct = importProgress.total > 0 ? Math.round((importProgress.imported + importProgress.failed) / importProgress.total * 100) : 0}
                                <div class="import-progress-wrap">
                                    <div class="import-done-icon">⚙️</div>
                                    <div class="import-loading-text">Agent created. Importing memories...</div>
                                    <div class="import-progress-bar-bg">
                                        <div class="import-progress-bar-fill" style="width:{pct}%"></div>
                                    </div>
                                    <div class="import-progress-label">{importProgress.imported + importProgress.failed} / {importProgress.total} ({pct}%)</div>
                                </div>
                            {/if}
                        {/if}
                        {#if importError}
                            <div class="import-error">{importError}</div>
                        {/if}
                    </div>
                    {#if !importProgress.done && !importLoading}
                        <div class="wizard-footer">
                            <span></span>
                            <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Close (import continues in background)</button>
                            <span></span>
                        </div>
                    {:else if importProgress.done}
                        <div class="wizard-footer">
                            <span></span>
                            <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Close</button>
                            <span></span>
                        </div>
                    {/if}
                {/if}

            {:else}
                <!-- Standard wizard steps -->
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
                        <div class="wizard-label">Heart Config</div>
                        <div class="wizard-hearts">
                            {#each [['sidekick','ᓚᘏᗢ','Sidekick','Personal assistant.'],['worker','>_','Worker','Heads-down coder.'],['lead','[*]','Team Lead','Reviews code, coordinates.'],['custom','{?}','Custom','Write your own.']] as [val, icon, title, desc]}
                                <div class="wizard-heart" class:selected={wizHeart === val} on:click={() => { wizHeart = val; wizRole = val === 'custom' ? 'sidekick' : val; wizAutoStart = (val === 'sidekick' || val === 'lead'); }}>
                                    <div class="wizard-heart-icon">{icon}</div>
                                    <div class="wizard-heart-name">{title}</div>
                                    <div class="wizard-heart-desc">{desc}</div>
                                </div>
                            {/each}
                        </div>
                        {#if wizHeart === 'custom'}
                            <textarea class="wizard-input" bind:value={wizCustomSoul} rows="5" placeholder="Write your agent's soul..."></textarea>
                        {/if}
                    {:else if wizStep === 3}
                        <div class="wizard-label">Outreach</div>
                        <div class="wizard-hint">Connect to the outside world. All optional.</div>
                        <div style="margin-bottom:1.5rem"><span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;color:var(--yellow)">TELEGRAM</span>
                            <input type="password" class="wizard-input" bind:value={wizTelegramToken} placeholder="Bot token..."></div>
                        <div style="margin-bottom:1.5rem"><span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;color:var(--yellow)">DISCORD</span>
                            <input type="password" class="wizard-input" bind:value={wizDiscordToken} placeholder="Discord bot token..."></div>
                        <div><span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;color:var(--yellow)">SLACK</span>
                            <input type="password" class="wizard-input" bind:value={wizSlackToken} placeholder="xoxb-..."></div>
                    {:else if wizStep === 4}
                        <div class="wizard-label">Ready to Deploy</div>
                        <div class="wizard-summary">
                            Name: <span class="val">{wizName || '(unnamed)'}</span><br>
                            Display: <span class="val">{wizDisplayName || wizName}</span><br>
                            Brain: <span class="val">{wizModel.toUpperCase()}</span><br>
                            Heart: <span class="val">{wizHeart.toUpperCase()}</span><br>
                            Auto-Start: <span class="val">{wizAutoStart ? 'Yes' : 'No'}</span><br>
                            Heartbeat: <span class="val">{wizHeartbeatInterval ? wizHeartbeatInterval + 's' : 'Disabled'}</span><br>
                            Outreach: <span class="val">{wizSummaryPlatforms.length ? wizSummaryPlatforms.join(', ') : 'None (local only)'}</span>
                        </div>
                    {/if}
                </div>
                <div class="wizard-footer">
                    <button class="wizard-btn" on:click={wizardPrev}>Back</button>
                    <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">Cancel</button>
                    <button class="wizard-btn wizard-btn-primary" on:click={wizardNext}>{wizStep === wizTotalSteps - 1 ? 'Summon' : 'Next'}</button>
                </div>
            {/if}
        </div>
    </div>
{/if}

<style>
    .agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(350px, 1fr)); gap: 0.75rem; }
    .agent-card { border: none; padding: 1.5rem; background: var(--surface-1); border-radius: var(--radius-lg); }
    .agent-card:hover { background: var(--hover-accent); }
    .agent-name { font-family: var(--font-grotesk); font-size: 1.1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .agent-meta { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-bottom: 0.8rem; }
    .agent-desc { font-size: 0.82rem; color: var(--gray-mid); margin-bottom: 0.7rem; max-height: 36px; overflow: hidden; line-height: 1.4; }
    .agent-runtime { display: flex; align-items: center; gap: 0.6rem; margin-bottom: 0.8rem; flex-wrap: wrap; }
    .status-pill { font-size: 0.68rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase; padding: 0.15rem 0.5rem; border-radius: 99px; font-family: var(--font-grotesk); }
    .status-working { background: #dcfce7; color: #166534; }
    .status-online  { background: #dbeafe; color: #1e40af; }
    .status-idle    { background: var(--gray-light); color: var(--gray-mid); }
    .status-offline,.status-unknown { background: var(--gray-light); color: var(--gray-mid); opacity:0.6; }
    .ctx-bar { display:inline-block; width:60px; height:5px; background:var(--gray-light); border-radius:99px; overflow:hidden; vertical-align:middle; }
    .ctx-fill { display:block; height:100%; border-radius:99px; transition:width 0.4s; }
    .ctx-ok   { background: var(--accent, #f5c842); }
    .ctx-warn { background: #f97316; }
    .ctx-label { font-size: 0.7rem; color: var(--gray-mid); font-family: var(--font-grotesk); }
    .last-active { font-size: 0.7rem; color: var(--gray-mid); font-family: var(--font-grotesk); margin-left: auto; }
    .agent-actions { display: flex; gap: 0.3rem; flex-wrap: wrap; }

    .directive-item { display: flex; align-items: center; gap: 0.8rem; padding: 0.6rem 1rem; background: var(--surface-1); border-radius: var(--radius-lg); }
    .directive-item:nth-child(even) { background: var(--surface-2); }
    .directive-priority { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; background: var(--yellow); padding: 0.1rem 0.4rem; min-width: 24px; text-align: center; border-radius: var(--radius-lg); }
    .directive-text { flex: 1; font-size: 0.88rem; }
    .directive-inactive { opacity: 0.5; text-decoration: line-through; }

    .token-item { display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem; background: var(--surface-1); border-radius: var(--radius-lg); }
    .token-item:nth-child(even) { background: var(--surface-2); }

    .wizard-overlay { position: fixed; inset: 0; background: var(--overlay-scrim); z-index: 999; display: flex; align-items: center; justify-content: center; }
    .wizard { background: var(--surface-inverse); color: var(--text-inverse); border: none; border-radius: var(--radius-xl); max-width: 600px; width: 95%; max-height: 90vh; overflow-y: auto; }
    .wizard-header { padding: 2rem 2rem 1rem; }
    .wizard-title { font-family: var(--font-grotesk); font-size: 1.5rem; font-weight: 700; }
    .wizard-title .y { color: var(--yellow); }
    .wizard-sub { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); margin-top: 0.3rem; }
    .wizard-progress { display: flex; gap: 0.2rem; padding: 0 2rem; margin-bottom: 1.5rem; }
    .wizard-step-dot { flex: 1; height: 4px; background: var(--text-muted); border-radius: 2px; }
    .wizard-step-dot.active { background: var(--yellow); }
    .wizard-step-dot.done { background: var(--green); }
    .wizard-body { padding: 0 2rem 2rem; }
    .wizard-label { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--yellow); margin-bottom: 0.5rem; }
    .wizard-hint { font-size: 0.8rem; color: var(--text-muted); margin-bottom: 1rem; }
    .wizard-id-preview { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-top: -0.7rem; margin-bottom: 0.8rem; }
    .wizard-input { font-family: var(--font-grotesk); font-size: 1rem; padding: 0.8rem 1rem; border: none; background: rgba(255,255,255,0.08); color: var(--text-inverse); width: 100%; margin-bottom: 1rem; border-radius: var(--radius-lg); }
    .wizard-input:focus { outline: 2px solid var(--accent); }
    .wizard-options { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-option { padding: 1rem; border: none; background: rgba(255,255,255,0.05); border-radius: var(--radius-lg); cursor: pointer; text-align: center; transition: all 0.15s; }
    .wizard-option:hover { background: rgba(255,255,255,0.1); }
    .wizard-option.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-option-title { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; margin-bottom: 0.2rem; }
    .wizard-option-desc { font-size: 0.75rem; color: var(--text-muted); }
    .wizard-option.selected .wizard-option-desc { color: var(--accent); }
    .wizard-hearts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-heart { padding: 1.2rem; border: none; background: rgba(255,255,255,0.05); border-radius: var(--radius-lg); cursor: pointer; transition: all 0.15s; }
    .wizard-heart:hover { background: rgba(255,255,255,0.1); }
    .wizard-heart.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-heart-icon { font-size: 1.5rem; margin-bottom: 0.3rem; }
    .wizard-heart-name { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; }
    .wizard-heart-desc { font-size: 0.7rem; color: var(--text-muted); margin-top: 0.2rem; }
    .wizard-heart.selected .wizard-heart-desc { color: var(--accent); }
    .wizard-footer { display: flex; justify-content: space-between; padding: 1.5rem 2rem; background: rgba(255,255,255,0.03); }
    .wizard-btn { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; padding: 0.6rem 1.5rem; border: none; background: rgba(255,255,255,0.08); color: var(--text-inverse); cursor: pointer; text-transform: uppercase; border-radius: var(--radius-lg); }
    .wizard-btn:hover { background: rgba(255,255,255,0.15); color: var(--accent); }
    .wizard-btn-primary { background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px var(--primary); }
    .wizard-btn-primary:hover { background: var(--primary-container); }
    .wizard-btn-primary:active { transform: scale(0.98); }
    .wizard-summary { font-family: var(--font-grotesk); font-size: 0.85rem; line-height: 2; }
    .wizard-summary :global(.val) { color: var(--accent); font-weight: 700; }
    .val { color: var(--yellow); font-weight: 700; }

    .btn-danger-text { background: none; border: none; color: var(--text-muted); font-size: 0.6rem; cursor: pointer; padding: 0.2rem 0.4rem; font-family: var(--font-grotesk); text-transform: uppercase; letter-spacing: 0.05em; }
    .btn-danger-text:hover { color: var(--red); }

    .btn-confirm-delete { background: var(--gray-light); color: var(--gray-mid); border: none; border-radius: var(--radius-lg); cursor: not-allowed; }
    .btn-confirm-delete.ready { background: var(--red); color: var(--white); cursor: pointer; }

    /* Detail panel tabs */
    .detail-tabs { display: flex; gap: 0.25rem; padding: 0 1rem; border-bottom: 1px solid var(--border); margin-bottom: 0.5rem; margin-top: 0.5rem; }
    .detail-tab { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; padding: 0.5rem 0.75rem; border: none; background: none; color: var(--gray-mid); cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; position: relative; }
    .detail-tab:hover { color: var(--text); }
    .detail-tab.active { color: var(--text); border-bottom-color: var(--accent); }
    .dirty-dot { position: absolute; top: 0.3rem; right: 0.15rem; width: 5px; height: 5px; background: var(--yellow); border-radius: 50%; }

    /* Section header within detail tabs — reusable pattern */
    .detail-section-header { padding: 1rem 1.5rem; background: var(--surface-2); border-radius: var(--radius-lg); display: flex; justify-content: space-between; align-items: center; }

    @media (max-width: 900px) {
        .agent-grid { grid-template-columns: 1fr; }
    }

    /* Import / OpenClaw migration wizard */
    .import-entry-grid { display: grid; grid-template-columns: 1fr; gap: 0.75rem; margin-bottom: 1rem; }
    .import-entry-card { padding: 1.2rem 1.4rem; background: rgba(255,255,255,0.05); border-radius: var(--radius-lg); cursor: pointer; transition: all 0.15s; border: 1px solid transparent; }
    .import-entry-card:hover { background: rgba(255,255,255,0.1); border-color: var(--accent); }
    .import-entry-card.import-entry-disabled { opacity: 0.45; cursor: not-allowed; }
    .import-entry-card.import-entry-disabled:hover { background: rgba(255,255,255,0.05); border-color: transparent; }
    .import-entry-icon { font-size: 1.4rem; margin-bottom: 0.4rem; }
    .import-entry-title { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 700; margin-bottom: 0.2rem; }
    .import-entry-desc { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; }

    .import-dropzone { border: 2px dashed rgba(255,255,255,0.2); border-radius: var(--radius-lg); padding: 2rem 1.5rem; text-align: center; cursor: pointer; transition: border-color 0.15s, background 0.15s; margin-bottom: 1rem; }
    .import-dropzone:hover { border-color: rgba(255,255,255,0.4); background: rgba(255,255,255,0.03); }
    .import-dropzone.dragover { border: 2px solid var(--accent); background: rgba(255,255,255,0.07); }
    .import-dropzone-icon { font-size: 2rem; margin-bottom: 0.5rem; }
    .import-dropzone-label { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 700; margin-bottom: 0.2rem; }
    .import-dropzone-hint { font-size: 0.75rem; color: var(--text-muted); }
    .import-dropzone-file { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 700; margin-bottom: 0.2rem; }

    .import-file-row { display: flex; align-items: center; gap: 0.75rem; margin-bottom: 0.75rem; }
    .import-file-name { font-family: var(--font-grotesk); font-size: 0.78rem; color: var(--text-muted); flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    .import-loading { display: flex; flex-direction: column; align-items: center; justify-content: center; padding: 3rem 0; gap: 1.2rem; }
    .import-spinner { width: 36px; height: 36px; border: 3px solid rgba(255,255,255,0.1); border-top-color: var(--accent); border-radius: 50%; animation: import-spin 0.8s linear infinite; }
    @keyframes import-spin { to { transform: rotate(360deg); } }
    .import-loading-text { font-family: var(--font-grotesk); font-size: 0.85rem; color: var(--text-muted); }

    .import-section { background: rgba(255,255,255,0.04); border-radius: var(--radius-lg); padding: 0.9rem 1rem; margin-bottom: 0.75rem; }
    .import-section-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.6rem; }
    .import-field-row { display: flex; gap: 0.75rem; padding: 0.25rem 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.8rem; }
    .import-field-row:last-child { border-bottom: none; }
    .import-field-key { font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 700; color: var(--text-muted); min-width: 90px; text-transform: uppercase; letter-spacing: 0.04em; padding-top: 0.1rem; }
    .import-field-val { flex: 1; color: var(--text-inverse); }
    .import-truncate { max-height: 2.6em; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; color: var(--text-muted); font-size: 0.78rem; }
    .import-conn-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.3rem 0; border-bottom: 1px solid rgba(255,255,255,0.06); font-size: 0.8rem; }
    .import-conn-row:last-child { border-bottom: none; }
    .import-badge { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; padding: 0.15rem 0.5rem; border-radius: 99px; cursor: default; }
    .import-count-badge { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; padding: 0.15rem 0.6rem; border-radius: 99px; background: rgba(255,255,255,0.1); color: var(--text-muted); }
    .import-memory-sample { font-size: 0.78rem; color: var(--text-muted); padding: 0.3rem 0; border-bottom: 1px solid rgba(255,255,255,0.06); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .import-memory-sample:last-child { border-bottom: none; }
    .import-empty { font-size: 0.78rem; color: var(--text-muted); font-style: italic; }

    .import-warnings { background: rgba(251,191,36,0.1); border: 1px solid rgba(251,191,36,0.25); border-radius: var(--radius-lg); padding: 0.8rem 1rem; margin-top: 0.5rem; }
    .import-warnings-title { font-family: var(--font-grotesk); font-size: 0.78rem; font-weight: 700; color: #fbbf24; margin-bottom: 0.4rem; }
    .import-warning-item { font-size: 0.75rem; color: #fde68a; line-height: 1.6; }

    .import-error { background: rgba(239,68,68,0.12); border: 1px solid rgba(239,68,68,0.3); border-radius: var(--radius-lg); padding: 0.7rem 1rem; margin-top: 0.75rem; font-size: 0.8rem; color: var(--red, #ef4444); font-family: var(--font-grotesk); }

    .import-progress-wrap { display: flex; flex-direction: column; align-items: center; gap: 1rem; padding: 2rem 0; text-align: center; }
    .import-progress-bar-bg { width: 100%; max-width: 360px; height: 8px; background: rgba(255,255,255,0.1); border-radius: 99px; overflow: hidden; }
    .import-progress-bar-fill { height: 100%; background: var(--accent, #f5c842); border-radius: 99px; transition: width 0.4s; }
    .import-progress-label { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); }

    .import-done { display: flex; flex-direction: column; align-items: center; gap: 0.75rem; padding: 2rem 0 1rem; text-align: center; }
    .import-done-icon { font-size: 2.5rem; }
    .import-done-title { font-family: var(--font-grotesk); font-size: 1.1rem; font-weight: 700; }
    .import-done-note { font-size: 0.8rem; color: var(--text-muted); max-width: 360px; }
    .import-done-warn { font-size: 0.8rem; color: #fbbf24; }
    .import-done-stat { font-size: 0.8rem; color: var(--text-muted); }
    .import-done-actions { display: flex; gap: 0.75rem; margin-top: 0.5rem; }

    .hb-pulse {
        display: inline-block;
        width: 6px;
        height: 6px;
        border-radius: 50%;
        vertical-align: middle;
        margin-left: 4px;
        flex-shrink: 0;
    }
    .hb-fresh { background: #22c55e; animation: hb-beat 2s ease-in-out infinite; }
    .hb-stale { background: #f59e0b; }
    .hb-old   { background: #ef4444; }
    .hb-unknown { background: var(--gray-mid); }

    @keyframes hb-beat {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.5; transform: scale(0.85); }
    }
</style>
