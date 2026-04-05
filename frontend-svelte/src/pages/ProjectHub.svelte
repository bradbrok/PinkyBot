<script>
    import { onMount } from 'svelte';
    import { _ } from 'svelte-i18n';
    import { api } from '../lib/api.js';
    import { timeAgo } from '../lib/utils.js';

    export let params = {};

    let hub = null;
    let loading = true;
    let error = '';

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
        if (!sprint?.task_counts?.total) return 0;
        return Math.round((sprint.task_counts.completed / sprint.task_counts.total) * 100);
    }

    const MILESTONE_BADGE = { completed: 'on', in_progress: 'running', pending: 'model', cancelled: 'off' };
    const TASK_BADGE = { completed: 'on', in_progress: 'running', pending: 'model', blocked: 'off', cancelled: 'closed' };
    const PRIORITY_COLOR = { urgent: 'var(--red)', high: 'var(--yellow)', normal: 'var(--text-secondary)', low: 'var(--text-muted)' };
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
                        <a href={p.repo_url} target="_blank" rel="noopener noreferrer" class="btn btn-sm">{$_('project_hub.repo')} →</a>
                    {/if}
                    <a href="#/tasks" class="btn btn-sm">{$_('nav.tasks')} →</a>
                </div>
            </div>
            {#if p.members?.length}
                <div class="member-chips">
                    {#each p.members as m}
                        <span class="member-chip">{m}</span>
                    {/each}
                </div>
            {/if}
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
                            <div class="sprint-progress-fill" style="width:{pct}%"></div>
                        </div>
                        <div class="sprint-pct-label">{$_('project_hub.sprint_progress', { values: { pct, completed: sprint.task_counts?.completed ?? 0, total: sprint.task_counts?.total ?? 0 } })}</div>
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
    {/if}
</div>

<style>
    .proj-desc { font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem; }
    .proj-meta-row { display: flex; gap: 0.5rem; align-items: center; margin-left: auto; }
    .member-chips { display: flex; flex-wrap: wrap; gap: 0.4rem; padding: 0.5rem 1rem 1rem; }
    .member-chip { font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 600; background: var(--surface-3); color: var(--text-secondary); padding: 0.2rem 0.6rem; border-radius: var(--radius-lg); }

    .hub-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-bottom: 0; }
    @media (max-width: 700px) { .hub-grid { grid-template-columns: 1fr; } }

    .stat-row { display: flex; gap: 1rem; padding: 0.75rem 1rem; flex-wrap: wrap; }
    .hub-stat { text-align: center; min-width: 50px; }
    .hub-stat-val { font-family: var(--font-grotesk); font-size: 1.4rem; font-weight: 800; line-height: 1; }
    .hub-stat-label { font-size: 0.65rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-top: 0.15rem; }

    .sprint-name { font-family: var(--font-grotesk); font-size: 0.95rem; font-weight: 700; padding: 0.75rem 1rem 0.25rem; }
    .sprint-dates { font-size: 0.72rem; color: var(--text-muted); padding: 0 1rem 0.5rem; font-family: var(--font-body); }
    .sprint-progress-wrap { height: 4px; background: var(--surface-3); margin: 0 1rem 0.4rem; border-radius: 2px; overflow: hidden; }
    .sprint-progress-fill { height: 100%; background: var(--yellow); border-radius: 2px; transition: width 0.3s; }
    .sprint-pct-label { font-size: 0.72rem; color: var(--text-muted); padding: 0 1rem 0.75rem; font-family: var(--font-body); }

    .milestone-list { display: flex; flex-direction: column; gap: 0; }
    .milestone-row { display: flex; align-items: center; gap: 0.75rem; padding: 0.5rem 1rem; border-bottom: 1px solid var(--surface-2); font-size: 0.85rem; }
    .milestone-row:last-child { border-bottom: none; }
    .milestone-name { flex: 1; }
    .milestone-due { font-family: var(--font-body); font-size: 0.72rem; color: var(--text-muted); }

    .pres-chips { display: flex; flex-wrap: wrap; gap: 0.6rem; padding: 0.75rem 1rem; }
    .pres-chip { display: flex; flex-direction: column; gap: 0.15rem; background: var(--surface-2); border: 1px solid var(--surface-3); border-radius: var(--radius-lg); padding: 0.5rem 0.75rem; text-decoration: none; transition: border-color 0.15s; min-width: 180px; }
    .pres-chip:hover { border-color: var(--yellow); }
    .pres-chip-title { font-family: var(--font-grotesk); font-size: 0.82rem; font-weight: 600; color: var(--text-primary); }
    .pres-chip-meta { font-size: 0.68rem; color: var(--text-muted); }
</style>
