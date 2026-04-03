<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';

    export let value = '';
    export let label = 'Agent';

    let agentList = [];

    onMount(async () => {
        try {
            const data = await api('GET', '/agents');
            agentList = data.agents || [];
        } catch (e) {
            console.error('Failed to load agents:', e);
        }
    });
</script>

<div class="controls-group">
    {#if label}
        <span class="controls-label">{label}:</span>
    {/if}
    <select class="form-select" bind:value on:change>
        <option value="">Select agent...</option>
        {#each agentList as agent}
            <option value={agent.name}>{agent.display_name || agent.name}</option>
        {/each}
    </select>
</div>

<style>
    .controls-group { display: flex; gap: 0.5rem; align-items: center; }
    .controls-label { font-family: var(--font-mono); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--gray-mid); white-space: nowrap; }
</style>
