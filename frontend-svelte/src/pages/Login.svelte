<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import AuthShell from '../components/AuthShell.svelte';

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
            error = 'Enter your password.';
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
    kicker="Pinky Admin"
    title="Log in"
    copy="Use the owner password to unlock the Pinky dashboard and admin APIs."
    {error}
    {loading}
    buttonLabel={loading ? 'Signing in...' : 'Sign in'}
    onSubmit={submit}
>
    <label>
        <span>Password</span>
        <input
            type="password"
            bind:value={password}
            placeholder="Enter password"
            on:keydown={(e) => e.key === 'Enter' && submit()}
        />
    </label>
</AuthShell>
