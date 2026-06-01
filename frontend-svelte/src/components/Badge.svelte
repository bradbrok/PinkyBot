<!-- NOTE: StatusBadge.svelte is the canonical badge primitive (functional superset). Prefer it for new usage; this component is retained only for existing KnowledgeBase.svelte callers. -->
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
