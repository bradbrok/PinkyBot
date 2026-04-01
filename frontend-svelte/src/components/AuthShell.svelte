<script>
    export let title = '';
    export let kicker = '';
    export let copy = '';
    export let error = '';
    export let loading = false;
    export let buttonLabel = 'Submit';
    export let onSubmit = () => {};
</script>

<div class="auth-shell">
    <div class="auth-card">
        <div class="auth-kicker">{kicker}</div>
        <h1>{title}</h1>
        <p class="auth-copy">{copy}</p>
        <slot />
        {#if error}
            <div class="auth-error">{error}</div>
        {/if}
        <button class="auth-button" on:click={onSubmit} disabled={loading}>
            {loading ? 'Working...' : buttonLabel}
        </button>
    </div>
</div>

<style>
    .auth-shell {
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 2rem;
        background:
            radial-gradient(circle at top, rgba(255, 125, 68, 0.25), transparent 38%),
            linear-gradient(160deg, #120f16 0%, #1d1f2a 45%, #11151c 100%);
    }
    .auth-card {
        width: min(100%, 30rem);
        padding: 2rem;
        border: 1px solid rgba(255, 255, 255, 0.12);
        background: rgba(12, 16, 24, 0.84);
        box-shadow: 0 24px 60px rgba(0, 0, 0, 0.45);
        backdrop-filter: blur(18px);
    }
    .auth-kicker {
        margin-bottom: 0.8rem;
        font-family: var(--font-mono, monospace);
        font-size: 0.75rem;
        letter-spacing: 0.16em;
        text-transform: uppercase;
        color: #f6a16f;
    }
    h1 {
        margin: 0 0 0.6rem 0;
        font-size: 2rem;
    }
    .auth-copy {
        margin: 0 0 1.2rem 0;
        color: rgba(240, 244, 247, 0.74);
        line-height: 1.5;
    }
    :global(.auth-shell label) {
        display: grid;
        gap: 0.45rem;
        margin-bottom: 0.9rem;
    }
    :global(.auth-shell span) {
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.08em;
        color: rgba(240, 244, 247, 0.72);
    }
    :global(.auth-shell input) {
        width: 100%;
        padding: 0.95rem 1rem;
        border: 1px solid rgba(255, 255, 255, 0.14);
        background: rgba(4, 7, 12, 0.65);
        color: #f7f9fb;
        font-size: 1rem;
    }
    .auth-error {
        margin-top: 0.2rem;
        padding: 0.8rem 0.9rem;
        background: rgba(167, 45, 45, 0.18);
        border: 1px solid rgba(255, 122, 122, 0.35);
        color: #ffc0c0;
    }
    .auth-button {
        width: 100%;
        margin-top: 1.1rem;
        padding: 0.95rem 1rem;
        border: 0;
        background: linear-gradient(135deg, #ff8f50, #ff5b7e);
        color: #140e12;
        font-weight: 700;
        cursor: pointer;
    }
    .auth-button:disabled {
        cursor: progress;
        opacity: 0.7;
    }
    @media (max-width: 640px) {
        .auth-card {
            padding: 1.4rem;
        }
    }
</style>
