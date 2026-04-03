<script>
    import { onMount, onDestroy } from 'svelte';
    import Modal from '../components/Modal.svelte';
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
    let taskMilestoneId = '0';
    let comments = []; let newComment = ''; let showDelete = false;

    // Cron
    let cronJobs = [];
    let cronModalOpen = false; let cronAgent = ''; let cronName = ''; let cronExpr = '';
    let cronPrompt = ''; let cronTimezone = 'America/Los_Angeles'; let cronPreset = '';

    // Project modal
    let projectModalOpen = false; let projectModalTitle = 'New Project';
    let editProjectId = ''; let projectName = ''; let projectDesc = ''; let projectStatus = 'active'; let projectDueDate = ''; let showProjectStatus = false;
    let projectCards = [];

    // Milestones
    let milestonesByProject = {};       // project_id -> milestone[]
    let milestoneFormProjectId = 0;     // which project's inline form is open
    let newMilestoneName = '';
    let newMilestoneDueDate = '';
    let editMilestoneId = 0;            // id of milestone being edited inline (0 = none)
    let editMilestoneName = '';
    let editMilestoneDueDate = '';
    let editMilestoneStatus = 'open';

    // Milestones for task modal (keyed by project_id)
    let taskMilestones = [];            // milestones for currently-selected project in task modal

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
        taskMilestoneId = '0'; comments = []; newComment = ''; showDelete = false;
        loadTaskMilestones(taskProject); taskModalOpen = true;
    }

    async function openEditTask(taskId) {
        const data = await api('GET', `/tasks/${taskId}`);
        const task = data.task;
        modalTitle = `Task #${task.id}`; editTaskId = task.id; taskTitle = task.title;
        taskDesc = task.description; taskPriority = task.priority; taskAgent = task.assigned_agent;
        taskProject = String(task.project_id || '0'); taskDueDate = task.due_date; taskTags = task.tags.join(', ');
        taskMilestoneId = String(task.milestone_id || '0');
        await loadTaskMilestones(taskProject);
        comments = data.comments || []; showDelete = true; taskModalOpen = true;
    }

    async function saveTask() {
        if (!taskTitle.trim()) { toast('Title is required', 'error'); return; }
        const tags = taskTags.split(',').map(t => t.trim()).filter(Boolean);
        const payload = { title: taskTitle, description: taskDesc, priority: taskPriority, assigned_agent: taskAgent, project_id: parseInt(taskProject) || 0, milestone_id: parseInt(taskMilestoneId) || 0, due_date: taskDueDate, tags };
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
        const [details, msData] = await Promise.all([
            Promise.all(projects.map(p => api('GET', `/projects/${p.id}`))),
            Promise.all(projects.map(p => api('GET', `/projects/${p.id}/milestones`))),
        ]);
        projectCards = projects.map((p, i) => ({ ...p, taskCount: details[i].task_count || 0 }));
        const newMsByProject = {};
        projects.forEach((p, i) => { newMsByProject[p.id] = msData[i].milestones || []; });
        milestonesByProject = newMsByProject;
    }

    function createProject() { projectModalTitle = 'New Project'; editProjectId = ''; projectName = ''; projectDesc = ''; projectDueDate = ''; showProjectStatus = false; projectModalOpen = true; }
    function editProject(p) { projectModalTitle = 'Edit Project'; editProjectId = p.id; projectName = p.name; projectDesc = p.description || ''; projectStatus = p.status; projectDueDate = p.due_date || ''; showProjectStatus = true; projectModalOpen = true; }

    async function saveProject() {
        if (!projectName.trim()) { toast('Name required', 'error'); return; }
        if (editProjectId) { await api('PUT', `/projects/${editProjectId}`, { name: projectName, description: projectDesc, status: projectStatus, due_date: projectDueDate }); toast('Updated'); }
        else { await api('POST', '/projects', { name: projectName, description: projectDesc }); toast(`"${projectName}" created`); }
        projectModalOpen = false; refresh(); if (activeTab === 'projects') refreshProjects();
    }

    async function archiveProject(id) { await api('PUT', `/projects/${id}`, { status: 'archived' }); toast('Archived'); refreshProjects(); refresh(); }

    async function deleteProjectFromTab(id) { if (!confirm('Delete project?')) return; await api('DELETE', `/projects/${id}`); toast('Deleted'); refreshProjects(); refresh(); }

    // Milestone functions
    function openMilestoneForm(projectId) { milestoneFormProjectId = projectId; newMilestoneName = ''; newMilestoneDueDate = ''; }
    function closeMilestoneForm() { milestoneFormProjectId = 0; }

    async function addMilestone(projectId) {
        if (!newMilestoneName.trim()) { toast('Name required', 'error'); return; }
        await api('POST', `/projects/${projectId}/milestones`, { name: newMilestoneName, due_date: newMilestoneDueDate });
        toast('Milestone added'); closeMilestoneForm(); refreshProjects();
    }

    function startEditMilestone(m) { editMilestoneId = m.id; editMilestoneName = m.name; editMilestoneDueDate = m.due_date || ''; editMilestoneStatus = m.status; }
    function cancelEditMilestone() { editMilestoneId = 0; }

    async function saveEditMilestone() {
        if (!editMilestoneName.trim()) { toast('Name required', 'error'); return; }
        await api('PUT', `/milestones/${editMilestoneId}`, { name: editMilestoneName, due_date: editMilestoneDueDate, status: editMilestoneStatus });
        toast('Milestone updated'); editMilestoneId = 0; refreshProjects();
    }

    async function deleteMilestone(id) {
        if (!confirm('Delete milestone?')) return;
        await api('DELETE', `/milestones/${id}`); toast('Milestone deleted'); refreshProjects();
    }

    // Load milestones for the selected project in task modal
    async function loadTaskMilestones(projectId) {
        if (!projectId || projectId === '0' || projectId === 0) { taskMilestones = []; return; }
        try {
            const data = await api('GET', `/projects/${projectId}/milestones`);
            taskMilestones = data.milestones || [];
        } catch { taskMilestones = []; }
    }

    onMount(() => { refresh(); refreshInterval = setInterval(refresh, 15000); });
    onDestroy(() => { clearInterval(refreshInterval); });
</script>

<div class="content">
    <div class="stats-grid">
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
                                    <div style="padding:1rem;text-align:center;color:var(--gray-mid);font-family:var(--font-grotesk);font-size:0.75rem">Empty</div>
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
        <div class="section" style="padding:1rem 0">
            <div class="section-header" style="padding:1rem 0">
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
                        {#if p.due_date}
                            <div class="project-due" class:overdue={p.due_date < new Date().toISOString().slice(0,10)}>
                                Due: {new Date(p.due_date + 'T00:00:00').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}
                            </div>
                        {/if}
                        <div class="project-stats-lg"><span>{p.taskCount} task{p.taskCount !== 1 ? 's' : ''}</span><span>created {timeAgo(p.created_at)}</span></div>

                        <!-- Milestones section -->
                        <div class="milestones-section">
                            <div class="milestones-header">
                                <span class="milestones-title">Milestones</span>
                                <button class="btn btn-sm" on:click={() => openMilestoneForm(p.id)}>+ Add</button>
                            </div>
                            {#if milestoneFormProjectId === p.id}
                                <div class="milestone-form">
                                    <input type="text" class="form-input" bind:value={newMilestoneName} placeholder="Milestone name" style="flex:1">
                                    <input type="date" class="form-input" bind:value={newMilestoneDueDate} style="width:140px">
                                    <button class="btn btn-sm btn-primary" on:click={() => addMilestone(p.id)}>Add</button>
                                    <button class="btn btn-sm" on:click={closeMilestoneForm}>Cancel</button>
                                </div>
                            {/if}
                            {#each (milestonesByProject[p.id] || []) as m}
                                {#if editMilestoneId === m.id}
                                    <div class="milestone-form">
                                        <input type="text" class="form-input" bind:value={editMilestoneName} style="flex:1">
                                        <input type="date" class="form-input" bind:value={editMilestoneDueDate} style="width:140px">
                                        <select class="form-select" bind:value={editMilestoneStatus} style="width:90px">
                                            <option value="open">Open</option>
                                            <option value="reached">Reached</option>
                                            <option value="missed">Missed</option>
                                        </select>
                                        <button class="btn btn-sm btn-primary" on:click={saveEditMilestone}>Save</button>
                                        <button class="btn btn-sm" on:click={cancelEditMilestone}>Cancel</button>
                                    </div>
                                {:else}
                                    <div class="milestone-row">
                                        <span class="milestone-status-dot status-{m.status}"></span>
                                        <span class="milestone-name" on:click={() => startEditMilestone(m)} title="Click to edit">{m.name}</span>
                                        {#if m.due_date}<span class="milestone-due">{m.due_date}</span>{/if}
                                        <span class="badge badge-milestone-{m.status}">{m.status}</span>
                                        {#if m.task_count > 0}<span class="milestone-tasks">{m.task_count} task{m.task_count !== 1 ? 's' : ''}</span>{/if}
                                        <button class="btn btn-sm btn-danger" style="padding:0.1rem 0.4rem;font-size:0.65rem" on:click={() => deleteMilestone(m.id)}>x</button>
                                    </div>
                                {/if}
                            {:else}
                                <div class="milestones-empty">No milestones yet</div>
                            {/each}
                        </div>

                        <div style="margin-top:0.8rem;display:flex;gap:0.3rem;flex-wrap:wrap">
                            <button class="btn btn-sm btn-primary" on:click={() => { selectProject(p.id); switchTab('board'); }}>View Board</button>
                            <button class="btn btn-sm" on:click={() => editProject(p)}>Edit</button>
                            {#if p.status !== 'archived'}<button class="btn btn-sm" on:click={() => archiveProject(p.id)}>Archive</button>{/if}
                            <button class="btn btn-sm btn-danger" on:click={() => deleteProjectFromTab(p.id)}>Delete</button>
                        </div>
                    </div>
                {/each}
            </div>
        {/if}
    {/if}

    <!-- Cron Tab -->
    {#if activeTab === 'cron'}
        <div class="section" style="padding:1rem 0">
            <div class="section-header" style="padding:1rem 0">
                <div class="section-title">Cron Jobs / Wake Schedules</div>
                <button class="btn btn-primary" on:click={createCronJob}>+ New Schedule</button>
            </div>
        </div>
        <div style="background:var(--surface-1);border-radius:var(--radius-lg)">
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

<Modal bind:show={taskModalOpen} title={modalTitle} width="700px" footerClass="spread">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">Title</label><input type="text" class="form-input w-full" bind:value={taskTitle}></div>
        <div class="form-row"><label class="form-label">Description</label><textarea class="form-input" bind:value={taskDesc} rows="3"></textarea></div>
        <div class="form-row-inline">
            <div class="form-row"><label class="form-label">Priority</label><select class="form-select w-full" bind:value={taskPriority}><option value="normal">Normal</option><option value="low">Low</option><option value="high">High</option><option value="urgent">Urgent</option></select></div>
            <div class="form-row"><label class="form-label">Assign To</label><select class="form-select w-full" bind:value={taskAgent}><option value="">Unassigned</option>{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
        </div>
        <div class="form-row-inline">
            <div class="form-row"><label class="form-label">Project</label><select class="form-select w-full" bind:value={taskProject} on:change={() => { taskMilestoneId = '0'; loadTaskMilestones(taskProject); }}><option value="0">None</option>{#each projectsList as p}<option value={p.id}>{p.name}</option>{/each}</select></div>
            <div class="form-row"><label class="form-label">Due Date</label><input type="date" class="form-input w-full" bind:value={taskDueDate}></div>
        </div>
        <div class="form-row-inline">
            <div class="form-row"><label class="form-label">Milestone</label><select class="form-select w-full" bind:value={taskMilestoneId}><option value="0">No milestone</option>{#each taskMilestones as m}<option value={String(m.id)}>{m.name}{m.due_date ? ' (' + m.due_date + ')' : ''}</option>{/each}</select></div>
            <div class="form-row"><label class="form-label">Tags</label><input type="text" class="form-input w-full" bind:value={taskTags} placeholder="Comma-separated"></div>
        </div>
        {#if editTaskId}
            <div class="surface-panel">
                <label class="form-label">Activity</label>
                {#each comments as c}
                    <div class="comment-item"><div class="comment-header"><strong>{c.author || 'unknown'}</strong> &middot; {timeAgo(c.created_at)}</div><div class="comment-body">{c.content}</div></div>
                {:else}
                    <div class="muted-note" style="padding:0.5rem 0">No activity yet</div>
                {/each}
                <div class="inline-spread" style="margin-top:0.75rem">
                    <input type="text" class="form-input grow" bind:value={newComment} placeholder="Add a comment...">
                    <button class="btn btn-sm btn-primary" on:click={addComment}>Post</button>
                </div>
            </div>
        {/if}
    </div>
    <div slot="footer" class="inline-spread grow">
        <div class="inline-spread">
            {#if showDelete}<button class="btn btn-danger btn-sm" on:click={deleteTask}>Delete</button>{/if}
        </div>
        <div class="inline-spread">
            <button class="btn" on:click={() => taskModalOpen = false}>Cancel</button>
            <button class="btn btn-primary" on:click={saveTask}>Save</button>
        </div>
    </div>
</Modal>

<Modal bind:show={cronModalOpen} title="New Cron Schedule" width="550px">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">Agent</label><select class="form-select w-full" bind:value={cronAgent}>{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
        <div class="form-row"><label class="form-label">Name</label><input type="text" class="form-input w-full" bind:value={cronName}></div>
        <div class="form-row"><label class="form-label">Cron Expression</label>
            <select class="form-select w-full" bind:value={cronPreset} on:change={applyCronPreset} style="margin-bottom:0.5rem">
                <option value="">Custom...</option>
                <option value="*/5 * * * *">Every 5 min</option><option value="0 * * * *">Every hour</option>
                <option value="0 8 * * *">Daily 8am</option><option value="0 9 * * 1-5">Weekdays 9am</option>
            </select>
            <input type="text" class="form-input w-full" bind:value={cronExpr} placeholder="min hour day month weekday">
        </div>
        <div class="form-row"><label class="form-label">Prompt</label><textarea class="form-input" bind:value={cronPrompt} rows="2"></textarea></div>
        <div class="form-row"><label class="form-label">Timezone</label><select class="form-select w-full" bind:value={cronTimezone}>
            <option value="America/Los_Angeles">Pacific</option><option value="America/New_York">Eastern</option><option value="UTC">UTC</option>
        </select></div>
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => cronModalOpen = false}>Cancel</button>
        <button class="btn btn-primary" on:click={saveCronJob}>Create</button>
    </div>
</Modal>

<Modal bind:show={projectModalOpen} title={projectModalTitle} width="500px">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">Name</label><input type="text" class="form-input w-full" bind:value={projectName}></div>
        <div class="form-row"><label class="form-label">Description</label><textarea class="form-input" bind:value={projectDesc} rows="3"></textarea></div>
        <div class="form-row"><label class="form-label">Due Date</label><input type="date" class="form-input w-full" bind:value={projectDueDate}></div>
        {#if showProjectStatus}<div class="form-row"><label class="form-label">Status</label><select class="form-select w-full" bind:value={projectStatus}><option value="active">Active</option><option value="completed">Completed</option><option value="archived">Archived</option></select></div>{/if}
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => projectModalOpen = false}>Cancel</button>
        <button class="btn btn-primary" on:click={saveProject}>Save</button>
    </div>
</Modal>

<style>
    .tabs { display: flex; gap: 0; background: var(--surface-1); border-radius: var(--radius-lg); margin-bottom: 2rem; }
    .tab { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; text-transform: uppercase; padding: 0.8rem 1.5rem; cursor: pointer; border-bottom: 3px solid transparent; margin-bottom: -3px; }
    .tab:hover { background: var(--surface-2); border-radius: var(--radius-lg) var(--radius-lg) 0 0; }
    .tab.active { border-bottom-color: var(--primary-container); background: var(--surface-1); }

    .layout { display: grid; grid-template-columns: 220px 1fr; gap: 0; }
    .sidebar { background: var(--surface-1); border-radius: var(--radius-lg); }
    .sidebar-header { padding: 1rem; font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: flex; justify-content: space-between; align-items: center; }
    .project-item { padding: 0.6rem 1rem; font-family: var(--font-grotesk); font-size: 0.8rem; cursor: pointer; display: flex; justify-content: space-between; align-items: center; border-radius: var(--radius-lg); }
    .project-item:hover { background: var(--surface-2); }
    .project-item.active { background: var(--selected-bg); color: var(--selected-text); font-weight: 700; }
    .project-count { font-size: 0.65rem; color: var(--text-muted); }
    .main-content { padding: 2rem; }

    .toolbar { display: flex; gap: 0.5rem; align-items: center; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .toolbar select, .toolbar input { font-family: var(--font-grotesk); font-size: 0.75rem; padding: 0.3rem 0.6rem; border: none; background: var(--input-bg); border-radius: var(--radius-lg); color: var(--text-primary); }
    .toolbar select:focus, .toolbar input:focus { outline: 2px solid var(--primary-container); }

    .board { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.5rem; margin-bottom: 2rem; }
    .column { background: var(--surface-1); border-radius: var(--radius-lg); min-height: 300px; }
    .column-header { padding: 0.8rem 1rem; background: var(--surface-2); border-radius: var(--radius-lg) var(--radius-lg) 0 0; font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; text-transform: uppercase; display: flex; justify-content: space-between; }
    .column-count { background: var(--surface-inverse); color: var(--accent); padding: 0.1rem 0.4rem; font-size: 0.65rem; border-radius: var(--radius-lg); }
    .column-body { padding: 0.5rem; background: var(--surface-1); border-radius: 0 0 var(--radius-lg) var(--radius-lg); }

    .task-card { padding: 0.8rem; margin-bottom: 0.5rem; cursor: pointer; transition: background 0.1s; background: var(--surface-1); border-radius: var(--radius-lg); }
    .task-card:hover { background: var(--hover-accent); }
    .task-card.priority-urgent { border-left: 5px solid var(--red); }
    .task-card.priority-high { border-left: 5px solid #f59e0b; }
    .task-title { font-family: var(--font-grotesk); font-size: 0.8rem; font-weight: 700; margin-bottom: 0.4rem; }
    .task-meta { display: flex; gap: 0.3rem; flex-wrap: wrap; margin-bottom: 0.3rem; }
    .task-desc { font-size: 0.75rem; color: var(--text-muted); max-height: 2.4em; overflow: hidden; }
    .task-footer { display: flex; justify-content: space-between; align-items: center; margin-top: 0.4rem; font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--text-muted); }

    .comment-item { padding: 0.6rem 0; background: var(--surface-1); border-radius: var(--radius-lg); margin-bottom: 0.3rem; }
    .comment-header { font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-bottom: 0.2rem; }
    .comment-body { font-size: 0.85rem; }

    .project-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 0.5rem; }
    .project-card-lg { background: var(--surface-1); border-radius: var(--radius-lg); padding: 1.5rem; }
    .project-card-lg:hover { background: var(--hover-accent); }
    .project-name-lg { font-family: var(--font-grotesk); font-size: 1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .project-desc-lg { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem; }
    .project-stats-lg { display: flex; gap: 1rem; font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); }
    .project-due { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.4rem; }
    .project-due.overdue { color: var(--red, #ef4444); font-weight: 700; }

    /* Milestones */
    .milestones-section { margin-top: 1rem; border-top: 1px solid var(--surface-2); padding-top: 0.8rem; }
    .milestones-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
    .milestones-title { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); }
    .milestone-form { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.5rem; }
    .milestone-form .form-input { font-size: 0.75rem; padding: 0.25rem 0.5rem; }
    .milestone-form .form-select { font-size: 0.75rem; padding: 0.25rem 0.5rem; }
    .milestone-row { display: flex; align-items: center; gap: 0.4rem; padding: 0.3rem 0; font-size: 0.75rem; flex-wrap: wrap; }
    .milestone-name { font-family: var(--font-grotesk); font-weight: 600; cursor: pointer; flex: 1; min-width: 80px; }
    .milestone-name:hover { text-decoration: underline; }
    .milestone-due { font-family: var(--font-mono, monospace); font-size: 0.65rem; color: var(--text-muted); }
    .milestone-tasks { font-size: 0.65rem; color: var(--text-muted); }
    .milestones-empty { font-size: 0.72rem; color: var(--text-muted); font-style: italic; padding: 0.2rem 0; }
    .milestone-status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .milestone-status-dot.status-open { background: var(--gray-mid, #9ca3af); }
    .milestone-status-dot.status-reached { background: #22c55e; }
    .milestone-status-dot.status-missed { background: var(--red, #ef4444); }
    .badge-milestone-open { background: var(--surface-2); color: var(--text-muted); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius-lg); }
    .badge-milestone-reached { background: #dcfce7; color: #15803d; font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius-lg); }
    .badge-milestone-missed { background: #fee2e2; color: #b91c1c; font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius-lg); }

    @media (max-width: 1000px) {
        .layout { grid-template-columns: 1fr; }
        .sidebar { border-radius: var(--radius-lg); margin-bottom: 0.5rem; }
        .board { grid-template-columns: repeat(2, 1fr); }
        .stats-bar { grid-template-columns: repeat(3, 1fr); }
    }
    @media (max-width: 600px) {
        .board { grid-template-columns: 1fr; }
    }
</style>
