<script>
    /**
     * ProviderConfig — unified provider configuration form.
     *
     * Used in:
     *   - Settings (global provider add/edit) — mode="global"
     *   - Agents (per-agent provider config) — mode="agent"
     *
     * Agent mode: single dropdown of configured providers + "Anthropic (Default)" + "Custom..."
     * Global mode: preset buttons + name field for creating/editing providers
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

    // Global mode: which presets to show as buttons
    const GLOBAL_PRESETS = ['ollama', 'openrouter', 'deepseek', 'zai', 'custom'];

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

    // Agent mode: derive the selected value for the unified dropdown
    // "anthropic" = default, provider ID = global provider, "custom" = manual config
    $: agentSelection = providerRef ? providerRef : (providerUrl ? 'custom' : 'anthropic');
    $: isCustomAgent = mode === 'agent' && agentSelection === 'custom';

    function handleAgentSelect(value) {
        if (value === 'anthropic') {
            // Anthropic default — clear everything
            providerRef = '';
            providerUrl = '';
            providerKey = '';
            providerModel = '';
            providerPreset = 'anthropic';
        } else if (value === 'custom') {
            // Custom — clear ref, let user fill in fields
            providerRef = '';
            providerPreset = 'custom';
            providerUrl = '';
            providerKey = '';
            providerModel = '';
        } else {
            // Global provider selected by ID
            providerRef = value;
            providerUrl = '';
            providerKey = '';
            providerModel = '';
            providerPreset = 'anthropic';
        }
        dirty = true;
        dispatch('change');
    }

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
        if (mode === 'agent') {
            handleAgentSelect(id || 'anthropic');
        }
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
    {#if mode === 'agent'}
        <!-- Agent mode: single dropdown of providers -->
        <FormField label={$_('agents_extra.global_provider_label')}>
            <select class="form-select" value={agentSelection} on:change={(e) => handleAgentSelect(e.target.value)} style="width:100%;max-width:400px">
                <option value="anthropic">{$_('settings.default_provider_none')}</option>
                {#each globalProviders as gp}
                    <option value={gp.id}>{gp.name}{gp.provider_model ? ' · ' + gp.provider_model : ''}</option>
                {/each}
                <option value="custom">{$_('agents_extra.provider_preset_custom')}</option>
            </select>
        </FormField>

        <!-- Custom fields (only when "Custom..." is selected) -->
        {#if isCustomAgent}
            <div class="provider-fields">
                <FormField label={$_('agents_extra.base_url_label')}>
                    <input type="text" class="form-input" bind:value={providerUrl} on:input={markDirty} placeholder="http://localhost:11434" style="width:100%">
                </FormField>
                <FormField label={$_('agents_extra.api_key_label')}>
                    <input type="password" class="form-input" bind:value={providerKey} on:input={markDirty} placeholder="API key" style="width:100%">
                </FormField>
                <FormField label={$_('agents_extra.model_label')}>
                    <input type="text" class="form-input" bind:value={providerModel} on:input={markDirty} placeholder="model name" style="width:100%">
                </FormField>
            </div>
        {/if}
    {:else}
        <!-- Global mode: name field + preset buttons -->
        <FormField label="Name" style="margin-bottom:0.75rem">
            <input type="text" class="form-input" bind:value={providerName} on:input={markDirty} placeholder="My provider" style="width:100%;max-width:320px">
        </FormField>

        <div style="display:flex;gap:0.4rem;flex-wrap:wrap">
            {#each GLOBAL_PRESETS as preset}
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
    {/if}
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
