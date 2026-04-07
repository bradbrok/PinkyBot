<script>
    import { onMount, onDestroy, tick } from 'svelte';
    import { api } from '../lib/api.js';
    import { escapeHtml, renderMarkdown } from '../lib/utils.js';

    /**
     * Parse broker metadata header from user messages.
     * DM format:    [platform | dm | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Group format: [platform | group | display | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Legacy format: [platform | sender | chat_id | timestamp tz | msg_id:123]\ncontent
     * Returns { meta: {platform, type, sender, chatId, timestamp, msgId, groupName} | null, content: string }
     */
    function parseBrokerMessage(text) {
        const match = text.match(/^\[([^\]]+)\]\n?([\s\S]*)$/);
        if (!match) return { meta: null, content: text };
        const parts = match[1].split('|').map(s => s.trim());
        if (parts.length < 3) return { meta: null, content: text };

        const meta = { platform: parts[0] };
        if (parts[1] === 'dm') {
            // [platform | dm | sender | chat_id | ts | msg_id]
            meta.type = 'dm';
            meta.sender = parts[2] || '';
            meta.chatId = parts[3] || '';
            meta.timestamp = parts[4] || '';
            if (parts[5]) meta.msgId = parts[5].replace('msg_id:', '');
        } else if (parts[1] === 'group') {
            // [platform | group | display | sender | chat_id | ts | msg_id]
            meta.type = 'group';
            meta.groupName = parts[2] || '';
            meta.sender = parts[3] || '';
            meta.chatId = parts[4] || '';
            meta.timestamp = parts[5] || '';
            if (parts[6]) meta.msgId = parts[6].replace('msg_id:', '');
        } else {
            // Legacy: [platform | sender | chat_id | ts | msg_id]
            meta.type = 'dm';
            meta.sender = parts[1];
            meta.chatId = parts[2];
            if (parts.length >= 4) meta.timestamp = parts[3];
            if (parts.length >= 5) meta.msgId = parts[4].replace('msg_id:', '');
        }
        return { meta, content: match[2] || '' };
    }

    let agentsList = [];
    let sessionsList = [];
    let activeSession = null;
    let activeAgent = null;
    let activeSessionLabel = 'main';
    let activeSessionStreaming = false;
    let messages = [];
    let messageInput = '';
    let sending = false;
    let thinking = false;
    let thinkingActivity = '';
    let activityLog = [];
    let agentWorking = false;       // true when agent is processing (any source)
    let activityPollInterval = null;
    let wasWorking = false;         // detect working→idle transition to trigger chat refresh
    let connected = true;

    let infoModel = '--';
    let infoContext = '0%';
    let infoContextPct = 0;
    let infoMessages = 0;
    let infoSession = '--';

    let sidebarCollapsed = false;
    let messagesContainer;
    let refreshInterval;
    let workingStatusInterval;
    let restarting = false;
    let compacting = false;
    let archiving = false;

    // Pagination
    const PAGE_SIZE = 100;
    let hasMore = false;
    let totalMessages = 0;      // total messages in store for this session
    let olderPrepended = 0;     // count of older messages prepended via scroll-back
    let loadingOlder = false;

    // Settings panel
    let showSettings = false;
    let selectedModel = '';
    let contextNudgePct = 80;
    let savingModel = false;
    let savingNudge = false;

    const availableModels = [
        { value: 'claude-sonnet-4-6', label: 'Sonnet 4.6 (1M)' },
        { value: 'claude-opus-4-6', label: 'Opus 4.6 (1M)' },
        { value: 'claude-sonnet-4-5-20250514', label: 'Sonnet 4.5' },
        { value: 'claude-haiku-4-5-20251001', label: 'Haiku 4.5' },
    ];

    // Group sessions by agent
    $: agentSessions = groupByAgent(agentsList, sessionsList);

    function groupByAgent(agents, sessions) {
        const agentNames = new Set(agents.map(a => a.name));
        const groups = {};
        const orphans = [];
        for (const s of sessions) {
            const owner = s.agent_name || '';
            if (owner && agentNames.has(owner)) {
                if (!groups[owner]) groups[owner] = [];
                groups[owner].push(s);
            } else {
                let matched = false;
                for (const aName of agentNames) {
                    if (s.id.startsWith(aName + '-') || s.id === aName) {
                        if (!groups[aName]) groups[aName] = [];
                        groups[aName].push(s);
                        matched = true;
                        break;
                    }
                }
                if (!matched) orphans.push(s);
            }
        }
        for (const sessionGroup of Object.values(groups)) {
            sessionGroup.sort((a, b) => {
                const aMain = (a.label || '') === 'main' ? 1 : 0;
                const bMain = (b.label || '') === 'main' ? 1 : 0;
                if (aMain !== bMain) return bMain - aMain;
                return (b.last_active || 0) - (a.last_active || 0);
            });
        }
        return { groups, orphans };
    }

    function inferAgentName(sessionId) {
        if (!sessionId || !sessionId.includes('-')) return '';
        return sessionId.split('-')[0];
    }

    function normalizeSessionLabel(raw) {
        return (raw || '')
            .trim()
            .toLowerCase()
            .replace(/\s+/g, '-')
            .replace(/[^a-z0-9_-]/g, '')
            .replace(/-+/g, '-')
            .replace(/^[-_]+|[-_]+$/g, '');
    }

    async function refreshSessions() {
        try {
            const [agentsData, sessData, convsData] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
                api('GET', '/conversations'),
            ]);
            agentsList = agentsData.agents || [];
            const convById = new Map((convsData.conversations || []).map(c => [c.session_id, c]));
            const streamingResults = await Promise.all(
                agentsList.map(agent =>
                    api('GET', `/agents/${agent.name}/streaming-sessions`).catch(() => ({ sessions: [] }))
                )
            );

            const knownIds = new Set();
            const streamingSessions = [];
            for (let i = 0; i < agentsList.length; i++) {
                const agent = agentsList[i];
                for (const ss of (streamingResults[i].sessions || [])) {
                    const convo = convById.get(ss.id);
                    knownIds.add(ss.id);
                    streamingSessions.push({
                        id: ss.id,
                        label: ss.label || 'main',
                        state: ss.connected ? 'streaming' : 'idle',
                        model: agent.model || 'streaming',
                        message_count: convo?.message_count ?? 0,
                        last_active: convo?.last_message_at ?? 0,
                        agent_name: agent.name,
                        session_type: 'streaming',
                        streaming: true,
                        connected: !!ss.connected,
                        stats: ss.stats || {},
                    });
                }
            }

            const standaloneSessions = (sessData || []).map(s => ({
                ...s,
                label: s.label || '',
                streaming: false,
            }));
            standaloneSessions.forEach(s => knownIds.add(s.id));

            const convSessions = (convsData.conversations || [])
                .filter(c => !knownIds.has(c.session_id))
                .map(c => ({
                    id: c.session_id,
                    label: '',
                    state: 'history',
                    model: 'history',
                    message_count: c.message_count,
                    last_active: c.last_message_at,
                    agent_name: inferAgentName(c.session_id),
                    session_type: 'history',
                    streaming: false,
                    _from_store: true,
                }));

            sessionsList = [...streamingSessions, ...standaloneSessions, ...convSessions];
            connected = true;
        } catch {
            connected = false;
        }
    }

    async function selectSession(session) {
        activeSession = session?.id || null;
        activeAgent = session?.agent_name || null;
        activeSessionLabel = session?.label || 'main';
        activeSessionStreaming = !!session?.streaming;
        agentWorking = false;
        thinkingActivity = '';
        activityLog = [];
        wasWorking = false;
        hasMore = false;
        totalMessages = 0;
        olderPrepended = 0;
        if (window.innerWidth <= 768) sidebarCollapsed = true;
        await refreshChat();
        startChatPolling();
        startActivityPolling();
    }

    let chatPollInterval;

    async function refreshChat() {
        if (!activeSession) return;
        const agentName = activeAgent || inferAgentName(activeSession);

        // Primary source: conversation store
        let allMessages = [];
        try {
            const streamHistory = await api('GET', `/conversations/${activeSession}/history?limit=${PAGE_SIZE}`);
            allMessages = (streamHistory.messages || []).sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
            hasMore = streamHistory.has_more || false;
            totalMessages = streamHistory.total || allMessages.length;
            infoMessages = totalMessages;
        } catch {
            // Fall back to session manager history
            try {
                const history = await api('GET', `/sessions/${activeSession}/history`);
                allMessages = history.messages || [];
                hasMore = false;
                totalMessages = allMessages.length;
                infoMessages = allMessages.length;
            } catch {}
        }
        infoSession = activeSession;

        // Load agent config for model/nudge settings (only on first load)
        try {
            if (agentName) {
                const agentData = await api('GET', `/agents/${agentName}`);
                if (agentData.model && !selectedModel) selectedModel = agentData.model;
                if (agentData.restart_threshold_pct != null) contextNudgePct = agentData.restart_threshold_pct;
                if (agentData.model) infoModel = agentData.model;
            }
        } catch {
            infoModel = '--';
        }

        // Get context from the selected streaming session when available
        let gotStreamingContext = false;
        if (activeSessionStreaming && agentName) {
            try {
                const streamStatus = await api('GET', `/agents/${agentName}/streaming/status?label=${encodeURIComponent(activeSessionLabel)}`);
                if (streamStatus.connected) {
                    const ctx = streamStatus.context || {};
                    if (ctx.percentage != null) {
                        infoContext = `${ctx.percentage}%`;
                        infoContextPct = ctx.percentage;
                        gotStreamingContext = true;
                    }
                }
            } catch {}
        }

        // Fallback to session manager context if streaming unavailable
        if (!gotStreamingContext) {
            try {
                const context = await api('GET', `/sessions/${activeSession}/context`);
                infoContext = `${context.context_used_pct}%`;
                infoContextPct = context.context_used_pct || 0;
            } catch {
                infoContext = '--';
                infoContextPct = 0;
            }
        }

        const oldLen = messages.length;
        // If user scrolled back and loaded older messages, preserve them at the front
        if (olderPrepended > 0) {
            const olderMessages = messages.slice(0, olderPrepended);
            messages = [...olderMessages, ...allMessages];
        } else {
            messages = allMessages;
        }
        // Update hasMore based on whether there are still unloaded messages
        hasMore = totalMessages > messages.length;
        await tick();
        if (messages.length > oldLen) scrollToBottom();
    }

    async function loadOlderMessages() {
        if (!activeSession || loadingOlder || !hasMore) return;
        loadingOlder = true;
        // offset = total messages currently loaded
        const currentOffset = messages.length;
        try {
            const older = await api('GET', `/conversations/${activeSession}/history?limit=${PAGE_SIZE}&offset=${currentOffset}`);
            const olderMsgs = (older.messages || []).sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
            if (olderMsgs.length > 0) {
                const container = messagesContainer;
                const prevHeight = container.scrollHeight;
                const prevTop = container.scrollTop;
                messages = [...olderMsgs, ...messages];
                olderPrepended += olderMsgs.length;
                hasMore = older.has_more || false;
                await tick();
                // Restore scroll so content doesn't jump
                container.scrollTop = container.scrollHeight - prevHeight + prevTop;
            } else {
                hasMore = false;
            }
        } catch {
            // Silently fail — user can try scrolling up again
        }
        loadingOlder = false;
    }

    function handleMessagesScroll() {
        if (!messagesContainer) return;
        // Load older when scrolled near the top
        if (messagesContainer.scrollTop < 200 && hasMore && !loadingOlder) {
            loadOlderMessages();
        }
    }

    function startChatPolling() {
        stopChatPolling();
        chatPollInterval = setInterval(refreshChat, 3000);
    }

    function stopActivityPolling() {
        if (activityPollInterval) { clearInterval(activityPollInterval); activityPollInterval = null; }
    }

    function startActivityPolling() {
        stopActivityPolling();
        activityPollInterval = setInterval(async () => {
            const agentName = activeAgent || (activeSession ? inferAgentName(activeSession) : null);
            if (!agentName || !activeSessionStreaming) return;
            try {
                const status = await api('GET', `/agents/${agentName}/streaming/status?label=${encodeURIComponent(activeSessionLabel)}`);
                const activity = status?.stats?.current_activity || '';
                const log = status?.stats?.activity_log || [];
                const isWorking = !!activity;

                // Update thinking bubble only when not in a web-UI send flow
                if (!thinking) {
                    agentWorking = isWorking;
                    if (isWorking) {
                        thinkingActivity = activity;
                        activityLog = log;
                    }
                }

                // Detect working→idle: refresh chat to pull in new messages
                if (wasWorking && !isWorking && !thinking) {
                    agentWorking = false;
                    thinkingActivity = '';
                    activityLog = [];
                    await refreshChat();
                }
                wasWorking = isWorking;
            } catch { /* ignore */ }
        }, 1000);
    }

    function stopChatPolling() {
        if (chatPollInterval) { clearInterval(chatPollInterval); chatPollInterval = null; }
    }

    async function sendMessage() {
        if (!activeSession || !messageInput.trim() || sending) return;
        const text = messageInput.trim();
        messageInput = '';
        sending = true;
        thinking = true;
        messages = [...messages, { role: 'user', content: text }];
        await tick();
        scrollToBottom();

        try {
            const agentName = activeAgent || inferAgentName(activeSession);
            if (activeSessionStreaming && agentName) {
                await api('POST', `/agents/${agentName}/chat?label=${encodeURIComponent(activeSessionLabel)}`, { content: text });
                // Response comes async via streaming — poll for it
                thinking = true;
                thinkingActivity = '';
                let attempts = 0;
                while (attempts < 30) {
                    await new Promise(r => setTimeout(r, 1000));
                    const [hist, status] = await Promise.all([
                        api('GET', `/conversations/${activeSession}/history?limit=5`),
                        api('GET', `/agents/${agentName}/streaming/status?label=${encodeURIComponent(activeSessionLabel)}`).catch(() => null),
                    ]);
                    if (status?.stats) {
                        thinkingActivity = status.stats.current_activity || '';
                        activityLog = status.stats.activity_log || [];
                    }
                    const lastMsg = (hist.messages || []).slice(-1)[0];
                    if (lastMsg && lastMsg.role === 'assistant' && lastMsg.timestamp > (Date.now() / 1000 - 5)) {
                        messages = [...messages, { role: 'assistant', content: lastMsg.content, metadata: lastMsg.metadata }];
                        break;
                    }
                    attempts++;
                }
                thinking = false;
                thinkingActivity = '';
                activityLog = [];
            } else {
                const data = await api('POST', `/sessions/${activeSession}/message`, { content: text });
                thinking = false;
                messages = [...messages, { role: 'assistant', content: data.content }];
            }
            infoMessages += 2;
        } catch (e) {
            thinking = false;
            messages = [...messages, { role: 'system', content: `Error: ${e.message}` }];
        }

        sending = false;
        await tick();
        scrollToBottom();
    }

    function scrollToBottom() {
        if (messagesContainer) messagesContainer.scrollTop = messagesContainer.scrollHeight;
    }

    function handleKeydown(e) {
        if (e.key === 'Enter') sendMessage();
    }

    let fileInput;

    async function handleFileUpload() {
        if (!fileInput.files[0] || !activeAgent || !activeSessionStreaming) return;
        const file = fileInput.files[0];
        const formData = new FormData();
        formData.append('file', file);

        sending = true;
        messages = [...messages, { role: 'user', content: `📎 Uploading: ${file.name} (${(file.size / 1024).toFixed(1)} KB)` }];
        await tick();
        scrollToBottom();

        try {
            const resp = await fetch(`/agents/${activeAgent}/upload?label=${encodeURIComponent(activeSessionLabel)}`, {
                method: 'POST',
                body: formData,
            });
            if (!resp.ok) throw new Error(await resp.text());
            const data = await resp.json();
            messages = [...messages, { role: 'system', content: `File uploaded: ${data.filename} (${data.size} bytes) → ${data.path}` }];
        } catch (e) {
            messages = [...messages, { role: 'system', content: `Upload failed: ${e.message}` }];
        }

        sending = false;
        fileInput.value = '';
        await tick();
        scrollToBottom();
    }

    async function contextRestart() {
        if (!activeSession || restarting) return;
        restarting = true;

        try {
            messages = [...messages, { role: 'system', content: 'Context restart initiated...', metadata: { checkpoint: 'context-restart' } }];
            await tick(); scrollToBottom();

            if (activeSessionStreaming && activeAgent) {
                await api('POST', `/agents/${activeAgent}/streaming/restart?label=${encodeURIComponent(activeSessionLabel)}`);
            } else {
                const savePrompt = 'Your session is about to be restarted. Save your current state now:\n\n' +
                    '1. Use your save_my_context or set wake context tool to persist what you were working on\n' +
                    '2. Include: current task, key context, any blockers, and what to do next\n' +
                    '3. Confirm when saved\n\n' +
                    'This is a context restart — your conversation will reset but your saved state will carry over.';

                const saveResult = await api('POST', `/sessions/${activeSession}/message`, { content: savePrompt });
                messages = [...messages, { role: 'assistant', content: saveResult.content, duration_ms: saveResult.duration_ms }];
                await tick(); scrollToBottom();
                await api('POST', `/sessions/${activeSession}/restart`);
            }
            await logCheckpoint('context-restart', 'Context restarted via UI');
            messages = [...messages, { role: 'system', content: 'Session restarted.', metadata: { checkpoint: 'context-restart' } }];

            await refreshChat();
            await refreshSessions();
        } catch (e) {
            messages = [...messages, { role: 'system', content: `Restart failed: ${e.message}` }];
        }

        restarting = false;
        await tick(); scrollToBottom();
    }

    async function logCheckpoint(type, detail) {
        if (!activeSession) return;
        try {
            await api('POST', `/conversations/${activeSession}/checkpoint`, { type, detail });
        } catch { /* best effort */ }
    }

    async function compactContext() {
        if (!activeAgent || !activeSessionStreaming || compacting) return;
        compacting = true;
        messages = [...messages, { role: 'system', content: 'Compacting context...', metadata: { checkpoint: 'compact' } }];
        await tick(); scrollToBottom();
        try {
            await api('POST', `/agents/${activeAgent}/streaming/compact?label=${encodeURIComponent(activeSessionLabel)}`);
            await logCheckpoint('compact', 'Context compacted');
            messages = [...messages, { role: 'system', content: 'Context compacted.', metadata: { checkpoint: 'compact' } }];
            await refreshChat();
        } catch (e) {
            messages = [...messages, { role: 'system', content: `Compact failed: ${e.message}` }];
        }
        compacting = false;
        await tick(); scrollToBottom();
    }

    async function archiveSession() {
        if (!activeAgent || !activeSessionStreaming || archiving) return;
        if (!confirm('Archive this session? The agent will save its memory, then get a fresh context.')) return;
        archiving = true;
        messages = [...messages, { role: 'system', content: 'Archiving — asking agent to save memory...', metadata: { checkpoint: 'archive' } }];
        await tick(); scrollToBottom();
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/archive?label=${encodeURIComponent(activeSessionLabel)}`);
            await logCheckpoint('archive', `Archived. ${result.old_turns} turns. Session: ${result.old_session_id}`);
            messages = [...messages, { role: 'system', content: `Archived. Old session had ${result.old_turns} turns. Fresh session started.`, metadata: { checkpoint: 'archive' } }];
            await refreshChat();
            await refreshSessions();
        } catch (e) {
            messages = [...messages, { role: 'system', content: `Archive failed: ${e.message}` }];
        }
        archiving = false;
        await tick(); scrollToBottom();
    }

    async function saveModel() {
        if (!activeAgent || !activeSessionStreaming || !selectedModel) return;
        savingModel = true;
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/model?label=${encodeURIComponent(activeSessionLabel)}`, { model: selectedModel });
            if (result.restarted) {
                messages = [...messages, { role: 'system', content: `Model changed to ${selectedModel} — session restarted for new context window (${result.old_turns} turns saved)` }];
                await refreshChat();
                await refreshSessions();
            } else {
                messages = [...messages, { role: 'system', content: `Model switched to ${selectedModel} (mid-session, no restart needed)` }];
            }
            await tick(); scrollToBottom();
        } catch {
            try {
                await api('PUT', `/agents/${activeAgent}`, { model: selectedModel });
                messages = [...messages, { role: 'system', content: `Model set to ${selectedModel} (takes effect on next session)` }];
                await tick(); scrollToBottom();
            } catch (e) {
                alert(`Failed to update model: ${e.message}`);
            }
        }
        savingModel = false;
    }

    async function saveNudge() {
        if (!activeAgent) return;
        savingNudge = true;
        try {
            await api('PUT', `/agents/${activeAgent}`, { restart_threshold_pct: contextNudgePct });
        } catch (e) {
            alert(`Failed to update nudge: ${e.message}`);
        }
        savingNudge = false;
    }

    async function spawnAgentSession(agentName) {
        const requested = prompt('New session name:', 'chat');
        if (requested === null) return;
        const label = normalizeSessionLabel(requested);
        if (!label) {
            alert('Session name must contain letters or numbers.');
            return;
        }
        if (label === 'main') {
            alert('Use a different name. "main" already exists.');
            return;
        }
        await api('POST', `/agents/${agentName}/streaming-sessions?label=${encodeURIComponent(label)}`);
        await refreshSessions();
        const created = sessionsList.find(s => s.id === `${agentName}-${label}`);
        if (created) await selectSession(created);
    }

    onMount(() => {
        refreshSessions();
        refreshInterval = setInterval(refreshSessions, 10000);
        workingStatusInterval = setInterval(async () => {
            try {
                const data = await api('GET', '/agents');
                agentsList = data.agents || agentsList;
            } catch {}
        }, 5000);
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        clearInterval(workingStatusInterval);
        stopChatPolling();
        stopActivityPolling();
    });
</script>

<div class="main">
    <button class="sidebar-toggle" on:click={() => sidebarCollapsed = !sidebarCollapsed}>
        {sidebarCollapsed ? '▼ Select Agent' : '▲ Hide Agents'}
    </button>
    <div class="sidebar" class:collapsed={sidebarCollapsed}>
        <div class="sidebar-header">Agents</div>
        <div class="session-list">
            {#each agentsList as agent}
                {@const aSessions = agentSessions.groups[agent.name] || []}
                <div class="agent-group">
                    <div class="agent-group-header" on:click={() => { if (aSessions.length > 0) selectSession(aSessions[0]); }}>
                        <span class="chat-working-dot" class:working={agent.working_status === 'working'} title={agent.working_status === 'working' ? 'Working' : 'Idle'}></span>
                        <span class="agent-name-text">{agent.display_name || agent.name}</span>
                        <span class="agent-model">{(agent.model || '').replace(/^claude-/i, '')}</span>
                        <button class="btn-new" on:click|stopPropagation={() => spawnAgentSession(agent.name)}>+</button>
                    </div>
                    {#each aSessions as s}
                        {@const isMain = (s.label || '') === 'main'}
                        {@const label = s.label || s.id}
                        <div
                            class="session-item"
                            class:active={activeSession === s.id}
                            class:main-session={isMain}
                            on:click={() => selectSession(s)}
                        >
                            <span class="session-label">{label}</span>
                            <span class="session-count">{s.message_count}</span>
                        </div>
                    {/each}
                </div>
            {/each}
            {#if agentSessions.orphans.length > 0}
                <div class="agent-group">
                    <div class="agent-group-header"><span style="color:var(--gray-mid)">Standalone</span></div>
                    {#each agentSessions.orphans as s}
                        <div class="session-item" class:active={activeSession === s.id} on:click={() => selectSession(s)}>
                            <span class="session-label">{s.id}</span>
                            <span class="session-count">{s.message_count}</span>
                        </div>
                    {/each}
                </div>
            {/if}
        </div>
    </div>

    <div class="chat-area">
        {#if !activeSession}
            <div class="empty-state">Select an agent to start chatting</div>
        {:else}
            <div class="chat-info">
                <span class="info-context" class:warning={infoContextPct >= contextNudgePct}>Context: <strong>{infoContext}</strong></span>
                <span>Messages: <strong>{infoMessages}</strong></span>
                <span>Session: <strong>{infoSession}</strong></span>
                <div class="chat-actions">
                    <button class="btn-action" on:click={() => showSettings = !showSettings} disabled={!activeSessionStreaming}>Model</button>
                    <button class="btn-action" class:active-action={compacting} on:click={compactContext} disabled={compacting || !activeSessionStreaming}>{compacting ? 'Compacting...' : 'Compact'}</button>
                    <button class="btn-restart" class:restarting on:click={contextRestart} disabled={restarting}>{restarting ? 'Restarting...' : 'Context Restart'}</button>
                    <button class="btn-action btn-archive" class:active-action={archiving} on:click={archiveSession} disabled={archiving || !activeSessionStreaming}>{archiving ? 'Archiving...' : 'Archive'}</button>
                </div>
            </div>
            {#if showSettings}
                <div class="settings-bar">
                    <label class="setting-item">
                        <span>Model</span>
                        <select bind:value={selectedModel} on:change={saveModel} disabled={savingModel || !activeSessionStreaming}>
                            {#each availableModels as m}
                                <option value={m.value}>{m.label}</option>
                            {/each}
                        </select>
                    </label>
                    <label class="setting-item">
                        <span>Context nudge %</span>
                        <input type="number" min="10" max="95" step="5" bind:value={contextNudgePct} on:change={saveNudge} disabled={savingNudge}>
                    </label>
                </div>
            {/if}
            <div class="messages" bind:this={messagesContainer} on:scroll={handleMessagesScroll}>
                {#if loadingOlder}
                    <div class="loading-older">Loading older messages...</div>
                {/if}
                {#if hasMore && !loadingOlder}
                    <div class="loading-older"><button class="btn-load-more" on:click={loadOlderMessages}>Load older messages</button></div>
                {/if}
                {#each messages as msg}
                    {@const parsed = msg.role === 'user' ? parseBrokerMessage(msg.content) : null}
                    <div class="message {msg.role}">
                        {#if msg.role === 'user'}
                            {#if parsed?.meta}
                                <div class="broker-content">{@html escapeHtml(parsed.content)}</div>
                                <details class="broker-meta">
                                    <summary>{parsed.meta.sender} {parsed.meta.type === 'group' ? `in ${parsed.meta.groupName}` : ''} via {parsed.meta.platform}</summary>
                                    <div class="broker-meta-detail">
                                        <span>▾ {parsed.meta.type} via {parsed.meta.platform}</span>
                                        {#if parsed.meta.timestamp}<span>Time: {parsed.meta.timestamp}</span>{/if}
                                        <span>Chat: {parsed.meta.sender} ({parsed.meta.chatId})</span>
                                        {#if parsed.meta.groupName}<span>Group: {parsed.meta.groupName}</span>{/if}
                                        {#if parsed.meta.msgId}<span>Msg ID: {parsed.meta.msgId}</span>{/if}
                                    </div>
                                </details>
                            {:else}
                                {@html escapeHtml(msg.content)}
                            {/if}
                        {:else if msg.role === 'system' && msg.metadata?.checkpoint}
                            <div class="checkpoint-divider checkpoint-{msg.metadata.checkpoint}">
                                <span class="checkpoint-icon">{msg.metadata.checkpoint === 'context-restart' ? '↻' : msg.metadata.checkpoint === 'compact' ? '⊘' : msg.metadata.checkpoint === 'archive' ? '▣' : '●'}</span>
                                {msg.content}
                            </div>
                        {:else if msg.role === 'system'}
                            <div class="system-timeline-row">── {msg.content} ──</div>
                        {:else}
                            {#if msg.metadata?.thinking?.length}
                                <details class="thinking-meta">
                                    <summary class="thinking-summary">↳ thinking ({msg.metadata.thinking.length} block{msg.metadata.thinking.length > 1 ? 's' : ''})</summary>
                                    <div class="thinking-blocks">
                                        {#each msg.metadata.thinking as t}
                                            <div class="thinking-block">{t}</div>
                                        {/each}
                                    </div>
                                </details>
                            {/if}
                            {@html renderMarkdown(msg.content)}
                            {#if msg.metadata?.tool_uses?.length}
                                <details class="tool-meta">
                                    <summary>{msg.metadata.tool_uses.length} tool{msg.metadata.tool_uses.length > 1 ? 's' : ''} used</summary>
                                    <div class="tool-list">
                                        {#each msg.metadata.tool_uses as tu}
                                            <div class="tool-item" class:tool-error={tu.error}>
                                                <span class="tool-name">{tu.tool}</span>
                                                {#if tu.input && typeof tu.input === 'object'}
                                                    <span class="tool-input">{Object.entries(tu.input).map(([k,v]) => `${k}: ${String(v).slice(0,60)}`).join(', ')}</span>
                                                {/if}
                                            </div>
                                        {/each}
                                    </div>
                                </details>
                            {/if}
                            {#if msg.metadata?.cost_usd}
                                <div class="meta">${msg.metadata.cost_usd.toFixed(4)}</div>
                            {/if}
                            {#if msg.duration_ms}
                                <div class="meta">{(msg.duration_ms / 1000).toFixed(1)}s</div>
                            {/if}
                        {/if}
                    </div>
                {/each}
                {#if thinking || agentWorking}
                    <div class="thinking-bubble">
                        <div class="thinking-dots-row">
                            <span class="dot"></span>
                            <span class="dot"></span>
                            <span class="dot"></span>
                        </div>
                        {#if activityLog.length > 0}
                            <div class="thinking-log">
                                {#each activityLog as entry, i}
                                    <div class="thinking-log-entry" class:current={i === activityLog.length - 1}>
                                        {entry}
                                    </div>
                                {/each}
                            </div>
                        {:else if thinkingActivity}
                            <div class="thinking-log">
                                <div class="thinking-log-entry current">{thinkingActivity}</div>
                            </div>
                        {/if}
                    </div>
                {/if}
            </div>
            <div class="input-area">
                <input type="file" bind:this={fileInput} on:change={handleFileUpload} style="display:none">
                <button class="btn-upload" on:click={() => fileInput.click()} disabled={sending || !activeSessionStreaming} title={activeSessionStreaming ? 'Upload file' : 'File upload is available for streaming sessions only'}>📎</button>
                <input type="text" bind:value={messageInput} placeholder="Type a message..." on:keydown={handleKeydown} disabled={sending}>
                <button on:click={sendMessage} disabled={sending}>Send</button>
            </div>
        {/if}
    </div>
</div>

<style>
    .main { display: flex; flex: 1; overflow: hidden; height: 100vh; height: 100dvh; }

    /* Session sidebar (inside main content) */
    .sidebar { width: 260px; display: flex; flex-direction: column; background: var(--surface-1); }
    .sidebar.collapsed { display: none; }
    .sidebar-header { padding: 0.8rem 1rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; display: flex; justify-content: space-between; align-items: center; }
    .session-list { flex: 1; overflow-y: auto; padding: 0.3rem; }
    .agent-group { margin-bottom: 0.5rem; }
    .agent-group-header { padding: 0.3rem 0.6rem; font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 700; text-transform: uppercase; color: var(--text-secondary); cursor: pointer; display: flex; align-items: center; gap: 0.4rem; border-radius: var(--radius-lg); }
    .agent-group-header:hover { background: var(--surface-2); }
    .agent-name-text { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .agent-model { font-size: 0.58rem; color: var(--text-muted); font-weight: 400; font-family: var(--font-body); letter-spacing: 0.02em; flex-shrink: 0; }
    .btn-new { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; padding: 0.15rem 0.45rem; background: var(--primary-container); color: var(--on-primary-container); border: none; border-radius: var(--radius); cursor: pointer; text-transform: uppercase; flex-shrink: 0; }
    .btn-new:hover { background: var(--primary); color: #fff; }
    .session-item { padding: 0.2rem 0.6rem 0.2rem 1rem; cursor: pointer; font-family: var(--font-grotesk); font-size: 0.68rem; border-radius: var(--radius-lg); margin-bottom: 1px; transition: background 0.1s; display: flex; align-items: center; justify-content: space-between; gap: 0.4rem; }
    .session-item:hover { background: var(--surface-2); }
    .session-item.active { background: var(--primary-container); color: var(--on-primary-container); }
    .session-item.main-session { border-left: 2px solid var(--yellow); }
    .session-label { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .session-count { font-size: 0.6rem; color: var(--text-muted); flex-shrink: 0; }
    .session-item.active .session-count { color: var(--on-primary-container); opacity: 0.65; }

    /* Chat area */
    .chat-area { flex: 1; display: flex; flex-direction: column; background: var(--app-bg); }
    .chat-info { padding: 0.6rem 1.5rem; background: var(--surface-1); font-family: var(--font-grotesk); font-size: 0.72rem; color: var(--text-muted); display: flex; align-items: center; gap: 1.5rem; }
    .chat-info span { display: flex; align-items: center; gap: 0.3rem; }
    .info-context.warning { color: var(--danger-outline); font-weight: 700; }
    .chat-actions { display: flex; gap: 0.3rem; margin-left: auto; align-items: center; }
    .btn-action { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.25rem 0.6rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.1s; }
    .btn-action:hover { color: var(--text-primary); background: var(--surface-3); }
    .btn-action:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-action.active-action { color: var(--accent); background: var(--accent-soft); animation: pulse 1s infinite; }
    .btn-archive { color: var(--danger-outline); }
    .btn-archive:hover { background: var(--red); color: #fff; }
    .btn-restart { font-family: var(--font-grotesk); font-size: 0.6rem; font-weight: 700; padding: 0.25rem 0.6rem; background: var(--surface-2); color: var(--text-muted); border: none; border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; letter-spacing: 0.04em; transition: all 0.1s; }
    .btn-restart:hover { color: var(--primary-container); background: var(--surface-inverse); }
    .btn-restart:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-restart.restarting { color: var(--accent); background: var(--accent-soft); animation: pulse 1s infinite; }
    .settings-bar { padding: 0.5rem 1.5rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.7rem; display: flex; gap: 2rem; align-items: center; border-radius: 0 0 var(--radius-lg) var(--radius-lg); }
    .setting-item { display: flex; align-items: center; gap: 0.5rem; color: var(--text-secondary); }
    .setting-item select, .setting-item input { font-family: var(--font-body); font-size: 0.7rem; padding: 0.25rem 0.4rem; border: none; border-radius: var(--radius-lg); background: var(--input-bg); color: var(--text-primary); }
    .setting-item input[type="number"] { width: 4rem; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }

    /* Messages */
    .messages { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
    .loading-older { text-align: center; padding: 0.5rem; font-size: 0.8rem; color: var(--text-muted); font-family: var(--font-grotesk); }
    .btn-load-more { background: none; border: 1px solid var(--border); padding: 0.3rem 1rem; border-radius: var(--radius); cursor: pointer; font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); }
    .btn-load-more:hover { background: var(--surface-1); color: var(--text-primary); }
    .message { max-width: 75%; min-width: 0; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; overflow-wrap: break-word; word-break: break-word; border-radius: var(--radius-lg); }
    .message.user { align-self: flex-end; background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px rgba(0,0,0,0.1); }
    .message.assistant { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); }
    .message .meta { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); margin-top: 0.5rem; }
    .thinking-meta { margin-bottom: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .thinking-summary { color: var(--text-muted); cursor: pointer; user-select: none; letter-spacing: 0.03em; }
    .thinking-summary:hover { color: var(--text-secondary); }
    .thinking-blocks { margin-top: 0.4rem; display: flex; flex-direction: column; gap: 0.4rem; border-left: 2px solid var(--surface-3); padding-left: 0.6rem; }
    .thinking-block { font-family: var(--font-body); font-size: 0.75rem; color: var(--text-muted); line-height: 1.55; white-space: pre-wrap; }
    .tool-meta { margin-top: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .tool-meta summary { color: var(--text-muted); cursor: pointer; user-select: none; }
    .tool-meta summary:hover { color: var(--text-primary); }
    .tool-list { display: flex; flex-direction: column; gap: 0.2rem; margin-top: 0.3rem; }
    .tool-item { display: flex; gap: 0.4rem; align-items: baseline; color: var(--text-secondary); font-size: 0.6rem; }
    .tool-name { font-weight: 700; color: var(--tone-neutral-text); background: var(--tone-neutral-bg); padding: 0 0.3rem; border-radius: var(--radius); }
    .tool-input { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 400px; }
    .tool-error .tool-name { background: var(--tone-error-bg); color: var(--tone-error-text); }
    .broker-meta { margin-top: 0.4rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .broker-meta summary { color: var(--on-primary-container); opacity: 0.5; cursor: pointer; user-select: none; }
    .broker-meta summary:hover { opacity: 0.8; }
    .broker-meta-detail { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; color: var(--on-primary-container); opacity: 0.6; font-size: 0.6rem; }
    .message.system { align-self: center; font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); padding: 0.5rem; }
    .system-timeline-row { text-align: center; color: var(--text-muted); font-family: var(--font-grotesk); font-size: 0.7rem; letter-spacing: 0.04em; padding: 0.1rem 0; }
    .chat-working-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; display: inline-block; margin-right: 0.3rem; vertical-align: middle; }
    .chat-working-dot.working { background: var(--green); box-shadow: 0 0 5px rgba(74,222,128,0.5); animation: working-pulse 1.5s ease-in-out infinite; }
    @keyframes working-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
    .checkpoint-divider { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.4rem 1rem; font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-radius: var(--radius-lg); margin: 0.5rem 0; }
    .checkpoint-icon { font-size: 0.9rem; }
    .checkpoint-context-restart { color: var(--accent-contrast); background: var(--accent-soft); }
    .checkpoint-compact { color: var(--tone-info-text); background: var(--tone-info-bg); }
    .checkpoint-archive { color: var(--tone-error-text); background: var(--tone-error-bg); }
    .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; font-family: var(--font-grotesk); color: var(--text-muted); font-size: 0.9rem; }
    .thinking-bubble { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); border-radius: var(--radius-lg); padding: 0.85rem 1.2rem; display: flex; flex-direction: column; gap: 0.4rem; }
    .thinking-dots-row { display: flex; align-items: center; gap: 5px; height: 18px; }
    .thinking-dots-row .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--text-muted); opacity: 0.5; animation: dot-bounce 1.2s ease-in-out infinite; }
    .thinking-dots-row .dot:nth-child(2) { animation-delay: 0.2s; }
    .thinking-dots-row .dot:nth-child(3) { animation-delay: 0.4s; }
    @keyframes dot-bounce { 0%, 60%, 100% { transform: translateY(0); opacity: 0.4; } 30% { transform: translateY(-6px); opacity: 1; } }
    .thinking-activity { font-family: var(--font-grotesk); font-size: 0.68rem; color: var(--text-muted); max-width: 260px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .thinking-log { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.1rem; max-width: 300px; }
    .thinking-log-entry { font-family: var(--font-grotesk); font-size: 0.67rem; color: var(--text-subtle); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; line-height: 1.4; transition: color 0.2s; }
    .thinking-log-entry.current { color: var(--text-muted); font-weight: 600; }

    /* Markdown in messages */
    .message :global(code) { font-family: monospace; font-size: 0.82em; padding: 0.2em 0.5em; border-radius: var(--radius); word-break: break-word; }
    .message.assistant :global(code) { background: var(--code-inline-bg); color: var(--text-primary); }
    .message.user :global(code) { background: rgba(0,0,0,0.12); color: var(--on-primary-container); }
    .message :global(pre) { margin: 0.8rem 0; padding: 1.2rem 1.4rem; overflow-x: auto; font-family: monospace; font-size: 0.82rem; line-height: 1.6; position: relative; border-radius: var(--radius-lg); }
    .message.assistant :global(pre) { background: var(--code-pre-bg); color: var(--code-pre-text); border-left: 4px solid var(--accent); }
    .message.user :global(pre) { background: rgba(0,0,0,0.2); color: var(--on-primary-container); border-left: 4px solid rgba(0,0,0,0.2); }
    .message :global(pre code) { background: none !important; padding: 0 !important; color: inherit !important; font-size: inherit; }
    .message :global(pre .lang-label) { position: absolute; top: 0; right: 0; font-size: 0.65rem; padding: 0.2rem 0.6rem; background: var(--accent); color: var(--accent-contrast); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-radius: 0 var(--radius-lg) 0 var(--radius); }
    .message :global(strong) { font-weight: 700; }
    .message :global(em) { font-style: italic; }
    .message :global(ul), .message :global(ol) { margin: 0.5rem 0; padding-left: 1.5rem; }
    .message :global(li) { margin-bottom: 0.3rem; line-height: 1.5; }
    .message :global(p) { margin-bottom: 0.5rem; }
    .message :global(p:last-child) { margin-bottom: 0; }
    .message :global(blockquote) { border-left: 3px solid var(--accent); padding-left: 0.8rem; margin: 0.5rem 0; color: var(--text-secondary); font-style: italic; }
    .message :global(a) { color: var(--link-chip-text); background: var(--link-chip-bg); padding: 0 0.2em; border-radius: var(--radius); }
    .message :global(table) { border-collapse: collapse; margin: 0.8rem 0; font-size: 0.88rem; width: 100%; }
    .message :global(thead th) { font-family: var(--font-grotesk); font-weight: 700; text-align: left; padding: 0.5rem 0.8rem; background: var(--surface-2); font-size: 0.82rem; text-transform: uppercase; }
    .message :global(tbody td) { padding: 0.4rem 0.8rem; }
    .message :global(tbody tr:nth-child(even) td) { background: var(--surface-1); }
    .message :global(tbody tr:hover td) { background: var(--hover-accent); }

    /* Input area */
    .input-area { padding: 1rem 2rem; padding-bottom: calc(1rem + env(safe-area-inset-bottom, 0px)); background: var(--surface-1); display: flex; gap: 0.8rem; }
    .input-area input { flex: 1; font-family: var(--font-body); font-size: 1rem; padding: 0.8rem 1rem; border: none; border-radius: var(--radius-lg); outline: none; background: var(--input-bg); color: var(--text-primary); }
    .input-area input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; background: var(--input-focus-bg); }
    .input-area button { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 700; padding: 0.8rem 1.5rem; background: var(--primary-container); color: var(--on-primary-container); border: none; border-radius: var(--radius-lg); cursor: pointer; text-transform: uppercase; box-shadow: 4px 4px 0px var(--primary); transition: all 0.1s; }
    .input-area button:hover { box-shadow: 2px 2px 0px var(--primary); }
    .input-area button:active { box-shadow: none; transform: translate(2px, 2px) scale(0.98); }
    .input-area button:disabled { background: var(--surface-2); color: var(--text-muted); cursor: not-allowed; box-shadow: none; }
    .btn-upload { background: var(--surface-2); border: none; border-radius: var(--radius-lg); cursor: pointer; font-size: 1.1rem; padding: 0.5rem 0.7rem; display: flex; align-items: center; color: var(--text-primary); transition: all 0.1s; }
    .btn-upload:hover { background: var(--primary-container); color: var(--on-primary-container); }

    .sidebar-toggle { display: none; width: 100%; padding: 0.5rem; font-family: var(--font-grotesk); font-size: 0.7rem; text-align: center; background: var(--surface-2); border: none; cursor: pointer; text-transform: uppercase; color: var(--text-muted); }

    @media (max-width: 768px) {
        .main { flex-direction: column; height: 100dvh; overflow: hidden; }
        .sidebar { width: 100%; max-height: 40vh; overflow-y: auto; flex-shrink: 0; }
        .sidebar.collapsed { max-height: 0; overflow: hidden; display: flex; }
        .sidebar-toggle { display: block; flex-shrink: 0; }
        .chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
        .messages { flex: 1; overflow-y: auto; padding: 1rem; min-height: 0; }
        .input-area { flex-shrink: 0; padding: 0.8rem 1rem; padding-bottom: calc(0.8rem + env(safe-area-inset-bottom, 0px)); z-index: 10; }
        .input-area input { font-size: 16px; }
        .chat-info { padding: 0.5rem 1rem; gap: 0.8rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
        .chat-actions { gap: 0.3rem; flex-wrap: wrap; }
        .settings-bar { padding: 0.5rem 1rem; gap: 1rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
    }
</style>
