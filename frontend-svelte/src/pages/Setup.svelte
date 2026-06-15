<script>
    import { onMount } from 'svelte';
    import { _ } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { sanitizeNextPath } from '../lib/safe.js';
    import AuthShell from '../components/AuthShell.svelte';

    let password = '';
    let confirmPassword = '';
    let loading = false;
    let error = '';

    const params = new URLSearchParams(window.location.search);
    const nextPath = sanitizeNextPath(params.get('next'));

    onMount(async () => {
        try {
            const status = await api('GET', '/auth/status');
            if (!status.setup_required) {
                window.location.href = status.authenticated ? nextPath : `/login?next=${encodeURIComponent(nextPath)}`;
            }
        } catch (e) {
            error = e.message.replace(/^\d+:\s*/, '');
        }
    });

    async function submit() {
        if (loading) return;
        if (!password.trim()) {
            error = $_('setup.enter_password');
            return;
        }
        if (password !== confirmPassword) {
            error = $_('setup.passwords_mismatch');
            return;
        }
        loading = true;
        error = '';
        try {
            const result = await api('POST', '/auth/setup', { password, next: nextPath });
            window.location.href = sanitizeNextPath(result.next || nextPath);
        } catch (e) {
            error = e.message.replace(/^\d+:\s*/, '');
        } finally {
            loading = false;
        }
    }
</script>

<svelte:head>
    <title>Pinky Setup</title>
</svelte:head>

<AuthShell
    kicker={$_('setup.kicker')}
    title={$_('setup.title')}
    copy={$_('setup.copy')}
    {error}
    {loading}
    buttonLabel={loading ? $_('common.saving') : $_('setup.finish')}
    onSubmit={submit}
>
    <label>
        <span>{$_('setup.new_password')}</span>
        <input type="password" bind:value={password} placeholder={$_('setup.create_password')} />
    </label>
    <label>
        <span>{$_('setup.confirm_password')}</span>
        <input
            type="password"
            bind:value={confirmPassword}
            placeholder={$_('setup.repeat_password')}
            on:keydown={(e) => e.key === 'Enter' && submit()}
        />
    </label>
</AuthShell>
