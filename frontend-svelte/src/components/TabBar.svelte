<script>
    import { _ } from 'svelte-i18n';

    /** @type {Array<{id: string, label?: string, badge?: string}>} */
    export let tabs = [];

    /** @type {string} */
    export let active = '';

    /** @type {string} i18n prefix — tabs rendered as $_(`${i18nPrefix}${tab.id}`) */
    export let i18nPrefix = '';

    /** @type {'page' | 'detail'} */
    export let variant = 'page';

    import { createEventDispatcher } from 'svelte';
    const dispatch = createEventDispatcher();

    function select(id) {
        active = id;
        dispatch('change', id);
    }
</script>

{#if variant === 'page'}
    <div class="tab-bar">
        {#each tabs as tab}
            <button
                class="tab-btn"
                class:active={active === tab.id}
                on:click={() => select(tab.id)}
            >
                {tab.label || (i18nPrefix ? $_(`${i18nPrefix}${tab.id}`) : tab.id)}
                {#if tab.badge}<span class="tab-badge">{tab.badge}</span>{/if}
            </button>
        {/each}
    </div>
{:else}
    <div class="detail-tabs">
        {#each tabs as tab}
            <button
                class="detail-tab"
                class:active={active === tab.id}
                on:click={() => select(tab.id)}
            >
                {tab.label || (i18nPrefix ? $_(`${i18nPrefix}${tab.id}`) : tab.id)}
                {#if tab.badge}<span class="tab-badge">{tab.badge}</span>{/if}
            </button>
        {/each}
    </div>
{/if}

<style>
    /* Page-level tabs (Settings, Tasks, KB, etc.) */
    .tab-bar {
        display: flex;
        gap: 0.4rem;
        margin-bottom: 1.5rem;
        flex-wrap: wrap;
    }
    .tab-btn {
        padding: 0.4rem 1rem;
        font-size: 0.85rem;
        font-weight: 600;
        font-family: var(--font-grotesk);
        background: none;
        border: none;
        border-radius: 4px;
        color: var(--text-primary, #111);
        cursor: pointer;
        letter-spacing: 0.02em;
        transition: background 0.12s;
    }
    .tab-btn:hover { background: rgba(0,0,0,0.06); }
    .tab-btn.active {
        background: var(--accent, #f5c842);
        color: #000;
    }

    /* Detail-level tabs (Agent detail panel) */
    .detail-tabs {
        display: flex;
        gap: 0.25rem;
        padding: 0 1rem;
        border-bottom: 1px solid var(--border);
        margin-bottom: 0.5rem;
        margin-top: 0.5rem;
    }
    .detail-tab {
        font-family: var(--font-grotesk);
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        padding: 0.5rem 0.75rem;
        border: none;
        background: none;
        color: var(--gray-mid);
        cursor: pointer;
        border-bottom: 2px solid transparent;
        margin-bottom: -1px;
        position: relative;
    }
    .detail-tab:hover { color: var(--text); }
    .detail-tab.active { color: var(--text); border-bottom-color: var(--accent); }

    .tab-badge {
        display: inline-block;
        margin-left: 0.3rem;
        font-size: 0.65rem;
        padding: 0.1rem 0.35rem;
        border-radius: 999px;
        background: var(--accent);
        color: #000;
        font-weight: 700;
        vertical-align: middle;
    }
</style>
