<script>
    import { _ } from 'svelte-i18n';

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
        <div class="auth-header">
            <span class="auth-logo">{$_('auth.logo')}</span>
        </div>
        <div class="auth-body">
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
</div>

<style>
    .auth-shell {
        min-height: 100vh;
        display: grid;
        place-items: center;
        padding: 2rem;
        background: var(--app-bg);
    }
    .auth-card {
        width: min(100%, 28rem);
        border: none;
        border-radius: var(--radius-xl);
        background: var(--surface-1);
        box-shadow: 0 0 60px var(--shadow-color);
        overflow: hidden;
    }
    .auth-header {
        padding: 1rem 1.5rem;
        background: var(--surface-2);
    }
    .auth-logo {
        font-family: var(--font-grotesk);
        font-size: 1.1rem;
        font-weight: 900;
        background: var(--primary-container);
        color: #000;
        padding: 0.2rem 0.6rem;
        border-radius: var(--radius);
        letter-spacing: -0.02em;
    }
    .auth-body {
        padding: 1.5rem;
    }
    .auth-kicker {
        margin-bottom: 0.6rem;
        font-family: var(--font-grotesk);
        font-size: 0.7rem;
        font-weight: 700;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--text-muted);
    }
    h1 {
        margin: 0 0 0.5rem 0;
        font-family: var(--font-grotesk);
        font-size: 1.6rem;
        font-weight: 700;
        color: var(--text-primary);
    }
    .auth-copy {
        margin: 0 0 1.2rem 0;
        color: var(--text-secondary);
        font-size: 0.9rem;
        line-height: 1.5;
    }
    :global(.auth-shell label) {
        display: grid;
        gap: 0.3rem;
        margin-bottom: 0.9rem;
    }
    :global(.auth-shell label span) {
        font-family: var(--font-grotesk);
        font-size: 0.7rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        color: var(--text-muted);
    }
    :global(.auth-shell input) {
        width: 100%;
        font-family: var(--font-body);
        font-size: 0.85rem;
        padding: 0.5rem 0.8rem;
        border: none;
        border-radius: var(--radius-lg);
        background: var(--input-bg);
        color: var(--text-primary);
    }
    :global(.auth-shell input::placeholder) {
        color: var(--text-subtle);
    }
    :global(.auth-shell input:focus) {
        outline: 2px solid var(--primary-container);
        outline-offset: -2px;
        background: var(--input-focus-bg);
    }
    .auth-error {
        margin-top: 0.5rem;
        padding: 0.6rem 0.8rem;
        font-family: var(--font-grotesk);
        font-size: 0.8rem;
        font-weight: 700;
        background: var(--tone-error-bg);
        color: var(--tone-error-text);
        border-radius: var(--radius-lg);
    }
    .auth-button {
        width: 100%;
        margin-top: 1rem;
        padding: 0.7rem 1rem;
        font-family: var(--font-grotesk);
        font-size: 0.75rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        border: none;
        border-radius: var(--radius-lg);
        background: var(--primary-container);
        color: var(--on-primary-container);
        cursor: pointer;
        box-shadow: 4px 4px 0px var(--primary);
        transition: all 0.1s;
    }
    .auth-button:hover {
        background: var(--primary);
        color: #fff;
        box-shadow: 2px 2px 0px var(--primary);
    }
    .auth-button:active {
        box-shadow: none;
        transform: translate(2px, 2px) scale(0.98);
    }
    .auth-button:disabled {
        cursor: progress;
        opacity: 0.6;
        box-shadow: none;
    }
    @media (max-width: 640px) {
        .auth-shell {
            padding: 1rem;
        }
    }
</style>
