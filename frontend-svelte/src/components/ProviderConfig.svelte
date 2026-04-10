<script>
    /**
     * ProviderConfig — unified provider configuration form.
     *
     * Used in:
     *   - Settings (global provider add/edit)
     *   - Agents (per-agent provider config)
     *
     * Modes:
     *   - "agent": shows global ref dropdown, anthropic preset, preset buttons
     *   - "global": shows name field, simpler preset buttons (no anthropic/codex)
     */
    import { _ } from 'svelte-i18n';
    import FormField from './FormField.svelte';
    import { createEventDispatcher } from 'svelte';

    const dispatch = createEventDispatcher();

    /** @type {'agent' | 'global'} */
    export let mode = 'agent';

    // Bound values
    export let providerUrl = '';
    export let providerKey = '';
    export let providerModel = '';
    export let providerPreset = 'anthropic';
    export let providerRef = '';   // agent mode only
    export let providerName = '';  // global mode only

    /** @type {Array<{id: string, name: string, provider_model?: string}>} */
    export let globalProviders = [];

    /** @type {boolean} */
    export let dirty = false;

    // Preset definitions
    const PRESETS = {
        anthropic:   { url: '',                                   key: '',       model: '' },
        ollama:      { url: 'http://localhost:11434',             key: 'ollama', model: '' },
        openrouter:  { url: 'https://openrouter.ai/api',         key: '',       model: 'anthropic/claude-sonnet-4-5' },
        deepseek:    { url: 'https://api.deepseek.com/anthropic', key: '',      model: 'deepseek-chat' },
        zai:         { url: 'https://api.z.ai/api/anthropic',    key: '',       model: 'glm-5.1' },
        codex_cli:   { url: 'codex_cli',                         key: '',       model: '' },
        custom:      { url: '',                                   key: '',       model: '' },
    };

    // Which presets to show per mode
    $: presetList = mode === 'agent'
        ? ['anthropic', 'ollama', 'openrouter', 'deepseek', 'zai', 'codex_cli', 'custom']
        : ['ollama', 'openrouter', 'deepseek', 'zai', 'custom'];

    // Preset label lookup
    const PRESET_LABELS = {
        anthropic:  'agents_extra.provider_preset_anthropic',
        ollama:     'agents_extra.provider_preset_ollama',
        openrouter: 'agents_extra.provider_preset_openrouter',
        deepseek:   'agents_extra.provider_preset_deepseek',
        zai:        'agents_extra.provider_preset_zai',
        codex_cli:  'agents_extra.provider_preset_codex_cli',
        custom:     'agents_extra.provider_preset_custom',
    };

    // Which fields each preset needs
    $: showUrlAndKey = providerPreset === 'ollama' || providerPreset === 'custom';
    $: showKeyOnly = providerPreset === 'openrouter' || providerPreset === 'deepseek' || providerPreset === 'zai';
    $: showModel = providerPreset !== 'anthropic';
    $: refActive = mode === 'agent' && !!providerRef;

    export function applyPreset(preset) {
        providerPreset = preset;
        if (mode === 'agent') providerRef = '';
        const p = PRESETS[preset];
        if (p) {
            providerUrl = p.url;
            providerKey = p.key;
            providerModel = p.model;
        }
        dirty = true;
        dispatch('change');
    }

    export function selectGlobalProvider(id) {
        providerRef = id;
        if (id) {
            providerUrl = '';
            providerKey = '';
            providerModel = '';
            providerPreset = 'anthropic';
        }
        dirty = true;
        dispatch('change');
    }

    export function detectPreset(url) {
        if (!url) return 'anthropic';
        if (url === 'http://localhost:11434') return 'ollama';
        if (url === 'https://api.z.ai/api/anthropic') return 'zai';
        if (url === 'https://openrouter.ai/api') return 'openrouter';
        if (url === 'https://api.deepseek.com/anthropic') return 'deepseek';
        if (url === 'codex_cli') return 'codex_cli';
        return 'custom';
    }

    function markDirty() {
        dirty = true;
        dispatch('change');
    }

    // Preset-specific descriptions
    const PRESET_DESCS = {
        openrouter: 'agents_extra.openrouter_desc',
        codex_cli:  'agents_extra.codex_cli_desc',
        deepseek:   'agents_extra.deepseek_desc',
        zai:        'agents_extra.zai_desc',
    };

    // Preset-specific model hints
    const MODEL_HINTS = {
        openrouter: 'agents_extra.openrouter_model_examples',
        deepseek:   'agents_extra.deepseek_model_options',
        zai:        'agents_extra.zai_model_options',
        codex_cli:  'agents_extra.codex_cli_model_options',
    };

    // Model placeholder per preset
    const MODEL_PLACEHOLDERS = {
        openrouter: 'anthropic/claude-sonnet-4-5',
        deepseek:   'deepseek-chat',
        zai:        'glm-5.1',
        codex_cli:  'gpt-5.4 (default)',
        ollama:     '',
        custom:     '',
    };

    // Key placeholder per preset
    const KEY_PLACEHOLDERS = {
        openrouter: 'sk-or-...',
        deepseek:   'sk-...',
        zai:        'sk-...',
        ollama:     'ollama or your key',
        custom:     'API key',
    };
</script>

<div class="provider-config">
    <!-- Global provider name (global mode only) -->
    {#if mode === 'global'}
        <FormField label="Name" style="margin-bottom:0.75rem">
            <input type="text" class="form-input" bind:value={providerName} on:input={markDirty} placeholder="My provider" style="width:100%;max-width:320px">
        </FormField>
    {/if}

    <!-- Global provider ref dropdown (agent mode only) -->
    {#if mode === 'agent' && globalProviders.length > 0}
        <div style="margin-bottom:0.75rem">
            <FormField label={$_('agents_extra.global_provider_label')}>
                <select class="form-select" value={providerRef} on:change={(e) => selectGlobalProvider(e.target.value)} style="width:100%;max-width:320px">
                    <option value="">{$_('agents_extra.global_provider_none')}</option>
                    {#each globalProviders as gp}
                        <option value={gp.id}>{gp.name}{gp.provider_model ? ' · ' + gp.provider_model : ''}</option>
                    {/each}
                </select>
            </FormField>
        </div>
    {/if}

    <!-- Preset buttons + fields (dimmed when global ref is active) -->
    <div style="{refActive ? 'opacity:0.4;pointer-events:none' : ''}">
        <div style="display:flex;gap:0.4rem;flex-wrap:wrap">
            {#each presetList as preset}
                <button
                    class="btn btn-sm"
                    class:btn-primary={providerPreset === preset}
                    style={providerPreset !== preset ? 'background:var(--surface-3);color:var(--text-muted)' : ''}
                    on:click={() => applyPreset(preset)}
                >{$_(PRESET_LABELS[preset] || preset)}</button>
            {/each}
        </div>

        <!-- Preset description -->
        {#if PRESET_DESCS[providerPreset]}
            <div class="preset-desc">{$_(PRESET_DESCS[providerPreset])}</div>
        {/if}

        <!-- Provider fields -->
        {#if providerPreset !== 'anthropic'}
            <div class="provider-fields">
                {#if showUrlAndKey}
                    <FormField label={$_('agents_extra.base_url_label')}>
                        <input type="text" class="form-input" bind:value={providerUrl} on:input={markDirty} placeholder="http://localhost:11434" style="width:100%">
                    </FormField>
                {/if}

                {#if showUrlAndKey || showKeyOnly}
                    <FormField label={$_('agents_extra.api_key_label')}>
                        <input type="password" class="form-input" bind:value={providerKey} on:input={markDirty} placeholder={KEY_PLACEHOLDERS[providerPreset] || 'API key'} style="width:100%">
                    </FormField>
                {/if}

                {#if showModel}
                    <FormField label={$_('agents_extra.model_label')} hint={MODEL_HINTS[providerPreset] ? $_(MODEL_HINTS[providerPreset]) : ''}>
                        <input type="text" class="form-input" bind:value={providerModel} on:input={markDirty} placeholder={MODEL_PLACEHOLDERS[providerPreset] || ''} style="width:100%">
                    </FormField>
                {/if}
            </div>
        {/if}
    </div>
</div>

<style>
    .provider-config {
        padding: 1rem 1.5rem;
        background: var(--surface-2);
        border-radius: var(--radius-lg);
    }
    .preset-desc {
        margin-top: 0.75rem;
        padding: 0.6rem 0.75rem;
        background: var(--surface-1);
        border-radius: var(--radius-md);
        font-size: 0.78rem;
        color: var(--text-muted);
    }
    .provider-fields {
        display: flex;
        flex-direction: column;
        gap: 0.5rem;
        margin-top: 0.75rem;
    }
</style>
