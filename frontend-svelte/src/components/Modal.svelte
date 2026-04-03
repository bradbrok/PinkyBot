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
    export let fullscreen = false;

    function onOverlayClick(e) {
        if (e.target === e.currentTarget && !fullscreen) {
            show = false;
        }
    }

    function onKeydown(e) {
        if (e.key === 'Escape') show = false;
    }

    $: modalStyle = fullscreen
        ? 'width:100vw;height:100dvh;max-width:100vw;border-radius:0;margin:0;top:0;left:0;'
        : `width:${width || '95%'};${maxWidth ? `max-width:${maxWidth};` : ''}${contentStyle}`;
</script>

<svelte:window on:keydown={onKeydown} />

{#if show}
    <div class="modal-overlay" class:fullscreen-overlay={fullscreen} on:click={onOverlayClick}>
        <div
            class={`modal ${stack ? 'modal-stack' : ''} ${contentClass} ${fullscreen ? 'modal-fullscreen' : ''}`.trim()}
            style={modalStyle}
        >
            <div class="modal-header">
                {#if $$slots.header}
                    <slot name="header" />
                {:else}
                    <div class="modal-title">{title}</div>
                {/if}
                <button class="modal-close" on:click={() => show = false} aria-label="Close dialog">&times;</button>
            </div>
            <div class={`modal-body ${flush ? 'flush' : ''} ${bodyClass} ${fullscreen ? 'fullscreen-body' : ''}`.trim()}>
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
