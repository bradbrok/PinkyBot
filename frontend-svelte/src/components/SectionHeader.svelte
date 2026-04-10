<script>
    import { _ } from 'svelte-i18n';

    /** @type {string} */
    export let title = '';

    /** @type {string} i18n key — if set, overrides title */
    export let i18nKey = '';

    /** @type {Function|null} refresh handler — shows refresh button when set */
    export let onRefresh = null;

    /** @type {string} optional extra style */
    export let style = '';

    /** @type {'section' | 'detail'} */
    export let variant = 'section';
</script>

{#if variant === 'section'}
    <div class="section-header" style={style}>
        <div class="section-title">{i18nKey ? $_(`${i18nKey}`) : title}</div>
        <div class="section-actions">
            <slot name="actions" />
            {#if onRefresh}
                <button class="btn btn-sm" on:click={onRefresh}>{$_('common.refresh')}</button>
            {/if}
        </div>
    </div>
{:else}
    <div class="detail-section-header" style={style}>
        <span class="detail-section-title">{i18nKey ? $_(`${i18nKey}`) : title}</span>
        <div class="section-actions">
            <slot name="actions" />
            {#if onRefresh}
                <button class="btn btn-sm" on:click={onRefresh}>{$_('common.refresh')}</button>
            {/if}
        </div>
    </div>
{/if}

<style>
    .section-actions {
        display: flex;
        gap: 0.4rem;
        align-items: center;
    }
    .detail-section-header {
        padding: 1rem 1.5rem;
        background: var(--surface-2);
        border-radius: var(--radius-lg);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }
    .detail-section-title {
        font-family: var(--font-grotesk);
        font-size: 0.8rem;
        font-weight: 700;
        text-transform: uppercase;
    }
</style>
