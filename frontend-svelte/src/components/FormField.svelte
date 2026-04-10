<script>
    /**
     * FormField — labeled form input with consistent styling.
     * Replaces the repeated pattern of div > label-div + input.
     *
     * Usage:
     *   <FormField label="API Key">
     *     <input type="password" class="form-input" bind:value={key} />
     *   </FormField>
     *
     *   <FormField label="Model" hint="e.g. claude-sonnet-4-5">
     *     <input type="text" class="form-input" bind:value={model} />
     *   </FormField>
     */

    /** @type {string} */
    export let label = '';

    /** @type {string} optional hint text below input */
    export let hint = '';

    /** @type {string} optional inline style on wrapper */
    export let style = '';
</script>

<div class="form-field" style={style}>
    {#if label}
        <div class="form-field-label">{label}</div>
    {/if}
    <slot />
    {#if hint}
        <div class="form-field-hint">{hint}</div>
    {/if}
</div>

<style>
    .form-field {
        display: flex;
        flex-direction: column;
        gap: 0.25rem;
    }
    .form-field-label {
        font-family: var(--font-grotesk);
        font-size: 0.7rem;
        font-weight: 700;
        text-transform: uppercase;
        color: var(--gray-mid);
        letter-spacing: 0.03em;
    }
    .form-field-hint {
        font-size: 0.72rem;
        color: var(--text-muted);
        margin-top: 0.1rem;
    }
</style>
