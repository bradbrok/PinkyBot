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
    let messages = [];
    let messageInput = '';
    let sending = false;
    let thinking = false;
    let connected = true;

    let infoModel = '--';
    let infoContext = '0%';
    let infoContextPct = 0;
    let infoMessages = 0;
    let infoSession = '--';

    let sidebarCollapsed = false;
    let messagesContainer;
    let refreshInterval;
    let restarting = false;
    let compacting = false;
    let archiving = false;

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
        return { groups, orphans };
    }

    async function refreshSessions() {
        try {
            const [agentsData, sessData, convsData] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
                api('GET', '/conversations'),
            ]);
            agentsList = agentsData.agents || [];
            // Merge session manager sessions with conversation store entries
            const sessIds = new Set(sessData.map(s => s.id));
            const convSessions = (convsData.conversations || [])
                .filter(c => !sessIds.has(c.session_id))
                .map(c => ({
                    id: c.session_id,
                    state: 'streaming',
                    model: 'streaming',
                    message_count: c.message_count,
                    last_active: c.last_message_at,
                    agent_name: c.session_id.split('-')[0],
                    session_type: 'streaming',
                    _from_store: true,
                }));
            sessionsList = [...sessData, ...convSessions];
            connected = true;
        } catch {
            connected = false;
        }
    }

    async function selectSession(id, agentName) {
        activeSession = id;
        activeAgent = agentName || null;
        if (window.innerWidth <= 768) sidebarCollapsed = true;
        await refreshChat();
        startChatPolling();
    }

    let chatPollInterval;

    async function refreshChat() {
        if (!activeSession) return;
        const agentName = activeAgent || activeSession.split('-')[0];

        // Conversation store ID — streaming sessions now use {agent}-main
        const convId = `${agentName}-main`;

        // Primary source: conversation store (streaming sessions log here)
        let allMessages = [];
        try {
            const streamHistory = await api('GET', `/conversations/${convId}/history?limit=200`);
            allMessages = (streamHistory.messages || []).sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
        } catch {
            // Fall back to session manager history
            try {
                const history = await api('GET', `/sessions/${activeSession}/history`);
                allMessages = history.messages || [];
            } catch {}
        }

        infoMessages = allMessages.length;
        infoSession = activeSession;

        // Load agent config for model/nudge settings (only on first load)
        try {
            const agentData = await api('GET', `/agents/${agentName}`);
            if (agentData.model && !selectedModel) selectedModel = agentData.model;
            if (agentData.restart_threshold_pct != null) contextNudgePct = agentData.restart_threshold_pct;
            if (agentData.model) infoModel = agentData.model;
        } catch {}

        // Get context from streaming session (primary source)
        let gotStreamingContext = false;
        try {
            const streamStatus = await api('GET', `/agents/${agentName}/streaming/status`);
            if (streamStatus.connected) {
                const ctx = streamStatus.context || {};
                if (ctx.percentage != null) {
                    infoContext = `${ctx.percentage}%`;
                    infoContextPct = ctx.percentage;
                    gotStreamingContext = true;
                }
            }
        } catch {}

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
        messages = allMessages;
        await tick();
        if (allMessages.length > oldLen) scrollToBottom();
    }

    function startChatPolling() {
        stopChatPolling();
        chatPollInterval = setInterval(refreshChat, 3000);
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
            const agentName = activeAgent || activeSession.split('-')[0];
            // Try streaming chat endpoint first, fall back to session message
            try {
                await api('POST', `/agents/${agentName}/chat`, { content: text });
                // Response comes async via streaming — poll for it
                thinking = true;
                let attempts = 0;
                while (attempts < 30) {
                    await new Promise(r => setTimeout(r, 1000));
                    const convId = `${agentName}-main`;
                    const hist = await api('GET', `/conversations/${convId}/history?limit=5`);
                    const lastMsg = (hist.messages || []).slice(-1)[0];
                    if (lastMsg && lastMsg.role === 'assistant' && lastMsg.timestamp > (Date.now() / 1000 - 5)) {
                        messages = [...messages, { role: 'assistant', content: lastMsg.content }];
                        break;
                    }
                    attempts++;
                }
                thinking = false;
            } catch {
                // Fall back to old session message endpoint
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
        if (!fileInput.files[0] || !activeAgent) return;
        const file = fileInput.files[0];
        const formData = new FormData();
        formData.append('file', file);

        sending = true;
        messages = [...messages, { role: 'user', content: `📎 Uploading: ${file.name} (${(file.size / 1024).toFixed(1)} KB)` }];
        await tick();
        scrollToBottom();

        try {
            const resp = await fetch(`/agents/${activeAgent}/upload`, {
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
            const savePrompt = 'Your session is about to be restarted. Save your current state now:\n\n' +
                '1. Use your save_my_context or set wake context tool to persist what you were working on\n' +
                '2. Include: current task, key context, any blockers, and what to do next\n' +
                '3. Confirm when saved\n\n' +
                'This is a context restart — your conversation will reset but your saved state will carry over.';

            messages = [...messages, { role: 'system', content: 'Context restart initiated — asking agent to save state...', metadata: { checkpoint: 'context-restart' } }];
            await tick(); scrollToBottom();

            const saveResult = await api('POST', `/sessions/${activeSession}/message`, { content: savePrompt });
            messages = [...messages, { role: 'assistant', content: saveResult.content, duration_ms: saveResult.duration_ms }];
            await tick(); scrollToBottom();

            await api('POST', `/sessions/${activeSession}/restart`);
            await logCheckpoint('context-restart', 'Context restarted via UI');
            messages = [...messages, { role: 'system', content: 'Session restarted. Sending wake prompt...', metadata: { checkpoint: 'context-restart' } }];
            await tick(); scrollToBottom();

            const wakePrompt = 'Session was restarted via context restart (UI). Check your wake context or saved context for continuation state. Pick up where you left off.';
            const wakeResult = await api('POST', `/sessions/${activeSession}/message`, { content: wakePrompt });
            messages = [...messages, { role: 'assistant', content: wakeResult.content, duration_ms: wakeResult.duration_ms }];

            await refreshChat();
            await refreshSessions();
        } catch (e) {
            messages = [...messages, { role: 'system', content: `Restart failed: ${e.message}` }];
        }

        restarting = false;
        await tick(); scrollToBottom();
    }

    async function logCheckpoint(type, detail) {
        const agentName = activeAgent || activeSession?.split('-')[0];
        const convId = agentName ? `${agentName}-main` : activeSession;
        if (!convId) return;
        try {
            await api('POST', `/conversations/${convId}/checkpoint`, { type, detail });
        } catch { /* best effort */ }
    }

    async function compactContext() {
        if (!activeAgent || compacting) return;
        compacting = true;
        messages = [...messages, { role: 'system', content: 'Compacting context...', metadata: { checkpoint: 'compact' } }];
        await tick(); scrollToBottom();
        try {
            await api('POST', `/agents/${activeAgent}/streaming/compact`);
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
        if (!activeAgent || archiving) return;
        if (!confirm('Archive this session? The agent will save its memory, then get a fresh context.')) return;
        archiving = true;
        messages = [...messages, { role: 'system', content: 'Archiving — asking agent to save memory...', metadata: { checkpoint: 'archive' } }];
        await tick(); scrollToBottom();
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/archive`);
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
        if (!activeAgent || !selectedModel) return;
        savingModel = true;
        try {
            const result = await api('POST', `/agents/${activeAgent}/streaming/model`, { model: selectedModel });
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
        await api('POST', `/agents/${agentName}/streaming-sessions?label=chat`);
        await refreshSessions();
        selectSession(`${agentName}-chat`, agentName);
    }

    onMount(() => {
        refreshSessions();
        refreshInterval = setInterval(refreshSessions, 10000);
    });

    onDestroy(() => {
        clearInterval(refreshInterval);
        stopChatPolling();
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
                    <div class="agent-group-header" on:click={() => { if (aSessions.length > 0) selectSession(aSessions[0].id, agent.name); }}>
                        <span>{agent.display_name || agent.name}</span>
                        <span class="agent-model">{agent.model} | {aSessions.length} sess</span>
                        <button class="btn-new" on:click|stopPropagation={() => spawnAgentSession(agent.name)}>+</button>
                    </div>
                    {#each aSessions as s}
                        {@const isMain = (s.session_type || '') === 'main'}
                        {@const typeStyle = isMain ? 'background:var(--accent);color:var(--accent-contrast)' : s.session_type === 'worker' ? 'background:var(--tone-neutral-bg);color:var(--tone-neutral-text)' : 'background:var(--tone-info-bg);color:var(--tone-info-text)'}
                        <div
                            class="session-item"
                            class:active={activeSession === s.id}
                            class:main-session={isMain}
                            on:click={() => selectSession(s.id, agent.name)}
                        >
                            <div class="session-id">
                                {#if s.session_type}
                                    <span style="font-family:var(--font-mono);font-size:0.55rem;font-weight:700;text-transform:uppercase;padding:0.1rem 0.3rem;{typeStyle}">{s.session_type}</span>
                                {/if}
                                {s.id}
                            </div>
                            <div class="session-meta">{s.message_count} msgs | {s.context_used_pct}% ctx</div>
                        </div>
                    {/each}
                </div>
            {/each}
            {#if agentSessions.orphans.length > 0}
                <div class="agent-group">
                    <div class="agent-group-header"><span style="color:var(--gray-mid)">Standalone</span></div>
                    {#each agentSessions.orphans as s}
                        <div class="session-item" class:active={activeSession === s.id} on:click={() => selectSession(s.id, null)}>
                            <div class="session-id">{s.id}</div>
                            <div class="session-meta">{s.model || 'default'} | {s.message_count} msgs</div>
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
                    <button class="btn-action" on:click={() => showSettings = !showSettings}>Model</button>
                    <button class="btn-action" class:active-action={compacting} on:click={compactContext} disabled={compacting}>{compacting ? 'Compacting...' : 'Compact'}</button>
                    <button class="btn-restart" class:restarting on:click={contextRestart} disabled={restarting}>{restarting ? 'Restarting...' : 'Context Restart'}</button>
                    <button class="btn-action btn-archive" class:active-action={archiving} on:click={archiveSession} disabled={archiving}>{archiving ? 'Archiving...' : 'Archive'}</button>
                </div>
            </div>
            {#if showSettings}
                <div class="settings-bar">
                    <label class="setting-item">
                        <span>Model</span>
                        <select bind:value={selectedModel} on:change={saveModel} disabled={savingModel}>
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
            <div class="messages" bind:this={messagesContainer}>
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
                            {msg.content}
                        {:else}
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
                {#if thinking}
                    <div class="message system"><span class="thinking-dots">thinking...</span></div>
                {/if}
            </div>
            <div class="input-area">
                <input type="file" bind:this={fileInput} on:change={handleFileUpload} style="display:none">
                <button class="btn-upload" on:click={() => fileInput.click()} disabled={sending} title="Upload file">📎</button>
                <input type="text" bind:value={messageInput} placeholder="Type a message..." on:keydown={handleKeydown} disabled={sending}>
                <button on:click={sendMessage} disabled={sending}>Send</button>
            </div>
        {/if}
    </div>
</div>

<style>
    .main { display: flex; flex: 1; overflow: hidden; height: calc(100vh - 60px); height: calc(100dvh - 60px); }

    .sidebar { width: 280px; border-right: var(--border); display: flex; flex-direction: column; background: var(--surface-2); }
    .sidebar.collapsed { display: none; }
    .sidebar-header { padding: 1rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; display: flex; justify-content: space-between; align-items: center; }
    .session-list { flex: 1; overflow-y: auto; padding: 0.5rem; }
    .agent-group { margin-bottom: 0.5rem; }
    .agent-group-header { padding: 0.6rem 0.8rem; font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; color: var(--text-secondary); cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
    .agent-group-header:hover { background: var(--surface-1); }
    .agent-model { font-size: 0.6rem; color: var(--text-muted); font-weight: 400; }
    .btn-new { font-family: var(--font-mono); font-size: 0.65rem; font-weight: 700; padding: 0.2rem 0.5rem; background: var(--accent); color: var(--accent-contrast); border: none; cursor: pointer; text-transform: uppercase; margin-left: auto; }
    .btn-new:hover { background: var(--surface-inverse); color: var(--accent); outline: 2px solid var(--border-strong); }
    .session-item { padding: 0.5rem 0.8rem 0.5rem 1.4rem; cursor: pointer; font-family: var(--font-mono); font-size: 0.75rem; border: 2px solid transparent; margin-bottom: 1px; }
    .session-item:hover { background: var(--surface-1); }
    .session-item.active { background: var(--selected-bg); border-color: var(--border-strong); color: var(--selected-text); }
    .session-id { font-weight: 700; font-size: 0.7rem; }
    .session-meta { color: var(--gray-mid); font-size: 0.65rem; margin-top: 0.1rem; }
    .session-item.main-session { border-left: 3px solid var(--yellow); }

    .chat-area { flex: 1; display: flex; flex-direction: column; background: var(--surface-1); }
    .chat-info { padding: 0.8rem 1.5rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); display: flex; align-items: center; gap: 2rem; background: var(--surface-1); }
    .chat-info span { display: flex; align-items: center; gap: 0.3rem; }
    .info-context.warning { color: var(--danger-outline); font-weight: 700; }
    .chat-actions { display: flex; gap: 0.4rem; margin-left: auto; align-items: center; }
    .btn-action { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.2rem 0.6rem; background: none; color: var(--text-muted); border: 1px solid var(--border-color); cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em; }
    .btn-action:hover { color: var(--text-primary); border-color: var(--border-strong); background: var(--hover-soft); }
    .btn-action:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-action.active-action { color: var(--accent); border-color: var(--accent); animation: pulse 1s infinite; }
    .btn-archive { color: var(--danger-outline); border-color: var(--danger-outline); }
    .btn-archive:hover { background: var(--red); color: var(--text-inverse); border-color: var(--red); }
    .btn-restart { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.2rem 0.6rem; background: none; color: var(--text-muted); border: 1px solid var(--border-color); cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em; }
    .btn-restart:hover { color: var(--accent); border-color: var(--accent); background: var(--surface-inverse); }
    .btn-restart:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-restart.restarting { color: var(--accent); border-color: var(--accent); animation: pulse 1s infinite; }
    .settings-bar { padding: 0.6rem 1.5rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.7rem; display: flex; gap: 2rem; align-items: center; background: var(--surface-2); }
    .setting-item { display: flex; align-items: center; gap: 0.5rem; color: var(--text-secondary); }
    .setting-item select, .setting-item input { font-family: var(--font-mono); font-size: 0.7rem; padding: 0.2rem 0.4rem; border: 1px solid var(--border-color); background: var(--input-bg); color: var(--text-primary); }
    .setting-item input[type="number"] { width: 4rem; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
    .messages { flex: 1; overflow-y: auto; overflow-x: hidden; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; min-width: 0; }
    .message { max-width: 75%; min-width: 0; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; overflow-wrap: break-word; word-break: break-word; }
    .message.user { align-self: flex-end; background: var(--surface-inverse); color: var(--text-inverse); border: var(--border); }
    .message.assistant { align-self: flex-start; background: var(--surface-2); border: var(--border); }
    .message .meta { font-family: var(--font-mono); font-size: 0.65rem; color: var(--text-muted); margin-top: 0.5rem; }
    .tool-meta { margin-top: 0.5rem; font-family: var(--font-mono); font-size: 0.65rem; }
    .tool-meta summary { color: var(--text-muted); cursor: pointer; user-select: none; }
    .tool-meta summary:hover { color: var(--text-primary); }
    .tool-list { display: flex; flex-direction: column; gap: 0.2rem; margin-top: 0.3rem; }
    .tool-item { display: flex; gap: 0.4rem; align-items: baseline; color: var(--text-secondary); font-size: 0.6rem; }
    .tool-name { font-weight: 700; color: var(--tone-neutral-text); background: var(--tone-neutral-bg); padding: 0 0.3rem; }
    .tool-input { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 400px; }
    .tool-error .tool-name { background: var(--tone-error-bg); color: var(--tone-error-text); }
    .broker-meta { margin-top: 0.4rem; font-family: var(--font-mono); font-size: 0.65rem; }
    .broker-meta summary { color: rgba(255,255,255,0.4); cursor: pointer; user-select: none; }
    .broker-meta summary:hover { color: rgba(255,255,255,0.7); }
    .broker-meta-detail { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; color: rgba(255,255,255,0.5); font-size: 0.6rem; }
    .message.system { align-self: center; font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-muted); padding: 0.5rem; }
    .checkpoint-divider { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.3rem 1rem; font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-top: 2px dashed; border-bottom: 2px dashed; margin: 0.5rem 0; }
    .checkpoint-icon { font-size: 0.9rem; }
    .checkpoint-context-restart { color: var(--accent); border-color: var(--accent); background: var(--accent-soft); }
    .checkpoint-compact { color: var(--tone-info-text); border-color: var(--tone-info-text); background: var(--tone-info-bg); }
    .checkpoint-archive { color: var(--tone-error-text); border-color: var(--tone-error-text); background: var(--tone-error-bg); }
    .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; font-family: var(--font-mono); color: var(--text-muted); font-size: 0.9rem; }
    .thinking-dots { font-family: var(--font-mono); color: var(--text-muted); }

    .message :global(code) { font-family: var(--font-mono); font-size: 0.82em; padding: 0.2em 0.5em; border-radius: 2px; word-break: break-word; }
    .message.assistant :global(code) { background: var(--code-inline-bg); color: var(--text-primary); }
    .message.user :global(code) { background: rgba(255,255,255,0.15); color: var(--text-inverse); }
    .message :global(pre) { margin: 0.8rem 0; padding: 1.2rem 1.4rem; overflow-x: auto; font-family: var(--font-mono); font-size: 0.82rem; line-height: 1.6; position: relative; }
    .message.assistant :global(pre) { background: var(--code-pre-bg); color: var(--code-pre-text); border-left: 4px solid var(--accent); }
    .message.user :global(pre) { background: rgba(0,0,0,0.35); color: var(--code-pre-text); border-left: 4px solid rgba(255,255,255,0.3); }
    .message :global(pre code) { background: none !important; padding: 0 !important; color: inherit !important; font-size: inherit; }
    .message :global(pre .lang-label) { position: absolute; top: 0; right: 0; font-size: 0.65rem; padding: 0.2rem 0.6rem; background: var(--accent); color: var(--accent-contrast); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
    .message :global(strong) { font-weight: 700; }
    .message :global(em) { font-style: italic; }
    .message :global(ul), .message :global(ol) { margin: 0.5rem 0; padding-left: 1.5rem; }
    .message :global(li) { margin-bottom: 0.3rem; line-height: 1.5; }
    .message :global(p) { margin-bottom: 0.5rem; }
    .message :global(p:last-child) { margin-bottom: 0; }
    .message :global(blockquote) { border-left: 3px solid var(--accent); padding-left: 0.8rem; margin: 0.5rem 0; color: var(--text-secondary); font-style: italic; }
    .message :global(a) { color: var(--link-chip-text); background: var(--link-chip-bg); padding: 0 0.2em; }
    .message :global(table) { border-collapse: collapse; margin: 0.8rem 0; font-size: 0.88rem; width: 100%; }
    .message :global(thead th) { font-family: var(--font-mono); font-weight: 700; text-align: left; padding: 0.5rem 0.8rem; border-bottom: 3px solid var(--border-strong); font-size: 0.82rem; text-transform: uppercase; }
    .message :global(tbody td) { padding: 0.4rem 0.8rem; border-bottom: 1px solid var(--row-divider); }
    .message :global(tbody tr:hover td) { background: var(--hover-accent); }

    .input-area { padding: 1rem 2rem; padding-bottom: calc(1rem + env(safe-area-inset-bottom, 0px)); border-top: var(--border); display: flex; gap: 0.8rem; background: var(--surface-1); }
    .input-area input { flex: 1; font-family: var(--font-grotesk); font-size: 1rem; padding: 0.8rem 1rem; border: var(--border); outline: none; background: var(--input-bg); color: var(--text-primary); }
    .input-area input:focus { border-color: var(--accent); background: var(--input-focus-bg); }
    .input-area button { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; padding: 0.8rem 1.5rem; background: var(--accent); color: var(--accent-contrast); border: var(--border); cursor: pointer; text-transform: uppercase; }
    .input-area button:hover { background: var(--surface-inverse); color: var(--accent); }
    .input-area button:disabled { background: var(--surface-2); color: var(--text-muted); cursor: not-allowed; }
    .btn-upload { background: none; border: 2px solid var(--border-color); cursor: pointer; font-size: 1.1rem; padding: 0.5rem 0.7rem; display: flex; align-items: center; color: var(--text-primary); }
    .btn-upload:hover { border-color: var(--border-strong); background: var(--hover-soft); }

    .sidebar-toggle { display: none; width: 100%; padding: 0.5rem; font-family: var(--font-mono); font-size: 0.7rem; text-align: center; background: var(--surface-2); border: none; border-bottom: var(--border); cursor: pointer; text-transform: uppercase; color: var(--text-muted); }

    @media (max-width: 768px) {
        .main { flex-direction: column; height: 100dvh; overflow: hidden; }
        .sidebar { width: 100%; border-right: none; border-bottom: var(--border); max-height: 40vh; overflow-y: auto; flex-shrink: 0; }
        .sidebar.collapsed { max-height: 0; overflow: hidden; border-bottom: none; display: flex; }
        .sidebar-toggle { display: block; flex-shrink: 0; }
        .chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
        .messages { flex: 1; overflow-y: auto; padding: 1rem; min-height: 0; }
        .input-area { flex-shrink: 0; padding: 0.8rem 1rem; padding-bottom: calc(0.8rem + env(safe-area-inset-bottom, 0px)); background: var(--surface-1); z-index: 10; }
        .input-area input { font-size: 16px; }
        .chat-info { padding: 0.5rem 1rem; gap: 0.8rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
        .chat-actions { gap: 0.3rem; flex-wrap: wrap; }
        .settings-bar { padding: 0.5rem 1rem; gap: 1rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
    }
</style>
