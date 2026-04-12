<script>
    import { createEventDispatcher } from 'svelte';
    import { escapeHtml, renderMarkdown, timeAgo } from '../lib/utils.js';
    import { parseBrokerMessage, formatMsgTime, deriveMessageKey } from '../lib/chatUtils.js';

    const dispatch = createEventDispatcher();

    export let msg;
    export let index = 0;

    $: parsed = msg.role === 'user' ? parseBrokerMessage(msg.content) : null;
    $: key = deriveMessageKey(msg, index);

    function copyMessage() {
        const text = msg.content || '';
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).catch(() => fallbackCopy(text));
        } else {
            fallbackCopy(text);
        }
    }

    function fallbackCopy(text) {
        const ta = document.createElement('textarea');
        ta.value = text;
        ta.style.position = 'fixed';
        ta.style.opacity = '0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
    }

    function replyTo() {
        const content = parsed?.content || msg.content || '';
        const msgId = parsed?.meta?.msgId || msg.id || msg.message_id || '';
        dispatch('reply', { id: key, role: msg.role, content: content.slice(0, 200), msgId });
    }

    function forward() {
        dispatch('forward', msg);
    }
</script>

<div class="message {msg.role}">
    {#if msg.role === 'user'}
        {#if parsed?.meta}
            <div class="broker-content">{@html escapeHtml(parsed.content)}</div>
            <details class="broker-meta">
                <summary>{parsed.meta.sender} {parsed.meta.type === 'group' ? `in ${parsed.meta.groupName}` : ''} via {parsed.meta.platform}</summary>
                <div class="broker-meta-detail">
                    <span>&#x25BE; {parsed.meta.type} via {parsed.meta.platform}</span>
                    {#if parsed.meta.timestamp}<span>Time: {parsed.meta.timestamp}</span>{/if}
                    <span>Chat: {parsed.meta.sender} ({parsed.meta.chatId})</span>
                    {#if parsed.meta.groupName}<span>Group: {parsed.meta.groupName}</span>{/if}
                    {#if parsed.meta.msgId}<span>Msg ID: {parsed.meta.msgId}</span>{/if}
                </div>
            </details>
        {:else}
            {@html escapeHtml(msg.content)}
        {/if}
    {:else if msg.role === 'system' && msg.metadata?.session_event}
        <div class="session-event-divider event-{msg.metadata.event_type}">
            {msg.content}
            {#if msg.timestamp}
                <span class="session-event-time">{timeAgo(msg.timestamp * 1000)}</span>
            {/if}
        </div>
    {:else if msg.role === 'system' && msg.metadata?.checkpoint}
        <div class="checkpoint-divider checkpoint-{msg.metadata.checkpoint}">
            <span class="checkpoint-icon">{msg.metadata.checkpoint === 'context-restart' ? '\u21BB' : msg.metadata.checkpoint === 'compact' ? '\u2298' : msg.metadata.checkpoint === 'archive' ? '\u25A3' : '\u25CF'}</span>
            {msg.content}
        </div>
    {:else if msg.role === 'system' && msg.metadata?.reaction}
        <div class="reaction-row">
            <span class="reaction-emoji">{msg.metadata.emoji}</span>
            <span class="reaction-label">reacted</span>
        </div>
    {:else if msg.role === 'system'}
        <div class="system-timeline-row">&mdash;&mdash; {msg.content} &mdash;&mdash;</div>
    {:else}
        {#if msg.metadata?.thinking?.length}
            <details class="thinking-meta">
                <summary class="thinking-summary">&darr; thinking ({msg.metadata.thinking.length} block{msg.metadata.thinking.length > 1 ? 's' : ''})</summary>
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
    {#if msg.role !== 'system'}
        <div class="msg-actions">
            {#if msg.timestamp}
                <span class="msg-time">{formatMsgTime(msg.timestamp)}</span>
            {/if}
            <button class="msg-action-btn" title="Copy" aria-label="Copy message" on:click|stopPropagation={copyMessage}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>
            </button>
            <button class="msg-action-btn" title="Reply" aria-label="Reply to message" on:click|stopPropagation={replyTo}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 17 4 12 9 7"/><path d="M20 18v-2a4 4 0 0 0-4-4H4"/></svg>
            </button>
            <button class="msg-action-btn" title="Forward to agent" aria-label="Forward message to another agent" on:click|stopPropagation={forward}>
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 17 20 12 15 7"/><path d="M4 18v-2a4 4 0 0 1 4-4h12"/></svg>
            </button>
        </div>
    {/if}
</div>

<style>
    .message { max-width: 75%; min-width: 0; padding: 1rem 1.2rem; line-height: 1.6; font-size: 0.95rem; overflow-wrap: break-word; word-break: break-word; border-radius: var(--radius-lg); position: relative; }
    .message.user { align-self: flex-end; background: var(--primary-container); color: var(--on-primary-container); box-shadow: 4px 4px 0px rgba(0,0,0,0.1); }
    .message.assistant { align-self: flex-start; background: var(--surface-1); box-shadow: 4px 4px 0px var(--shadow-color); }
    .message.system { align-self: center; font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); padding: 0.5rem; }
    .message .meta { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); margin-top: 0.5rem; }

    /* Broker metadata */
    .broker-meta { margin-top: 0.4rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .broker-meta summary { color: var(--on-primary-container); opacity: 0.5; cursor: pointer; user-select: none; }
    .broker-meta summary:hover { opacity: 0.8; }
    .broker-meta-detail { display: flex; flex-direction: column; gap: 0.15rem; margin-top: 0.3rem; color: var(--on-primary-container); opacity: 0.6; font-size: 0.6rem; }

    /* Thinking blocks */
    .thinking-meta { margin-bottom: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .thinking-summary { color: var(--text-muted); cursor: pointer; user-select: none; letter-spacing: 0.03em; }
    .thinking-summary:hover { color: var(--text-secondary); }
    .thinking-blocks { margin-top: 0.4rem; display: flex; flex-direction: column; gap: 0.4rem; border-left: 2px solid var(--surface-3); padding-left: 0.6rem; }
    .thinking-block { font-family: var(--font-body); font-size: 0.75rem; color: var(--text-muted); line-height: 1.55; white-space: pre-wrap; }

    /* Tool uses */
    .tool-meta { margin-top: 0.5rem; font-family: var(--font-grotesk); font-size: 0.65rem; }
    .tool-meta summary { color: var(--text-muted); cursor: pointer; user-select: none; }
    .tool-meta summary:hover { color: var(--text-primary); }
    .tool-list { display: flex; flex-direction: column; gap: 0.2rem; margin-top: 0.3rem; }
    .tool-item { display: flex; gap: 0.4rem; align-items: baseline; color: var(--text-secondary); font-size: 0.6rem; }
    .tool-name { font-weight: 700; color: var(--tone-neutral-text); background: var(--tone-neutral-bg); padding: 0 0.3rem; border-radius: var(--radius); }
    .tool-input { color: var(--text-muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 400px; }
    .tool-error .tool-name { background: var(--tone-error-bg); color: var(--tone-error-text); }

    /* Session events & checkpoints */
    .session-event-divider { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.3rem 1rem; font-family: var(--font-mono); font-size: 0.65rem; letter-spacing: 0.03em; border-radius: var(--radius-lg); margin: 0.4rem 0; color: var(--text-muted); background: var(--surface-0); border-left: 2px solid var(--border-subtle); }
    .session-event-divider.event-context_restart { color: var(--accent-contrast); border-left-color: var(--yellow); }
    .session-event-divider.event-session_resume, .session-event-divider.event-session_resumed { color: var(--green); border-left-color: var(--green); }
    .session-event-divider.event-session_start { color: var(--text-secondary); border-left-color: var(--text-muted); }
    .session-event-divider.event-session_end { color: var(--text-muted); border-left-color: var(--text-muted); opacity: 0.7; }
    .session-event-divider.event-idle_sleep { color: var(--text-muted); border-left-color: var(--text-muted); opacity: 0.7; }
    .session-event-divider.event-compact { color: var(--tone-info-text); border-left-color: var(--tone-info-text); }
    .session-event-divider.event-archive { color: var(--tone-error-text); border-left-color: var(--tone-error-text); }
    .session-event-divider.event-wake { color: var(--yellow); border-left-color: var(--yellow); }
    .session-event-divider.event-agent_started { color: var(--green); border-left-color: var(--green); }
    .session-event-divider.event-agent_stopped { color: var(--text-muted); border-left-color: var(--text-muted); opacity: 0.7; }
    .session-event-time { margin-left: auto; font-size: 0.6rem; opacity: 0.6; }
    .checkpoint-divider { display: flex; align-items: center; gap: 0.5rem; width: 100%; padding: 0.4rem 1rem; font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em; border-radius: var(--radius-lg); margin: 0.5rem 0; }
    .checkpoint-icon { font-size: 0.9rem; }
    .checkpoint-context-restart { color: var(--accent-contrast); background: var(--accent-soft); }
    .checkpoint-compact { color: var(--tone-info-text); background: var(--tone-info-bg); }
    .checkpoint-archive { color: var(--tone-error-text); background: var(--tone-error-bg); }

    /* Reactions & system */
    .reaction-row { display: flex; align-items: center; gap: 0.3rem; font-family: var(--font-grotesk); font-size: 0.75rem; }
    .reaction-emoji { font-size: 1rem; }
    .reaction-label { color: var(--text-muted); font-size: 0.65rem; text-transform: uppercase; letter-spacing: 0.04em; }
    .system-timeline-row { text-align: center; color: var(--text-muted); font-family: var(--font-grotesk); font-size: 0.7rem; letter-spacing: 0.04em; padding: 0.1rem 0; }

    /* Message actions */
    .msg-actions { display: flex; gap: 0.3rem; margin-top: 0.3rem; opacity: 0.5; transition: opacity 0.15s; align-items: center; }
    .msg-time { font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--text-muted); margin-right: 0.2rem; white-space: nowrap; }
    .message:hover .msg-actions { opacity: 1; }
    .msg-action-btn { background: var(--surface-2); border: none; border-radius: var(--radius); cursor: pointer; padding: 0.2rem 0.35rem; color: var(--text-muted); display: flex; align-items: center; transition: all 0.1s; }
    .msg-action-btn:hover { background: var(--primary-container); color: var(--on-primary-container); }

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
</style>
