<!--
  TmuxPaneModal — read-only live viewer for a tmux agent's terminal pane.

  Subscribes to `/agents/{agent}/tmux/pane/stream` (SSE, snapshot per frame)
  and renders the captured ANSI output into an xterm.js terminal in
  disableStdin mode. Each frame is a full pane snapshot — we clear + redraw
  in one write so cursor/colours come through but the previous frame doesn't
  leak. xterm.js is dynamic-imported so the ~150 KB dependency only loads
  when the modal opens.

  Props:
    show:  bool   — modal open state (bind from parent)
    agent: string — agent name (e.g. "barsik")
    label: string — session label (default "main")

  Dispatches: no custom events; toggle via bound `show`.
-->
<script>
    import { onDestroy } from 'svelte';
    import Modal from './Modal.svelte';
    import { sse } from '../lib/api.js';

    export let show = false;
    export let agent = '';
    export let label = 'main';

    let hostEl;
    let terminal = null;
    let fitAddon = null;
    let sseSource = null;
    let resizeObserver = null;
    let statusMessage = '';
    let lastFrameAt = 0;

    // Lifecycle reacts to (show, agent, label, hostEl). Mount when modal
    // opens + host div is rendered + we have an agent. Teardown when the
    // modal closes or the target changes.
    $: void reconcile(show, agent, label, hostEl);

    let lastKey = '';
    async function reconcile(s, a, l, host) {
        const key = s && host && a ? `${a}|${l}` : '';
        if (key === lastKey) return;
        if (lastKey) teardown();
        lastKey = key;
        if (key) await mount();
    }

    async function mount() {
        statusMessage = 'Loading terminal…';
        try {
            const [{ Terminal }, { FitAddon }] = await Promise.all([
                import('@xterm/xterm'),
                import('@xterm/addon-fit'),
            ]);
            // CSS comes alongside the JS bundle — pulled in lazily too.
            await import('@xterm/xterm/css/xterm.css');

            terminal = new Terminal({
                disableStdin: true,
                cursorBlink: false,
                convertEol: true,
                scrollback: 5000,
                fontSize: 12,
                fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Monaco, "Cascadia Mono", "Roboto Mono", Consolas, monospace',
                theme: {
                    background: '#0a0a0a',
                    foreground: '#e0e0e0',
                    cursor: '#888',
                },
            });
            fitAddon = new FitAddon();
            terminal.loadAddon(fitAddon);

            if (!hostEl) {
                // Host unmounted before async resolved — abort.
                teardown();
                return;
            }
            terminal.open(hostEl);
            try { fitAddon.fit(); } catch {}

            // Track container resizes (modal resize, viewport rotate) so
            // xterm reflows. ResizeObserver fires on initial observe too.
            if (typeof ResizeObserver !== 'undefined') {
                resizeObserver = new ResizeObserver(() => {
                    try { fitAddon && fitAddon.fit(); } catch {}
                });
                resizeObserver.observe(hostEl);
            }

            statusMessage = 'Connecting to stream…';
            sseSource = sse(`/agents/${encodeURIComponent(agent)}/tmux/pane/stream?label=${encodeURIComponent(label)}`);
            sseSource.onmessage = (evt) => {
                let data = null;
                try { data = JSON.parse(evt.data || '{}'); } catch { return; }
                if (!data || !data.type) return;
                if (data.type === 'snapshot') {
                    lastFrameAt = data.ts || (Date.now() / 1000);
                    if (terminal) {
                        // Clear screen + home + redraw. The captured output
                        // already contains ANSI for colours/cursor.
                        terminal.write('\x1b[2J\x1b[H' + (data.data || ''));
                    }
                    if (statusMessage) statusMessage = '';
                } else if (data.type === 'not_tmux') {
                    statusMessage = 'This agent is not running under the tmux transport — no pane to view.';
                    if (sseSource) { sseSource.close(); sseSource = null; }
                }
            };
            sseSource.onerror = () => {
                statusMessage = 'Stream disconnected. Close + reopen to retry.';
            };
        } catch (e) {
            statusMessage = `Failed to load terminal: ${e?.message || e}`;
            teardown();
        }
    }

    function teardown() {
        if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
        if (resizeObserver) { try { resizeObserver.disconnect(); } catch {} resizeObserver = null; }
        if (terminal) { try { terminal.dispose(); } catch {} terminal = null; }
        fitAddon = null;
        statusMessage = '';
        lastFrameAt = 0;
    }

    onDestroy(teardown);
</script>

<Modal bind:show title={`Terminal · ${agent}${label && label !== 'main' ? ` · ${label}` : ''}`} maxWidth="1200px" width="92%" flush>
    <div class="pane-wrap">
        {#if statusMessage}
            <div class="pane-status">{statusMessage}</div>
        {/if}
        <div class="pane-host" bind:this={hostEl}></div>
        {#if lastFrameAt > 0}
            <div class="pane-footer">read-only · live · last frame {new Date(lastFrameAt * 1000).toLocaleTimeString()}</div>
        {/if}
    </div>
</Modal>

<style>
    .pane-wrap {
        display: flex;
        flex-direction: column;
        min-height: 0;
        height: 70vh;
        background: #0a0a0a;
    }
    .pane-host {
        flex: 1 1 auto;
        min-height: 0;
        background: #0a0a0a;
        padding: 0.5rem;
    }
    /* xterm renders its own <canvas>; ensure the host fills the flex slot */
    .pane-host :global(.xterm),
    .pane-host :global(.xterm-viewport) {
        background-color: #0a0a0a !important;
        width: 100% !important;
        height: 100% !important;
    }
    .pane-status {
        font-family: var(--font-grotesk, monospace);
        font-size: 0.75rem;
        color: var(--text-muted, #888);
        padding: 0.5rem 0.8rem;
        border-bottom: 1px solid #222;
    }
    .pane-footer {
        font-family: var(--font-grotesk, monospace);
        font-size: 0.65rem;
        color: var(--text-muted, #666);
        padding: 0.3rem 0.8rem;
        border-top: 1px solid #222;
        text-align: right;
    }
</style>
