<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import AuthShell from '../components/AuthShell.svelte';
    import { _ } from 'svelte-i18n';

    let password = '';
    let loading = false;
    let error = '';

    const params = new URLSearchParams(window.location.search);
    const nextPath = params.get('next') || '/';

    onMount(async () => {
        try {
            const status = await api('GET', '/auth/status');
            if (status.setup_required) {
                window.location.href = `/setup?next=${encodeURIComponent(nextPath)}`;
                return;
            }
            if (status.authenticated) {
                window.location.href = nextPath;
            }
        } catch (e) {
            error = e.message;
        }
    });

    async function submit() {
        if (!password.trim()) {
            error = $_('auth.password_label') + ' required.';
            return;
        }
        loading = true;
        error = '';
        try {
            const result = await api('POST', '/auth/login', { password, next: nextPath });
            window.location.href = result.next || nextPath;
        } catch (e) {
            error = e.message.replace(/^\d+:\s*/, '');
        } finally {
            loading = false;
        }
    }
</script>

<svelte:head>
    <title>Pinky Login</title>
</svelte:head>

<AuthShell
    kicker={$_('auth.kicker')}
    title={$_('auth.title')}
    copy={$_('auth.copy')}
    {error}
    {loading}
    buttonLabel={loading ? $_('auth.button_signing_in') : $_('auth.button_sign_in')}
    onSubmit={submit}
>
    <label>
        <span>{$_('auth.password_label')}</span>
        <input
            type="password"
            bind:value={password}
            placeholder={$_('auth.password_placeholder')}
            on:keydown={(e) => e.key === 'Enter' && submit()}
        />
    </label>
</AuthShell>
