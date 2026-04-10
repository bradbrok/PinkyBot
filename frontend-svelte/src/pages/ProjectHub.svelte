<script>
    import { onMount } from 'svelte';
    import { _ } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { timeAgo } from '../lib/utils.js';

    export let params = {};

    let hub = null;
    let loading = true;
    let error = '';

    // Team member form
    let newMemberName = '';
    let newMemberRole = '';
    let addingMember = false;

    // Asset form
    let newAssetTitle = '';
    let newAssetUrl = '';
    let newAssetType = 'link';
    let addingAsset = false;

    $: projectId = params.id;
    $: if (projectId) loadHub();

    async function loadHub() {
        loading = true;
        error = '';
        try {
            hub = await api('GET', `/projects/${projectId}/hub`);
        } catch (e) {
            error = e.message || 'Failed to load project hub';
        } finally {
            loading = false;
        }
    }

    function sprintPct(sprint) {
        if (sprint?.progress_pct != null) return sprint.progress_pct;
        if (!sprint?.task_counts?.total) return 0;
        return Math.round((sprint.task_counts.completed / sprint.task_counts.total) * 100);
    }

    function sprintTotal(sprint) {
        return sprint?.tasks_total ?? sprint?.task_counts?.total ?? 0;
    }

    function sprintCompleted(sprint) {
        return sprint?.tasks_completed ?? sprint?.task_counts?.completed ?? 0;
    }

    async function addTeamMember() {
        if (!newMemberName.trim()) return;
        addingMember = true;
        try {
            const res = await api('POST', `/projects/${projectId}/team`, {
                name: newMemberName.trim(),
                role: newMemberRole.trim(),
            });
            hub.project.team_members = res.team_members;
            newMemberName = '';
            newMemberRole = '';
        } catch (e) {
            error = e.message || 'Failed to add member';
        } finally {
            addingMember = false;
        }
    }

    async function removeTeamMember(index) {
        const name = hub.project.team_members?.[index]?.name || 'this member';
        if (!confirm(`Remove ${name} from the team?`)) return;
        try {
            const res = await api('DELETE', `/projects/${projectId}/team/${index}`);
            hub.project.team_members = res.team_members;
        } catch (e) {
            error = e.message || 'Failed to remove member';
        }
    }

    async function addLinkedAsset() {
        if (!newAssetTitle.trim()) return;
        addingAsset = true;
        try {
            const res = await api('POST', `/projects/${projectId}/assets`, {
                type: newAssetType,
                title: newAssetTitle.trim(),
                url: newAssetUrl.trim(),
            });
            hub.project.linked_assets = res.linked_assets;
            newAssetTitle = '';
            newAssetUrl = '';
            newAssetType = 'link';
        } catch (e) {
            error = e.message || 'Failed to add asset';
        } finally {
            addingAsset = false;
        }
    }

    async function removeLinkedAsset(index) {
        const title = hub.project.linked_assets?.[index]?.title || 'this asset';
        if (!confirm(`Remove "${title}"?`)) return;
        try {
            const res = await api('DELETE', `/projects/${projectId}/assets/${index}`);
            hub.project.linked_assets = res.linked_assets;
        } catch (e) {
            error = e.message || 'Failed to remove asset';
        }
    }

    const MILESTONE_BADGE = { completed: 'on', in_progress: 'running', pending: 'model', cancelled: 'off' };
    const TASK_BADGE = { completed: 'on', in_progress: 'running', pending: 'model', blocked: 'off', cancelled: 'closed' };
    const PRIORITY_COLOR = { urgent: 'var(--red)', high: 'var(--yellow)', normal: 'var(--text-secondary)', low: 'var(--text-muted)' };
    const ASSET_ICON = { doc: '📄', sheet: '📊', drive: '📁', link: '🔗', slide: '📑', video: '🎬', image: '🖼️', research: '🔬', presentation: '📊' };
    const ASSET_TYPES = ['link', 'doc', 'sheet', 'drive', 'slide', 'video', 'image', 'research', 'presentation'];
    function assetIcon(type) { return ASSET_ICON[type] || '🔗'; }
</script>

<div class="content">
    {#if loading}
        <div class="empty">{$_('project_hub.loading')}</div>
    {:else if error}
        <div class="empty" style="color: var(--red)">{error}</div>
    {:else if hub}
        {@const p = hub.project}
        {@const sprint = hub.active_sprint}
        {@const pct = sprintPct(sprint)}

        <!-- Project header -->
        <div class="section">
            <div class="section-header">
                <div>
                    <div class="section-title">{p.name}</div>
                    {#if p.description}
                        <div class="proj-desc">{p.description}</div>
                    {/if}
                </div>
                <div class="proj-meta-row">
                    {#if p.repo_url}
                        <a href={p.repo_url} target="_blank" rel="noopener noreferrer" class="btn btn-sm">Repo →</a>
                    {/if}
                    <a href="#/tasks" class="btn btn-sm">{$_('nav.tasks')} →</a>
                </div>
            </div>
        </div>

        <!-- Team Members -->
        <div class="section">
            <div class="section-header"><div class="section-title">Team</div></div>
            <div class="section-body">
                {#if p.team_members?.length}
                    <div class="member-chips">
                        {#each p.team_members as m, i}
                            <span class="member-chip">
                                {m.name}{m.role ? ' · ' + m.role : ''}
                                <button class="member-remove" on:click={() => removeTeamMember(i)} title="Remove">&times;</button>
                            </span>
                        {/each}
                    </div>
                {:else}
                    <div style="padding: 0.5rem 1rem; font-size: 0.8rem; color: var(--text-muted);">No team members yet.</div>
                {/if}
                <div class="add-member-row">
                    <input class="input-sm" type="text" placeholder="Name" bind:value={newMemberName} on:keydown={(e) => e.key === 'Enter' && addTeamMember()} />
                    <input class="input-sm" type="text" placeholder="Role (optional)" bind:value={newMemberRole} on:keydown={(e) => e.key === 'Enter' && addTeamMember()} />
                    <button class="btn btn-sm" on:click={addTeamMember} disabled={addingMember || !newMemberName.trim()}>+ Add</button>
                </div>
            </div>
        </div>

        <!-- Stats + Sprint -->
        <div class="hub-grid">
            <!-- Task stats -->
            <div class="section">
                <div class="section-header"><div class="section-title">{$_('nav.tasks')}</div></div>
                <div class="section-body">
                    <div class="stat-row">
                        <div class="hub-stat"><div class="hub-stat-val">{hub.task_stats?.total ?? 0}</div><div class="hub-stat-label">{$_('tasks.stat_total')}</div></div>
                        <div class="hub-stat"><div class="hub-stat-val" style="color:var(--green)">{hub.task_stats?.completed ?? 0}</div><div class="hub-stat-label">{$_('tasks.stat_done')}</div></div>
                        <div class="hub-stat"><div class="hub-stat-val" style="color:var(--yellow)">{hub.task_stats?.in_progress ?? 0}</div><div class="hub-stat-label">{$_('tasks.stat_active')}</div></div>
                        <div class="hub-stat"><div class="hub-stat-val" style="color:var(--text-muted)">{hub.task_stats?.pending ?? 0}</div><div class="hub-stat-label">{$_('tasks.stat_pending')}</div></div>
                        <div class="hub-stat"><div class="hub-stat-val" style="color:var(--red)">{hub.task_stats?.blocked ?? 0}</div><div class="hub-stat-label">{$_('tasks.stat_blocked')}</div></div>
                    </div>
                </div>
            </div>

            <!-- Active sprint -->
            <div class="section">
                <div class="section-header"><div class="section-title">{$_('project_hub.active_sprint')}</div></div>
                <div class="section-body">
                    {#if sprint}
                        <div class="sprint-name">{sprint.name}</div>
                        <div class="sprint-dates">{sprint.start_date || '?'} – {sprint.end_date || '?'}</div>
                        <div class="sprint-progress-wrap">
                            <div class="sprint-progress-fill" style="width:{pct}%; background: {pct >= 100 ? 'var(--green)' : pct >= 50 ? 'var(--yellow)' : 'var(--accent)'}"></div>
                        </div>
                        <div class="sprint-pct-label">{pct}% complete · {sprintCompleted(sprint)}/{sprintTotal(sprint)} tasks</div>
                    {:else}
                        <div class="empty">{$_('project_hub.no_sprint')}</div>
                    {/if}
                </div>
            </div>
        </div>

        <!-- Milestones -->
        {#if hub.milestones?.length}
        <div class="section">
            <div class="section-header"><div class="section-title">{$_('project_hub.milestones')}</div></div>
            <div class="section-body">
                <div class="milestone-list">
                    {#each hub.milestones as ms}
                        <div class="milestone-row">
                            <span class="badge badge-{MILESTONE_BADGE[ms.status] || 'model'}">{ms.status}</span>
                            <span class="milestone-name">{ms.name}</span>
                            <div class="milestone-progress">
                                <div class="milestone-progress-bar">
                                    <div class="milestone-progress-fill" style="width:{ms.progress_pct ?? 0}%; background: {(ms.progress_pct ?? 0) >= 100 ? 'var(--green)' : 'var(--yellow)'}"></div>
                                </div>
                                <span class="milestone-pct">{ms.progress_pct ?? 0}% · {ms.tasks_completed ?? 0}/{ms.task_count ?? 0}</span>
                            </div>
                            {#if ms.due_date}
                                <span class="milestone-due">{ms.due_date}</span>
                            {/if}
                        </div>
                    {/each}
                </div>
            </div>
        </div>
        {/if}

        <!-- Recent tasks -->
        {#if hub.recent_tasks?.length}
        <div class="section">
            <div class="section-header">
                <div class="section-title">{$_('project_hub.recent_tasks')}</div>
                <a href="#/tasks" class="btn btn-sm">{$_('common.view_all')} →</a>
            </div>
            <div class="section-body">
                <table class="data-table">
                    <thead><tr><th>#</th><th>{$_('tasks.col_title')}</th><th>{$_('dashboard.col_status')}</th><th>{$_('dashboard.col_agent')}</th><th>{$_('project_hub.col_updated')}</th></tr></thead>
                    <tbody>
                        {#each hub.recent_tasks as t}
                            <tr>
                                <td class="mono" style="font-size:0.75rem;color:var(--text-muted)">{t.id}</td>
                                <td>
                                    <span style="color:{PRIORITY_COLOR[t.priority] || 'var(--text-secondary)'}">·</span>
                                    {t.title}
                                </td>
                                <td><span class="badge badge-{TASK_BADGE[t.status] || 'model'}">{t.status}</span></td>
                                <td class="mono" style="font-size:0.75rem">{t.assigned_agent || '--'}</td>
                                <td class="mono" style="font-size:0.75rem;color:var(--text-muted)">{timeAgo(t.updated_at)}</td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            </div>
        </div>
        {/if}

        <!-- Linked presentations -->
        {#if hub.linked_presentations?.length}
        <div class="section">
            <div class="section-header">
                <div class="section-title">{$_('nav.presentations')}</div>
                <a href="#/presentations" class="btn btn-sm">{$_('common.view_all')} →</a>
            </div>
            <div class="section-body">
                <div class="pres-chips">
                    {#each hub.linked_presentations as pr}
                        <a href="#/presentations" class="pres-chip">
                            <span class="pres-chip-title">{pr.title}</span>
                            <span class="pres-chip-meta">by {pr.created_by} · {timeAgo(pr.updated_at)}</span>
                        </a>
                    {/each}
                </div>
            </div>
        </div>
        {/if}

        <!-- Linked assets -->
        <div class="section">
            <div class="section-header">
                <div class="section-title">Linked Assets</div>
            </div>
            <div class="section-body">
                {#if p.linked_assets?.length}
                    <div class="asset-chips">
                        {#each p.linked_assets as a, i}
                            <span class="asset-chip">
                                <span class="asset-icon">{assetIcon(a.type)}</span>
                                {#if a.url}
                                    <a href={a.url} target="_blank" rel="noopener noreferrer" class="asset-link" title={a.description || a.title}>{a.title}</a>
                                {:else}
                                    <span class="asset-title">{a.title}</span>
                                {/if}
                                <button class="asset-remove" on:click={() => removeLinkedAsset(i)} title="Remove">&times;</button>
                            </span>
                        {/each}
                    </div>
                {:else}
                    <div style="padding: 0.5rem 1rem; font-size: 0.8rem; color: var(--text-muted);">No linked assets yet.</div>
                {/if}
                <div class="add-asset-row">
                    <select class="input-sm" bind:value={newAssetType}>
                        {#each ASSET_TYPES as t}
                            <option value={t}>{assetIcon(t)} {t}</option>
                        {/each}
                    </select>
                    <input class="input-sm" style="flex:1" type="text" placeholder="Title" bind:value={newAssetTitle} on:keydown={(e) => e.key === 'Enter' && addLinkedAsset()} />
                    <input class="input-sm" style="flex:1" type="text" placeholder="URL (optional)" bind:value={newAssetUrl} on:keydown={(e) => e.key === 'Enter' && addLinkedAsset()} />
                    <button class="btn btn-sm" on:click={addLinkedAsset} disabled={addingAsset || !newAssetTitle.trim()}>+ Add</button>
                </div>
            </div>
        </div>

        <!-- Recent activity -->
        {#if hub.recent_activity?.length}
        <div class="section">
            <div class="section-header">
                <div class="section-title">Recent Activity</div>
            </div>
            <div class="section-body">
                <div class="activity-list">
                    {#each hub.recent_activity as ev}
                        <div class="activity-row">
                            <span class="activity-title">{ev.title}</span>
                            <span class="activity-meta">{ev.agent_name ? ev.agent_name + ' · ' : ''}{timeAgo(ev.created_at * 1000)}</span>
                        </div>
                    {/each}
                </div>
            </div>
        </div>
        {/if}
    {/if}
</div>

<style>
    .proj-desc { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem; }
    .proj-meta-row { display: flex; gap: 0.5rem; align-items: center; margin-left: auto; }

    /* Team members */
    .member-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; padding: 0.5rem 1rem 0.5rem; }
    .member-chip { font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 600; background: var(--surface-3); color: var(--text-secondary); padding: 0.2rem 0.6rem; border-radius: var(--radius-lg); display: inline-flex; align-items: center; gap: 0.3rem; }
    .member-remove { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 0.8rem; line-height: 1; padding: 0 0.1rem; opacity: 0.5; transition: opacity 0.15s; }
    .member-remove:hover { opacity: 1; color: var(--red); }
    .add-member-row { display: flex; gap: 0.4rem; padding: 0.4rem 1rem 0.75rem; align-items: center; }
    .input-sm { font-family: var(--font-grotesk); font-size: 0.75rem; padding: 0.25rem 0.5rem; background: var(--surface-2); border: 1px solid var(--surface-3); border-radius: var(--radius-lg); color: var(--text-primary); outline: none; }
    .input-sm:focus { border-color: var(--accent); }

    .hub-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 0; }
    @media (max-width: 700px) { .hub-grid { grid-template-columns: 1fr; } }

    .stat-row { display: flex; gap: 1rem; padding: 0.75rem 1rem; flex-wrap: wrap; }
    .hub-stat { text-align: center; min-width: 50px; }
    .hub-stat-val { font-family: var(--font-grotesk); font-size: 1.4rem; font-weight: 800; line-height: 1; }
    .hub-stat-label { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 0.15rem; }

    .sprint-name { font-family: var(--font-grotesk); font-size: 0.95rem; font-weight: 700; padding: 0.75rem 1rem 0.25rem; }
    .sprint-dates { font-size: 0.72rem; color: var(--text-muted); padding: 0 1rem 0.5rem; font-family: var(--font-body); }
    .sprint-progress-wrap { height: 6px; background: var(--surface-3); margin: 0 1rem 0.4rem; border-radius: 3px; overflow: hidden; }
    .sprint-progress-fill { height: 100%; border-radius: 3px; transition: width 0.3s; }
    .sprint-pct-label { font-size: 0.72rem; color: var(--text-muted); padding: 0 1rem 0.75rem; font-family: var(--font-body); }

    /* Milestones */
    .milestone-list { display: flex; flex-direction: column; gap: 0; }
    .milestone-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 1rem; border-bottom: 1px solid var(--surface-2); font-size: 0.85rem; }
    .milestone-row:last-child { border-bottom: none; }
    .milestone-name { flex: 1; min-width: 0; }
    .milestone-progress { display: flex; align-items: center; gap: 0.5rem; min-width: 140px; }
    .milestone-progress-bar { flex: 1; height: 4px; background: var(--surface-3); border-radius: 2px; overflow: hidden; min-width: 50px; }
    .milestone-progress-fill { height: 100%; border-radius: 2px; transition: width 0.3s; }
    .milestone-pct { font-family: var(--font-body); font-size: 0.68rem; color: var(--text-muted); white-space: nowrap; }
    .milestone-due { font-family: var(--font-body); font-size: 0.72rem; color: var(--text-muted); }

    .pres-chips { display: flex; flex-wrap: wrap; gap: 0.6rem; padding: 0.75rem 1rem; }
    .pres-chip { display: flex; flex-direction: column; gap: 0.15rem; background: var(--surface-2); border: 1px solid var(--surface-3); border-radius: var(--radius-lg); padding: 0.5rem 0.75rem; text-decoration: none; transition: border-color 0.15s; min-width: 180px; }
    .pres-chip:hover { border-color: var(--yellow); }
    .pres-chip-title { font-family: var(--font-grotesk); font-size: 0.82rem; font-weight: 600; color: var(--text-primary); }
    .pres-chip-meta { font-size: 0.68rem; color: var(--text-muted); }

    /* Linked assets */
    .asset-chips { display: flex; flex-wrap: wrap; gap: 0.5rem; padding: 0.75rem 1rem; }
    .asset-chip { display: flex; align-items: center; gap: 0.35rem; background: var(--surface-2); border: 1px solid var(--surface-3); border-radius: var(--radius-lg); padding: 0.3rem 0.65rem; transition: border-color 0.15s; }
    .asset-chip:hover { border-color: var(--yellow); }
    .asset-icon { font-size: 0.85rem; line-height: 1; }
    .asset-link { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 600; color: var(--text-primary); text-decoration: none; }
    .asset-link:hover { text-decoration: underline; }
    .asset-title { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 600; color: var(--text-primary); }
    .asset-remove { background: none; border: none; color: var(--text-muted); cursor: pointer; font-size: 0.8rem; line-height: 1; padding: 0 0.1rem; opacity: 0.5; transition: opacity 0.15s; margin-left: 0.15rem; }
    .asset-remove:hover { opacity: 1; color: var(--red); }
    .add-asset-row { display: flex; gap: 0.4rem; padding: 0.4rem 1rem 0.75rem; align-items: center; flex-wrap: wrap; }

    .activity-list { display: flex; flex-direction: column; }
    .activity-row { display: flex; align-items: baseline; justify-content: space-between; gap: 1rem; padding: 0.45rem 1rem; border-bottom: 1px solid var(--surface-2); font-size: 0.82rem; }
    .activity-row:last-child { border-bottom: none; }
    .activity-title { flex: 1; color: var(--text-primary); }
    .activity-meta { font-size: 0.7rem; color: var(--text-muted); white-space: nowrap; }
</style>
