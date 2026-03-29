<script>
    import { onMount, onDestroy, tick } from 'svelte';
    import { api } from '../lib/api.js';
    import { escapeHtml, renderMarkdown } from '../lib/utils.js';

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
            const [agentsData, sessData] = await Promise.all([
                api('GET', '/agents'),
                api('GET', '/sessions'),
            ]);
            agentsList = agentsData.agents || [];
            sessionsList = sessData;
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
    }

    async function refreshChat() {
        if (!activeSession) return;
        const [session, history, context] = await Promise.all([
            api('GET', `/sessions/${activeSession}`),
            api('GET', `/sessions/${activeSession}/history`),
            api('GET', `/sessions/${activeSession}/context`),
        ]);
        infoModel = session.model || 'default';
        infoContext = `${context.context_used_pct}%`;
        infoMessages = session.message_count;
        infoSession = session.id;
        messages = history.messages || [];
        await tick();
        scrollToBottom();
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
            </div>
            <div class="messages" bind:this={messagesContainer}>
                {#each messages as msg}
                    <div class="message {msg.role}">
                        {#if msg.role === 'user'}
                            {@html escapeHtml(msg.content)}
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
    .messages { flex: 1; overflow-y: auto; padding: 1.5rem 2rem; display: flex; flex-direction: column; gap: 1rem; }
    .message { max-width: 75%; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; }
    .message.user { align-self: flex-end; background: var(--black); color: var(--white); border: var(--border); }
    .message.assistant { align-self: flex-start; background: var(--gray-light); border: var(--border); }
    .message .meta { font-family: var(--font-mono); font-size: 0.65rem; color: var(--gray-mid); margin-top: 0.5rem; }
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
