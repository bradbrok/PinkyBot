<script>
    import { _ } from 'svelte-i18n';
    import { createEventDispatcher } from 'svelte';

    const dispatch = createEventDispatcher();

    export let messageInput = '';
    export let sending = false;
    export let canSendMessage = false;
    export let activeSession = null;
    export let activeAgent = null;
    export let replyTo = null;

    let fileInput;

    function handleKeydown(e) {
        if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            dispatch('send');
        }
    }

    function handleFileUpload() {
        if (!fileInput?.files?.[0] || !activeAgent) return;
        dispatch('upload', fileInput.files[0]);
        fileInput.value = '';
    }
</script>

{#if replyTo}
    <div class="reply-bar">
        <span class="reply-bar-label">&larr; replying to {replyTo.role}</span>
        <span class="reply-bar-content">{replyTo.content.slice(0, 100)}{replyTo.content.length > 100 ? '\u2026' : ''}</span>
        <button class="reply-bar-close" on:click={() => dispatch('clearReply')}>&times;</button>
    </div>
{/if}
<div class="input-area">
    <input type="file" bind:this={fileInput} on:change={handleFileUpload} style="display:none">
    <button class="btn-upload" on:click={() => fileInput.click()} disabled={sending || !activeAgent} title={$_('chat.upload_file')}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
    </button>
    <input type="text" bind:value={messageInput} placeholder={!activeSession ? $_('chat.select_agent') : canSendMessage ? $_('chat.type_message') : $_('chat.main_session_only')} on:keydown={handleKeydown} disabled={sending || !canSendMessage}>
    <button class="btn-send" on:click={() => dispatch('send')} disabled={sending || !canSendMessage}>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
    </button>
</div>

<style>
    .reply-bar { display: flex; align-items: center; gap: 0.5rem; padding: 0.4rem 2rem; background: var(--surface-2); border-left: 3px solid var(--accent); font-family: var(--font-grotesk); font-size: 0.7rem; }
    .reply-bar-label { font-weight: 700; text-transform: uppercase; font-size: 0.6rem; color: var(--accent); flex-shrink: 0; }
    .reply-bar-content { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text-muted); }
    .reply-bar-close { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 0.8rem; padding: 0.1rem 0.3rem; flex-shrink: 0; }
    .reply-bar-close:hover { color: var(--text-primary); }
    .input-area { padding: 0.6rem 1rem; padding-bottom: calc(0.6rem + env(safe-area-inset-bottom, 0px)); background: var(--surface-1); display: flex; gap: 0.5rem; align-items: center; }
    .input-area input { flex: 1; font-family: var(--font-body); font-size: 1rem; padding: 0.7rem 1rem; border: none; border-radius: var(--radius-lg); outline: none; background: var(--input-bg); color: var(--text-primary); }
    .input-area input:focus { outline: 2px solid var(--primary-container); outline-offset: -2px; background: var(--input-focus-bg); }
    .btn-upload { background: none; border: none; cursor: pointer; padding: 0.5rem; display: flex; align-items: center; justify-content: center; color: var(--text-muted); transition: color 0.15s; border-radius: var(--radius-md); }
    .btn-upload:hover { color: var(--text-primary); background: var(--surface-2); }
    .btn-upload:disabled { color: var(--text-muted); opacity: 0.4; cursor: not-allowed; }
    .btn-send { background: var(--primary-container); border: none; cursor: pointer; padding: 0.55rem; display: flex; align-items: center; justify-content: center; color: var(--on-primary-container); border-radius: var(--radius-md); transition: all 0.1s; }
    .btn-send:hover { background: var(--primary); }
    .btn-send:active { transform: scale(0.95); }
    .btn-send:disabled { background: var(--surface-2); color: var(--text-muted); cursor: not-allowed; }

    @media (max-width: 768px) {
        .input-area { padding: 0.4rem 0.5rem; padding-bottom: calc(0.4rem + env(safe-area-inset-bottom, 0px)); }
        .input-area input { font-size: 0.9rem; padding: 0.6rem 0.8rem; }
    }
</style>
