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
    let infoMessages = 0;
    let infoSession = '--';

    let sidebarCollapsed = false;
    let messagesContainer;
    let refreshInterval;
    let restarting = false;

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

        // Load session info if available
        try {
            const [session, context] = await Promise.all([
                api('GET', `/sessions/${activeSession}`),
                api('GET', `/sessions/${activeSession}/context`),
            ]);
            infoModel = session.model || 'default';
            infoContext = `${context.context_used_pct}%`;
        } catch {
            infoModel = 'streaming';
            infoContext = '--';
        }
        infoMessages = allMessages.length;
        infoSession = activeSession;

        // Also try streaming session context for more accurate info
        try {
            const streamStatus = await api('GET', `/agents/${agentName}/streaming/status`);
            if (streamStatus.connected) {
                const ctx = streamStatus.context || {};
                if (ctx.percentage) infoContext = `${ctx.percentage}%`;
                infoMessages = allMessages.length;
                infoSession = `${activeSession} + streaming`;
            }
        } catch {}

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
            const data = await api('POST', `/sessions/${activeSession}/message`, { content: text });
            thinking = false;
            messages = [...messages, { role: 'assistant', content: data.content, duration_ms: data.duration_ms }];
            await refreshSessions();
            const context = await api('GET', `/sessions/${activeSession}/context`);
            infoContext = `${context.context_used_pct}%`;
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

    async function contextRestart() {
        if (!activeSession || restarting) return;
        restarting = true;

        try {
            const savePrompt = 'Your session is about to be restarted. Save your current state now:\n\n' +
                '1. Use your save_my_context or set wake context tool to persist what you were working on\n' +
                '2. Include: current task, key context, any blockers, and what to do next\n' +
                '3. Confirm when saved\n\n' +
                'This is a context restart — your conversation will reset but your saved state will carry over.';

            messages = [...messages, { role: 'system', content: 'Context restart initiated — asking agent to save state...' }];
            await tick(); scrollToBottom();

            const saveResult = await api('POST', `/sessions/${activeSession}/message`, { content: savePrompt });
            messages = [...messages, { role: 'assistant', content: saveResult.content, duration_ms: saveResult.duration_ms }];
            await tick(); scrollToBottom();

            await api('POST', `/sessions/${activeSession}/restart`);
            messages = [...messages, { role: 'system', content: 'Session restarted. Sending wake prompt...' }];
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

    async function spawnAgentSession(agentName) {
        const result = await api('POST', `/agents/${agentName}/sessions`, {});
        await refreshSessions();
        selectSession(result.session.id, agentName);
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
                        {@const typeStyle = isMain ? 'background:#FFE600;color:#1E293B' : s.session_type === 'worker' ? 'background:#e2e8f0;color:#334155' : 'background:#dbeafe;color:#1e40af'}
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
                <span>Model: <strong>{infoModel}</strong></span>
                <span>Context: <strong>{infoContext}</strong></span>
                <span>Messages: <strong>{infoMessages}</strong></span>
                <span>Session: <strong>{infoSession}</strong></span>
                <button class="btn-restart" class:restarting on:click={contextRestart} disabled={restarting}>{restarting ? 'Restarting...' : 'Restart'}</button>
            </div>
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
                        {:else if msg.role === 'system'}
                            {msg.content}
                        {:else}
                            {@html renderMarkdown(msg.content)}
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
                <input type="text" bind:value={messageInput} placeholder="Type a message..." on:keydown={handleKeydown} disabled={sending}>
                <button on:click={sendMessage} disabled={sending}>Send</button>
            </div>
        {/if}
    </div>
</div>

<style>
    .main { display: flex; flex: 1; overflow: hidden; height: calc(100vh - 60px); height: calc(100dvh - 60px); }

    .sidebar { width: 280px; border-right: var(--border); display: flex; flex-direction: column; background: var(--gray-light); }
    .sidebar.collapsed { display: none; }
    .sidebar-header { padding: 1rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; display: flex; justify-content: space-between; align-items: center; }
    .session-list { flex: 1; overflow-y: auto; padding: 0.5rem; }
    .agent-group { margin-bottom: 0.5rem; }
    .agent-group-header { padding: 0.6rem 0.8rem; font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; color: var(--gray-dark); cursor: pointer; display: flex; justify-content: space-between; align-items: center; }
    .agent-group-header:hover { background: var(--white); }
    .agent-model { font-size: 0.6rem; color: var(--gray-mid); font-weight: 400; }
    .btn-new { font-family: var(--font-mono); font-size: 0.65rem; font-weight: 700; padding: 0.2rem 0.5rem; background: var(--black); color: var(--yellow); border: none; cursor: pointer; text-transform: uppercase; margin-left: auto; }
    .btn-new:hover { background: var(--yellow); color: var(--black); outline: 2px solid var(--black); }
    .session-item { padding: 0.5rem 0.8rem 0.5rem 1.4rem; cursor: pointer; font-family: var(--font-mono); font-size: 0.75rem; border: 2px solid transparent; margin-bottom: 1px; }
    .session-item:hover { background: var(--white); }
    .session-item.active { background: var(--yellow); border-color: var(--black); }
    .session-id { font-weight: 700; font-size: 0.7rem; }
    .session-meta { color: var(--gray-mid); font-size: 0.65rem; margin-top: 0.1rem; }
    .session-item.main-session { border-left: 3px solid var(--yellow); }

    .chat-area { flex: 1; display: flex; flex-direction: column; background: var(--white); }
    .chat-info { padding: 0.8rem 1.5rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.75rem; color: var(--gray-mid); display: flex; gap: 2rem; }
    .chat-info span { display: flex; align-items: center; gap: 0.3rem; }
    .btn-restart { font-family: var(--font-mono); font-size: 0.6rem; font-weight: 700; padding: 0.2rem 0.6rem; background: none; color: var(--gray-mid); border: 1px solid var(--gray-mid); cursor: pointer; text-transform: uppercase; letter-spacing: 0.05em; margin-left: auto; }
    .btn-restart:hover { color: var(--yellow); border-color: var(--yellow); background: var(--black); }
    .btn-restart:disabled { opacity: 0.4; cursor: not-allowed; }
    .btn-restart.restarting { color: var(--yellow); border-color: var(--yellow); animation: pulse 1s infinite; }
    @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.5; } }
    .messages { flex: 1; overflow-y: auto; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; }
    .message { max-width: 75%; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; }
    .message.user { align-self: flex-end; background: var(--black); color: var(--white); border: var(--border); }
    .message.assistant { align-self: flex-start; background: var(--gray-light); border: var(--border); }
    .message .meta { font-family: var(--font-mono); font-size: 0.65rem; color: var(--gray-mid); margin-top: 0.5rem; }
    .broker-meta { margin-top: 0.4rem; font-family: var(--font-mono); font-size: 0.65rem; }
    .broker-meta summary { color: rgba(255,255,255,0.4); cursor: pointer; user-select: none; }
    .broker-meta summary:hover { color: rgba(255,255,255,0.7); }
    .broker-meta-detail { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; color: rgba(255,255,255,0.5); font-size: 0.6rem; }
    .message.system { align-self: center; font-family: var(--font-mono); font-size: 0.75rem; color: var(--gray-mid); padding: 0.5rem; }
    .empty-state { flex: 1; display: flex; align-items: center; justify-content: center; font-family: var(--font-mono); color: var(--gray-mid); font-size: 0.9rem; }
    .thinking-dots { font-family: var(--font-mono); color: var(--gray-mid); }

    .message :global(code) { font-family: var(--font-mono); font-size: 0.82em; padding: 0.2em 0.5em; border-radius: 2px; word-break: break-word; }
    .message.assistant :global(code) { background: #e2e8f0; color: var(--black); }
    .message.user :global(code) { background: rgba(255,255,255,0.15); color: var(--white); }
    .message :global(pre) { margin: 0.8rem 0; padding: 1.2rem 1.4rem; overflow-x: auto; font-family: var(--font-mono); font-size: 0.82rem; line-height: 1.6; position: relative; }
    .message.assistant :global(pre) { background: #0f172a; color: #e2e8f0; border-left: 4px solid var(--yellow); }
    .message.user :global(pre) { background: rgba(0,0,0,0.4); color: #e2e8f0; border-left: 4px solid rgba(255,255,255,0.3); }
    .message :global(pre code) { background: none !important; padding: 0 !important; color: inherit !important; font-size: inherit; }
    .message :global(pre .lang-label) { position: absolute; top: 0; right: 0; font-size: 0.65rem; padding: 0.2rem 0.6rem; background: var(--yellow); color: var(--black); font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; }
    .message :global(strong) { font-weight: 700; }
    .message :global(em) { font-style: italic; }
    .message :global(ul), .message :global(ol) { margin: 0.5rem 0; padding-left: 1.5rem; }
    .message :global(li) { margin-bottom: 0.3rem; line-height: 1.5; }
    .message :global(p) { margin-bottom: 0.5rem; }
    .message :global(p:last-child) { margin-bottom: 0; }
    .message :global(blockquote) { border-left: 3px solid var(--yellow); padding-left: 0.8rem; margin: 0.5rem 0; color: var(--gray-dark); font-style: italic; }
    .message :global(a) { color: var(--yellow); background: var(--black); padding: 0 0.2em; }
    .message :global(table) { border-collapse: collapse; margin: 0.8rem 0; font-size: 0.88rem; width: 100%; }
    .message :global(thead th) { font-family: var(--font-mono); font-weight: 700; text-align: left; padding: 0.5rem 0.8rem; border-bottom: 3px solid var(--black); font-size: 0.82rem; text-transform: uppercase; }
    .message :global(tbody td) { padding: 0.4rem 0.8rem; border-bottom: 1px solid #e2e8f0; }
    .message :global(tbody tr:hover td) { background: #fefce8; }

    .input-area { padding: 1rem 2rem; padding-bottom: calc(1rem + env(safe-area-inset-bottom, 0px)); border-top: var(--border); display: flex; gap: 0.8rem; }
    .input-area input { flex: 1; font-family: var(--font-grotesk); font-size: 1rem; padding: 0.8rem 1rem; border: var(--border); outline: none; }
    .input-area input:focus { border-color: var(--yellow); background: #fffde6; }
    .input-area button { font-family: var(--font-mono); font-size: 0.85rem; font-weight: 700; padding: 0.8rem 1.5rem; background: var(--yellow); color: var(--black); border: var(--border); cursor: pointer; text-transform: uppercase; }
    .input-area button:hover { background: var(--black); color: var(--yellow); }
    .input-area button:disabled { background: var(--gray-light); color: var(--gray-mid); cursor: not-allowed; }

    .sidebar-toggle { display: none; width: 100%; padding: 0.5rem; font-family: var(--font-mono); font-size: 0.7rem; text-align: center; background: var(--gray-light); border: none; border-bottom: var(--border); cursor: pointer; text-transform: uppercase; color: var(--gray-mid); }

    @media (max-width: 768px) {
        .main { flex-direction: column; height: 100dvh; overflow: hidden; }
        .sidebar { width: 100%; border-right: none; border-bottom: var(--border); max-height: 40vh; overflow-y: auto; flex-shrink: 0; }
        .sidebar.collapsed { max-height: 0; overflow: hidden; border-bottom: none; display: flex; }
        .sidebar-toggle { display: block; flex-shrink: 0; }
        .chat-area { flex: 1; display: flex; flex-direction: column; min-height: 0; overflow: hidden; }
        .messages { flex: 1; overflow-y: auto; padding: 1rem; min-height: 0; }
        .input-area { flex-shrink: 0; padding: 0.8rem 1rem; padding-bottom: calc(0.8rem + env(safe-area-inset-bottom, 0px)); background: var(--white); z-index: 10; }
        .input-area input { font-size: 16px; }
        .chat-info { padding: 0.5rem 1rem; gap: 1rem; flex-wrap: wrap; font-size: 0.65rem; flex-shrink: 0; }
    }
</style>
