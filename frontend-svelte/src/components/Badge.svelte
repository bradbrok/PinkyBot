<script>
    export let text = '';
    export let variant = '';
    export let style = '';
</script>

{#if style}
    <span class="badge" style={style}>{text}</span>
{:else if variant}
    <span class="badge badge-{variant}">{text}</span>
{:else}
    <span class="badge">{text}</span>
{/if}
