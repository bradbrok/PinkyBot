<script>
    import { onMount, onDestroy } from 'svelte';
    import { api } from '../lib/api.js';
    import { toastMessage } from '../lib/stores.js';
    import { timeAgo } from '../lib/utils.js';

    function toast(msg, type = 'success') { toastMessage.set({ message: msg, type }); }

    let statPending = '--'; let statProgress = '--'; let statBlocked = '--'; let statCompleted = '--'; let statTotal = '--';
    let activeTab = 'board';
    let activeProjectId = 0;
    let agentsList = [];
    let projectsList = [];
    let columns = { pending: [], in_progress: [], blocked: [], completed: [] };
    let refreshInterval;

    // Filters
    let filterAgent = ''; let filterPriority = ''; let filterTag = '';

    // Task modal
    let taskModalOpen = false; let modalTitle = 'New Task';
    let editTaskId = ''; let taskTitle = ''; let taskDesc = ''; let taskPriority = 'normal';
    let taskAgent = ''; let taskProject = '0'; let taskDueDate = ''; let taskTags = '';
    let comments = []; let newComment = ''; let showDelete = false;

    // Cron
    let cronJobs = [];
    let cronModalOpen = false; let cronAgent = ''; let cronName = ''; let cronExpr = '';
    let cronPrompt = ''; let cronTimezone = 'America/Los_Angeles'; let cronPreset = '';

    // Project modal
    let projectModalOpen = false; let projectModalTitle = 'New Project';
    let editProjectId = ''; let projectName = ''; let projectDesc = ''; let projectStatus = 'active'; let showProjectStatus = false;
    let projectCards = [];

    function switchTab(tab) { activeTab = tab; if (tab === 'cron') refreshCron(); if (tab === 'projects') refreshProjects(); }

    async function refresh() {
        try {
            const [root, stats, agentsData, projectsData] = await Promise.all([
                api('GET', '/api'), api('GET', '/tasks/stats'), api('GET', '/agents'), api('GET', '/projects'),
            ]);
            const bs = stats.by_status;
            statPending = bs.pending || 0; statProgress = bs.in_progress || 0;
            statBlocked = bs.blocked || 0; statCompleted = bs.completed || 0;
            statTotal = Object.values(bs).reduce((a, b) => a + b, 0);

            agentsList = agentsData.agents || [];
            projectsList = projectsData.projects || [];

            let qs = '?include_completed=true&limit=200';
            if (filterAgent) qs += `&assigned_agent=${encodeURIComponent(filterAgent)}`;
            if (filterPriority) qs += `&priority=${filterPriority}`;
            if (filterTag) qs += `&tag=${encodeURIComponent(filterTag)}`;
            if (activeProjectId) qs += `&project_id=${activeProjectId}`;

            const tasksData = await api('GET', `/tasks${qs}`);
            const allTasks = tasksData.tasks || [];
            columns = { pending: [], in_progress: [], blocked: [], completed: [] };
            for (const t of allTasks) (columns[t.status] || columns.pending).push(t);
        } catch (e) { console.error('Tasks refresh error:', e); }
    }

    function selectProject(id) { activeProjectId = id; refresh(); }

    function openCreateTask() {
        modalTitle = 'New Task'; editTaskId = ''; taskTitle = ''; taskDesc = ''; taskPriority = 'normal';
        taskAgent = ''; taskProject = String(activeProjectId || '0'); taskDueDate = ''; taskTags = '';
        comments = []; newComment = ''; showDelete = false; taskModalOpen = true;
    }

    async function openEditTask(taskId) {
        const data = await api('GET', `/tasks/${taskId}`);
        const task = data.task;
        modalTitle = `Task #${task.id}`; editTaskId = task.id; taskTitle = task.title;
        taskDesc = task.description; taskPriority = task.priority; taskAgent = task.assigned_agent;
        taskProject = String(task.project_id || '0'); taskDueDate = task.due_date; taskTags = task.tags.join(', ');
        comments = data.comments || []; showDelete = true; taskModalOpen = true;
    }

    async function saveTask() {
        if (!taskTitle.trim()) { toast('Title is required', 'error'); return; }
        const tags = taskTags.split(',').map(t => t.trim()).filter(Boolean);
        const payload = { title: taskTitle, description: taskDesc, priority: taskPriority, assigned_agent: taskAgent, project_id: parseInt(taskProject) || 0, due_date: taskDueDate, tags };
        if (editTaskId) { await api('PUT', `/tasks/${editTaskId}`, payload); toast('Task updated'); }
        else { payload.created_by = 'user'; await api('POST', '/tasks', payload); toast('Task created'); }
        taskModalOpen = false; refresh();
    }

    async function deleteTask() { if (!editTaskId || !confirm('Delete this task?')) return; await api('DELETE', `/tasks/${editTaskId}`); toast('Task deleted'); taskModalOpen = false; refresh(); }
    async function addComment() { if (!editTaskId || !newComment.trim()) return; await api('POST', `/tasks/${editTaskId}/comments`, { author: 'user', content: newComment }); newComment = ''; openEditTask(parseInt(editTaskId)); }

    // Cron
    async function refreshCron() {
        const data = await api('GET', '/schedules?enabled_only=false');
        cronJobs = data.schedules || [];
    }

    async function createCronJob() {
        const agentsData = await api('GET', '/agents');
        if (!agentsData.agents.length) { toast('Register an agent first', 'error'); return; }
        cronAgent = agentsData.agents[0].name; cronName = ''; cronExpr = ''; cronPrompt = '';
        cronTimezone = 'America/Los_Angeles'; cronPreset = ''; cronModalOpen = true;
    }

    function applyCronPreset() { if (cronPreset) cronExpr = cronPreset; }

    async function saveCronJob() {
        if (!cronExpr.trim()) { toast('Cron expression is required', 'error'); return; }
        if (!cronName.trim()) { toast('Give this schedule a name', 'error'); return; }
        await api('POST', `/agents/${cronAgent}/schedules`, { name: cronName, cron: cronExpr, prompt: cronPrompt || `Scheduled wake: ${cronName}`, timezone: cronTimezone });
        toast(`Schedule "${cronName}" created`); cronModalOpen = false; refreshCron();
    }

    async function toggleCron(agentName, id, enabled) { await api('POST', `/agents/${agentName}/schedules/${id}/toggle?enabled=${enabled}`); refreshCron(); }
    async function deleteCron(agentName, id) { if (!confirm('Delete?')) return; await api('DELETE', `/agents/${agentName}/schedules/${id}`); toast('Deleted'); refreshCron(); }

    // Projects
    async function refreshProjects() {
        const projectsData = await api('GET', '/projects?include_archived=true');
        const projects = projectsData.projects || [];
        const details = await Promise.all(projects.map(p => api('GET', `/projects/${p.id}`)));
        projectCards = projects.map((p, i) => ({ ...p, taskCount: details[i].task_count || 0 }));
    }

    function createProject() { projectModalTitle = 'New Project'; editProjectId = ''; projectName = ''; projectDesc = ''; showProjectStatus = false; projectModalOpen = true; }
    function editProject(p) { projectModalTitle = 'Edit Project'; editProjectId = p.id; projectName = p.name; projectDesc = p.description || ''; projectStatus = p.status; showProjectStatus = true; projectModalOpen = true; }

    async function saveProject() {
        if (!projectName.trim()) { toast('Name required', 'error'); return; }
        if (editProjectId) { await api('PUT', `/projects/${editProjectId}`, { name: projectName, description: projectDesc, status: projectStatus }); toast('Updated'); }
        else { await api('POST', '/projects', { name: projectName, description: projectDesc }); toast(`"${projectName}" created`); }
        projectModalOpen = false; refresh(); if (activeTab === 'projects') refreshProjects();
    }

    async function deleteProjectFromTab(id) { if (!confirm('Delete project?')) return; await api('DELETE', `/projects/${id}`); toast('Deleted'); refreshProjects(); refresh(); }

    onMount(() => { refresh(); refreshInterval = setInterval(refresh, 15000); });
    onDestroy(() => { clearInterval(refreshInterval); });
</script>

<div class="content" style="max-width:1600px">
    <!-- Stats -->
    <div class="stats-bar">
        <div class="stat-card"><div class="stat-value">{statPending}</div><div class="stat-label">Pending</div></div>
        <div class="stat-card"><div class="stat-value">{statProgress}</div><div class="stat-label">In Progress</div></div>
        <div class="stat-card"><div class="stat-value">{statBlocked}</div><div class="stat-label">Blocked</div></div>
        <div class="stat-card"><div class="stat-value">{statCompleted}</div><div class="stat-label">Completed</div></div>
        <div class="stat-card"><div class="stat-value">{statTotal}</div><div class="stat-label">Total</div></div>
    </div>

    <!-- Tabs -->
    <div class="tabs">
        <div class="tab" class:active={activeTab === 'board'} on:click={() => switchTab('board')}>Board</div>
        <div class="tab" class:active={activeTab === 'projects'} on:click={() => switchTab('projects')}>Projects</div>
        <div class="tab" class:active={activeTab === 'cron'} on:click={() => switchTab('cron')}>Cron Jobs</div>
    </div>

    <!-- Board Tab -->
    {#if activeTab === 'board'}
        <div class="layout">
            <div class="sidebar">
                <div class="sidebar-header"><span>Projects</span><button class="btn btn-sm btn-primary" on:click={createProject}>+</button></div>
                <div class="project-item" class:active={activeProjectId === 0} on:click={() => selectProject(0)}>
                    <span>All Tasks</span><span class="project-count">{statTotal}</span>
                </div>
                {#each projectsList as p}
                    <div class="project-item" class:active={activeProjectId === p.id} on:click={() => selectProject(p.id)}>
                        <span>{p.name}</span>
                    </div>
                {/each}
            </div>
            <div class="main-content">
                <div class="toolbar">
                    <button class="btn btn-primary" on:click={openCreateTask}>+ New Task</button>
                    <select bind:value={filterAgent} on:change={refresh}>
                        <option value="">All Agents</option>
                        {#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}
                    </select>
                    <select bind:value={filterPriority} on:change={refresh}>
                        <option value="">All Priorities</option>
                        <option value="urgent">Urgent</option><option value="high">High</option>
                        <option value="normal">Normal</option><option value="low">Low</option>
                    </select>
                    <input type="text" bind:value={filterTag} placeholder="Filter by tag..." on:keydown={e => { if (e.key === 'Enter') refresh(); }} style="width:120px">
                </div>
                <div class="board">
                    {#each [['pending','Pending'],['in_progress','In Progress'],['blocked','Blocked'],['completed','Completed']] as [key, label]}
                        <div class="column">
                            <div class="column-header">{label} <span class="column-count">{columns[key].length}</span></div>
                            <div class="column-body">
                                {#each columns[key] as task}
                                    <div class="task-card" class:priority-urgent={task.priority === 'urgent'} class:priority-high={task.priority === 'high'} on:click={() => openEditTask(task.id)}>
                                        <div class="task-title">{task.title}</div>
                                        <div class="task-meta">
                                            <span class="badge badge-{task.priority}">{task.priority}</span>
                                            {#if task.assigned_agent}<span class="badge badge-agent">{task.assigned_agent}</span>{/if}
                                            {#each task.tags as t}<span class="badge badge-tag">{t}</span>{/each}
                                        </div>
                                        {#if task.description}<div class="task-desc">{task.description}</div>{/if}
                                        <div class="task-footer"><span>#{task.id}</span><span>{task.due_date || timeAgo(task.updated_at)}</span></div>
                                    </div>
                                {:else}
                                    <div style="padding:1rem;text-align:center;color:var(--gray-mid);font-family:var(--font-mono);font-size:0.75rem">Empty</div>
                                {/each}
                            </div>
                        </div>
                    {/each}
                </div>
            </div>
        </div>
    {/if}

    <!-- Projects Tab -->
    {#if activeTab === 'projects'}
        <div class="section" style="border:none;padding:1rem 0">
            <div class="section-header" style="border:none;padding:1rem 0">
                <div class="section-title">Projects</div>
                <button class="btn btn-primary" on:click={createProject}>+ New Project</button>
            </div>
        </div>
        {#if projectCards.length === 0}
            <div class="empty">No projects yet.</div>
        {:else}
            <div class="project-grid">
                {#each projectCards as p}
                    <div class="project-card-lg">
                        <div style="display:flex;justify-content:space-between;align-items:flex-start">
                            <div class="project-name-lg">{p.name}</div>
                            <span class="badge badge-{p.status === 'active' ? 'normal' : 'low'}">{p.status}</span>
                        </div>
                        <div class="project-desc-lg">{p.description || 'No description'}</div>
                        <div class="project-stats-lg"><span>{p.taskCount} task{p.taskCount !== 1 ? 's' : ''}</span><span>created {timeAgo(p.created_at)}</span></div>
                        <div style="margin-top:0.8rem;display:flex;gap:0.3rem">
                            <button class="btn btn-sm btn-primary" on:click={() => { selectProject(p.id); switchTab('board'); }}>View Board</button>
                            <button class="btn btn-sm" on:click={() => editProject(p)}>Edit</button>
                            <button class="btn btn-sm btn-danger" on:click={() => deleteProjectFromTab(p.id)}>Delete</button>
                        </div>
                    </div>
                {/each}
            </div>
        {/if}
    {/if}

    <!-- Cron Tab -->
    {#if activeTab === 'cron'}
        <div class="section" style="border:none;padding:1rem 0">
            <div class="section-header" style="border:none;padding:1rem 0">
                <div class="section-title">Cron Jobs / Wake Schedules</div>
                <button class="btn btn-primary" on:click={createCronJob}>+ New Schedule</button>
            </div>
        </div>
        <div style="background:var(--white);border:var(--border)">
            {#if cronJobs.length === 0}
                <div class="empty">No cron jobs configured.</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>Agent</th><th>Name</th><th>Schedule</th><th>Prompt</th><th>Timezone</th><th>Status</th><th>Last Run</th><th>Actions</th></tr></thead>
                    <tbody>
                        {#each cronJobs as s}
                            <tr>
                                <td class="mono" style="font-weight:700">{s.agent_name}</td>
                                <td class="mono">{s.name || '--'}</td>
                                <td class="mono" style="background:#fefce8">{s.cron}</td>
                                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{s.prompt || '--'}</td>
                                <td class="mono" style="font-size:0.75rem">{s.timezone}</td>
                                <td><span class="badge badge-{s.enabled ? 'normal' : 'low'}">{s.enabled ? 'Active' : 'Off'}</span></td>
                                <td class="mono" style="font-size:0.75rem">{s.last_run ? new Date(s.last_run * 1000).toLocaleString() : 'never'}</td>
                                <td>
                                    <button class="btn btn-sm" on:click={() => toggleCron(s.agent_name, s.id, !s.enabled)}>{s.enabled ? 'Disable' : 'Enable'}</button>
                                    <button class="btn btn-sm btn-danger" on:click={() => deleteCron(s.agent_name, s.id)}>X</button>
                                </td>
                            </tr>
                        {/each}
                    </tbody>
                </table>
            {/if}
        </div>
    {/if}
</div>

<!-- Task Modal -->
{#if taskModalOpen}
    <div class="modal-overlay" on:click|self={() => taskModalOpen = false}>
        <div class="modal" style="width:700px">
            <div class="modal-header"><div class="modal-title">{modalTitle}</div><button class="btn btn-sm" on:click={() => taskModalOpen = false}>X</button></div>
            <div class="modal-body">
                <div class="form-row"><label class="form-label">Title</label><input type="text" class="form-input" bind:value={taskTitle} style="width:100%"></div>
                <div class="form-row"><label class="form-label">Description</label><textarea class="form-input" bind:value={taskDesc} rows="3"></textarea></div>
                <div class="form-row-inline">
                    <div class="form-row"><label class="form-label">Priority</label><select class="form-select" bind:value={taskPriority} style="width:100%"><option value="normal">Normal</option><option value="low">Low</option><option value="high">High</option><option value="urgent">Urgent</option></select></div>
                    <div class="form-row"><label class="form-label">Assign To</label><select class="form-select" bind:value={taskAgent} style="width:100%"><option value="">Unassigned</option>{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
                </div>
                <div class="form-row-inline">
                    <div class="form-row"><label class="form-label">Project</label><select class="form-select" bind:value={taskProject} style="width:100%"><option value="0">None</option>{#each projectsList as p}<option value={p.id}>{p.name}</option>{/each}</select></div>
                    <div class="form-row"><label class="form-label">Due Date</label><input type="date" class="form-input" bind:value={taskDueDate} style="width:100%"></div>
                </div>
                <div class="form-row"><label class="form-label">Tags</label><input type="text" class="form-input" bind:value={taskTags} placeholder="Comma-separated" style="width:100%"></div>
                {#if editTaskId}
                    <div style="border-top:var(--border);margin-top:1rem;padding-top:1rem">
                        <label class="form-label">Activity</label>
                        {#each comments as c}
                            <div class="comment-item"><div class="comment-header"><strong>{c.author || 'unknown'}</strong> &middot; {timeAgo(c.created_at)}</div><div class="comment-body">{c.content}</div></div>
                        {:else}
                            <div style="color:var(--gray-mid);font-size:0.8rem;padding:0.5rem 0">No activity yet</div>
                        {/each}
                        <div style="display:flex;gap:0.5rem;margin-top:0.5rem">
                            <input type="text" class="form-input" bind:value={newComment} placeholder="Add a comment..." style="flex:1">
                            <button class="btn btn-sm btn-primary" on:click={addComment}>Post</button>
                        </div>
                    </div>
                {/if}
            </div>
            <div class="modal-footer">
                {#if showDelete}<button class="btn btn-danger btn-sm" on:click={deleteTask} style="margin-right:auto">Delete</button>{/if}
                <button class="btn" on:click={() => taskModalOpen = false}>Cancel</button>
                <button class="btn btn-primary" on:click={saveTask}>Save</button>
            </div>
        </div>
    </div>
{/if}

<!-- Cron Modal -->
{#if cronModalOpen}
    <div class="modal-overlay" on:click|self={() => cronModalOpen = false}>
        <div class="modal" style="width:550px">
            <div class="modal-header"><div class="modal-title">New Cron Schedule</div><button class="btn btn-sm" on:click={() => cronModalOpen = false}>X</button></div>
            <div class="modal-body">
                <div class="form-row"><label class="form-label">Agent</label><select class="form-select" bind:value={cronAgent} style="width:100%">{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
                <div class="form-row"><label class="form-label">Name</label><input type="text" class="form-input" bind:value={cronName} style="width:100%"></div>
                <div class="form-row"><label class="form-label">Cron Expression</label>
                    <select class="form-select" bind:value={cronPreset} on:change={applyCronPreset} style="width:100%;margin-bottom:0.5rem">
                        <option value="">Custom...</option>
                        <option value="*/5 * * * *">Every 5 min</option><option value="0 * * * *">Every hour</option>
                        <option value="0 8 * * *">Daily 8am</option><option value="0 9 * * 1-5">Weekdays 9am</option>
                    </select>
                    <input type="text" class="form-input" bind:value={cronExpr} placeholder="min hour day month weekday" style="width:100%">
                </div>
                <div class="form-row"><label class="form-label">Prompt</label><textarea class="form-input" bind:value={cronPrompt} rows="2"></textarea></div>
                <div class="form-row"><label class="form-label">Timezone</label><select class="form-select" bind:value={cronTimezone} style="width:100%">
                    <option value="America/Los_Angeles">Pacific</option><option value="America/New_York">Eastern</option><option value="UTC">UTC</option>
                </select></div>
            </div>
            <div class="modal-footer"><button class="btn" on:click={() => cronModalOpen = false}>Cancel</button><button class="btn btn-primary" on:click={saveCronJob}>Create</button></div>
        </div>
    </div>
{/if}

<!-- Project Modal -->
{#if projectModalOpen}
    <div class="modal-overlay" on:click|self={() => projectModalOpen = false}>
        <div class="modal" style="width:500px">
            <div class="modal-header"><div class="modal-title">{projectModalTitle}</div><button class="btn btn-sm" on:click={() => projectModalOpen = false}>X</button></div>
            <div class="modal-body">
                <div class="form-row"><label class="form-label">Name</label><input type="text" class="form-input" bind:value={projectName} style="width:100%"></div>
                <div class="form-row"><label class="form-label">Description</label><textarea class="form-input" bind:value={projectDesc} rows="3"></textarea></div>
                {#if showProjectStatus}<div class="form-row"><label class="form-label">Status</label><select class="form-select" bind:value={projectStatus} style="width:100%"><option value="active">Active</option><option value="completed">Completed</option><option value="archived">Archived</option></select></div>{/if}
            </div>
            <div class="modal-footer"><button class="btn" on:click={() => projectModalOpen = false}>Cancel</button><button class="btn btn-primary" on:click={saveProject}>Save</button></div>
        </div>
    </div>
{/if}

<style>
    .stats-bar { display: grid; grid-template-columns: repeat(5, 1fr); gap: 0; margin-bottom: 2rem; }
    .stat-card { padding: 1.2rem; background: var(--surface-1); border: var(--border); margin: -1.5px; text-align: center; }
    .stat-value { font-family: var(--font-mono); font-size: 1.8rem; font-weight: 700; }
    .stat-label { font-family: var(--font-mono); font-size: 0.65rem; text-transform: uppercase; color: var(--text-muted); }

    .tabs { display: flex; gap: 0; border-bottom: var(--border); background: var(--surface-1); margin-bottom: 2rem; }
    .tab { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; text-transform: uppercase; padding: 0.8rem 1.5rem; cursor: pointer; border-right: var(--border); border-bottom: 3px solid transparent; margin-bottom: -3px; }
    .tab:hover { background: var(--hover-accent); }
    .tab.active { border-bottom-color: var(--accent); background: var(--surface-1); }

    .layout { display: grid; grid-template-columns: 220px 1fr; gap: 0; }
    .sidebar { border-right: var(--border); background: var(--surface-1); }
    .sidebar-header { padding: 1rem; border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: flex; justify-content: space-between; align-items: center; }
    .project-item { padding: 0.6rem 1rem; font-family: var(--font-mono); font-size: 0.8rem; cursor: pointer; border-bottom: 1px solid var(--row-divider); display: flex; justify-content: space-between; align-items: center; }
    .project-item:hover { background: var(--hover-accent); }
    .project-item.active { background: var(--selected-bg); color: var(--selected-text); font-weight: 700; }
    .project-count { font-size: 0.65rem; color: var(--text-muted); }
    .main-content { padding: 2rem; }

    .toolbar { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .toolbar select, .toolbar input { font-family: var(--font-mono); font-size: 0.75rem; padding: 0.3rem 0.6rem; border: 2px solid var(--border-strong); background: var(--input-bg); color: var(--text-primary); }

    .board { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0; margin-bottom: 2rem; }
    .column { border: var(--border); margin: -1.5px; min-height: 300px; }
    .column-header { padding: 0.8rem 1rem; background: var(--surface-2); border-bottom: var(--border); font-family: var(--font-mono); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: flex; justify-content: space-between; }
    .column-count { background: var(--surface-inverse); color: var(--accent); padding: 0.1rem 0.4rem; font-size: 0.65rem; }
    .column-body { padding: 0.5rem; background: var(--surface-1); }

    .task-card { border: 2px solid var(--border-strong); padding: 0.8rem; margin-bottom: 0.5rem; cursor: pointer; transition: background 0.1s; background: var(--surface-1); }
    .task-card:hover { background: var(--hover-accent); }
    .task-card.priority-urgent { border-left: 5px solid var(--red); }
    .task-card.priority-high { border-left: 5px solid #f59e0b; }
    .task-title { font-family: var(--font-mono); font-size: 0.8rem; font-weight: 700; margin-bottom: 0.4rem; }
    .task-meta { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.3rem; }
    .task-desc { font-size: 0.75rem; color: var(--text-muted); max-height: 2.4em; overflow: hidden; }
    .task-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 0.4rem; font-family: var(--font-mono); font-size: 0.6rem; color: var(--text-muted); }

    .comment-item { padding: 0.6rem 0; border-bottom: 1px solid var(--row-divider); }
    .comment-header { font-family: var(--font-mono); font-size: 0.7rem; color: var(--text-muted); margin-bottom: 0.2rem; }
    .comment-body { font-size: 0.85rem; }

    .project-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0; }
    .project-card-lg { border: var(--border); margin: -1.5px; padding: 1.5rem; }
    .project-card-lg:hover { background: var(--hover-accent); }
    .project-name-lg { font-family: var(--font-mono); font-size: 1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .project-desc-lg { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem; }
    .project-stats-lg { display: flex; gap: 1rem; font-family: var(--font-mono); font-size: 0.7rem; color: var(--text-muted); }

    @media (max-width: 1000px) {
        .layout { grid-template-columns: 1fr; }
        .sidebar { border-right: none; border-bottom: var(--border); }
        .board { grid-template-columns: repeat(2, 1fr); }
        .stats-bar { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 600px) {
        .board { grid-template-columns: 1fr; }
    }
</style>
