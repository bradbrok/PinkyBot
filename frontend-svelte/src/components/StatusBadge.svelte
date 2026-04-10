<script>
    /**
     * StatusBadge — semantic status indicator with color variants.
     *
     * Usage:
     *   <StatusBadge status="on" label="Running" />
     *   <StatusBadge status="off" />
     *   <StatusBadge status="warn" label="Degraded" />
     *   <StatusBadge variant="model" label="opus" />
     */

    /** @type {string} status key — maps to badge-{status} class */
    export let status = '';

    /** @type {string} display text — defaults to status if not set */
    export let label = '';

    /** @type {string} optional variant override (e.g. 'model', 'mcp', 'builtin') */
    export let variant = '';

    /** @type {string} optional inline style */
    export let style = '';

    $: cssClass = variant ? `badge badge-${variant}` : (status ? `badge badge-${status}` : 'badge');
    $: displayText = label || status;
</script>

<span class={cssClass} style={style}>{displayText}</span>
