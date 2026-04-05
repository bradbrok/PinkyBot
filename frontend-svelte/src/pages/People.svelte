<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let users = [];
    let stats = {};
    let selectedUser = null;
    let entries = [];
    let categories = {};
    let agents = [];
    let visibility = [];

    // Edit state
    let editingEntry = null;
    let editValue = '';
    let editConfidence = 0;

    // Add entry state
    let addMode = false;
    let addCategory = 'identity';
    let addKey = '';
    let addValue = '';
    let addConfidence = 0.8;

    const CATEGORY_ICONS = {
        identity: 'badge',
        communication: 'forum',
        preferences: 'tune',
        work: 'work',
        personal: 'person',
        patterns: 'schedule',
    };

    onMount(async () => {
        await Promise.all([loadUsers(), loadAgents()]);
    });

    async function loadUsers() {
        try {
            const data = await api('GET', '/user-profiles');
            users = data.users || [];
            stats = data.stats || {};
        } catch (e) { toast('Failed to load profiles', 'error'); }
    }

    async function loadAgents() {
        try {
            const data = await api('GET', '/agents');
            agents = (data.agents || []).map(a => a.name);
        } catch { }
    }

    async function selectUser(uid) {
        selectedUser = uid;
        addMode = false;
        editingEntry = null;
        await Promise.all([loadEntries(), loadVisibility()]);
    }

    async function loadEntries() {
        if (!selectedUser) return;
        try {
            const data = await api('GET', `/user-profiles/${selectedUser}`);
            entries = data.entries || [];
            categories = data.categories || {};
        } catch (e) { toast('Failed to load entries', 'error'); }
    }

    async function loadVisibility() {
        if (!selectedUser) return;
        try {
            const data = await api('GET', `/user-profiles/${selectedUser}/visibility`);
            visibility = data.agents || [];
        } catch { visibility = []; }
    }

    async function toggleVisibility(agentName, currentVisible) {
        try {
            await api('PUT', `/user-profiles/${selectedUser}/visibility/${agentName}`, { visible: !currentVisible });
            await loadVisibility();
            toast(`${agentName}: ${!currentVisible ? 'visible' : 'hidden'}`);
        } catch (e) { toast('Failed to update visibility', 'error'); }
    }

    function startEdit(entry) {
        editingEntry = entry.id;
        editValue = entry.value;
        editConfidence = entry.confidence;
    }

    async function saveEdit() {
        try {
            await api('PUT', `/user-profiles/entries/${editingEntry}`, {
                value: editValue,
                confidence: editConfidence,
            });
            editingEntry = null;
            await loadEntries();
            toast('Updated');
        } catch (e) { toast('Failed to update', 'error'); }
    }

    async function deleteEntry(id) {
        if (!confirm('Delete this entry?')) return;
        try {
            await api('DELETE', `/user-profiles/entries/${id}`);
            await loadEntries();
            toast('Deleted');
        } catch (e) { toast('Failed to delete', 'error'); }
    }

    async function addEntry() {
        if (!addKey.trim() || !addValue.trim()) return;
        try {
            await api('POST', `/user-profiles/${selectedUser}`, {
                category: addCategory,
                key: addKey.trim(),
                value: addValue.trim(),
                confidence: addConfidence,
                source: 'manual',
            });
            addMode = false;
            addKey = '';
            addValue = '';
            addConfidence = 0.8;
            await loadEntries();
            toast('Entry added');
        } catch (e) { toast('Failed to add entry', 'error'); }
    }

    let deleteModalOpen = false;

    async function confirmDeleteUser() {
        try {
            await api('DELETE', `/user-profiles/${selectedUser}`);
            deleteModalOpen = false;
            selectedUser = null;
            entries = [];
            await loadUsers();
            toast('Profile deleted');
        } catch (e) { toast('Failed to delete profile', 'error'); }
    }

    function confidenceLabel(c) {
        if (c >= 0.8) return 'high';
        if (c >= 0.5) return 'med';
        return 'low';
    }

    function confidenceColor(c) {
        if (c >= 0.8) return 'var(--accent, #7c6af7)';
        if (c >= 0.5) return 'var(--accent2, #f7c56a)';
        return 'var(--gray-mid, #999)';
    }

    function formatDate(ts) {
        if (!ts) return '--';
        return new Date(ts * 1000).toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
    }

    // Group entries by category
    $: groupedEntries = Object.entries(
        entries.reduce((acc, e) => {
            (acc[e.category] = acc[e.category] || []).push(e);
            return acc;
        }, {})
    ).sort(([a], [b]) => {
        const order = ['identity', 'communication', 'preferences', 'work', 'personal', 'patterns'];
        return order.indexOf(a) - order.indexOf(b);
    });

    $: displayName = users.find(u => u.chat_id === selectedUser)?.display_name || selectedUser;
</script>

<div class="page">
    <div class="page-header">
        <h1>
            <span class="material-symbols-outlined" style="font-size:1.3rem;vertical-align:middle;margin-right:0.4rem">people</span>
            People
        </h1>
        <div class="stats-row">
            <span class="stat">{stats.total_users || 0} users</span>
            <span class="stat">{stats.total_entries || 0} traits</span>
        </div>
    </div>

    <div class="content-layout">
        <!-- User list -->
        <div class="user-list">
            <div class="list-header">Users</div>
            {#if users.length === 0}
                <div class="empty-state">
                    <span class="material-symbols-outlined" style="font-size:2rem;color:var(--gray-mid)">person_off</span>
                    <p>No learned profiles yet.</p>
                    <p style="font-size:0.75rem;color:var(--gray-mid)">Profiles are built automatically from dream consolidation, or you can add entries manually.</p>
                </div>
            {:else}
                {#each users as u}
                    <button
                        class="user-item"
                        class:active={selectedUser === u.chat_id}
                        on:click={() => selectUser(u.chat_id)}
                    >
                        <span class="material-symbols-outlined user-icon">person</span>
                        <div class="user-info">
                            <div class="user-name">{u.display_name}</div>
                            <div class="user-meta">{u.entry_count} traits &middot; {u.categories.length} categories</div>
                        </div>
                    </button>
                {/each}
            {/if}
        </div>

        <!-- Profile detail -->
        <div class="profile-detail">
            {#if !selectedUser}
                <div class="empty-state">
                    <span class="material-symbols-outlined" style="font-size:3rem;color:var(--gray-mid)">psychology</span>
                    <p>Select a user to view their learned profile</p>
                </div>
            {:else}
                <div class="detail-header">
                    <div>
                        <h2>{displayName}</h2>
                        <span class="chat-id">{selectedUser}</span>
                    </div>
                    <div class="detail-actions">
                        <button class="btn btn-sm" on:click={() => { addMode = !addMode; }}>
                            <span class="material-symbols-outlined" style="font-size:0.9rem">add</span>
                            Add trait
                        </button>
                        <button class="btn btn-sm btn-danger" on:click={() => { deleteModalOpen = true; }}>
                            <span class="material-symbols-outlined" style="font-size:0.9rem">delete</span>
                            Delete profile
                        </button>
                    </div>
                </div>

                <!-- Add form -->
                {#if addMode}
                    <div class="add-form">
                        <select bind:value={addCategory}>
                            {#each Object.keys(CATEGORY_ICONS) as cat}
                                <option value={cat}>{cat}</option>
                            {/each}
                        </select>
                        <input type="text" bind:value={addKey} placeholder="Trait name (e.g. timezone)">
                        <input type="text" bind:value={addValue} placeholder="Value (e.g. America/Los_Angeles)">
                        <input type="range" min="0" max="1" step="0.1" bind:value={addConfidence} style="width:80px" title="Confidence: {addConfidence}">
                        <button class="btn btn-sm btn-primary" on:click={addEntry}>Save</button>
                        <button class="btn btn-sm" on:click={() => { addMode = false; }}>Cancel</button>
                    </div>
                {/if}

                <!-- Entries by category -->
                {#each groupedEntries as [category, catEntries]}
                    <div class="category-section">
                        <div class="category-header">
                            <span class="material-symbols-outlined" style="font-size:1rem">{CATEGORY_ICONS[category] || 'label'}</span>
                            <span class="category-name">{category}</span>
                            <span class="category-desc">{categories[category] || ''}</span>
                        </div>
                        <div class="entries">
                            {#each catEntries as entry}
                                <div class="entry-row">
                                    {#if editingEntry === entry.id}
                                        <div class="entry-edit">
                                            <span class="entry-key">{entry.key}</span>
                                            <input type="text" bind:value={editValue} class="edit-input">
                                            <input type="range" min="0" max="1" step="0.1" bind:value={editConfidence} style="width:60px" title="Confidence">
                                            <button class="btn btn-xs btn-primary" on:click={saveEdit}>Save</button>
                                            <button class="btn btn-xs" on:click={() => { editingEntry = null; }}>Cancel</button>
                                        </div>
                                    {:else}
                                        <div class="entry-content">
                                            <span class="entry-key">{entry.key}</span>
                                            <span class="entry-value">{entry.value}</span>
                                        </div>
                                        <div class="entry-meta">
                                            <span class="confidence-badge" style="color:{confidenceColor(entry.confidence)}">{confidenceLabel(entry.confidence)}</span>
                                            <span class="source-badge">{entry.source}</span>
                                            <span class="entry-date">{formatDate(entry.updated_at)}</span>
                                            <button class="btn-icon" on:click={() => startEdit(entry)} title="Edit">
                                                <span class="material-symbols-outlined" style="font-size:0.85rem">edit</span>
                                            </button>
                                            <button class="btn-icon" on:click={() => deleteEntry(entry.id)} title="Delete">
                                                <span class="material-symbols-outlined" style="font-size:0.85rem">delete</span>
                                            </button>
                                        </div>
                                    {/if}
                                </div>
                            {/each}
                        </div>
                    </div>
                {/each}

                <!-- Visibility -->
                {#if agents.length > 0}
                    <div class="category-section">
                        <div class="category-header">
                            <span class="material-symbols-outlined" style="font-size:1rem">visibility</span>
                            <span class="category-name">Agent Visibility</span>
                            <span class="category-desc">Which agents can see this user's profile</span>
                        </div>
                        <div class="visibility-grid">
                            {#each visibility as v}
                                <button
                                    class="visibility-chip"
                                    class:visible={v.visible}
                                    on:click={() => toggleVisibility(v.agent_name, v.visible)}
                                >
                                    <span class="material-symbols-outlined" style="font-size:0.85rem">
                                        {v.visible ? 'visibility' : 'visibility_off'}
                                    </span>
                                    {v.agent_name}
                                </button>
                            {/each}
                        </div>
                    </div>
                {/if}
            {/if}
        </div>
    </div>
</div>

<!-- Delete confirmation modal -->
{#if deleteModalOpen}
    <div class="modal-overlay" on:click={() => { deleteModalOpen = false; }}>
        <div class="modal-box" on:click|stopPropagation>
            <div class="modal-icon">
                <span class="material-symbols-outlined" style="font-size:2rem;color:#f87171">delete_forever</span>
            </div>
            <h3>Delete profile for {displayName}?</h3>
            <p>This will permanently remove all {entries.length} learned traits. This action cannot be undone.</p>
            <div class="modal-actions">
                <button class="btn btn-sm" on:click={() => { deleteModalOpen = false; }}>Cancel</button>
                <button class="btn btn-sm btn-danger" on:click={confirmDeleteUser}>Delete profile</button>
            </div>
        </div>
    </div>
{/if}

<style>
    .page { padding: 1.5rem; max-width: 1200px; margin: 0 auto; color: var(--text-primary); }
    .page-header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 1.5rem; }
    .page-header h1 { font-size: 1.3rem; font-weight: 600; font-family: var(--font-grotesk); display: flex; align-items: center; }
    .stats-row { display: flex; gap: 1rem; }
    .stat { font-family: var(--font-grotesk); font-size: 0.8rem; color: var(--text-muted); padding: 0.3rem 0.6rem; background: var(--surface-2); border-radius: 6px; }

    .content-layout { display: grid; grid-template-columns: 240px 1fr; gap: 1.5rem; min-height: 500px; }
    @media (max-width: 768px) { .content-layout { grid-template-columns: 1fr; } }

    /* User list */
    .user-list { background: var(--surface-1); border-radius: 10px; padding: 0.5rem; overflow-y: auto; max-height: 80vh; }
    .list-header { font-family: var(--font-grotesk); font-size: 0.72rem; text-transform: uppercase; letter-spacing: 0.1em; color: var(--text-muted); padding: 0.6rem 0.75rem 0.4rem; }
    .user-item { display: flex; align-items: center; gap: 0.6rem; width: 100%; padding: 0.6rem 0.75rem; border: none; background: transparent; color: var(--text-primary); cursor: pointer; border-radius: 8px; text-align: left; transition: background 0.15s; font-family: var(--font-grotesk); }
    .user-item:hover { background: var(--hover-soft); }
    .user-item.active { background: var(--accent); color: var(--accent-contrast); }
    .user-item.active .user-meta { color: var(--accent-contrast); opacity: 0.7; }
    .user-icon { font-size: 1.2rem; color: var(--text-muted); }
    .user-item.active .user-icon { color: var(--accent-contrast); }
    .user-name { font-size: 0.88rem; font-weight: 500; }
    .user-meta { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.1rem; }

    .empty-state { padding: 2rem; text-align: center; color: var(--text-muted); font-family: var(--font-grotesk); display: flex; flex-direction: column; align-items: center; gap: 0.5rem; }
    .empty-state p { font-size: 0.85rem; }

    /* Profile detail */
    .profile-detail { min-height: 400px; }
    .detail-header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 1.25rem; padding-bottom: 1rem; border-bottom: 1px solid var(--surface-3); }
    .detail-header h2 { font-size: 1.2rem; font-weight: 600; font-family: var(--font-grotesk); color: var(--text-primary); }
    .chat-id { font-family: var(--font-grotesk); font-size: 0.72rem; color: var(--text-muted); }
    .detail-actions { display: flex; gap: 0.4rem; }

    /* Add form */
    .add-form { display: flex; gap: 0.5rem; align-items: center; flex-wrap: wrap; padding: 0.75rem; background: var(--surface-2); border-radius: 8px; margin-bottom: 1rem; font-family: var(--font-grotesk); font-size: 0.82rem; }
    .add-form select, .add-form input[type="text"] { font-family: var(--font-grotesk); font-size: 0.82rem; padding: 0.35rem 0.5rem; border: 1px solid var(--surface-3); border-radius: 6px; background: var(--input-bg, var(--surface-1)); color: var(--text-primary); }
    .add-form select { width: 120px; }
    .add-form input[type="text"] { flex: 1; min-width: 100px; }

    /* Categories */
    .category-section { margin-bottom: 1.5rem; }
    .category-header { display: flex; align-items: center; gap: 0.5rem; padding-bottom: 0.5rem; border-bottom: 1px solid var(--surface-3); margin-bottom: 0.5rem; }
    .category-name { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 600; text-transform: capitalize; color: var(--text-primary); }
    .category-desc { font-size: 0.72rem; color: var(--text-muted); }

    /* Entries */
    .entries { display: flex; flex-direction: column; }
    .entry-row { display: flex; justify-content: space-between; align-items: center; padding: 0.45rem 0.5rem; border-radius: 6px; transition: background 0.1s; gap: 0.5rem; }
    .entry-row:hover { background: var(--hover-soft); }
    .entry-content { display: flex; gap: 0.5rem; flex: 1; align-items: baseline; min-width: 0; }
    .entry-key { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 500; color: var(--text-primary); min-width: 120px; flex-shrink: 0; }
    .entry-value { font-size: 0.82rem; color: var(--text-secondary); word-break: break-word; }
    .entry-meta { display: flex; align-items: center; gap: 0.4rem; flex-shrink: 0; }
    .confidence-badge { font-family: var(--font-grotesk); font-size: 0.68rem; font-weight: 600; text-transform: uppercase; letter-spacing: 0.05em; }
    .source-badge { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); padding: 0.15rem 0.4rem; background: var(--surface-2); border-radius: 4px; }
    .entry-date { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); }

    .entry-edit { display: flex; align-items: center; gap: 0.4rem; flex: 1; }
    .edit-input { font-family: var(--font-grotesk); font-size: 0.82rem; padding: 0.3rem 0.5rem; border: 1px solid var(--accent); border-radius: 6px; flex: 1; background: var(--input-bg, var(--surface-1)); color: var(--text-primary); }

    .btn-icon { background: none; border: none; cursor: pointer; color: var(--text-muted); padding: 0.2rem; border-radius: 4px; display: flex; align-items: center; transition: color 0.15s; }
    .btn-icon:hover { color: var(--text-primary); }

    /* Visibility */
    .visibility-grid { display: flex; flex-wrap: wrap; gap: 0.4rem; padding: 0.5rem 0; }
    .visibility-chip { display: flex; align-items: center; gap: 0.3rem; padding: 0.35rem 0.65rem; border-radius: 999px; font-family: var(--font-grotesk); font-size: 0.78rem; cursor: pointer; border: 1px solid var(--surface-3); background: var(--surface-2); color: var(--text-muted); transition: all 0.15s; }
    .visibility-chip.visible { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }

    /* Buttons */
    .btn { font-family: var(--font-grotesk); font-size: 0.8rem; padding: 0.4rem 0.8rem; border: 1px solid var(--surface-3); border-radius: 6px; background: var(--surface-2); color: var(--text-primary); cursor: pointer; display: flex; align-items: center; gap: 0.3rem; transition: all 0.15s; }
    .btn:hover { border-color: var(--accent); }
    .btn-sm { font-size: 0.75rem; padding: 0.3rem 0.6rem; }
    .btn-xs { font-size: 0.7rem; padding: 0.2rem 0.5rem; }
    .btn-primary { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }
    .btn-danger { color: #f87171; border-color: #f87171; }
    .btn-danger:hover { background: #f87171; color: var(--surface-1); }

    /* Modal */
    .modal-overlay { position: fixed; inset: 0; background: var(--overlay-scrim, rgba(0,0,0,0.5)); display: flex; align-items: center; justify-content: center; z-index: 1000; }
    .modal-box { background: var(--surface-1); border-radius: 12px; padding: 2rem; max-width: 400px; width: 90%; text-align: center; box-shadow: 0 8px 32px var(--shadow-color, rgba(0,0,0,0.2)); }
    .modal-icon { margin-bottom: 1rem; }
    .modal-box h3 { font-family: var(--font-grotesk); font-size: 1.05rem; font-weight: 600; color: var(--text-primary); margin-bottom: 0.5rem; }
    .modal-box p { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 1.5rem; line-height: 1.5; }
    .modal-actions { display: flex; justify-content: center; gap: 0.75rem; }
</style>
