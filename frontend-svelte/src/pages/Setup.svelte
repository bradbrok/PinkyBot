<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import AuthShell from '../components/AuthShell.svelte';

    let password = '';
    let confirmPassword = '';
    let loading = false;
    let error = '';

    const params = new URLSearchParams(window.location.search);
    const nextPath = params.get('next') || '/';

    onMount(async () => {
        try {
            const status = await api('GET', '/auth/status');
            if (!status.setup_required) {
                window.location.href = status.authenticated ? nextPath : `/login?next=${encodeURIComponent(nextPath)}`;
            }
        } catch (e) {
            error = e.message;
        }
    });

    async function submit() {
        if (!password.trim()) {
            error = 'Enter a password.';
            return;
        }
        if (password !== confirmPassword) {
            error = 'Passwords do not match.';
            return;
        }
        loading = true;
        error = '';
        try {
            const result = await api('POST', '/auth/setup', { password, next: nextPath });
            window.location.href = result.next || nextPath;
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
    kicker="First Run"
    title="Secure this UI"
    copy="Create the owner password that will gate the Pinky dashboard and browser admin APIs."
    {error}
    {loading}
    buttonLabel={loading ? 'Saving...' : 'Finish setup'}
    onSubmit={submit}
>
    <label>
        <span>New password</span>
        <input type="password" bind:value={password} placeholder="Create password" />
    </label>
    <label>
        <span>Confirm password</span>
        <input
            type="password"
            bind:value={confirmPassword}
            placeholder="Repeat password"
            on:keydown={(e) => e.key === 'Enter' && submit()}
        />
    </label>
</AuthShell>
