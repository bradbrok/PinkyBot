<script>
    import { locale } from 'svelte-i18n';
    import { SUPPORTED_LOCALES, setLocale } from '../lib/i18n.js';

    let open = false;

    $: currentCode = ($locale || 'en').split('-')[0].toLowerCase();
    $: currentLocale = SUPPORTED_LOCALES.find((l) => l.code === currentCode) || SUPPORTED_LOCALES[0];

    function toggle() {
        open = !open;
    }

    function pick(code) {
        setLocale(code);
        open = false;
    }

    function handleKeydown(e) {
        if (e.key === 'Escape') open = false;
    }

    function handleOutsideClick(e) {
        if (!e.currentTarget.contains(e.relatedTarget)) open = false;
    }
</script>

<svelte:window on:keydown={handleKeydown} />

<div class="locale-picker" on:focusout={handleOutsideClick}>
    <button
        class="locale-trigger"
        on:click={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        title="Language / Язык"
    >
        <span class="locale-code">{currentCode.toUpperCase()}</span>
        <span class="locale-caret">{open ? '▲' : '▼'}</span>
    </button>

    {#if open}
        <div class="locale-dropdown" role="listbox" aria-label="Language">
            {#each SUPPORTED_LOCALES as lang}
                {@const isSelected = lang.code === currentCode}
                <button
                    class="locale-option"
                    class:selected={isSelected}
                    role="option"
                    aria-selected={isSelected}
                    on:click={() => pick(lang.code)}
                >
                    <span class="locale-opt-code">{lang.code.toUpperCase()}</span>
                    <span class="locale-opt-label">{lang.label}</span>
                </button>
            {/each}
        </div>
    {/if}
</div>

<style>
    .locale-picker {
        position: relative;
    }

    .locale-trigger {
        display: inline-flex;
        align-items: center;
        gap: 0.25rem;
        padding: 0.25rem 0.45rem;
        border: none;
        border-radius: var(--radius-lg);
        background: var(--surface-2);
        color: var(--text-muted);
        cursor: pointer;
        font-family: var(--font-grotesk);
        font-size: 0.65rem;
        font-weight: 700;
        letter-spacing: 0.08em;
        transition: background 0.12s, color 0.12s;
        white-space: nowrap;
    }

    .locale-trigger:hover {
        background: var(--surface-3, var(--surface-2));
        color: var(--text-primary);
    }

    .locale-code {
        color: #b8a000;
        font-weight: 800;
    }

    .locale-caret {
        font-size: 0.5rem;
        opacity: 0.6;
    }

    /* Dropdown — opens upward since it's in the sidebar footer */
    .locale-dropdown {
        position: absolute;
        bottom: calc(100% + 0.35rem);
        left: 0;
        min-width: 160px;
        background: var(--surface-1);
        border: 1px solid var(--border-subtle, rgba(255,255,255,0.08));
        border-radius: var(--radius-lg);
        box-shadow: 0 8px 32px rgba(0, 0, 0, 0.45);
        overflow: hidden;
        z-index: 200;
    }

    .locale-option {
        display: flex;
        align-items: center;
        gap: 0.75rem;
        width: 100%;
        padding: 0.45rem 0.75rem;
        border: none;
        background: transparent;
        cursor: pointer;
        text-align: left;
        transition: background 0.1s;
    }

    .locale-option:hover {
        background: var(--surface-2);
    }

    .locale-option.selected {
        background: var(--surface-2);
    }

    .locale-opt-code {
        font-family: var(--font-grotesk);
        font-size: 0.72rem;
        font-weight: 800;
        color: #b8a000;
        min-width: 1.8rem;
        letter-spacing: 0.05em;
    }

    .locale-opt-label {
        font-family: var(--font-body);
        font-size: 0.82rem;
        font-weight: 500;
        color: var(--text-muted);
    }

    .locale-option.selected .locale-opt-label {
        color: var(--text-primary);
        font-weight: 600;
    }
</style>
