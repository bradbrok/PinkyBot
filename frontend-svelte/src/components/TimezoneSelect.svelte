<script>
    /**
     * TimezoneSelect — consolidated timezone picker.
     * Replaces 3+ independent timezone selectors across Settings and Agents.
     */

    /** @type {string} */
    export let value = '';

    /** @type {string} */
    export let style = '';

    import { createEventDispatcher } from 'svelte';
    const dispatch = createEventDispatcher();

    const commonTimezones = [
        'America/Los_Angeles', 'America/Denver', 'America/Chicago', 'America/New_York',
        'America/Anchorage', 'Pacific/Honolulu', 'America/Phoenix',
        'America/Toronto', 'America/Vancouver', 'America/Sao_Paulo',
        'Europe/London', 'Europe/Paris', 'Europe/Berlin', 'Europe/Moscow',
        'Asia/Tokyo', 'Asia/Shanghai', 'Asia/Kolkata', 'Asia/Dubai',
        'Australia/Sydney', 'Pacific/Auckland', 'UTC',
    ];

    function onChange(e) {
        value = e.target.value;
        dispatch('change', value);
    }
</script>

<select class="form-select" style={style} value={value} on:change={onChange}>
    <option value="">—</option>
    {#each commonTimezones as tz}
        <option value={tz}>{tz}</option>
    {/each}
</select>
