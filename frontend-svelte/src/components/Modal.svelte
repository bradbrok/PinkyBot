<script>
    export let show = false;
    export let title = '';

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
        <div class="modal">
            <div class="modal-header">
                <div class="modal-title">{title}</div>
                <button class="modal-close" on:click={() => show = false}>&times;</button>
            </div>
            <div class="modal-body">
                <slot />
            </div>
            {#if $$slots.footer}
                <div class="modal-footer">
                    <slot name="footer" />
                </div>
            {/if}
        </div>
    </div>
{/if}
