<script>
    import { onMount, afterUpdate } from 'svelte';

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

    let modalEl;

    function onOverlayClick(e) {
        if (e.target === e.currentTarget && !fullscreen) {
            show = false;
        }
    }

    function onKeydown(e) {
        if (e.key === 'Escape') show = false;
        // Focus trap: keep Tab within the modal
        if (e.key === 'Tab' && show && modalEl) {
            const focusable = modalEl.querySelectorAll(
                'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
            );
            if (!focusable.length) return;
            const first = focusable[0];
            const last = focusable[focusable.length - 1];
            if (e.shiftKey && document.activeElement === first) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && document.activeElement === last) {
                e.preventDefault();
                first.focus();
            }
        }
    }

    $: modalStyle = fullscreen
        ? 'width:100vw;height:100dvh;max-width:100vw;border-radius:0;margin:0;top:0;left:0;'
        : `width:${width || '95%'};${maxWidth ? `max-width:${maxWidth};` : ''}${contentStyle}`;
</script>

<svelte:window on:keydown={onKeydown} />

{#if show}
    <div class="modal-overlay" class:fullscreen-overlay={fullscreen} class:stacked={stack} on:click={onOverlayClick}>
        <div
            bind:this={modalEl}
            class={`modal ${stack ? 'modal-stack' : ''} ${contentClass} ${fullscreen ? 'modal-fullscreen' : ''}`.trim()}
            style={modalStyle}
            role="dialog"
            aria-modal="true"
            aria-label={title || 'Dialog'}
        >
            <div class="modal-header">
                {#if $$slots.header}
                    <slot name="header" />
                {:else}
                    <div class="modal-title">{title}</div>
                {/if}
                <div class="modal-header-actions">
                    <slot name="headerActions" />
                    <button class="modal-close" on:click={() => show = false} aria-label="Close dialog">&times;</button>
                </div>
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
