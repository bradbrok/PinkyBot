<!--
  TmuxPaneModal — live viewer (+ optional input) for a tmux agent's terminal pane.

  Subscribes to `/agents/{agent}/tmux/pane/stream` (SSE, snapshot per frame)
  and renders the captured ANSI output into an xterm.js terminal. Each frame
  is a full pane snapshot — we clear + redraw in one write so cursor/colours
  come through but the previous frame doesn't leak. xterm.js is
  dynamic-imported so the ~150 KB dependency only loads when the modal opens.

  Input: read-only by default. The footer's "input" toggle arms keystroke
  passthrough — xterm onData chunks are mapped to the backend's
  `/tmux/pane/keys` endpoint (named keys for control sequences, literal text
  otherwise), so an operator can answer first-run dialogs or interrupt a
  wedged REPL without SSH + `tmux attach`. Sends start immediately and repeat
  unacknowledged events; the daemon orders and de-duplicates them by sequence.

  Props:
    show:  bool   — modal open state (bind from parent)
    agent: string — agent name (e.g. "barsik")
    label: string — session label (default "main")

  Dispatches: no custom events; toggle via bound `show`.
-->
<script>
    import { onDestroy } from 'svelte';
    import Modal from './Modal.svelte';
    import { api, sse } from '../lib/api.js';
    import { createPaneInputSender } from '../lib/paneInputSender.js';

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
    // Last dims we asked the backend to resize the pane to. Used to
    // skip no-op reconnects when ResizeObserver fires for changes that
    // didn't move the character grid (e.g. sub-pixel reflows).
    let lastReqCols = 0;
    let lastReqRows = 0;
    // Debounce timer for reconnect-on-resize. ResizeObserver can fire
    // a flurry of events during drag-resize; we want to coalesce.
    let resizeDebounceId = null;

    // Lifecycle reacts to (show, agent, label, hostEl). Mount when modal
    // opens + host div is rendered + we have an agent. Teardown when the
    // modal closes or the target changes.
    $: void reconcile(show, agent, label, hostEl);

    // ── Typeable input (off by default) ──────────────────────────
    let inputEnabled = false;
    let inputError = '';
    let onDataDisposable = null;
    let paneInputSender = null;

    // xterm onData control sequences → backend named keys (tmux names).
    const KEY_SEQUENCES = {
        '\r': 'Enter',
        '\t': 'Tab',
        '\x7f': 'BSpace',
        '\x03': 'C-c',
        '\x15': 'C-u',
        '\x1b': 'Escape',
        '\x1b[A': 'Up',
        '\x1b[B': 'Down',
        '\x1b[C': 'Right',
        '\x1b[D': 'Left',
        '\x1b[Z': 'BTab',
        '\x1b[3~': 'DC',
        '\x1b[H': 'Home',
        '\x1b[1~': 'Home',
        '\x1b[F': 'End',
        '\x1b[4~': 'End',
        '\x1b[5~': 'PPage',
        '\x1b[6~': 'NPage',
    };

    function queueSend(payload) {
        paneInputSender?.enqueue(payload);
    }

    function handleTerminalData(data) {
        if (!inputEnabled || !data) return;
        const named = KEY_SEQUENCES[data];
        if (named) {
            queueSend({ key: named });
            return;
        }
        // Mixed chunk (e.g. IME/paste or burst typing). Split out any
        // embedded control sequences; send printable runs literally.
        // Unknown control/escape bytes are DROPPED, never sent as text —
        // the backend rejects control chars in the literal channel (they'd
        // bypass the named-key whitelist: literal "\x04" is C-d).
        const isControl = (ch) => {
            const code = ch.charCodeAt(0);
            return code < 0x20 || code === 0x7f;
        };
        let literal = '';
        let i = 0;
        while (i < data.length) {
            let matched = '';
            for (const seq of Object.keys(KEY_SEQUENCES)) {
                if (data.startsWith(seq, i) && seq.length > matched.length) matched = seq;
            }
            if (matched === '\x1b' && (data[i + 1] === '[' || data[i + 1] === 'O')) {
                // Unknown CSI/SS3 sequence (a known one would out-match bare
                // ESC). Consume through its final byte (0x40–0x7e) and drop.
                let j = i + 2;
                while (j < data.length && !(data.charCodeAt(j) >= 0x40 && data.charCodeAt(j) <= 0x7e)) j += 1;
                i = Math.min(j + 1, data.length);
                continue;
            }
            if (matched) {
                if (literal) { queueSend({ text: literal }); literal = ''; }
                queueSend({ key: KEY_SEQUENCES[matched] });
                i += matched.length;
            } else if (isControl(data[i])) {
                i += 1; // unknown control byte — drop, never forward
            } else {
                literal += data[i];
                i += 1;
            }
        }
        if (literal) queueSend({ text: literal });
    }

    function toggleInput() {
        inputEnabled = !inputEnabled;
        inputError = '';
        if (terminal) {
            terminal.options.disableStdin = !inputEnabled;
            if (inputEnabled) terminal.focus();
        }
    }

    let lastKey = '';
    async function reconcile(s, a, l, host) {
        const key = s && host && a ? `${a}|${l}` : '';
        if (key === lastKey) return;
        if (lastKey) teardown();
        lastKey = key;
        if (key) await mount();
    }

    // Build the SSE URL with the current xterm grid dims so the
    // backend reshapes the tmux pane to fill the viewer.
    function streamUrl() {
        const params = new URLSearchParams({ label });
        if (terminal && terminal.cols > 0 && terminal.rows > 0) {
            params.set('cols', String(terminal.cols));
            params.set('rows', String(terminal.rows));
            lastReqCols = terminal.cols;
            lastReqRows = terminal.rows;
        }
        return `/agents/${encodeURIComponent(agent)}/tmux/pane/stream?${params.toString()}`;
    }

    // Close + reopen the stream with the current grid dims. The
    // backend resizes the pane on stream-open, so a reconnect is how
    // we propagate a resize. Cheap because SSE is HTTP/1.1 + the
    // pane snapshot rate is ~2.5 Hz.
    function reconnectStream() {
        if (!terminal || !agent) return;
        if (terminal.cols === lastReqCols && terminal.rows === lastReqRows) return;
        if (sseSource) {
            try { sseSource.close(); } catch {}
            sseSource = null;
        }
        attachStream();
    }

    function attachStream() {
        sseSource = sse(streamUrl());
        sseSource.onmessage = (evt) => {
            let data = null;
            try { data = JSON.parse(evt.data || '{}'); } catch { return; }
            if (!data || !data.type) return;
            if (data.type === 'snapshot') {
                lastFrameAt = data.ts || (Date.now() / 1000);
                if (terminal) {
                    // Reset + clear viewport + clear scrollback + home,
                    // then redraw. The captured output already contains
                    // ANSI for colours/cursor. \x1b[3J clears scrollback
                    // (belt-and-suspenders alongside scrollback:0) so
                    // previous frames never accumulate in history.
                    terminal.write('\x1b[2J\x1b[3J\x1b[H' + (data.data || ''));
                }
                if (statusMessage) statusMessage = '';
            } else if (data.type === 'not_tmux') {
                statusMessage = 'This agent is not running under the tmux transport — no pane to view.';
                if (sseSource) { sseSource.close(); sseSource = null; }
            }
        };
        sseSource.onerror = () => {
            statusMessage = 'Stream disconnected. Close + reopen to retry.';
            // Close it: EventSource auto-reconnects forever by default, and on a
            // persistent backend error that loops silently. We tell the user to
            // reopen, so stop the native retry loop.
            if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
        };
    }

    async function mount() {
        statusMessage = 'Loading terminal…';
        // Capture this mount's target. Requests initiated before a close/switch
        // must finish against the old pane, never the newly selected one.
        const targetAgent = agent;
        const targetLabel = label;
        paneInputSender = createPaneInputSender({
            send: (body, options) => api(
                'POST',
                `/agents/${encodeURIComponent(targetAgent)}/tmux/pane/keys`,
                { ...body, label: targetLabel },
                options,
            ),
            onSuccess: () => { if (inputError) inputError = ''; },
            onError: (e) => { inputError = `send failed: ${e?.message || e}`; },
        });
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
                // Snapshot model: each frame is a full pane redraw, so we
                // don't want scrollback at all. Keeping a buffer would mean
                // every \x1b[2J pushes the previous frame into history,
                // accumulating frame-after-frame as a memory + UX leak
                // (modal grows an endless scroll of stale frames).
                scrollback: 0,
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
            // Input passthrough — inert until the footer toggle arms it
            // (disableStdin stays true, so xterm emits no data).
            onDataDisposable = terminal.onData(handleTerminalData);

            if (!hostEl) {
                // Host unmounted before async resolved — abort.
                teardown();
                return;
            }
            terminal.open(hostEl);
            try { fitAddon.fit(); } catch {}

            // Track container resizes (modal resize, viewport rotate)
            // so xterm reflows AND so we reshape the backend tmux pane
            // to the new grid dims. Debounced (200ms) so drag-resize
            // doesn't spam the stream with reconnects. ResizeObserver
            // fires on initial observe — that first fire is harmless
            // because lastReqCols/Rows match (we just attached with
            // them).
            if (typeof ResizeObserver !== 'undefined') {
                resizeObserver = new ResizeObserver(() => {
                    try { fitAddon && fitAddon.fit(); } catch {}
                    if (resizeDebounceId) clearTimeout(resizeDebounceId);
                    resizeDebounceId = setTimeout(() => {
                        resizeDebounceId = null;
                        reconnectStream();
                    }, 200);
                });
                resizeObserver.observe(hostEl);
            }

            statusMessage = 'Connecting to stream…';
            attachStream();
        } catch (e) {
            statusMessage = `Failed to load terminal: ${e?.message || e}`;
            teardown();
        }
    }

    function teardown() {
        if (resizeDebounceId) { clearTimeout(resizeDebounceId); resizeDebounceId = null; }
        if (sseSource) { try { sseSource.close(); } catch {} sseSource = null; }
        if (resizeObserver) { try { resizeObserver.disconnect(); } catch {} resizeObserver = null; }
        if (onDataDisposable) { try { onDataDisposable.dispose(); } catch {} onDataDisposable = null; }
        if (paneInputSender) { paneInputSender.dispose(); paneInputSender = null; }
        if (terminal) { try { terminal.dispose(); } catch {} terminal = null; }
        fitAddon = null;
        statusMessage = '';
        lastFrameAt = 0;
        lastReqCols = 0;
        lastReqRows = 0;
        inputEnabled = false;
        inputError = '';
    }

    onDestroy(teardown);
</script>

<Modal bind:show title={`Terminal · ${agent}${label && label !== 'main' ? ` · ${label}` : ''}`} maxWidth="1800px" width="96%" flush>
    <div class="pane-wrap">
        {#if statusMessage}
            <div class="pane-status">{statusMessage}</div>
        {/if}
        <div class="pane-host" bind:this={hostEl}></div>
        {#if lastFrameAt > 0}
            <div class="pane-footer">
                <button class="input-toggle" class:armed={inputEnabled} on:click={toggleInput}
                        title={inputEnabled ? 'Disable keyboard passthrough' : 'Type into this terminal'}>
                    ⌨ input: {inputEnabled ? 'ON' : 'off'}
                </button>
                {#if inputError}
                    <span class="input-error">{inputError}</span>
                {/if}
                <span class="footer-status">
                    {inputEnabled ? 'keystrokes go to the agent' : 'read-only'} · live · last frame {new Date(lastFrameAt * 1000).toLocaleTimeString()}
                </span>
            </div>
        {/if}
    </div>
</Modal>

<style>
    .pane-wrap {
        display: flex;
        flex-direction: column;
        min-height: 0;
        height: 88vh;
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
        display: flex;
        align-items: center;
        gap: 0.8rem;
    }
    .footer-status {
        margin-left: auto;
    }
    .input-toggle {
        font-family: inherit;
        font-size: inherit;
        background: transparent;
        color: var(--text-muted, #888);
        border: 1px solid #333;
        border-radius: 3px;
        padding: 0.15rem 0.5rem;
        cursor: pointer;
    }
    .input-toggle:hover {
        border-color: #555;
        color: #ccc;
    }
    .input-toggle.armed {
        color: #7dd87d;
        border-color: #3a6b3a;
        background: #14240f;
    }
    .input-error {
        color: #e07070;
    }
</style>
