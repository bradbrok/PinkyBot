<script>
    import { _ } from 'svelte-i18n';
    import { createEventDispatcher } from 'svelte';

    const dispatch = createEventDispatcher();

    export let agentsList = [];
    export let agentSessions = { groups: {}, orphans: [] };
    export let activeSession = null;
    export let renamingSession = null;
    export let renameValue = '';
    export let chatSearchQuery = '';

    function selectSession(id, agentName) {
        dispatch('selectSession', { id, agentName });
    }

    function spawnSession(agentName) {
        dispatch('spawnSession', { agentName });
    }

    function startRename(agentName, label) {
        dispatch('startRename', { agentName, label });
    }

    function finishRename() {
        dispatch('finishRename');
    }

    function cancelRename() {
        dispatch('cancelRename');
    }

    function searchChats() {
        dispatch('searchChats');
    }
</script>

<div class="sidebar-header">{$_('chat.agents')}</div>
<div class="sidebar-search">
    <input
        type="text"
        class="sidebar-search-input"
        placeholder="Search chats..."
        bind:value={chatSearchQuery}
        on:keydown={(e) => e.key === 'Enter' && searchChats()}
    >
</div>
<div class="session-list">
    {#each agentsList as agent}
        {@const aSessions = agentSessions.groups[agent.name] || []}
        <div class="agent-group">
            <div class="agent-group-header" on:click={() => { if (aSessions.length > 0) selectSession(aSessions[0].id, agent.name); }}>
                <span class="chat-working-dot" class:working={agent.working_status === 'working'} class:offline={agent.working_status === 'offline'} title={agent.working_status || 'idle'}></span>
                <span class="agent-name-text">{agent.display_name || agent.name}</span>
                <span class="agent-status-tag" class:status-working={agent.working_status === 'working'} class:status-offline={agent.working_status === 'offline'}>{agent.working_status === 'working' ? 'working' : agent.working_status === 'offline' ? 'offline' : 'idle'}</span>
                <span class="agent-model">{(agent.model || '').replace(/^claude-/i, '')}</span>
                <button class="btn-new" on:click|stopPropagation={() => spawnSession(agent.name)}>+</button>
            </div>
            {#each aSessions as s}
                {@const isMain = (s.session_type || '') === 'main'}
                {@const label = s.id.replace(new RegExp(`^${agent.name}-`), '').replace(/-?main$/, '') || 'main'}
                {@const isRenaming = renamingSession && renamingSession.agentName === agent.name && renamingSession.label === label}
                <div
                    class="session-item"
                    class:active={activeSession === s.id}
                    class:main-session={isMain}
                    on:click={() => selectSession(s.id, agent.name)}
                >
                    {#if isRenaming}
                        <input
                            class="rename-input"
                            bind:value={renameValue}
                            on:blur={finishRename}
                            on:keydown={e => { if (e.key === 'Enter') finishRename(); if (e.key === 'Escape') cancelRename(); }}
                            on:click|stopPropagation
                            autofocus
                        />
                    {:else}
                        <span class="session-label" on:dblclick|stopPropagation={() => startRename(agent.name, label)}>{label}</span>
                    {/if}
                    <span class="session-count">{s.message_count}</span>
                </div>
            {/each}
        </div>
    {/each}
    {#if agentSessions.orphans.length > 0}
        <div class="agent-group">
            <div class="agent-group-header"><span style="color:var(--gray-mid)">{$_('chat.standalone')}</span></div>
            {#each agentSessions.orphans as s}
                <div class="session-item" class:active={activeSession === s.id} on:click={() => selectSession(s.id, null)}>
                    <span class="session-label">{s.id}</span>
                    <span class="session-count">{s.message_count}</span>
                </div>
            {/each}
        </div>
    {/if}
</div>

<style>
    .sidebar-header { padding: 0.8rem 1rem; background: var(--surface-2); font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.08em; display: flex; justify-content: space-between; align-items: center; }
    .sidebar-search { padding: 0.3rem 0.5rem; }
    .sidebar-search-input { width: 100%; font-family: var(--font-body); font-size: 0.75rem; padding: 0.35rem 0.5rem; border: none; border-radius: var(--radius-lg); background: var(--surface-2); color: var(--text-primary); }
    .sidebar-search-input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; background: var(--input-focus-bg); }
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
    .rename-input { font-family: var(--font-grotesk); font-size: 0.68rem; background: var(--surface-2); border: 1px solid var(--yellow); border-radius: var(--radius); padding: 0.1rem 0.3rem; outline: none; color: var(--text); width: 100%; }
    .session-label { font-weight: 600; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1; }
    .session-count { font-size: 0.6rem; color: var(--text-muted); flex-shrink: 0; }
    .session-item.active .session-count { color: var(--on-primary-container); opacity: 0.65; }
    .chat-working-dot { width: 5px; height: 5px; border-radius: 50%; background: var(--green); flex-shrink: 0; display: inline-block; margin-right: 0.3rem; vertical-align: middle; }
    .chat-working-dot.working { background: var(--green); box-shadow: 0 0 5px rgba(74,222,128,0.5); animation: working-pulse 1.5s ease-in-out infinite; }
    .chat-working-dot.offline { background: var(--text-muted); opacity: 0.5; }
    .agent-status-tag { font-size: 0.5rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.04em; color: var(--green); flex-shrink: 0; }
    .agent-status-tag.status-working { color: var(--green); }
    .agent-status-tag.status-offline { color: var(--text-muted); opacity: 0.5; }
    @keyframes working-pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.35; } }
</style>
