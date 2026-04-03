<script>
    import { toastMessage } from '../lib/stores.js';

    let visible = false;
    let message = '';
    let type = 'success';
    let timeout;

    toastMessage.subscribe(val => {
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
</script>

<div class="toast" class:show={visible} class:error={type === 'error'} class:success={type === 'success'}>
    {message}
</div>
