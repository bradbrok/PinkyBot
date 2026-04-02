<script>
    export let show = false;
    export let title = '';
    export let width = '';
    export let maxWidth = '';
    export let stack = false;
    export let flush = false;
    export let bodyClass = '';
    export let footerClass = '';
    export let contentClass = '';
    export let contentStyle = '';

    function onOverlayClick(e) {
        if (e.target === e.currentTarget) {
            show = false;
        }
    }

    function onKeydown(e) {
        if (e.key === 'Escape') show = false;
    }
</script>

<svelte:window on:keydown={onKeydown} />

{#if show}
    <div class="modal-overlay" on:click={onOverlayClick}>
        <div
            class={`modal ${stack ? 'modal-stack' : ''} ${contentClass}`.trim()}
            style={`width:${width || '95%'};${maxWidth ? `max-width:${maxWidth};` : ''}${contentStyle}`}
        >
            <div class="modal-header">
                {#if $$slots.header}
                    <slot name="header" />
                {:else}
                    <div class="modal-title">{title}</div>
                {/if}
                <button class="modal-close" on:click={() => show = false} aria-label="Close dialog">&times;</button>
            </div>
            <div class={`modal-body ${flush ? 'flush' : ''} ${bodyClass}`.trim()}>
                <slot />
            </div>
            {#if $$slots.footer}
                <div class={`modal-footer ${footerClass}`.trim()}>
                    <slot name="footer" />
                </div>
            {/if}
        </div>
    </div>
{/if}
