<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
    import Modal from '../components/Modal.svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo, contextClass } from '../lib/utils.js';
    import { buildSoul } from '../lib/soulTemplates.js';

    let agentList = [];
    let agentCount = 0;
    let retiredList = [];
    let retiredCount = 0;
    let currentAgent = '';
    let mainAgent = '';
    let refreshInterval;
    let heartbeats = {};

    // Stats bar (from Fleet)
    let statSessions = '--';
    let statSessionsSub = '';
    let statMessages = '--';
    let statGroups = '--';
    let statScheduler = '--';
    let statSchedulerRunning = false;
    let statSchedulerSub = '';

    // Inline session/token data per agent
    let agentSessionsMap = {};  // name -> [sessions]
    let agentTokensMap = {};    // name -> [tokens]
    let agentTasksMap = {};     // name -> count
    let expandedAgents = new Set();

    // Groups
    let groups = [];
    let groupModalOpen = false;
    let groupName = '';
    let groupMembers = '';

    // Conversation search
    let searchQuery = '';
    let searchResults = [];
    let searchOpen = false;

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
        { id: 'identity' },
        { id: 'connections' },
        { id: 'behavior' },
        { id: 'automation' },
        { id: 'runtime' },
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
    let wizProviderRef = '';       // global provider ID (empty = use Claude tiers or custom)
    let wizCustomProvider = false; // custom provider tile expanded
    let wizProviderPreset = 'anthropic';
    let wizProviderUrl = '';
    let wizProviderKey = '';
    let wizProviderModel = '';     // specific model string for provider/custom
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
    let importDirPath = '';
    let importPollAttempts = 0;
    const IMPORT_POLL_MAX = 150; // 5 min at 2s intervals

    // Soul templates are in src/lib/soulTemplates.js — buildSoul() handles all heart types.

    function toast(msg, type = 'success') {
        toastMessage.set({ message: msg, type });
    }

    function heartbeatStatus(hb, agent) {
        if (!hb) return 'unknown';
        const age = Date.now() / 1000 - hb.timestamp;
        // Use the agent's configured wake_interval if available, else fall back to 10 min
        const interval = (agent?.wake_interval > 0) ? agent.wake_interval : 600;
        if (age < interval) return 'fresh';        // within 1 interval — all good
        if (age < interval * 2) return 'stale';   // 1-2 intervals missed — warn
        return 'old';                               // 2+ intervals missed — alert
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

    async function refreshAgents() {
        try {
            const [data, sessions, groupsData, schedulerStatus, taskStats] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
                api('GET', '/groups'),
                api('GET', '/scheduler/status'),
                api('GET', '/tasks/stats').catch(() => ({ by_agent: {} })),
            ]);
            agentList = data.agents || [];
            agentCount = data.count;
            mainAgent = data.main_agent || '';

            // Stats bar
            const taskCountByAgent = taskStats.by_agent || {};
            groups = groupsData.groups || [];
            statGroups = groups.length;
            statSchedulerRunning = schedulerStatus.running;
            statScheduler = schedulerStatus.running ? '●' : '○';
            statSchedulerSub = `${schedulerStatus.enabled_schedules} schedule${schedulerStatus.enabled_schedules !== 1 ? 's' : ''}`;

            // Build session map
            const agentNames = new Set(agentList.map(a => a.name));
            const sessMap = {};
            const seenIds = new Set();
            let totalMsgs = 0;
            for (const s of sessions) {
                seenIds.add(s.id);
                totalMsgs += s.message_count || 0;
                const owner = s.agent_name || '';
                if (owner && agentNames.has(owner)) {
                    if (!sessMap[owner]) sessMap[owner] = [];
                    sessMap[owner].push(s);
                } else {
                    for (const aName of agentNames) {
                        if (s.id.startsWith(aName + '-') || s.id === aName) {
                            if (!sessMap[aName]) sessMap[aName] = [];
                            sessMap[aName].push(s);
                            break;
                        }
                    }
                }
            }

            // Fetch streaming sessions per agent
            const streamingResults = await Promise.all(
                agentList.map(a =>
                    api('GET', `/agents/${a.name}/streaming-sessions`).catch(() => ({ sessions: [] }))
                )
            );
            let totalSessions = sessions.length;
            agentList.forEach((agent, i) => {
                const streamingSessions = streamingResults[i].sessions || [];
                for (const ss of streamingSessions) {
                    const normalized = normalizeStreamingSession(agent.name, ss);
                    if (seenIds.has(normalized.id)) continue;
                    seenIds.add(normalized.id);
                    if (!sessMap[agent.name]) sessMap[agent.name] = [];
                    sessMap[agent.name].push(normalized);
                    totalSessions++;
                }
            });

            agentSessionsMap = sessMap;
            agentTasksMap = taskCountByAgent;
            statSessions = totalSessions;
            const running = sessions.filter(s => s.state === 'running').length;
            statSessionsSub = running ? `${running} running` : 'all idle';
            statMessages = totalMsgs;

            // Fetch tokens per agent
            const tokenResults = await Promise.all(
                agentList.map(a => api('GET', `/agents/${a.name}/tokens`).catch(() => ({ tokens: [] })))
            );
            const tokMap = {};
            agentList.forEach((agent, i) => { tokMap[agent.name] = tokenResults[i].tokens || []; });
            agentTokensMap = tokMap;

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

    function toggleAgentExpand(name) {
        if (expandedAgents.has(name)) expandedAgents.delete(name);
        else expandedAgents.add(name);
        expandedAgents = expandedAgents;
    }

    async function restartSession(id) {
        await api('POST', `/sessions/${id}/restart`);
        refreshAgents();
    }

    function openChat(id) {
        window.location.hash = `/chat#${id}`;
    }

    function openGroupModal() { groupName = ''; groupMembers = ''; groupModalOpen = true; }
    async function submitGroup() {
        const members = groupMembers.split(',').map(m => m.trim()).filter(Boolean);
        if (!groupName.trim()) { toast('Group name required', 'error'); return; }
        if (!members.length) { toast('Add at least one member', 'error'); return; }
        groupModalOpen = false;
        await api('POST', '/groups', { name: groupName, members });
        toast(`Group "${groupName}" created`);
        refreshAgents();
    }

    async function searchConversations() {
        if (!searchQuery.trim()) return;
        const results = await api('GET', `/conversations/search?q=${encodeURIComponent(searchQuery)}`);
        searchResults = results.results || [];
        searchOpen = true;
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

    async function sleepAgent(name) {
        try {
            const result = await api('POST', `/agents/${name}/sleep`);
            toast(`${name} put to sleep (${result.sessions_closed} session${result.sessions_closed !== 1 ? 's' : ''} closed)`);
            refreshAgents();
        } catch (e) {
            toast(`Can't sleep ${name}: ${e.message}`);
        }
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
    function openWizard() { wizStep = -1; importMode = false; importStep = 1; importFiles = { workspace: null, config: null, lock: null }; importDirPath = ''; importParseId = null; importPreview = null; importLoading = false; importTaskId = null; importProgress = { total: 0, imported: 0, failed: 0, done: false }; importDragover = false; importError = null; importAgentName = null; if (importProgressInterval) { clearInterval(importProgressInterval); importProgressInterval = null; } wizName = ''; wizDisplayName = ''; wizPronouns = ''; wizModel = 'opus'; wizProviderRef = ''; wizCustomProvider = false; wizProviderPreset = 'anthropic'; wizProviderUrl = ''; wizProviderKey = ''; wizProviderModel = ''; wizMode = 'bypassPermissions'; wizHeart = 'sidekick'; wizRole = 'sidekick'; wizAutoStart = true; wizHeartbeatInterval = 300; wizCustomSoul = ''; wizTelegramToken = ''; wizDiscordToken = ''; wizSlackToken = ''; globalProviders = api('GET', '/providers').then(d => globalProviders = d || []).catch(() => []); wizardOpen = true; }
    function closeWizard() { if (importProgressInterval) { clearInterval(importProgressInterval); importProgressInterval = null; } wizardOpen = false; }

    // Import (OpenClaw migration) helpers
    async function importParse() {
        if (!importDirPath && !importFiles.workspace) { toast('Enter a directory path or select a zip file', 'error'); return; }
        importLoading = true;
        importError = null;
        try {
            let resp;
            if (importDirPath) {
                // Directory path — send as JSON to the dir-parse endpoint
                resp = await fetch('/api/migrate/openclaw/parse_dir', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    credentials: 'same-origin',
                    body: JSON.stringify({ dir_path: importDirPath }),
                });
            } else {
                const fd = new FormData();
                fd.append('workspace_zip', importFiles.workspace);
                if (importFiles.config) fd.append('openclaw_json', importFiles.config);
                if (importFiles.lock) fd.append('clawhub_lock', importFiles.lock);
                resp = await fetch('/api/migrate/openclaw/parse', { method: 'POST', body: fd, credentials: 'same-origin' });
            }
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
        // Determine the model alias to register — use 'opus' default if using a provider
        const registerModel = (!wizProviderRef && !wizCustomProvider) ? wizModel : (wizProviderModel || 'sonnet');
        await api('POST', '/agents', { name: wizName, display_name: wizDisplayName, model: registerModel, permission_mode: wizMode, soul, role: wizRole, auto_start: wizAutoStart, heartbeat_interval: wizHeartbeatInterval });
        // Apply provider config if a global provider or custom endpoint was selected
        if (wizProviderRef || wizCustomProvider) {
            await api('PUT', `/agents/${wizName}/provider`, {
                provider_url: wizProviderUrl,
                provider_key: wizProviderKey,
                provider_model: wizProviderModel,
                provider_ref: wizProviderRef,
            });
        }
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
    <!-- Stats Bar -->
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-label">Agents</div><div class="stat-value">{agentCount}</div><div class="stat-sub">{agentList.filter(a => a.enabled).length} active</div></div>
        <div class="stat-card"><div class="stat-label">Sessions</div><div class="stat-value">{statSessions}</div><div class="stat-sub">{statSessionsSub}</div></div>
        <div class="stat-card"><div class="stat-label">Messages</div><div class="stat-value">{statMessages}</div></div>
        <div class="stat-card"><div class="stat-label">Groups</div><div class="stat-value">{statGroups}</div></div>
        <div class="stat-card"><div class="stat-label">Scheduler</div><div class="stat-value" style="color:{statSchedulerRunning ? 'var(--green)' : 'var(--red)'}">{statScheduler}</div><div class="stat-sub">{statSchedulerSub}</div></div>
    </div>

    <!-- Agent Cards -->
    <div class="section">
        <div class="section-header">
            <div class="section-title">{$_('agents.title')} <span style="font-weight:400;color:var(--gray-mid)">({agentCount})</span></div>
            <button class="btn btn-primary" on:click={openWizard}>+ {$_('agents.new_agent')}</button>
        </div>
        <div class="section-body">
            {#if agentList.length === 0}
                <div class="empty">{$_('agents.no_agents')}</div>
            {:else}
                <div class="agent-grid">
                    {#each agentList as a}
                        {@const agentPresence = presenceMap[a.name] || {}}
                        {@const agentStatus = a.working_status === 'working' ? 'working' : (agentPresence.status || 'unknown')}
                        {@const statusColor = agentStatus === 'working' ? 'var(--green)' : agentStatus === 'online' || agentStatus === 'idle' ? 'var(--yellow)' : 'var(--text-muted)'}
                        {@const statusText = agentStatus === 'working' ? 'working' : agentStatus === 'online' ? 'idle' : agentStatus}
                        {@const agentCtxPct = contextMap[a.name] ?? 0}
                        {@const nudgePct = a.restart_threshold_pct || 80}
                        {@const ctxRatio = agentCtxPct / nudgePct}
                        {@const ctxColor = ctxRatio >= 1 ? 'var(--red)' : ctxRatio >= 0.75 ? 'var(--yellow)' : 'var(--green)'}
                        {@const aSessions = agentSessionsMap[a.name] || []}
                        {@const aTokens = agentTokensMap[a.name] || []}
                        {@const aTasks = agentTasksMap[a.name] || 0}
                        {@const isExpanded = expandedAgents.has(a.name)}
                        <div class="agent-card" on:click={() => toggleAgentExpand(a.name)}>
                            <!-- Header: dot + name + status -->
                            <div class="agent-header">
                                <div class="agent-dot" style="background:{statusColor}"></div>
                                <div class="agent-name">{a.display_name || a.name}</div>
                                <span class="agent-status-tag" style="color:{statusColor}">{statusText}</span>
                            </div>

                            <!-- Work summary -->
                            <div class="agent-work">
                                {#if aTasks > 0}
                                    <span style="color:var(--text-secondary)">{aTasks} task{aTasks > 1 ? 's' : ''}</span>
                                {:else if agentStatus === 'working'}
                                    <span style="color:var(--green);font-weight:600">Processing...</span>
                                {:else if agentPresence.streaming}
                                    <span style="color:var(--text-muted);font-style:italic">Listening</span>
                                {:else}
                                    <span style="color:var(--text-muted);font-style:italic">No active session</span>
                                {/if}
                            </div>

                            <!-- Context bar + stats -->
                            <div class="agent-stats">
                                <div class="agent-stat">
                                    <div class="stat-bar-wrap" title="Context {agentCtxPct.toFixed(0)}% · nudge at {nudgePct}%">
                                        <div class="stat-bar" style="width:{Math.min(agentCtxPct / nudgePct * 100, 100)}%;background:{ctxColor}"></div>
                                    </div>
                                    <span class="stat-val" style="color:{ctxColor}">{agentCtxPct.toFixed(0)}%</span>
                                </div>
                                <div class="agent-meta-stats">
                                    <span>{aSessions.length} sess</span>
                                    {#each aTokens.filter(t => t.token_set && t.enabled) as t}
                                        <span class="meta-dot">·</span>
                                        <span class="platform-tag">{t.platform}</span>
                                    {/each}
                                </div>
                            </div>

                            <!-- Tags row -->
                            <div class="agent-tags">
                                {#if a.name === mainAgent}<span class="badge" style="background:#fef3c7;color:#92400e">main</span>{/if}
                                <span class="agent-model-tag">{a.model}</span>
                                {#if !a.enabled}<span class="badge badge-off">disabled</span>{/if}
                                {#each a.groups as g}<span class="badge badge-group">{g}</span>{/each}
                            </div>

                            <!-- Expanded detail -->
                            {#if isExpanded}
                                <div class="agent-inline-detail">
                                    {#each aTokens as t}
                                        <div class="outreach-row">
                                            <span class="badge badge-platform">{t.platform}</span>
                                            <span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? 'Connected' : 'No token'}</span>
                                            <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? 'Active' : 'Disabled'}</span>
                                        </div>
                                    {/each}
                                    {#each aSessions as s}
                                        <div class="session-row">
                                            <span class="session-id">{s.id.replace(a.name + '-', '')}</span>
                                            <span class="badge badge-{s.state}">{s.state}</span>
                                            <div style="display:flex;align-items:center;gap:0.3rem">
                                                <div class="context-bar" style="width:50px">
                                                    <div class="context-fill {contextClass(s.context_used_pct)}" style="width:{Math.min(s.context_used_pct, 100)}%"></div>
                                                </div>
                                                <span style="font-size:0.65rem;color:var(--gray-mid)">{s.context_used_pct}%</span>
                                            </div>
                                            <span style="font-size:0.65rem;color:var(--gray-mid)">{s.message_count} msgs</span>
                                            <button class="btn btn-sm" on:click|stopPropagation={() => openChat(s.id)}>Chat</button>
                                            <button class="btn btn-sm" on:click|stopPropagation={() => restartSession(s.id)}>Restart</button>
                                        </div>
                                    {/each}
                                    {#if aSessions.length === 0}
                                        <div style="padding:0.5rem;font-size:0.7rem;color:var(--gray-mid)">No sessions</div>
                                    {/if}
                                </div>
                            {/if}

                            <!-- Actions -->
                            <div class="agent-actions">
                                <button class="btn btn-sm btn-primary" on:click|stopPropagation={() => openDetail(a.name)}>{$_('agents.configure')}</button>
                                {#if a.name !== mainAgent}
                                    <button class="btn btn-sm" on:click|stopPropagation={() => setMainAgent(a.name)}>{$_('agents.set_main')}</button>
                                {/if}
                                {#if agentPresence.streaming || aSessions.length > 0}
                                    <button class="btn btn-sm btn-sleep" on:click|stopPropagation={() => sleepAgent(a.name)} title="Put agent to sleep">
                                        <span class="material-symbols-outlined" style="font-size:14px">dark_mode</span> Sleep
                                    </button>
                                {/if}
                                <button class="btn-danger-text" on:click|stopPropagation={() => openRetireModal(a.name)}>{$_('agents.retire')}</button>
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
                <div class="section-title" style="color:var(--gray-mid)">{$_('agents.retired')} <span style="font-weight:400">({retiredCount})</span></div>
            </div>
            <div class="section-body">
                <div class="agent-grid">
                    {#each retiredList as a}
                        <div class="agent-card" style="opacity:0.6;background:var(--surface-2)">
                            <div class="agent-name">{a.display_name || a.name}</div>
                            <div class="agent-meta">
                                <span class="badge" style="background:var(--tone-error-bg);color:var(--tone-error-text)">{$_('agents.retired_badge')}</span>
                                <span class="badge badge-model">{a.model}</span>
                                {#if a.role}<span class="badge" style="background:var(--gray-light);color:var(--gray-dark)">{a.role}</span>{/if}
                            </div>
                            <div class="agent-stats">
                                <span>retired {timeAgo(a.retired_at)}</span>
                                <span>created {timeAgo(a.created_at)}</span>
                            </div>
                            <div class="agent-actions">
                                <button class="btn btn-sm btn-success" on:click={() => restoreAgent(a.name)}>{$_('agents.restore')}</button>
                            </div>
                        </div>
                    {/each}
                </div>
            </div>
        </div>
    {/if}

    <!-- Groups -->
    {#if groups.length > 0}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Groups <span style="font-weight:400;color:var(--gray-mid)">({groups.length})</span></div>
                <button class="btn btn-sm" on:click={openGroupModal}>+ New Group</button>
            </div>
            <div class="section-body">
                <div style="display:flex;flex-wrap:wrap;gap:0.5rem;padding:0.8rem">
                    {#each groups as g}
                        <div class="group-chip">
                            <span class="group-name">{g.name}</span>
                            <span class="group-count">{g.members?.length || 0}</span>
                        </div>
                    {/each}
                </div>
            </div>
        </div>
    {/if}

    <!-- Group Modal -->
    <Modal bind:show={groupModalOpen} title="New Group" width="420px">
        <div class="modal-form">
            <div class="form-row">
                <label class="form-label">Group Name</label>
                <input type="text" class="form-input w-full" bind:value={groupName} placeholder="my-group">
            </div>
            <div class="form-row">
                <label class="form-label">Members (comma-separated agent names)</label>
                <input type="text" class="form-input w-full" bind:value={groupMembers} placeholder="barsik, pushok">
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={() => groupModalOpen = false}>Cancel</button>
            <button class="btn btn-sm btn-primary" on:click={submitGroup}>Create</button>
        </div>
    </Modal>

    <Modal bind:show={retireModalOpen} title={$_('agents.retire_modal_title')} width="420px">
        <div class="modal-form">
            <p class="modal-note">{@html $_('agents.retire_modal_note', { values: { name: `<strong style="color:var(--red)">${pendingRetireAgent}</strong>` } })}</p>
            <div class="form-row">
                <label class="form-label">{$_('agents.retire_confirm_label')}</label>
                <input type="text" class="form-input w-full" bind:value={retireConfirmInput} autocomplete="off" spellcheck="false" placeholder={$_('agents.agent_name_placeholder')}>
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={closeRetireModal}>{$_('common.cancel')}</button>
            <button class="btn btn-sm btn-confirm-delete" class:ready={retireConfirmInput === pendingRetireAgent} disabled={retireConfirmInput !== pendingRetireAgent} on:click={confirmRetire}>{$_('agents.retire')}</button>
        </div>
    </Modal>

    <Modal bind:show={cronModalOpen} title={$_('agents.cron_modal_title')} width="460px">
        <div class="modal-form">
            <p class="modal-note">{$_('agents.cron_modal_note')}</p>
            <div class="form-row">
                <label class="form-label">{$_('common.name')}</label>
                <input type="text" class="form-input w-full" bind:value={cronName} placeholder="e.g. morning_check" autocomplete="off">
            </div>
            <div class="form-row">
                <label class="form-label">{$_('agents.cron_expression')}</label>
                <input type="text" class="form-input w-full" bind:value={cronExpression} placeholder="e.g. 0 8 * * *">
                <p class="modal-note" style="margin-top:0.4rem">min hour day month weekday — <a href="https://crontab.guru" target="_blank" rel="noreferrer">crontab.guru</a></p>
            </div>
            <div class="form-row">
                <label class="form-label">{$_('agents.cron_prompt')}</label>
                <textarea class="form-input w-full" bind:value={cronPrompt} placeholder={$_('agents.cron_prompt_placeholder')} rows="3"></textarea>
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={closeCronModal}>{$_('common.cancel')}</button>
            <button class="btn btn-sm btn-primary" disabled={!cronName || !cronExpression} on:click={submitCronJob}>{$_('common.create')}</button>
        </div>
    </Modal>

    <Modal bind:show={mcpModalOpen} title={$_('agents.mcp_modal_title')} width="500px">
        <div class="modal-form">
            <div class="form-row">
                <label class="form-label">{$_('agents.mcp_server_name')}</label>
                <input type="text" class="form-input w-full" bind:value={mcpName} placeholder="e.g. webclaw" autocomplete="off">
            </div>
            <div class="form-row">
                <label class="form-label">{$_('common.type')}</label>
                <select class="form-select w-full" bind:value={mcpType}>
                    <option value="stdio">stdio (command)</option>
                    <option value="http">HTTP (URL)</option>
                </select>
            </div>
            {#if mcpType === 'stdio'}
                <div class="form-row">
                    <label class="form-label">{$_('agents.mcp_command')}</label>
                    <input type="text" class="form-input w-full" bind:value={mcpCommand} placeholder="e.g. npx">
                </div>
                <div class="form-row">
                    <label class="form-label">{$_('agents.mcp_args')}</label>
                    <input type="text" class="form-input w-full" bind:value={mcpArgs} placeholder="e.g. -y @webclaw/mcp">
                </div>
            {:else}
                <div class="form-row">
                    <label class="form-label">URL</label>
                    <input type="text" class="form-input w-full" bind:value={mcpUrl} placeholder="e.g. http://localhost:8931/mcp">
                </div>
            {/if}
            <div class="form-row">
                <label class="form-label">{$_('agents.mcp_env_vars')}</label>
                {#each mcpEnvPairs as pair, i}
                    <div class="inline-spread" style="margin-bottom:0.35rem">
                        <input type="text" class="form-input grow" bind:value={pair.key} placeholder="KEY">
                        <input type="text" class="form-input grow" bind:value={pair.value} placeholder="value">
                        {#if mcpEnvPairs.length > 1}
                            <button class="btn btn-sm" on:click={() => { mcpEnvPairs = mcpEnvPairs.filter((_, j) => j !== i); }}>X</button>
                        {/if}
                    </div>
                {/each}
                <button class="btn btn-sm" on:click={() => { mcpEnvPairs = [...mcpEnvPairs, { key: '', value: '' }]; }}>+ {$_('agents.mcp_env_var')}</button>
            </div>
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn btn-sm" on:click={() => mcpModalOpen = false}>{$_('common.cancel')}</button>
            <button class="btn btn-sm btn-primary" disabled={!mcpName.trim()} on:click={addMcpServer}>{$_('agents.mcp_add_server')}</button>
        </div>
    </Modal>

    <Modal bind:show={triggerModalOpen} title={$_('agents.trigger_modal_title')} width="480px">
        <div class="modal-form">
            {#if newTriggerWebhookToken}
                <div style="background:var(--tone-success-bg);border-radius:var(--radius-lg);padding:0.75rem 1rem;font-size:0.82rem;color:var(--tone-success-text)">
                    {$_('agents_extra.trigger_webhook_created')}
                    <div style="font-family:var(--font-body);font-size:0.78rem;word-break:break-all;margin-top:0.4rem;color:var(--text-primary)">{newTriggerWebhookToken}</div>
                    <button class="btn btn-sm" style="margin-top:0.5rem" on:click={() => navigator.clipboard.writeText(newTriggerWebhookToken).then(() => toast('Copied'))}>{$_('agents_extra.trigger_copy')}</button>
                </div>
            {:else}
                <div class="form-row">
                    <label class="form-label">{$_('agents_extra.trigger_type_label')}</label>
                    <select class="form-select w-full" bind:value={newTriggerType}>
                        <option value="webhook">{$_('agents_extra.trigger_webhook_option')}</option>
                        <option value="url">{$_('agents_extra.trigger_url_option')}</option>
                        <option value="file">{$_('agents_extra.trigger_file_option')}</option>
                    </select>
                </div>
                <div class="form-row">
                    <label class="form-label">{$_('agents_extra.trigger_name_label')}</label>
                    <input type="text" class="form-input w-full" bind:value={newTriggerName} placeholder={$_('agents_extra.trigger_name_placeholder')}>
                </div>
                {#if newTriggerType === 'url'}
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_url_label')}</label>
                        <input type="url" class="form-input w-full" bind:value={newTriggerUrl} placeholder={$_('agents_extra.trigger_url_placeholder')}>
                    </div>
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_method_label')}</label>
                        <select class="form-select w-full" bind:value={newTriggerMethod}>
                            <option value="GET">GET</option>
                            <option value="POST">POST</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_condition_label')}</label>
                        <select class="form-select w-full" bind:value={newTriggerCondition}>
                            <option value="status_changed">{$_('agents_extra.trigger_condition_status_changed')}</option>
                            <option value="status_is">{$_('agents_extra.trigger_condition_status_is')}</option>
                            <option value="body_contains">{$_('agents_extra.trigger_condition_body_contains')}</option>
                            <option value="json_field_equals">{$_('agents_extra.trigger_condition_json_equals')}</option>
                            <option value="json_field_changed">{$_('agents_extra.trigger_condition_json_changed')}</option>
                        </select>
                    </div>
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_interval_label')}</label>
                        <input type="number" class="form-input w-full" bind:value={newTriggerInterval} min="30">
                    </div>
                {/if}
                {#if newTriggerType === 'file'}
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_file_path_label')}</label>
                        <input type="text" class="form-input w-full" bind:value={newTriggerFilePath} placeholder={$_('agents_extra.trigger_file_placeholder')}>
                    </div>
                    <div class="form-row">
                        <label class="form-label">{$_('agents_extra.trigger_interval_label')}</label>
                        <input type="number" class="form-input w-full" bind:value={newTriggerInterval} min="10">
                    </div>
                {/if}
                <div class="form-row">
                    <label class="form-label">{$_('agents_extra.trigger_prompt_label')}</label>
                    <textarea class="form-input w-full" bind:value={newTriggerPrompt} rows="3" placeholder="Wake message sent to agent. Use {'{{body.field}}'} for URL/webhook data."></textarea>
                </div>
            {/if}
        </div>
        <div slot="footer" class="inline-spread">
            <button class="btn" on:click={() => triggerModalOpen = false}>
                {newTriggerWebhookToken ? $_('common.close') : $_('common.cancel')}
            </button>
            {#if !newTriggerWebhookToken}
                <button class="btn btn-primary" on:click={createTrigger} disabled={creatingTrigger}>
                    {creatingTrigger ? $_('agents.trigger_creating') : $_('agents.trigger_create')}
                </button>
            {/if}
        </div>
    </Modal>

    <!-- Agent Detail Modal -->
    <Modal bind:show={detailOpen} title="" maxWidth="900px" flush={true} contentClass="detail-modal">
        <div slot="header" class="detail-modal-header">
            <div class="modal-title">{$_('agents.agent_label')}: {detailName} {#if currentAgent === mainAgent}<span style="font-size:0.75rem;color:#92400e;background:#fef3c7;padding:0.15rem 0.5rem;border-radius:var(--radius-lg);margin-left:0.5rem;vertical-align:middle">[*] {$_('agents.main_agent_badge')}</span>{/if}</div>
        </div>
            <!-- Compact metadata row -->
            <div style="padding:0.8rem 1.5rem;display:flex;flex-wrap:wrap;gap:0.8rem 1.5rem;align-items:center;background:var(--surface-2);border-radius:var(--radius-lg);font-family:var(--font-grotesk);font-size:0.8rem">
                <span><span style="color:var(--gray-mid)">{$_('agents.meta_model')}:</span> {detailModel}</span>
                <span><span style="color:var(--gray-mid)">{$_('agents.meta_perm')}:</span> {detailPermission}</span>
                <span><span style="color:var(--gray-mid)">{$_('agents.meta_max')}:</span> {detailMaxSessions}</span>
                <span><span style="color:var(--gray-mid)">{$_('agents.meta_groups')}:</span> {detailGroups}</span>
                <span style="display:flex;gap:0.3rem;align-items:center;flex:1;min-width:200px">
                    <span style="color:var(--gray-mid)">{$_('agents.meta_dir')}:</span>
                    <input type="text" class="form-input" bind:value={detailWorkingDir} style="font-size:0.8rem;flex:1;padding:0.2rem 0.4rem">
                    <button class="btn btn-sm" on:click={saveWorkingDir}>{$_('common.save')}</button>
                </span>
            </div>

            <!-- Tab Bar -->
            <div class="detail-tabs">
                {#each tabs as tab}
                    <button class="detail-tab" class:active={activeTab === tab.id} on:click={() => activeTab = tab.id}>
                        {$_(`agents.tab_${tab.id}`)}
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
                        {#if claudeMdDirty}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--accent);font-weight:700">{$_('common.unsaved')}</span>{/if}
                    </div>
                    <div style="display:flex;gap:0.3rem">
                        <button class="btn btn-sm btn-primary" on:click={saveClaudeMd} disabled={!claudeMdDirty}>{$_('common.save')}</button>
                    </div>
                </div>
                <textarea class="form-input" bind:value={claudeMdContent} rows="20" style="margin:0;border:none;width:100%;font-family:var(--font-grotesk);font-size:0.8rem;line-height:1.5;resize:vertical;padding:0.8rem 1.5rem;background:var(--input-bg);border-radius:0 0 var(--radius-lg) var(--radius-lg)" placeholder="Agent's full CLAUDE.md — identity, boundaries, directives, everything..."></textarea>
            </div>

            <!-- Directives -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.8rem">
                    <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.directives')}</span>
                </div>
                <div style="display:flex;gap:0.5rem;align-items:center">
                    <input type="text" class="form-input" bind:value={newDirective} placeholder={$_('agents.directive_placeholder')} style="flex:1">
                    <input type="number" class="form-input" bind:value={newDirectivePriority} placeholder={$_('agents.priority')} style="width:80px">
                    <button class="btn btn-primary" on:click={addDirective}>{$_('common.add')}</button>
                </div>
            </div>
            <div>
                {#if directives.length === 0}
                    <div class="empty">{$_('agents.no_directives')}</div>
                {:else}
                    {#each directives as d}
                        <div class="directive-item" class:directive-inactive={!d.active}>
                            <span class="directive-priority">{d.priority}</span>
                            <span class="directive-text">{d.directive}</span>
                            <button class="btn btn-sm" on:click={() => toggleDirective(d.id, !d.active)}>{d.active ? $_('common.disable') : $_('common.enable')}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => removeDirective(d.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Heart Files -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.heart_files')}</span>
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
                            <button class="btn btn-sm btn-primary" on:click={saveFile}>{$_('agents_extra.file_edit_save')}</button>
                            <button class="btn btn-sm" on:click={closeFileEditor}>{$_('agents_extra.file_edit_close')}</button>
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
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.bot_tokens')}</span>
            </div>
            <div style="padding:0.75rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
                    <select class="form-select" bind:value={tokenPlatform}>
                        <option value="telegram">Telegram</option>
                        <option value="discord">Discord</option>
                        <option value="slack">Slack</option>
                    </select>
                    <input type="password" class="form-input" bind:value={tokenValue} placeholder={$_('agents.bot_token_placeholder')} style="flex:1;min-width:120px">
                    <button class="btn btn-primary" on:click={setToken}>{$_('common.set')}</button>
                </div>
            </div>
            <div>
                {#if tokens.length === 0}
                    <div class="empty">{$_('agents.no_tokens')}</div>
                {:else}
                    {#each tokens as t}
                        <div class="token-item">
                            <span class="badge badge-model">{t.platform}</span>
                            <span class="badge badge-{t.token_set ? 'on' : 'off'}">{t.token_set ? $_('agents.token_set') : $_('agents.token_missing')}</span>
                            <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? $_('common.enabled') : $_('common.disabled')}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm btn-danger" on:click={() => removeToken(t.platform)}>{$_('agents_extra.bot_token_remove')}</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Users (approved + pending merged) -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.users')}</span>
                {#if pendingUserCount > 0}<span class="badge" style="background:#fef3c7;color:#92400e;margin-left:0.5rem">{$_('agents.pending_count', { values: { count: pendingUserCount } })}</span>{/if}
            </div>
            <div style="padding:0.75rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;gap:0.5rem;align-items:center;flex-wrap:wrap">
                    <input type="text" class="form-input" bind:value={newUserChatId} placeholder={$_('agents.chat_id')} style="width:130px">
                    <input type="text" class="form-input" bind:value={newUserName} placeholder={$_('agents.display_name_optional')} style="flex:1;min-width:120px">
                    <button class="btn btn-primary" on:click={approveUser}>{$_('agents.approve')}</button>
                </div>
            </div>
            <div>
                <!-- Pending users first (with yellow badge) -->
                {#each Object.entries(pendingMessages) as [chatId, msgs]}
                    <div class="token-item" style="flex-direction:column;align-items:flex-start;gap:0.5rem">
                        <div style="display:flex;width:100%;align-items:center;gap:0.5rem">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{msgs[0]?.sender_name || chatId}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{chatId}</span>
                            <span class="badge" style="background:#fef3c7;color:#92400e">{$_('agents_extra.pending_badge')}</span>
                            <span class="badge badge-model">{msgs.length} msg{msgs.length > 1 ? 's' : ''}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm btn-success" on:click={() => approveAndDeliver(chatId, msgs[0]?.sender_name)}>{$_('agents.approve')}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => denyPendingUser(chatId)}>{$_('agents.deny')}</button>
                        </div>
                        <div style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid);padding-left:0.5rem;max-height:3rem;overflow:hidden">
                            {msgs[0]?.content?.slice(0, 150)}{msgs[0]?.content?.length > 150 ? '...' : ''}
                        </div>
                    </div>
                {/each}
                <!-- Approved users -->
                {#if approvedUsers.length === 0 && pendingUserCount === 0}
                    <div class="empty">{$_('agents.no_users')}</div>
                {:else}
                    {#each approvedUsers as u}
                        <div class="token-item">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{u.display_name || u.chat_id}</span>
                            {#if u.display_name}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{u.chat_id}</span>{/if}
                            {#if u.status === 'approved'}
                                <span class="badge" style="background:#dcfce7;color:#166534">{$_('agents.approved')}</span>
                            {:else if u.status === 'denied'}
                                <span class="badge badge-off">{$_('agents.denied')}</span>
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
                                <button class="btn btn-sm btn-success" on:click={() => { api('POST', `/agents/${currentAgent}/approved-users`, { chat_id: u.chat_id, display_name: u.display_name }).then(() => { toast('User approved'); loadApprovedUsers(); }); }}>{$_('agents.approve')}</button>
                            {:else}
                                <button class="btn btn-sm" on:click={() => denyUser(u.chat_id)}>{$_('agents.deny')}</button>
                            {/if}
                            <button class="btn btn-sm btn-danger" on:click={() => revokeUser(u.chat_id)}>{$_('agents.revoke')}</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Group Chats -->
            {#if groupChats.length > 0}
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.group_chats')}</span>
                <span class="badge" style="margin-left:0.5rem">{groupChats.length}</span>
            </div>
            <div>
                {#each groupChats as gc}
                    <div class="token-item">
                        <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{gc.alias || gc.chat_title || gc.chat_id}</span>
                        {#if gc.alias}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_title}</span>{/if}
                        <span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{gc.chat_type}</span>
                        {#if gc.member_count > 0}<span style="font-family:var(--font-grotesk);font-size:0.7rem;color:var(--gray-mid)">{$_('agents_extra.group_members', { values: { count: gc.member_count } })}</span>{/if}
                        <select style="font-family:var(--font-grotesk);font-size:0.75rem;padding:0.15rem 0.3rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg)"
                            value={channelSessions[gc.chat_id] || 'main'}
                            on:change={(e) => setChannelSession(gc.chat_id, e.target.value)}>
                            <option value="main">main</option>
                            {#each streamingSessions.filter(s => s.label !== 'main') as ss}
                                <option value={ss.label}>{ss.label}</option>
                            {/each}
                        </select>
                        <span style="flex:1"></span>
                        <button class="btn btn-sm" on:click={() => { const alias = prompt('Set alias:', gc.alias || ''); if (alias !== null) setGroupAlias(gc.chat_id, alias); }}>{$_('agents_extra.group_alias_btn')}</button>
                        <button class="btn btn-sm btn-danger" on:click={() => deactivateGroup(gc.chat_id)}>{$_('agents_extra.group_leave_btn')}</button>
                    </div>
                {/each}
            </div>
            {/if}
            {/if}<!-- end connections tab -->

            {#if activeTab === 'behavior'}
            <!-- Voice Config -->
            <div class="detail-section-header">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.voice')}</span>
                {#if voiceDirty}<button class="btn btn-sm btn-primary" on:click={saveVoiceConfig}>{$_('common.save')}</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="margin-top:0;display:flex;flex-direction:column;gap:0.8rem">
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={voiceReply} on:change={() => voiceDirty = true}> {$_('agents_extra.auto_reply_voice')}
                    </label>
                    {#if voiceReply}
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.tts_provider_label')}</div>
                            <select class="form-select" bind:value={ttsProvider} on:change={() => voiceDirty = true} style="width:100%">
                                <option value="openai">OpenAI</option>
                                <option value="elevenlabs">ElevenLabs</option>
                                <option value="deepgram">Deepgram</option>
                            </select>
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.voice_label')}</div>
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
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.model_label')}</div>
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
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.transcription_provider_label')}</div>
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
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.dreaming')}</span>
                {#if dreamDirty}<button class="btn btn-sm btn-primary" on:click={saveDreamConfig}>{$_('common.save')}</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                <div style="display:flex;flex-direction:column;gap:0.8rem">
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={dreamEnabled} on:change={() => dreamDirty = true}> {$_('agents_extra.dream_enable_label')}
                    </label>
                    <label style="display:flex;align-items:center;gap:0.5rem;font-family:var(--font-grotesk);font-size:0.8rem;cursor:pointer">
                        <input type="checkbox" bind:checked={dreamNotify} on:change={() => dreamDirty = true}> {$_('agents_extra.dream_notify_label')}
                    </label>
                    {#if dreamEnabled}
                    <div style="display:flex;gap:1rem;flex-wrap:wrap">
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.dream_schedule_label')}</div>
                            <input type="text" class="form-input" bind:value={dreamSchedule} on:input={() => dreamDirty = true} placeholder="0 3 * * *" style="width:100%">
                        </div>
                        <div style="flex:1;min-width:140px">
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.dream_timezone_label')}</div>
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
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.3rem">{$_('agents_extra.dream_model_label')}</div>
                            <select class="form-select" bind:value={dreamModel} on:change={() => dreamDirty = true} style="width:100%">
                                <option value="">{$_('agents_extra.dream_model_default')}</option>
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
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.model_provider')}</span>
                {#if providerDirty}<button class="btn btn-sm btn-primary" on:click={saveProvider}>{$_('common.save')}</button>{/if}
            </div>
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem">
                {#if globalProviders.length > 0}
                <div style="margin-bottom:0.75rem">
                    <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.global_provider_label')}</div>
                    <select class="form-select" value={providerRef} on:change={(e) => selectGlobalProvider(e.target.value)} style="width:100%;max-width:320px">
                        <option value="">{$_('agents_extra.global_provider_none')}</option>
                        {#each globalProviders as gp}
                            <option value={gp.id}>{gp.name}{gp.provider_model ? ' · ' + gp.provider_model : ''}</option>
                        {/each}
                    </select>
                </div>
                {/if}
                <div style="{providerRef ? 'opacity:0.4;pointer-events:none' : ''}">
                    <div style="display:flex;gap:0.4rem;margin-top:0;flex-wrap:wrap">
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'anthropic'} style={providerPreset !== 'anthropic' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('anthropic')}>{$_('agents_extra.provider_preset_anthropic')}</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'ollama'} style={providerPreset !== 'ollama' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('ollama')}>{$_('agents_extra.provider_preset_ollama')}</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'openrouter'} style={providerPreset !== 'openrouter' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('openrouter')}>{$_('agents_extra.provider_preset_openrouter')}</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'deepseek'} style={providerPreset !== 'deepseek' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('deepseek')}>{$_('agents_extra.provider_preset_deepseek')}</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'zai'} style={providerPreset !== 'zai' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => applyProviderPreset('zai')}>{$_('agents_extra.provider_preset_zai')}</button>
                        <button class="btn btn-sm" class:btn-primary={providerPreset === 'custom'} style={providerPreset !== 'custom' ? 'background:var(--surface-3);color:var(--text-muted)' : ''} on:click={() => { providerPreset = 'custom'; providerRef = ''; providerDirty = true; }}>{$_('agents_extra.provider_preset_custom')}</button>
                    </div>
                    {#if providerPreset === 'openrouter'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        {$_('agents_extra.openrouter_desc')}
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.openrouter_api_key_label')}</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="sk-or-..." style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.openrouter_model_label')}</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="anthropic/claude-sonnet-4-5" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">{$_('agents_extra.openrouter_model_examples')}</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'deepseek'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        {$_('agents_extra.deepseek_desc')}
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.api_key_label')}</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder={$_('agents_extra.deepseek_api_key_placeholder')} style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.model_label')}</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="deepseek-chat" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">{$_('agents_extra.deepseek_model_options')}</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'zai'}
                    <div style="margin-top:0.75rem;padding:0.6rem 0.75rem;background:var(--surface-1);border-radius:var(--radius-md);font-size:0.78rem;color:var(--text-muted)">
                        {$_('agents_extra.zai_desc')}
                    </div>
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.api_key_label')}</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder={$_('agents_extra.zai_api_key_placeholder')} style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.model_label')}</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder="glm-5.1" style="width:100%">
                            <div style="font-size:0.72rem;color:var(--text-muted);margin-top:0.25rem">{$_('agents_extra.zai_model_options')}</div>
                        </div>
                    </div>
                    {/if}
                    {#if providerPreset === 'ollama' || providerPreset === 'custom'}
                    <div style="display:flex;flex-direction:column;gap:0.5rem;margin-top:0.75rem">
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.base_url_label')}</div>
                            <input type="text" class="form-input" bind:value={providerUrl} on:input={() => providerDirty = true} placeholder="http://localhost:11434" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.api_key_label')}</div>
                            <input type="password" class="form-input" bind:value={providerKey} on:input={() => providerDirty = true} placeholder="ollama or your key" style="width:100%">
                        </div>
                        <div>
                            <div style="font-family:var(--font-grotesk);font-size:0.7rem;font-weight:700;text-transform:uppercase;color:var(--gray-mid);margin-bottom:0.25rem">{$_('agents_extra.model_override_label')}</div>
                            <input type="text" class="form-input" bind:value={providerModel} on:input={() => providerDirty = true} placeholder={$_('agents_extra.model_override_placeholder')} style="width:100%">
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
                    <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.skills')}</span>
                    <div style="display:flex;gap:0.4rem;align-items:center">
                        <a href="https://github.com/anthropics/skills" target="_blank" rel="noopener" class="btn btn-sm" style="font-size:0.7rem">{$_('agents_extra.browse_community')}</a>
                        <button class="btn btn-sm btn-primary" on:click={() => createSkillOpen = !createSkillOpen}>+ {$_('agents_extra.create_skill_btn')}</button>
                        {#if skillsPendingApply}
                            <button class="btn btn-sm" style="background:var(--accent);color:#fff" on:click={applySkills}>{$_('agents.apply_restart')}</button>
                        {/if}
                    </div>
                </div>
                <div style="display:flex;gap:0.4rem;align-items:center;margin-top:0.5rem">
                    <input type="text" bind:value={gitSkillUrl} placeholder="https://github.com/org/skill-name" style="flex:1;font-family:var(--font-grotesk);font-size:0.8rem;padding:0.35rem 0.5rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg)">
                    <button class="btn btn-sm btn-primary" on:click={installSkillFromGit} disabled={gitSkillLoading}>
                        {gitSkillLoading ? $_('agents.skill_cloning') : $_('agents.skill_install_git')}
                    </button>
                </div>
                {#if skillsPendingApply}
                    <div style="background:var(--warning-bg, #fff3cd);border:none;border-radius:var(--radius-lg);padding:0.5rem 0.8rem;font-size:0.75rem;margin-top:0.5rem">
                        {$_('agents.skill_pending_note')}
                    </div>
                {/if}
            </div>
            <!-- Create Skill from SKILL.md -->
            {#if createSkillOpen}
                <div style="padding:1rem 1.5rem;background:var(--surface-1);border-radius:var(--radius-lg);margin-top:0.5rem">
                    <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.5rem">{$_('agents_extra.create_skill_md_title')}</div>
                    <p style="font-size:0.75rem;color:var(--gray-mid);margin:0 0 0.5rem 0">
                        {$_('agents_extra.create_skill_md_desc')}
                    </p>
                    <textarea bind:value={newSkillMd} rows="12" style="width:100%;font-family:var(--font-grotesk);font-size:0.8rem;padding:0.5rem;border:none;border-radius:var(--radius-lg);background:var(--input-bg);resize:vertical"></textarea>
                    <div style="display:flex;gap:0.5rem;margin-top:0.5rem">
                        <button class="btn btn-primary" on:click={createSkillFromMd}>{$_('agents_extra.create_assign_btn')}</button>
                        <button class="btn" on:click={() => createSkillOpen = false}>{$_('common.cancel')}</button>
                    </div>
                </div>
            {/if}
            <div>
                {#if visibleSkills.length === 0 && !showCoreSkills}
                    <div style="padding:0.8rem 1.5rem;font-size:0.8rem;color:var(--gray-mid)">
                        {$_('agents_extra.no_custom_skills')}
                        {#if agentSkills.length > 0}
                            <button style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:0.75rem;text-decoration:underline;padding:0" on:click={() => showCoreSkills = true}>
                                {$_('agents_extra.show_default_skills', { values: { count: agentSkills.length, plural: agentSkills.length !== 1 ? 's' : '' } })}
                            </button>
                        {/if}
                    </div>
                {:else}
                    {#each visibleSkills as s}
                        <div class="token-item" style={!s.effective_enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{s.name}</span>
                            <span class="badge" style="background:var(--gray-mid);color:#fff;font-size:0.65rem;padding:0.1rem 0.4rem;border-radius:3px">{s.category}</span>
                            {#if s.assigned_by === 'shared'}
                                <span class="badge badge-on" style="font-size:0.65rem">{$_('agents_extra.shared_badge')}</span>
                            {:else if s.assigned_by !== 'system'}
                                <span class="badge" style="font-size:0.65rem;background:var(--surface-3)">{s.assigned_by}</span>
                            {/if}
                            {#if s.description}
                                <span style="color:var(--gray-mid);font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:300px">{s.description}</span>
                            {/if}
                            <span style="flex:1"></span>
                            {#if s.category !== 'core'}
                                <button class="btn btn-sm" on:click={() => toggleAgentSkill(s.name, !s.effective_enabled)}>
                                    {s.effective_enabled ? $_('common.disable') : $_('common.enable')}
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
                                {$_('agents_extra.default_skills_always_on', { values: { count: coreSkillCount } })}
                            </button>
                        </div>
                    {:else if showCoreSkills}
                        <div style="padding:0.4rem 1.5rem">
                            <button style="background:none;border:none;color:var(--gray-mid);cursor:pointer;font-size:0.7rem;text-decoration:underline;padding:0" on:click={() => showCoreSkills = false}>
                                {$_('agents_extra.hide_default_skills')}
                            </button>
                        </div>
                    {/if}
                {/if}
            </div>
            <!-- Available Skills -->
            {#if availableSkills.length > 0}
                <div style="padding:0.8rem 1.5rem;background:var(--surface-1);border-radius:var(--radius-lg);margin-top:0.5rem">
                    <div style="font-size:0.75rem;font-weight:600;margin-bottom:0.5rem;color:var(--gray-mid)">{$_('agents_extra.available_to_add')}</div>
                    {#each availableSkills as s}
                        <div style="display:flex;align-items:center;gap:0.5rem;padding:0.3rem 0;font-size:0.8rem">
                            <span style="font-family:var(--font-grotesk);font-weight:600">{s.name}</span>
                            <span class="badge" style="background:var(--gray-mid);color:#fff;font-size:0.6rem;padding:0.1rem 0.3rem;border-radius:3px">{s.category}</span>
                            <span style="flex:1;color:var(--gray-mid);font-size:0.75rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{s.description}</span>
                            <button class="btn btn-sm btn-primary" on:click={() => assignSkill(s.name)}>{$_('agents_extra.add_skill_btn')}</button>
                        </div>
                    {/each}
                </div>
            {/if}

            <!-- MCP Servers -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem;display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.mcp_servers')}</span>
                <button class="btn btn-sm btn-primary" on:click={openMcpModal}>+ {$_('common.add')}</button>
            </div>
            <div>
                {#if mcpServers.length === 0}
                    <div class="empty">{$_('agents.no_mcp_servers')}</div>
                {:else}
                    {#each mcpServers as srv}
                        {@const sourceStyle = srv.source === 'core' ? 'background:var(--accent);color:var(--accent-contrast)' : srv.source === 'skill' ? 'background:var(--tone-lilac-bg);color:var(--tone-lilac-text)' : srv.source === 'custom' ? 'background:var(--tone-info-bg);color:var(--tone-info-text)' : 'background:var(--surface-3)'}
                        <div class="token-item" style={srv.source === 'custom' && !srv.enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{srv.name}</span>
                            <span class="badge" style="{sourceStyle};font-size:0.6rem">{srv.source}</span>
                            <span class="badge badge-model" style="font-size:0.6rem">{srv.server_type || 'stdio'}</span>
                            {#if srv.source === 'custom'}
                                <span class="badge badge-{srv.enabled ? 'on' : 'off'}">{srv.enabled ? $_('common.enabled') : $_('common.disabled')}</span>
                            {/if}
                            <span style="flex:1"></span>
                            {#if srv.source === 'custom'}
                                <button class="btn btn-sm" on:click={() => toggleMcpServer(srv.name, !srv.enabled)}>{srv.enabled ? $_('common.disable') : $_('common.enable')}</button>
                                <button class="btn btn-sm btn-danger" on:click={() => removeMcpServer(srv.name)}>X</button>
                            {/if}
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Triggers -->
            <div style="padding:1rem 1.5rem;background:var(--surface-2);border-radius:var(--radius-lg);margin-top:0.5rem;display:flex;justify-content:space-between;align-items:center">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.triggers')}</span>
                <button class="btn btn-sm btn-primary" on:click={openTriggerModal}>+ {$_('common.add')}</button>
            </div>
            <div>
                {#if triggers.length === 0}
                    <div class="empty">{$_('agents.no_triggers')}</div>
                {:else}
                    {#each triggers as t}
                        <div class="token-item">
                            <span class="badge badge-{t.trigger_type === 'webhook' ? 'model' : t.trigger_type === 'url' ? 'running' : 'off'}">{t.trigger_type}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:600;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{t.name || t.trigger_type}</span>
                            <span class="badge badge-{t.enabled ? 'on' : 'off'}">{t.enabled ? $_('common.on') : $_('common.off')}</span>
                            {#if t.fire_count > 0}
                                <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--text-muted)">{t.fire_count}× fired</span>
                            {/if}
                            {#if t.last_fired_at}
                                <span style="font-family:var(--font-body);font-size:0.7rem;color:var(--text-muted)">{timeAgo(t.last_fired_at * 1000)}</span>
                            {/if}
                            <button class="btn btn-sm" on:click={() => toggleTrigger(t.id, !t.enabled)}>{t.enabled ? $_('common.disable') : $_('common.enable')}</button>
                            <button class="btn btn-sm" on:click={() => testTrigger(t.id)}>{$_('common.test')}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => deleteTrigger(t.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Schedules / Cron Jobs -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.cron_jobs')}</span>
                <button class="btn btn-sm btn-primary" on:click={() => cronModalOpen = true}>+ {$_('agents.cron_job')}</button>
            </div>
            <div>
                {#if schedules.length === 0}
                    <div class="empty" style="padding:0.8rem 1.5rem;font-size:0.8rem">{$_('agents.no_schedules')}</div>
                {:else}
                    {#each schedules as s}
                        <div class="token-item" style={!s.enabled ? 'opacity:0.5' : ''}>
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{s.name || 'unnamed'}</span>
                            <span style="font-family:var(--font-grotesk);font-size:0.75rem;color:var(--gray-mid)">{s.cron}</span>
                            <span class="badge badge-{s.enabled ? 'on' : 'off'}">{s.enabled ? $_('common.active') : $_('common.off')}</span>
                            <span style="flex:1"></span>
                            <button class="btn btn-sm" on:click={() => toggleSchedule(s.id, !s.enabled)}>{s.enabled ? $_('common.disable') : $_('common.enable')}</button>
                            <button class="btn btn-sm btn-danger" on:click={() => removeSchedule(s.id)}>X</button>
                        </div>
                    {/each}
                {/if}
            </div>
            {/if}<!-- end automation tab -->

            {#if activeTab === 'runtime'}
            <!-- Live Sessions (formerly Streaming Sessions) -->
            <div class="detail-section-header">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.live_sessions')}</span>
                <button class="btn btn-sm btn-primary" on:click={createStreamingSession}>+ {$_('agents.session')}</button>
            </div>
            <div>
                {#if streamingSessions.length === 0}
                    <div class="empty">{$_('agents.no_live_sessions')}</div>
                {:else}
                    {#each streamingSessions as ss}
                        <div class="token-item">
                            <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700">{ss.label}</span>
                            <span class="badge badge-{ss.connected ? 'on' : 'off'}">{ss.connected ? $_('agents.connected') : $_('agents.disconnected')}</span>
                            {#if ss.stats?.pending_responses > 0}<span class="badge" style="background:#fef3c7;color:#92400e">{ss.stats.pending_responses} pending</span>{/if}
                            <span style="flex:1"></span>
                            {#if ss.label !== 'main'}<button class="btn btn-sm btn-danger" on:click={() => deleteStreamingSession(ss.label)}>X</button>{/if}
                        </div>
                    {/each}
                {/if}
            </div>

            <!-- Conversations (formerly Active Sessions) -->
            <div class="detail-section-header" style="margin-top:0.5rem">
                <span style="font-family:var(--font-grotesk);font-size:0.8rem;font-weight:700;text-transform:uppercase">{$_('agents.conversations')}</span>
            </div>
            <div>
                {#if agentSessions.length === 0}
                    <div class="empty">{$_('agents.no_conversations')}</div>
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
                            <button class="btn btn-sm" on:click={() => window.location.hash = `/chat#${s.id}`}>{$_('agents_extra.session_chat_btn')}</button>
                        </div>
                    {/each}
                {/if}
            </div>
            {/if}<!-- end runtime tab -->
    </Modal>
</div>

<!-- Wizard -->
{#if wizardOpen}
    <div class="wizard-overlay">
        <div class="wizard">
            <div class="wizard-header">
                <div class="wizard-title">{importMode ? $_('agents.wiz_import_title') : $_('agents.wiz_new_title')}<span class="y">.</span></div>
                <div class="wizard-sub">{importMode ? $_('agents.wiz_import_sub') : $_('agents.wiz_new_sub')}</div>
            </div>

            {#if !importMode && wizStep === -1}
                <!-- Entry choice screen -->
                <div class="wizard-body">
                    <div class="wizard-label">{$_('agents.wiz_how_start')}</div>
                    <div class="wizard-hint">{$_('agents.wiz_choose_path')}</div>
                    <div class="import-entry-grid">
                        <div class="import-entry-card" on:click={() => { wizStep = 0; }}>
                            <div class="import-entry-title">{$_('agents.wiz_scratch_title')}</div>
                            <div class="import-entry-desc">{$_('agents.wiz_scratch_desc')}</div>
                        </div>
                        <div class="import-entry-card import-entry-disabled">
                            <div class="import-entry-title">{$_('agents.wiz_template_title')}</div>
                            <div class="import-entry-desc">{$_('agents.wiz_template_desc')}</div>
                        </div>
                        <div class="import-entry-card" on:click={() => { importMode = true; importStep = 1; }}>
                            <div class="import-entry-title">{$_('agents.wiz_import_openclaw_title')}</div>
                            <div class="import-entry-desc">{$_('agents.wiz_import_openclaw_desc')}</div>
                        </div>
                    </div>
                </div>
                <div class="wizard-footer">
                    <span></span>
                    <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('common.cancel')}</button>
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
                        <div class="wizard-label">{$_('agents.import_workspace_label')}</div>
                        <div class="wizard-hint">{$_('agents.import_workspace_hint')}</div>

                        <!-- Directory path input -->
                        <div class="wizard-label" style="margin-top:0.75rem;font-size:0.65rem">{$_('agents.import_dir_label')}</div>
                        <div class="import-file-row">
                            <input type="text" class="wizard-input" style="margin:0;flex:1;font-size:0.8rem" bind:value={importDirPath} placeholder="/Users/you/.openclaw/agents/alice">
                        </div>

                        <!-- Divider -->
                        <div style="display:flex;align-items:center;gap:0.5rem;margin:0.75rem 0;color:var(--gray-mid);font-size:0.7rem">
                            <div style="flex:1;height:1px;background:rgba(255,255,255,0.1)"></div>
                            <span>{$_('agents.import_or_upload')}</span>
                            <div style="flex:1;height:1px;background:rgba(255,255,255,0.1)"></div>
                        </div>

                        <!-- Drop zone -->
                        <div
                            class="import-dropzone"
                            class:dragover={importDragover}
                            on:dragover|preventDefault={() => importDragover = true}
                            on:dragleave={() => importDragover = false}
                            on:drop|preventDefault={(e) => { importDragover = false; importDirPath = ''; const f = e.dataTransfer?.files?.[0]; if (f) importFiles = { ...importFiles, workspace: f }; }}
                            on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.zip'; i.onchange = (/** @type {any} */ e) => { if (e.target?.files?.[0]) { importDirPath = ''; importFiles = { ...importFiles, workspace: e.target.files[0] }; } }; i.click(); }}
                        >
                            {#if importFiles.workspace}
                                <div class="import-dropzone-file">{importFiles.workspace.name}</div>
                                <div class="import-dropzone-hint">{$_('agents.import_click_change')}</div>
                            {:else}
                                <div class="import-dropzone-label">{$_('agents.import_drop_label')}</div>
                                <div class="import-dropzone-hint">{$_('agents.import_drop_hint')}</div>
                            {/if}
                        </div>

                        <div class="wizard-label" style="margin-top:1rem">{$_('agents.import_config_label')} <span style="color:var(--gray-mid);font-weight:400;text-transform:none">({$_('agents.import_config_optional')})</span></div>
                        <div class="wizard-hint" style="margin-top:-0.5rem">{$_('agents.import_config_hint')}</div>
                        <div class="import-file-row">
                            <span class="import-file-name">{importFiles.config ? importFiles.config.name : $_('agents.import_no_file')}</span>
                            <button class="wizard-btn" style="padding:0.4rem 0.8rem;font-size:0.7rem" on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.json'; i.onchange=(/** @type {any} */ e)=>{if(e.target?.files?.[0]) importFiles={...importFiles,config:e.target.files[0]}}; i.click(); }}>{$_('agents.import_browse')}</button>
                        </div>

                        <div class="wizard-label" style="margin-top:0.75rem">{$_('agents.import_lock_label')} <span style="color:var(--gray-mid);font-weight:400;text-transform:none">({$_('agents.import_config_optional')})</span></div>
                        <div class="wizard-hint" style="margin-top:-0.5rem">{$_('agents.import_lock_hint')}</div>
                        <div class="import-file-row">
                            <span class="import-file-name">{importFiles.lock ? importFiles.lock.name : $_('agents.import_no_file')}</span>
                            <button class="wizard-btn" style="padding:0.4rem 0.8rem;font-size:0.7rem" on:click={() => { const i = document.createElement('input'); i.type='file'; i.accept='.json'; i.onchange=(/** @type {any} */ e)=>{if(e.target?.files?.[0]) importFiles={...importFiles,lock:e.target.files[0]}}; i.click(); }}>{$_('agents.import_browse')}</button>
                        </div>

                        {#if importError}
                            <div class="import-error">{importError}</div>
                        {/if}
                    </div>
                    <div class="wizard-footer">
                        <button class="wizard-btn" on:click={() => { importMode = false; wizStep = -1; }}>{$_('common.back')}</button>
                        <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('common.cancel')}</button>
                        <button class="wizard-btn wizard-btn-primary" on:click={importParse} disabled={importLoading || (!importFiles.workspace && !importDirPath)}>
                            {importLoading ? $_('agents.import_parsing') : $_('agents.import_parse')}
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
                                <div class="import-loading-text">{$_('agents.import_analyzing')}</div>
                            </div>
                        {:else if importPreview}
                            {@const p = importPreview}

                            <!-- Identity section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">{$_('agents.import_identity_section')}</span>
                                    <span class="import-badge" style={importStatusBadgeStyle(p.identity?.status || 'ok')}>{importStatusIcon(p.identity?.status || 'ok')}</span>
                                </div>
                                <div class="import-field-row">
                                    <span class="import-field-key">{$_('agents.import_name_field')}</span>
                                    <span class="import-field-val">{p.identity?.name || '—'}</span>
                                </div>
                                {#if p.identity?.soul_preview}
                                    <div class="import-field-row">
                                        <span class="import-field-key">{$_('agents.import_soul_field')}</span>
                                        <span class="import-field-val import-truncate">{p.identity.soul_preview}</span>
                                    </div>
                                {/if}
                                {#if p.identity?.boundaries_preview}
                                    <div class="import-field-row">
                                        <span class="import-field-key">{$_('agents.import_boundaries_field')}</span>
                                        <span class="import-field-val import-truncate">{p.identity.boundaries_preview}</span>
                                    </div>
                                {/if}
                            </div>

                            <!-- Memory section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">{$_('agents.import_memory_section')}</span>
                                    <span class="import-count-badge">{$_('agents.import_memories_count', { values: { count: p.memory?.count ?? 0 } })}</span>
                                </div>
                                {#if p.memory_store_available === false && (p.memory?.count ?? 0) > 0}
                                    <div style="background:rgba(239,68,68,0.12);border-radius:var(--radius-lg);padding:0.5rem 0.75rem;font-size:0.75rem;color:var(--red,#ef4444);margin-bottom:0.4rem">
                                        ⚠️ {$_('agents.import_memory_unavailable', { values: { count: p.memory.count } })}
                                    </div>
                                {/if}
                                {#if p.memory?.samples && p.memory.samples.length > 0}
                                    {#each p.memory.samples.slice(0,3) as sample}
                                        <div class="import-memory-sample">{sample}</div>
                                    {/each}
                                {:else}
                                    <div class="import-empty">{$_('agents.import_no_memory')}</div>
                                {/if}
                            </div>

                            <!-- Connections section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">{$_('agents.import_connections_section')}</span>
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
                                    <div class="import-empty">{$_('agents.import_no_connections')}</div>
                                {/if}
                            </div>

                            <!-- Automation section -->
                            <div class="import-section">
                                <div class="import-section-header">
                                    <span class="wizard-label" style="margin:0">{$_('agents.import_automation_section')}</span>
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
                                        <span class="import-field-key">{$_('agents.import_schedules_field')}</span>
                                        <span class="import-field-val">{$_('agents.import_schedules_count', { values: { count: p.schedules_count, plural: p.schedules_count !== 1 ? 's' : '' } })}</span>
                                    </div>
                                {/if}
                                {#if (!p.skills || p.skills.length === 0) && (p.schedules_count ?? 0) === 0}
                                    <div class="import-empty">{$_('agents.import_none_detected')}</div>
                                {/if}
                            </div>

                            <!-- Warnings summary -->
                            {#if p.warnings && p.warnings.length > 0}
                                <div class="import-warnings">
                                    <div class="import-warnings-title">⚠️ {$_('agents.import_warnings_title', { values: { count: p.warnings.length, plural: p.warnings.length !== 1 ? 's' : '', singular_s: p.warnings.length === 1 ? 's' : '' } })}</div>
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
                        <button class="wizard-btn" on:click={() => { importStep = 1; importPreview = null; importError = null; }}>{$_('common.back')}</button>
                        <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('common.cancel')}</button>
                        <button class="wizard-btn wizard-btn-primary" on:click={importApply} disabled={importLoading || !importPreview}>
                            {importLoading ? $_('agents.import_working') : $_('agents.import_confirm')}
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
                                <div class="import-loading-text">{$_('agents.import_creating_agent')}</div>
                            </div>
                        {:else if importProgress.done}
                            <div class="import-done">
                                <div class="import-done-icon">🎉</div>
                                <div class="import-done-title">{$_('agents.import_agent_ready', { values: { name: importAgentName } })}</div>
                                {#if importAgentName && importAgentName.endsWith('-imported')}
                                    <div class="import-done-note">{@html $_('agents.import_agent_renamed_note', { values: { name: importAgentName } })}</div>
                                {/if}
                                {#if importProgress.failed > 0}
                                    <div class="import-done-warn">⚠️ {$_('agents.import_memories_partial', { values: { imported: importProgress.imported, failed: importProgress.failed } })}</div>
                                {:else}
                                    <div class="import-done-stat">{$_('agents.import_memories_success', { values: { count: importProgress.imported } })}</div>
                                {/if}
                                <div class="import-done-actions">
                                    <button class="wizard-btn wizard-btn-primary" on:click={() => { closeWizard(); window.location.hash = `/agents/${importAgentName}`; }}>{$_('agents.import_configure_btn')}</button>
                                    <button class="wizard-btn" on:click={() => { closeWizard(); window.location.hash = `/chat`; }}>{$_('agents.import_chat_btn')}</button>
                                </div>
                            </div>
                        {:else}
                            <!-- Memory import in progress -->
                            {#if true}
                                {@const pct = importProgress.total > 0 ? Math.round((importProgress.imported + importProgress.failed) / importProgress.total * 100) : 0}
                                <div class="import-progress-wrap">
                                    <div class="import-done-icon">⚙️</div>
                                    <div class="import-loading-text">{$_('agents.import_importing_memories')}</div>
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
                            <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('agents.import_close_bg')}</button>
                            <span></span>
                        </div>
                    {:else if importProgress.done}
                        <div class="wizard-footer">
                            <span></span>
                            <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('common.close')}</button>
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
                                <div class="wizard-option" class:selected={wizModel === val && !wizProviderRef && !wizCustomProvider}
                                     on:click={() => { wizModel = val; wizProviderRef = ''; wizCustomProvider = false; }}>
                                    <div class="wizard-option-title">{title}</div>
                                    <div class="wizard-option-desc">{desc}</div>
                                </div>
                            {/each}
                        </div>

                        {#if globalProviders.length > 0}
                            <div class="wizard-label" style="margin-top:1rem">Your Providers</div>
                            <div class="wizard-options">
                                {#each globalProviders as gp}
                                    <div class="wizard-option" class:selected={wizProviderRef === gp.id}
                                         on:click={() => { wizProviderRef = gp.id; wizModel = ''; wizCustomProvider = false; wizProviderUrl = ''; wizProviderKey = ''; }}>
                                        <div class="wizard-option-title">{gp.name.toUpperCase()}</div>
                                        <div class="wizard-option-desc">{gp.preset || 'custom'}</div>
                                    </div>
                                {/each}
                            </div>
                            {#if wizProviderRef}
                                <input type="text" class="wizard-input" bind:value={wizProviderModel}
                                    placeholder="Model string, e.g. glm-5.1, gpt-4o"
                                    style="margin-top:0.5rem">
                            {/if}
                        {/if}

                        <div class="wizard-options" style="margin-top:0.75rem">
                            <div class="wizard-option" class:selected={wizCustomProvider}
                                 on:click={() => { wizCustomProvider = !wizCustomProvider; if (wizCustomProvider) { wizModel = ''; wizProviderRef = ''; } }}>
                                <div class="wizard-option-title">CUSTOM</div>
                                <div class="wizard-option-desc">Bring your own endpoint.</div>
                            </div>
                        </div>
                        {#if wizCustomProvider}
                            <div style="margin-top:0.75rem;display:flex;flex-direction:column;gap:0.5rem">
                                <div style="display:flex;flex-wrap:wrap;gap:0.4rem;margin-bottom:0.25rem">
                                    {#each [['anthropic','Anthropic'],['zai','Z.ai'],['openrouter','OpenRouter'],['deepseek','DeepSeek'],['ollama','Ollama']] as [preset, label]}
                                        <button class="wizard-btn" style="padding:0.3rem 0.75rem;font-size:0.7rem;{wizProviderPreset===preset?'background:var(--accent);color:#000':''}"
                                            on:click={() => {
                                                wizProviderPreset = preset;
                                                if (preset === 'zai') { wizProviderUrl = 'https://api.z.ai/api/anthropic'; wizProviderModel = wizProviderModel || 'glm-5.1'; }
                                                else if (preset === 'ollama') { wizProviderUrl = 'http://localhost:11434'; }
                                                else if (preset === 'openrouter') { wizProviderUrl = 'https://openrouter.ai/api'; wizProviderModel = wizProviderModel || 'anthropic/claude-sonnet-4-5'; }
                                                else if (preset === 'deepseek') { wizProviderUrl = 'https://api.deepseek.com/anthropic'; wizProviderModel = wizProviderModel || 'deepseek-chat'; }
                                                else { wizProviderUrl = ''; }
                                            }}>{label}</button>
                                    {/each}
                                </div>
                                <input type="text" class="wizard-input" bind:value={wizProviderUrl}
                                    placeholder="Provider URL (e.g. https://api.z.ai/api/anthropic)" style="margin:0">
                                <input type="password" class="wizard-input" bind:value={wizProviderKey}
                                    placeholder="API key" style="margin:0">
                                <input type="text" class="wizard-input" bind:value={wizProviderModel}
                                    placeholder="Model string (e.g. glm-5.1, gpt-4o)" style="margin:0">
                            </div>
                        {/if}
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
                            Brain: <span class="val">
                                {#if wizProviderRef}
                                    {(globalProviders.find(p => p.id === wizProviderRef)?.name || wizProviderRef).toUpperCase()}{wizProviderModel ? ' / ' + wizProviderModel : ''}
                                {:else if wizCustomProvider}
                                    Custom{wizProviderModel ? ': ' + wizProviderModel : ''}
                                {:else}
                                    {wizModel.toUpperCase()}
                                {/if}
                            </span><br>
                            Heart: <span class="val">{wizHeart.toUpperCase()}</span><br>
                            Auto-Start: <span class="val">{wizAutoStart ? 'Yes' : 'No'}</span><br>
                            Heartbeat: <span class="val">{wizHeartbeatInterval ? wizHeartbeatInterval + 's' : 'Disabled'}</span><br>
                            Outreach: <span class="val">{wizSummaryPlatforms.length ? wizSummaryPlatforms.join(', ') : 'None (local only)'}</span>
                        </div>
                    {/if}
                </div>
                <div class="wizard-footer">
                    <button class="wizard-btn" on:click={wizardPrev}>{$_('common.back')}</button>
                    <button class="wizard-btn" on:click={closeWizard} style="color:var(--gray-mid)">{$_('common.cancel')}</button>
                    <button class="wizard-btn wizard-btn-primary" on:click={wizardNext}>{wizStep === wizTotalSteps - 1 ? $_('agents.wiz_summon') : $_('common.next')}</button>
                </div>
            {/if}
        </div>
    </div>
{/if}

<style>
    /* Stats bar */
    .stats-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(140px, 1fr)); gap: 0.5rem; margin-bottom: 1rem; }
    .stat-card { background: var(--surface-1); border-radius: var(--radius-lg); padding: 0.8rem 1rem; }
    .stat-label { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; color: var(--gray-mid); }
    .stat-value { font-family: var(--font-grotesk); font-size: 1.5rem; font-weight: 700; }
    .stat-sub { font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--gray-mid); }

    /* Header with search */
    .header-actions { display: flex; gap: 0.5rem; align-items: center; }
    .search-bar { display: flex; gap: 0.3rem; align-items: center; }
    .search-input { font-family: var(--font-body); font-size: 0.8rem; padding: 0.35rem 0.6rem; border: none; border-radius: var(--radius-lg); background: var(--input-bg); color: var(--text-primary); width: 200px; }
    .search-input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; }

    .agent-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 1rem; }
    .agent-card {
        display: flex; flex-direction: column; gap: 0.6rem;
        padding: 1rem 1.2rem; background: var(--surface-1); border: 1px solid var(--border);
        border-radius: var(--radius-lg); cursor: pointer; transition: all 0.15s;
    }
    .agent-card:hover { background: var(--surface-2); border-color: var(--text-muted); transform: translateY(-1px); }
    .agent-header { display: flex; align-items: center; gap: 0.5rem; }
    .agent-dot { width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .agent-name { font-family: var(--font-grotesk); font-size: 0.95rem; font-weight: 700; flex: 1; }
    .agent-status-tag { font-family: var(--font-grotesk); font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.08em; font-weight: 600; }
    .agent-work { font-size: 0.78rem; color: var(--text-secondary); min-height: 1.2em; }
    .agent-stats { display: flex; flex-direction: column; gap: 0.35rem; }
    .agent-stat { display: flex; align-items: center; gap: 0.5rem; }
    .stat-bar-wrap { flex: 1; height: 4px; background: var(--surface-3); border-radius: 2px; overflow: hidden; }
    .stat-bar { height: 100%; border-radius: 2px; transition: width 0.3s; }
    .stat-val { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 600; min-width: 2.5rem; text-align: right; }
    .agent-meta-stats { display: flex; align-items: center; gap: 0.3rem; font-size: 0.68rem; color: var(--text-muted); }
    .meta-dot { color: var(--border); }
    .platform-tag { color: var(--tone-lilac-text); font-weight: 600; }
    .agent-tags { display: flex; gap: 0.4rem; flex-wrap: wrap; }
    .agent-model-tag { font-family: var(--font-grotesk); font-size: 0.6rem; text-transform: uppercase; letter-spacing: 0.06em; color: var(--text-muted); background: var(--surface-2); padding: 0.15rem 0.4rem; border-radius: var(--radius); }

    /* Inline detail (sessions/outreach) */
    .agent-inline-detail { border-top: 1px solid var(--surface-2); margin: 0.5rem -1.5rem; padding: 0.5rem 1.5rem; display: flex; flex-direction: column; gap: 0.3rem; }
    .outreach-row { display: flex; align-items: center; gap: 0.5rem; padding: 0.2rem 0; font-size: 0.7rem; }
    .session-row { display: flex; align-items: center; gap: 0.5rem; padding: 0.3rem 0; font-size: 0.7rem; flex-wrap: wrap; }
    .session-id { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 600; min-width: 50px; }
    .context-bar { height: 4px; background: var(--gray-light); border-radius: 2px; overflow: hidden; }
    .context-fill { height: 100%; border-radius: 2px; }
    .context-fill.ctx-ok { background: var(--accent, #f5c842); }
    .context-fill.ctx-warn { background: #f97316; }

    /* Search results */
    .search-result-row { display: flex; align-items: center; gap: 0.6rem; padding: 0.5rem 1rem; font-size: 0.75rem; border-bottom: 1px solid var(--surface-2); }
    .search-result-session { font-family: var(--font-mono); font-size: 0.65rem; color: var(--gray-mid); min-width: 80px; }
    .search-result-content { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-primary); }
    .search-result-time { font-size: 0.6rem; color: var(--gray-mid); flex-shrink: 0; }

    /* Groups */
    .group-chip { display: flex; align-items: center; gap: 0.4rem; background: var(--surface-2); padding: 0.4rem 0.8rem; border-radius: var(--radius-lg); }
    .group-name { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 600; }
    .group-count { font-size: 0.6rem; background: var(--primary-container); color: var(--on-primary-container); padding: 0.1rem 0.35rem; border-radius: 99px; }

    /* Badge additions */
    .badge-platform { background: #dbeafe; color: #1e40af; }
    .agent-actions { display: flex; gap: 0.3rem; flex-wrap: wrap; align-items: center; }
    .btn-sleep { display: flex; align-items: center; gap: 0.25rem; color: var(--text-muted); }
    .btn-sleep:hover { color: var(--yellow); }

    .directive-item { display: flex; align-items: center; gap: 0.8rem; padding: 0.6rem 1rem; background: var(--surface-1); border-radius: var(--radius-lg); }
    .directive-item:nth-child(even) { background: var(--surface-2); }
    .directive-priority { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; background: var(--yellow); padding: 0.1rem 0.4rem; min-width: 24px; text-align: center; border-radius: var(--radius-lg); }
    .directive-text { flex: 1; font-size: 0.88rem; }
    .directive-inactive { opacity: 0.5; text-decoration: line-through; }

    .token-item { display: flex; align-items: center; gap: 1rem; padding: 0.6rem 1rem; background: var(--surface-1); border-radius: var(--radius-lg); }
    .token-item:nth-child(even) { background: var(--surface-2); }

    .wizard-overlay { position: fixed; inset: 0; background: var(--overlay-scrim); z-index: 999; display: flex; align-items: center; justify-content: center; }
    .wizard { background: var(--surface-1); color: var(--text-primary); border: none; border-radius: var(--radius-xl); max-width: 600px; width: 95%; max-height: 90vh; overflow-y: auto; }
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
    .wizard-input { font-family: var(--font-grotesk); font-size: 1rem; padding: 0.8rem 1rem; border: none; background: var(--surface-2); color: var(--text-primary); width: 100%; margin-bottom: 1rem; border-radius: var(--radius-lg); }
    .wizard-input:focus { outline: 2px solid var(--accent); }
    .wizard-options { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-option { padding: 1rem; border: none; background: var(--surface-2); border-radius: var(--radius-lg); cursor: pointer; text-align: center; transition: all 0.15s; }
    .wizard-option:hover { background: var(--surface-3); }
    .wizard-option.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-option-title { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; margin-bottom: 0.2rem; color: var(--text-primary); }
    .wizard-option-desc { font-size: 0.75rem; color: var(--text-muted); }
    .wizard-option.selected .wizard-option-desc { color: var(--accent); }
    .wizard-hearts { display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 1rem; }
    .wizard-heart { padding: 1.2rem; border: none; background: var(--surface-2); border-radius: var(--radius-lg); cursor: pointer; transition: all 0.15s; }
    .wizard-heart:hover { background: var(--surface-3); }
    .wizard-heart.selected { background: var(--accent-soft); box-shadow: inset 0 0 0 2px var(--accent); }
    .wizard-heart-icon { font-size: 1.5rem; margin-bottom: 0.3rem; }
    .wizard-heart-name { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; color: var(--text-primary); }
    .wizard-heart-desc { font-size: 0.7rem; color: var(--text-muted); margin-top: 0.2rem; }
    .wizard-heart.selected .wizard-heart-desc { color: var(--accent); }
    .wizard-footer { display: flex; justify-content: space-between; padding: 1.5rem 2rem; background: var(--surface-2); }
    .wizard-btn { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; padding: 0.6rem 1.5rem; border: none; background: var(--surface-2); color: var(--text-primary); cursor: pointer; text-transform: uppercase; border-radius: var(--radius-lg); }
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
    .import-entry-card { padding: 1.2rem 1.4rem; background: var(--surface-2); border-radius: var(--radius-lg); cursor: pointer; transition: all 0.15s; border: 1px solid transparent; }
    .import-entry-card:hover { background: var(--surface-3); border-color: var(--accent); }
    .import-entry-card.import-entry-disabled { opacity: 0.45; cursor: not-allowed; }
    .import-entry-card.import-entry-disabled:hover { background: var(--surface-2); border-color: transparent; }
    .import-entry-icon { font-size: 1.4rem; margin-bottom: 0.4rem; }
    .import-entry-title { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 700; margin-bottom: 0.2rem; }
    .import-entry-desc { font-size: 0.78rem; color: var(--text-muted); line-height: 1.4; }

    .import-dropzone { border: 2px dashed var(--surface-dim); border-radius: var(--radius-lg); padding: 2rem 1.5rem; text-align: center; cursor: pointer; transition: border-color 0.15s, background 0.15s; margin-bottom: 1rem; }
    .import-dropzone:hover { border-color: var(--text-muted); background: var(--surface-2); }
    .import-dropzone.dragover { border: 2px solid var(--accent); background: var(--surface-3); }
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
