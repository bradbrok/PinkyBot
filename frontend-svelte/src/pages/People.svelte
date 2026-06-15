<script>
    import { onMount } from 'svelte';
    import { api } from '../lib/api.js';
    import { toast } from '../lib/stores.js';
    import Modal from '../components/Modal.svelte';

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

    // Relationship state
    let relationships = [];
    let reverseRelationships = [];
    let addRelMode = false;
    let addRelName = '';
    let addRelType = 'friend';
    let addRelContext = '';
    let addRelChatId = '';
    let addRelLinkedProfile = '';  // selected from existing profiles dropdown

    // Pending flags to prevent double-submit
    let savingEntry = false;
    let savingEdit = false;
    let savingRel = false;
    let deletingUser = false;
    let deletingEntries = new Set();
    let deletingRelationships = new Set();
    let togglingVisibility = new Set();

    function setPending(setValue, key, pending) {
        const next = new Set(setValue);
        if (pending) next.add(key);
        else next.delete(key);
        return next;
    }

    function visibilityPendingKey(uid, agentName) {
        return `${uid || ''}:${agentName}`;
    }

    const REL_TYPES = [
        'wife', 'husband', 'partner', 'friend', 'collaborator', 'colleague',
        'manager', 'child', 'parent', 'sibling', 'AI agent', 'other',
    ];

    const CATEGORY_ICONS = {
        identity: 'badge',
        communication: 'forum',
        preferences: 'tune',
        work: 'work',
        personal: 'person',
        patterns: 'schedule',
        relationships: 'group',
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
        addRelMode = false;
        editingEntry = null;
        await Promise.all([loadEntries(uid), loadVisibility(uid), loadRelationships(uid)]);
    }

    async function loadEntries(uid = selectedUser) {
        if (!uid) return;
        try {
            const data = await api('GET', `/user-profiles/${uid}`);
            if (selectedUser !== uid) return;
            entries = data.entries || [];
            categories = data.categories || {};
        } catch (e) { if (selectedUser === uid) toast('Failed to load entries', 'error'); }
    }

    async function loadVisibility(uid = selectedUser) {
        if (!uid) return;
        try {
            const data = await api('GET', `/user-profiles/${uid}/visibility`);
            if (selectedUser !== uid) return;
            visibility = data.agents || [];
        } catch { if (selectedUser === uid) visibility = []; }
    }

    async function loadRelationships(uid = selectedUser) {
        if (!uid) return;
        try {
            const data = await api('GET', `/user-profiles/${uid}/relationships`);
            if (selectedUser !== uid) return;
            relationships = data.relationships || [];
            reverseRelationships = data.reverse_relationships || [];
        } catch { if (selectedUser === uid) { relationships = []; reverseRelationships = []; } }
    }

    function onLinkedProfileChange() {
        if (addRelLinkedProfile) {
            const u = users.find(x => x.chat_id === addRelLinkedProfile);
            if (u) {
                addRelName = u.display_name;
                addRelChatId = u.chat_id;
            }
        } else {
            addRelChatId = '';
            addRelName = '';
        }
    }

    async function addRelationship() {
        if (!addRelName.trim() || !addRelType) return;
        if (savingRel) return;
        savingRel = true;
        try {
            await api('POST', `/user-profiles/${selectedUser}/relationships`, {
                to_display_name: addRelName.trim(),
                to_chat_id: addRelChatId.trim(),
                relation: addRelType,
                context: addRelContext.trim(),
            });
            addRelMode = false;
            addRelName = '';
            addRelType = 'friend';
            addRelContext = '';
            addRelChatId = '';
            addRelLinkedProfile = '';
            await loadRelationships();
            toast('Relationship added');
        } catch (e) { toast('Failed to add relationship', 'error'); }
        finally { savingRel = false; }
    }

    async function deleteRelationship(relId) {
        if (deletingRelationships.has(relId)) return;
        if (!confirm('Remove this relationship?')) return;
        deletingRelationships = setPending(deletingRelationships, relId, true);
        try {
            await api('DELETE', `/user-profiles/relationships/${relId}`);
            await loadRelationships();
            toast('Relationship removed');
        } catch (e) { toast('Failed to delete', 'error'); }
        finally { deletingRelationships = setPending(deletingRelationships, relId, false); }
    }

    async function toggleVisibility(agentName, currentVisible) {
        const uid = selectedUser;
        const pendingKey = visibilityPendingKey(uid, agentName);
        if (!uid || togglingVisibility.has(pendingKey)) return;
        togglingVisibility = setPending(togglingVisibility, pendingKey, true);
        try {
            await api('PUT', `/user-profiles/${uid}/visibility/${agentName}`, { visible: !currentVisible });
            await loadVisibility(uid);
            toast(`${agentName}: ${!currentVisible ? 'visible' : 'hidden'}`);
        } catch (e) { toast('Failed to update visibility', 'error'); }
        finally { togglingVisibility = setPending(togglingVisibility, pendingKey, false); }
    }

    function startEdit(entry) {
        editingEntry = entry.id;
        editValue = entry.value;
        editConfidence = entry.confidence;
    }

    async function saveEdit() {
        if (savingEdit) return;
        const value = String(editValue ?? '').trim();
        if (!value) {
            toast('Value is required', 'error');
            return;
        }
        savingEdit = true;
        try {
            await api('PUT', `/user-profiles/entries/${editingEntry}`, {
                value,
                confidence: editConfidence,
            });
            editingEntry = null;
            await loadEntries();
            toast('Updated');
        } catch (e) { toast('Failed to update', 'error'); }
        finally { savingEdit = false; }
    }

    async function deleteEntry(id) {
        if (deletingEntries.has(id)) return;
        if (!confirm('Delete this entry?')) return;
        deletingEntries = setPending(deletingEntries, id, true);
        try {
            await api('DELETE', `/user-profiles/entries/${id}`);
            await loadEntries();
            toast('Deleted');
        } catch (e) { toast('Failed to delete', 'error'); }
        finally { deletingEntries = setPending(deletingEntries, id, false); }
    }

    async function addEntry() {
        if (!addKey.trim() || !addValue.trim()) return;
        if (savingEntry) return;
        savingEntry = true;
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
        finally { savingEntry = false; }
    }

    let deleteModalOpen = false;

    async function confirmDeleteUser() {
        if (deletingUser) return;
        deletingUser = true;
        try {
            await api('DELETE', `/user-profiles/${selectedUser}`);
            deleteModalOpen = false;
            selectedUser = null;
            entries = [];
            await loadUsers();
            toast('Profile deleted');
        } catch (e) { toast('Failed to delete profile', 'error'); }
        finally { deletingUser = false; }
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

    function formatTimestamp(ts) {
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
        const order = ['identity', 'communication', 'preferences', 'work', 'personal', 'patterns', 'relationships'];
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
                        <input type="text" bind:value={addKey} placeholder="Trait name (e.g. timezone)" aria-label="Trait name" on:keydown={e => { if (e.key === 'Enter') addEntry(); }}>
                        <input type="text" bind:value={addValue} placeholder="Value (e.g. America/Los_Angeles)" aria-label="Trait value" on:keydown={e => { if (e.key === 'Enter') addEntry(); }}>
                        <input type="range" min="0" max="1" step="0.1" bind:value={addConfidence} style="width:80px" title="Confidence: {addConfidence}" aria-label="Confidence">
                        <button class="btn btn-sm btn-primary" on:click={addEntry} disabled={savingEntry}>Save</button>
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
                                            <button class="btn btn-xs btn-primary" on:click={saveEdit} disabled={savingEdit}>Save</button>
                                            <button class="btn btn-xs" on:click={() => { editingEntry = null; }} disabled={savingEdit}>Cancel</button>
                                        </div>
                                    {:else}
                                        <div class="entry-content">
                                            <span class="entry-key">{entry.key}</span>
                                            <span class="entry-value">{entry.value}</span>
                                        </div>
                                        <div class="entry-meta">
                                            <span class="confidence-badge" style="color:{confidenceColor(entry.confidence)}">{confidenceLabel(entry.confidence)}</span>
                                            <span class="source-badge">{entry.source}</span>
                                            <span class="entry-date">{formatTimestamp(entry.updated_at)}</span>
                                            <button class="btn-icon" on:click={() => startEdit(entry)} title="Edit">
                                                <span class="material-symbols-outlined" style="font-size:0.85rem">edit</span>
                                            </button>
                                            <button class="btn-icon" on:click={() => deleteEntry(entry.id)} disabled={deletingEntries.has(entry.id)} title="Delete">
                                                <span class="material-symbols-outlined" style="font-size:0.85rem">delete</span>
                                            </button>
                                        </div>
                                    {/if}
                                </div>
                            {/each}
                        </div>
                    </div>
                {/each}

                <!-- Social Circle -->
                <div class="category-section">
                    <div class="category-header">
                        <span class="material-symbols-outlined" style="font-size:1rem">group</span>
                        <span class="category-name">Social Circle</span>
                        <span class="category-desc">Relationships and connections</span>
                        <button class="btn btn-xs" style="margin-left:auto" on:click={() => { addRelMode = !addRelMode; }}>
                            <span class="material-symbols-outlined" style="font-size:0.8rem">add</span>
                            Add
                        </button>
                    </div>

                    {#if addRelMode}
                        <div class="add-form">
                            <select bind:value={addRelLinkedProfile} on:change={onLinkedProfileChange}>
                                <option value="">— custom name —</option>
                                {#each users.filter(u => u.chat_id !== selectedUser) as u}
                                    <option value={u.chat_id}>{u.display_name}</option>
                                {/each}
                            </select>
                            {#if !addRelLinkedProfile}
                                <input type="text" bind:value={addRelName} placeholder="Person's name" aria-label="Person's name" on:keydown={e => { if (e.key === 'Enter') addRelationship(); }}>
                            {/if}
                            <select bind:value={addRelType}>
                                {#each REL_TYPES as rt}
                                    <option value={rt}>{rt}</option>
                                {/each}
                            </select>
                            <input type="text" bind:value={addRelContext} placeholder="Context (optional)" style="flex:1" aria-label="Relationship context" on:keydown={e => { if (e.key === 'Enter') addRelationship(); }}>
                            <button class="btn btn-xs btn-primary" on:click={addRelationship} disabled={savingRel}>Save</button>
                            <button class="btn btn-xs" on:click={() => { addRelMode = false; }}>Cancel</button>
                        </div>
                    {/if}

                    {#if relationships.length === 0 && reverseRelationships.length === 0 && !addRelMode}
                        <div class="rel-empty">No known relationships</div>
                    {:else}
                        <div class="rel-list">
                            {#each relationships as rel}
                                {@const linkedProfile = users.find(u => u.chat_id === rel.to_chat_id)}
                                <div class="rel-row">
                                    <div class="rel-info">
                                        {#if linkedProfile}
                                            <button class="rel-name rel-name-link" on:click={() => selectUser(rel.to_chat_id)}>
                                                {rel.to_display_name}
                                            </button>
                                            <span class="rel-linked-badge">
                                                <span class="material-symbols-outlined" style="font-size:0.7rem">link</span>
                                                linked
                                            </span>
                                        {:else}
                                            <span class="rel-name">{rel.to_display_name}</span>
                                        {/if}
                                        <span class="rel-type">{rel.relation}</span>
                                    </div>
                                    <div class="rel-meta">
                                        {#if rel.context}
                                            <span class="rel-context">{rel.context}</span>
                                        {/if}
                                        <span class="confidence-badge" style="color:{confidenceColor(rel.confidence)}">{confidenceLabel(rel.confidence)}</span>
                                        <button class="btn-icon" on:click={() => deleteRelationship(rel.id)} disabled={deletingRelationships.has(rel.id)} title="Remove">
                                            <span class="material-symbols-outlined" style="font-size:0.85rem">close</span>
                                        </button>
                                    </div>
                                </div>
                            {/each}
                            {#each reverseRelationships as rel}
                                {@const fromProfile = users.find(u => u.chat_id === rel.from_chat_id)}
                                <div class="rel-row rel-reverse">
                                    <div class="rel-info">
                                        <span class="rel-arrow">←</span>
                                        {#if fromProfile}
                                            <button class="rel-name rel-name-link" on:click={() => selectUser(rel.from_chat_id)}>
                                                {fromProfile.display_name}
                                            </button>
                                        {:else}
                                            <span class="rel-name">{rel.from_chat_id}</span>
                                        {/if}
                                        <span class="rel-type">{rel.relation}</span>
                                    </div>
                                    <div class="rel-meta">
                                        <span class="rel-context" style="font-style:italic">reverse link</span>
                                    </div>
                                </div>
                            {/each}
                        </div>
                    {/if}
                </div>

                <!-- Visibility -->
                {#if visibility.length > 0}
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
                                    disabled={togglingVisibility.has(visibilityPendingKey(selectedUser, v.agent_name))}
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
    <Modal bind:show={deleteModalOpen} title="Delete profile" maxWidth="400px">
        <div class="modal-icon" style="text-align:center">
            <span class="material-symbols-outlined" style="font-size:2rem;color:var(--danger-outline)">delete_forever</span>
        </div>
        <h3 style="text-align:center">Delete profile for {displayName}?</h3>
        <p style="text-align:center">This will permanently remove all {entries.length} learned traits. This action cannot be undone.</p>
        <div slot="footer">
            <button class="btn btn-sm" on:click={() => { deleteModalOpen = false; }}>Cancel</button>
            <button class="btn btn-sm btn-danger" on:click={confirmDeleteUser} disabled={deletingUser}>Delete profile</button>
        </div>
    </Modal>
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

    /* Relationships */
    .rel-empty { font-family: var(--font-grotesk); font-size: 0.8rem; color: var(--text-muted); padding: 0.75rem 0.5rem; }
    .rel-list { display: flex; flex-direction: column; }
    .rel-row { display: flex; justify-content: space-between; align-items: center; padding: 0.45rem 0.5rem; border-radius: 6px; transition: background 0.1s; gap: 0.5rem; }
    .rel-row:hover { background: var(--hover-soft); }
    .rel-row.rel-reverse { opacity: 0.6; }
    .rel-info { display: flex; align-items: center; gap: 0.5rem; }
    .rel-name { font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 500; color: var(--text-primary); }
    .rel-type { font-family: var(--font-grotesk); font-size: 0.72rem; padding: 0.15rem 0.5rem; background: var(--surface-2); border-radius: 999px; color: var(--text-muted); }
    .rel-arrow { font-size: 0.8rem; color: var(--text-muted); }
    .rel-name-link { background: none; border: none; cursor: pointer; color: var(--accent); font-family: var(--font-grotesk); font-size: 0.85rem; font-weight: 500; padding: 0; text-decoration: none; }
    .rel-name-link:hover { text-decoration: underline; }
    .rel-linked-badge { display: inline-flex; align-items: center; gap: 0.15rem; font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--accent); background: var(--surface-2); padding: 0.1rem 0.4rem; border-radius: 999px; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600; }
    .rel-meta { display: flex; align-items: center; gap: 0.4rem; }
    .rel-context { font-family: var(--font-grotesk); font-size: 0.68rem; color: var(--text-muted); max-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }

    /* Buttons */
    .btn { font-family: var(--font-grotesk); font-size: 0.8rem; padding: 0.4rem 0.8rem; border: 1px solid var(--surface-3); border-radius: 6px; background: var(--surface-2); color: var(--text-primary); cursor: pointer; display: flex; align-items: center; gap: 0.3rem; transition: all 0.15s; }
    .btn:hover { border-color: var(--accent); }
    .btn-sm { font-size: 0.75rem; padding: 0.3rem 0.6rem; }
    .btn-xs { font-size: 0.7rem; padding: 0.2rem 0.5rem; }
    .btn-primary { background: var(--accent); color: var(--accent-contrast); border-color: var(--accent); }
    .btn-danger { color: var(--danger-outline); border-color: var(--danger-outline); }
    .btn-danger:hover { background: var(--danger-outline); color: #fff; }

    /* Modal */
    .modal-icon { margin-bottom: 1rem; }
</style>
