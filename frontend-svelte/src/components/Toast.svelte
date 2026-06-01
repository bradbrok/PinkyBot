<script>
    import { onDestroy } from 'svelte';
    import { toastMessage } from '../lib/stores.js';

    let visible = false;
    let message = '';
    let type = 'success';
    let timeout;

    const unsub = toastMessage.subscribe(val => {
        if (val) {
            message = val.message || val;
            type = val.type || 'success';
            visible = true;
            clearTimeout(timeout);
            timeout = setTimeout(() => {
                visible = false;
                toastMessage.set(null);
            }, 3000);
        }
    });

    onDestroy(() => {
        unsub();
        clearTimeout(timeout);
    });
</script>

<div class="toast" class:show={visible} class:error={type === 'error'} class:success={type === 'success'} aria-live="polite" aria-atomic="true">
    {message}
</div>
