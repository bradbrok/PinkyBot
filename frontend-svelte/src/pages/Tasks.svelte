<script>
    import { onMount, onDestroy } from 'svelte';
    import { _ } from 'svelte-i18n';
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
    let taskModalTab = 'content'; // 'content' | 'details'
    let taskFullscreen = false;
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

    // Sprints
    let sprintsByProject = {};          // project_id -> sprint[]
    let sprintFormProjectId = 0;        // which project's inline sprint form is open
    let newSprintName = '';
    let newSprintGoal = '';
    let newSprintStart = '';
    let newSprintEnd = '';

    // Sprints for task modal (for selected project)
    let taskSprints = [];               // sprints for currently-selected project in task modal
    let taskSprintId = '0';

    // Bulk create
    let bulkModalOpen = false;
    let bulkProject = '0';
    let bulkAgent = '';
    let bulkPriority = 'normal';
    let bulkSprintId = '0';
    let bulkMilestoneId = '0';
    let bulkText = '';
    let bulkCreating = false;

    // Milestones for task modal (keyed by project_id)
    let taskMilestones = [];            // milestones for currently-selected project in task modal

    // Board sidebar context panel (for selected project)
    let projectMilestones = [];         // milestones for currently-selected project in board sidebar
    let projectSprints = [];            // sprints for currently-selected project in board sidebar

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

    async function loadProjectContext(projectId) {
        if (!projectId) { projectMilestones = []; projectSprints = []; return; }
        try {
            const [msData, spData] = await Promise.all([
                api('GET', `/projects/${projectId}/milestones`),
                api('GET', `/projects/${projectId}/sprints?include_completed=false`),
            ]);
            projectMilestones = (msData.milestones || []).filter(m => m.status === 'open').sort((a, b) => {
                if (!a.due_date && !b.due_date) return 0;
                if (!a.due_date) return 1;
                if (!b.due_date) return -1;
                return a.due_date.localeCompare(b.due_date);
            });
            projectSprints = spData.sprints || [];
        } catch { projectMilestones = []; projectSprints = []; }
    }

    function selectProject(id) { activeProjectId = id; refresh(); loadProjectContext(id); }

    function openCreateTask() {
        modalTitle = 'New Task'; editTaskId = ''; taskTitle = ''; taskDesc = ''; taskPriority = 'normal';
        taskAgent = ''; taskProject = String(activeProjectId || '0'); taskDueDate = ''; taskTags = '';
        taskMilestoneId = '0'; taskSprintId = '0'; comments = []; newComment = ''; showDelete = false;
        loadTaskMilestones(taskProject); loadTaskSprints(taskProject); taskModalTab = 'content'; taskModalOpen = true;
    }

    async function openEditTask(taskId) {
        const data = await api('GET', `/tasks/${taskId}`);
        const task = data.task;
        modalTitle = `Task #${task.id}`; editTaskId = task.id; taskTitle = task.title;
        taskDesc = task.description; taskPriority = task.priority; taskAgent = task.assigned_agent;
        taskProject = String(task.project_id || '0'); taskDueDate = task.due_date; taskTags = task.tags.join(', ');
        taskMilestoneId = String(task.milestone_id || '0');
        taskSprintId = String(task.sprint_id || '0');
        await Promise.all([loadTaskMilestones(taskProject), loadTaskSprints(taskProject)]);
        comments = data.comments || []; showDelete = true; taskModalTab = 'content'; taskModalOpen = true;
    }

    async function saveTask() {
        if (!taskTitle.trim()) { toast('Title is required', 'error'); return; }
        const tags = taskTags.split(',').map(t => t.trim()).filter(Boolean);
        const payload = { title: taskTitle, description: taskDesc, priority: taskPriority, assigned_agent: taskAgent, project_id: parseInt(taskProject) || 0, milestone_id: parseInt(taskMilestoneId) || 0, sprint_id: parseInt(taskSprintId) || 0, due_date: taskDueDate, tags };
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
        const [details, msData, spData] = await Promise.all([
            Promise.all(projects.map(p => api('GET', `/projects/${p.id}`))),
            Promise.all(projects.map(p => api('GET', `/projects/${p.id}/milestones`))),
            Promise.all(projects.map(p => api('GET', `/projects/${p.id}/sprints?include_completed=true`))),
        ]);
        projectCards = projects.map((p, i) => ({ ...p, taskCount: details[i].task_count || 0 }));
        const newMsByProject = {};
        const newSpByProject = {};
        projects.forEach((p, i) => {
            newMsByProject[p.id] = msData[i].milestones || [];
            newSpByProject[p.id] = spData[i].sprints || [];
        });
        milestonesByProject = newMsByProject;
        sprintsByProject = newSpByProject;
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

    // Load sprints for the selected project in task modal (only planned/active)
    async function loadTaskSprints(projectId) {
        if (!projectId || projectId === '0' || projectId === 0) { taskSprints = []; return; }
        try {
            const data = await api('GET', `/projects/${projectId}/sprints?include_completed=false`);
            taskSprints = (data.sprints || []).filter(s => s.status !== 'completed');
        } catch { taskSprints = []; }
    }

    // Sprint management functions
    function openSprintForm(projectId) { sprintFormProjectId = projectId; newSprintName = ''; newSprintGoal = ''; newSprintStart = ''; newSprintEnd = ''; }
    function closeSprintForm() { sprintFormProjectId = 0; }

    async function addSprint(projectId) {
        if (!newSprintName.trim()) { toast('Name required', 'error'); return; }
        await api('POST', `/projects/${projectId}/sprints`, { name: newSprintName, goal: newSprintGoal, start_date: newSprintStart, end_date: newSprintEnd });
        toast('Sprint added'); closeSprintForm(); refreshProjects();
    }

    async function deleteSprint(id) {
        if (!confirm('Delete sprint? Tasks in this sprint will be unlinked.')) return;
        await api('DELETE', `/sprints/${id}`); toast('Sprint deleted'); refreshProjects();
    }

    async function startSprint(id) {
        await api('POST', `/sprints/${id}/start`); toast('Sprint started'); refreshProjects();
    }

    async function completeSprint(id) {
        if (!confirm('Complete this sprint?')) return;
        await api('POST', `/sprints/${id}/complete`); toast('Sprint completed'); refreshProjects();
    }

    // Bulk create
    function parseBulkTasks(text) {
        return text.split('\n')
            .map(line => line.replace(/^[-*•\d.]+\s*/, '').trim())
            .filter(Boolean)
            .map(line => {
                const sep = line.indexOf('::');
                if (sep !== -1) return { title: line.slice(0, sep).trim(), description: line.slice(sep + 2).trim() };
                return { title: line, description: '' };
            });
    }

    function openBulkCreate() {
        bulkProject = String(activeProjectId || '0');
        bulkAgent = ''; bulkPriority = 'normal'; bulkSprintId = '0'; bulkMilestoneId = '0';
        bulkText = ''; bulkCreating = false;
        loadTaskMilestones(bulkProject); loadTaskSprints(bulkProject);
        bulkModalOpen = true;
    }

    async function createBulkTasks() {
        const parsed = parseBulkTasks(bulkText);
        if (!parsed.length) { toast('No tasks to create', 'error'); return; }
        bulkCreating = true;
        let created = 0, failed = 0;
        for (const t of parsed) {
            try {
                await api('POST', '/tasks', {
                    title: t.title, description: t.description, priority: bulkPriority,
                    assigned_agent: bulkAgent, project_id: parseInt(bulkProject) || 0,
                    sprint_id: parseInt(bulkSprintId) || 0, milestone_id: parseInt(bulkMilestoneId) || 0,
                    created_by: 'user',
                });
                created++;
            } catch { failed++; }
        }
        bulkCreating = false;
        toast(`Created ${created} task${created !== 1 ? 's' : ''}${failed ? ` (${failed} failed)` : ''}`);
        bulkModalOpen = false; bulkText = ''; refresh();
    }

    onMount(() => { refresh(); refreshInterval = setInterval(refresh, 15000); });
    onDestroy(() => { clearInterval(refreshInterval); });
</script>

<div class="content">
    <div class="stats-grid">
        <div class="stat-card"><div class="stat-value">{statPending}</div><div class="stat-label">{$_('tasks.stat_pending')}</div></div>
        <div class="stat-card"><div class="stat-value">{statProgress}</div><div class="stat-label">{$_('tasks.stat_in_progress')}</div></div>
        <div class="stat-card"><div class="stat-value">{statBlocked}</div><div class="stat-label">{$_('tasks.stat_blocked')}</div></div>
        <div class="stat-card"><div class="stat-value">{statCompleted}</div><div class="stat-label">{$_('tasks.stat_completed')}</div></div>
        <div class="stat-card"><div class="stat-value">{statTotal}</div><div class="stat-label">{$_('tasks.stat_total')}</div></div>
    </div>

    <!-- Tabs -->
    <div class="tab-bar">
        <button class="tab-btn" class:active={activeTab === 'board'} on:click={() => switchTab('board')}>{$_('tasks.tab_board')}</button>
        <button class="tab-btn" class:active={activeTab === 'projects'} on:click={() => switchTab('projects')}>{$_('tasks.tab_projects')}</button>
        <button class="tab-btn" class:active={activeTab === 'cron'} on:click={() => switchTab('cron')}>{$_('tasks.tab_cron')}</button>
    </div>

    <!-- Board Tab -->
    {#if activeTab === 'board'}
        <div class="layout">
            <div class="sidebar">
                <div class="sidebar-header"><span>{$_('tasks.projects')}</span><button class="btn btn-sm btn-primary" on:click={createProject}>+</button></div>
                <div class="project-item" class:active={activeProjectId === 0} on:click={() => selectProject(0)}>
                    <span>{$_('tasks.all_tasks')}</span><span class="project-count">{statTotal}</span>
                </div>
                {#each projectsList as p}
                    {@const activeSprint = (sprintsByProject[p.id] || []).find(s => s.status === 'active')}
                    <div class="project-item" class:active={activeProjectId === p.id} on:click={() => selectProject(p.id)}>
                        <span>{p.name}</span>
                        {#if activeSprint}<span class="sprint-badge">{activeSprint.name}</span>{/if}
                    </div>
                {/each}

                <!-- Project context panel: shown when a specific project is selected -->
                {#if activeProjectId !== 0}
                    {@const activeSprint = projectSprints.find(s => s.status === 'active')}
                    {@const today = new Date().toISOString().slice(0, 10)}
                    <div class="ctx-panel">

                        <!-- Active sprint banner -->
                        {#if activeSprint}
                            {@const sprintTotal = activeSprint.task_counts ? activeSprint.task_counts.total : 0}
                            {@const sprintDone = activeSprint.task_counts ? activeSprint.task_counts.completed : 0}
                            {@const sprintPct = sprintTotal > 0 ? Math.round((sprintDone / sprintTotal) * 100) : 0}
                            {@const daysLeft = activeSprint.end_date ? Math.ceil((new Date(activeSprint.end_date + 'T00:00:00') - new Date()) / 86400000) : null}
                            <div class="ctx-sprint-banner">
                                <div class="ctx-sprint-name">{activeSprint.name}</div>
                                {#if activeSprint.goal}<div class="ctx-sprint-goal">{activeSprint.goal}</div>{/if}
                                {#if activeSprint.start_date || activeSprint.end_date}
                                    <div class="ctx-sprint-dates">{activeSprint.start_date || '?'} – {activeSprint.end_date || '?'}</div>
                                {/if}
                                <div class="context-bar" style="margin:0.35rem 0 0.2rem">
                                    <div class="context-fill" style="width:{sprintPct}%"></div>
                                </div>
                                <div class="ctx-sprint-meta">
                                    <span>{sprintDone}/{sprintTotal} done</span>
                                    {#if daysLeft !== null}<span class:ctx-overdue={daysLeft < 0}>{daysLeft < 0 ? `${Math.abs(daysLeft)}d overdue` : `${daysLeft}d left`}</span>{/if}
                                </div>
                            </div>
                        {/if}

                        <!-- Milestones -->
                        {#if projectMilestones.length > 0}
                            <div class="ctx-section-label">{$_('tasks.milestones')}</div>
                            {#each projectMilestones.slice(0, 3) as m}
                                {@const isOverdue = m.due_date && m.due_date < today}
                                <div class="ctx-milestone-row">
                                    <span class="milestone-status-dot status-{m.status}"></span>
                                    <span class="ctx-milestone-name" class:ctx-overdue={isOverdue}>{m.name}</span>
                                    <span class="ctx-milestone-meta" class:ctx-overdue={isOverdue}>{m.due_date || ''}</span>
                                    {#if m.task_count > 0 && m.completed_task_count !== undefined}
                                        <span class="ctx-milestone-tasks">{m.completed_task_count}/{m.task_count}</span>
                                    {:else if m.task_count > 0}
                                        <span class="ctx-milestone-tasks">{m.task_count}t</span>
                                    {/if}
                                </div>
                            {/each}
                            {#if projectMilestones.length > 3}
                                <div class="ctx-see-all" on:click={() => switchTab('projects')}>see all ({projectMilestones.length}) →</div>
                            {/if}
                        {/if}

                        <!-- Project stats -->
                        <div class="ctx-section-label">{$_('tasks.stats')}</div>
                        <div class="ctx-stats">
                            <div class="ctx-stat"><div class="ctx-stat-val">{columns.pending.length}</div><div class="ctx-stat-lbl">{$_('tasks.stat_pending')}</div></div>
                            <div class="ctx-stat"><div class="ctx-stat-val">{columns.in_progress.length}</div><div class="ctx-stat-lbl">{$_('tasks.stat_active')}</div></div>
                            <div class="ctx-stat"><div class="ctx-stat-val">{columns.blocked.length}</div><div class="ctx-stat-lbl">{$_('tasks.stat_blocked')}</div></div>
                            <div class="ctx-stat"><div class="ctx-stat-val">{columns.completed.length}</div><div class="ctx-stat-lbl">{$_('tasks.stat_done')}</div></div>
                        </div>
                    </div>
                {/if}
            </div>
            <div class="main-content">
                <div class="toolbar">
                    <button class="btn btn-primary" on:click={openCreateTask}>+ {$_('tasks.new_task')}</button>
                    <button class="btn" on:click={openBulkCreate}>⇉ {$_('tasks.bulk_import')}</button>
                    <select bind:value={filterAgent} on:change={refresh}>
                        <option value="">{$_('tasks.all_agents')}</option>
                        {#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}
                    </select>
                    <select bind:value={filterPriority} on:change={refresh}>
                        <option value="">{$_('tasks.all_priorities')}</option>
                        <option value="urgent">{$_('tasks.priority_urgent')}</option><option value="high">{$_('tasks.priority_high')}</option>
                        <option value="normal">{$_('tasks.priority_normal')}</option><option value="low">{$_('tasks.priority_low')}</option>
                    </select>
                    <input type="text" bind:value={filterTag} placeholder={$_('tasks.filter_by_tag')} on:keydown={e => { if (e.key === 'Enter') refresh(); }} style="width:120px">
                </div>
                <div class="board">
                    {#each [['pending', $_('tasks.col_pending')],['in_progress', $_('tasks.col_in_progress')],['blocked', $_('tasks.col_blocked')],['completed', $_('tasks.col_completed')]] as [key, label]}
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
                                    <div style="padding:1rem;text-align:center;color:var(--gray-mid);font-family:var(--font-grotesk);font-size:0.75rem">{$_('common.empty')}</div>
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
        <div class="section">
            <div class="section-header">
                <div class="section-title">{$_('tasks.projects')}</div>
                <button class="btn btn-primary" on:click={createProject}>+ {$_('tasks.new_project')}</button>
            </div>
        </div>
        {#if projectCards.length === 0}
            <div class="empty">{$_('tasks.no_projects')}</div>
        {:else}
            <div class="project-grid">
                {#each projectCards as p}
                    <div class="project-card-lg">
                        <div class="project-card-header">
                            <div class="project-name-lg">{p.name}</div>
                            <span class="badge badge-{p.status === 'active' ? 'normal' : 'low'}">{p.status}</span>
                        </div>
                        <div class="project-desc-lg">{p.description || $_('tasks.no_description')}</div>
                        {#if p.due_date}
                            <div class="project-due" class:overdue={p.due_date < new Date().toISOString().slice(0,10)}>
                                Due: {new Date(p.due_date + 'T00:00:00').toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })}
                            </div>
                        {/if}
                        <div class="project-stats-lg"><span>{p.taskCount} task{p.taskCount !== 1 ? 's' : ''}</span><span>created {timeAgo(p.created_at)}</span></div>

                        <!-- Milestones section -->
                        <div class="milestones-section">
                            <div class="milestones-header">
                                <span class="milestones-title">{$_('tasks.milestones')}</span>
                                <button class="btn btn-sm" on:click={() => openMilestoneForm(p.id)}>+ {$_('common.add')}</button>
                            </div>
                            {#if milestoneFormProjectId === p.id}
                                <div class="milestone-form">
                                    <input type="text" class="form-input" bind:value={newMilestoneName} placeholder={$_('tasks.milestone_name_placeholder')} style="flex:1">
                                    <input type="date" class="form-input" bind:value={newMilestoneDueDate} style="width:140px">
                                    <button class="btn btn-sm btn-primary" on:click={() => addMilestone(p.id)}>{$_('common.add')}</button>
                                    <button class="btn btn-sm" on:click={closeMilestoneForm}>{$_('common.cancel')}</button>
                                </div>
                            {/if}
                            {#each (milestonesByProject[p.id] || []) as m}
                                {#if editMilestoneId === m.id}
                                    <div class="milestone-form">
                                        <input type="text" class="form-input" bind:value={editMilestoneName} style="flex:1">
                                        <input type="date" class="form-input" bind:value={editMilestoneDueDate} style="width:140px">
                                        <select class="form-select" bind:value={editMilestoneStatus} style="width:90px">
                                            <option value="open">{$_('tasks.milestone_open')}</option>
                                            <option value="reached">{$_('tasks.milestone_reached')}</option>
                                            <option value="missed">{$_('tasks.milestone_missed')}</option>
                                        </select>
                                        <button class="btn btn-sm btn-primary" on:click={saveEditMilestone}>{$_('common.save')}</button>
                                        <button class="btn btn-sm" on:click={cancelEditMilestone}>{$_('common.cancel')}</button>
                                    </div>
                                {:else}
                                    <div class="milestone-row">
                                        <span class="milestone-status-dot status-{m.status}"></span>
                                        <span class="milestone-name" on:click={() => startEditMilestone(m)} title={$_('tasks.click_to_edit')}>{m.name}</span>
                                        {#if m.due_date}<span class="milestone-due">{m.due_date}</span>{/if}
                                        <span class="badge badge-milestone-{m.status}">{m.status}</span>
                                        {#if m.task_count > 0}<span class="milestone-tasks">{m.task_count} task{m.task_count !== 1 ? 's' : ''}</span>{/if}
                                        <button class="btn btn-sm btn-danger" on:click={() => deleteMilestone(m.id)}>x</button>
                                    </div>
                                {/if}
                            {:else}
                                <div class="milestones-empty">{$_('tasks.no_milestones')}</div>
                            {/each}
                        </div>

                        <!-- Sprints section -->
                        <div class="sprints-section">
                            <div class="sprints-header">
                                <span class="sprints-title">{$_('tasks.sprints')}</span>
                                <button class="btn btn-sm" on:click={() => openSprintForm(p.id)}>+ {$_('common.add')}</button>
                            </div>
                            {#if sprintFormProjectId === p.id}
                                <div class="sprint-form">
                                    <input type="text" class="form-input" bind:value={newSprintName} placeholder={$_('tasks.sprint_name_placeholder')} style="flex:1;min-width:100px">
                                    <input type="text" class="form-input" bind:value={newSprintGoal} placeholder={$_('tasks.sprint_goal_placeholder')} style="flex:1;min-width:80px">
                                    <input type="date" class="form-input" bind:value={newSprintStart} style="width:130px">
                                    <input type="date" class="form-input" bind:value={newSprintEnd} style="width:130px">
                                    <button class="btn btn-sm btn-primary" on:click={() => addSprint(p.id)}>{$_('common.add')}</button>
                                    <button class="btn btn-sm" on:click={closeSprintForm}>{$_('common.cancel')}</button>
                                </div>
                            {/if}
                            {#each (sprintsByProject[p.id] || []) as s}
                                <div class="sprint-row">
                                    <span class="sprint-status-dot status-sprint-{s.status}"></span>
                                    <span class="sprint-name">{s.name}</span>
                                    {#if s.start_date || s.end_date}<span class="sprint-dates">{s.start_date}{s.start_date && s.end_date ? ' – ' : ''}{s.end_date}</span>{/if}
                                    <span class="badge badge-sprint-{s.status}">{s.status}</span>
                                    {#if s.task_counts && s.task_counts.total > 0}<span class="sprint-tasks">{s.task_counts.completed}/{s.task_counts.total}</span>{/if}
                                    {#if s.status === 'planned'}<button class="btn btn-sm" on:click={() => startSprint(s.id)}>{$_('tasks.sprint_start')}</button>{/if}
                                    {#if s.status === 'active'}<button class="btn btn-sm btn-success" on:click={() => completeSprint(s.id)}>{$_('tasks.sprint_complete')}</button>{/if}
                                    <button class="btn btn-sm btn-danger" on:click={() => deleteSprint(s.id)}>x</button>
                                    {#if s.task_counts && s.task_counts.total > 0}
                                        {@const spPct = Math.round((s.task_counts.completed / s.task_counts.total) * 100)}
                                        <div class="sprint-progress-wrap">
                                            <div class="sprint-progress-fill {s.status === 'completed' ? 'sprint-progress-done' : ''}" style="width:{spPct}%"></div>
                                        </div>
                                    {/if}
                                </div>
                            {:else}
                                <div class="sprints-empty">{$_('tasks.no_sprints')}</div>
                            {/each}
                        </div>

                        <div class="project-actions">
                            <button class="btn btn-sm btn-primary" on:click={() => { selectProject(p.id); switchTab('board'); }}>{$_('tasks.view_board')}</button>
                            <button class="btn btn-sm" on:click={() => editProject(p)}>{$_('common.edit')}</button>
                            {#if p.status !== 'archived'}<button class="btn btn-sm" on:click={() => archiveProject(p.id)}>{$_('common.archive')}</button>{/if}
                            <button class="btn btn-sm btn-danger" on:click={() => deleteProjectFromTab(p.id)}>{$_('common.delete')}</button>
                        </div>
                    </div>
                {/each}
            </div>
        {/if}
    {/if}

    <!-- Cron Tab -->
    {#if activeTab === 'cron'}
        <div class="section">
            <div class="section-header">
                <div class="section-title">{$_('tasks.cron_title')}</div>
                <button class="btn btn-primary" on:click={createCronJob}>+ {$_('tasks.new_schedule')}</button>
            </div>
        </div>
        <div class="section section-body">
            {#if cronJobs.length === 0}
                <div class="empty">{$_('tasks.no_cron_jobs')}</div>
            {:else}
                <table class="data-table">
                    <thead><tr><th>{$_('dashboard.col_agent')}</th><th>{$_('tasks.col_name')}</th><th>{$_('tasks.col_schedule')}</th><th>{$_('tasks.col_prompt')}</th><th>{$_('tasks.col_timezone')}</th><th>{$_('dashboard.col_status')}</th><th>{$_('tasks.col_last_run')}</th><th>{$_('tasks.col_actions')}</th></tr></thead>
                    <tbody>
                        {#each cronJobs as s}
                            <tr>
                                <td class="mono" style="font-weight:700">{s.agent_name}</td>
                                <td class="mono">{s.name || '--'}</td>
                                <td class="mono" style="background:var(--accent-soft)">{s.cron}</td>
                                <td style="max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{s.prompt || '--'}</td>
                                <td class="mono" style="font-size:0.75rem">{s.timezone}</td>
                                <td><span class="badge badge-{s.enabled ? 'normal' : 'low'}">{s.enabled ? $_('tasks.cron_active') : $_('tasks.cron_off')}</span></td>
                                <td class="mono" style="font-size:0.75rem">{s.last_run ? new Date(s.last_run * 1000).toLocaleString() : $_('tasks.never')}</td>
                                <td>
                                    <button class="btn btn-sm" on:click={() => toggleCron(s.agent_name, s.id, !s.enabled)}>{s.enabled ? $_('tasks.disable') : $_('tasks.enable')}</button>
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

<Modal bind:show={taskModalOpen} title={modalTitle} width="700px" footerClass="spread" fullscreen={taskFullscreen}>
    <button slot="headerActions" class="modal-close" on:click={() => taskFullscreen = !taskFullscreen} title={taskFullscreen ? $_('tasks.exit_fullscreen') : $_('tasks.fullscreen')} style="font-size:0.95rem">{taskFullscreen ? '⊡' : '⛶'}</button>
    <div class="modal-form">
        <!-- Inner tab bar -->
        <div class="task-inner-tabs">
            <button class="tab-btn" class:active={taskModalTab === 'content'} on:click={() => taskModalTab = 'content'}>{$_('tasks.tab_content')}</button>
            <button class="tab-btn" class:active={taskModalTab === 'details'} on:click={() => taskModalTab = 'details'}>{$_('tasks.tab_details')}</button>
        </div>

        <!-- Content tab -->
        {#if taskModalTab === 'content'}
            <div class="task-tab-content">
                <div class="form-row"><label class="form-label">{$_('tasks.title')}</label><input type="text" class="form-input w-full" bind:value={taskTitle}></div>
                <div class="form-row" style="flex:1;display:flex;flex-direction:column">
                    <label class="form-label">{$_('tasks.description')}</label>
                    <textarea class="form-input task-desc-textarea" bind:value={taskDesc} rows="10" placeholder={$_('tasks.desc_placeholder')}></textarea>
                </div>
            </div>
        {/if}

        <!-- Details tab -->
        {#if taskModalTab === 'details'}
            <div class="task-tab-content">
                <div class="form-row-inline">
                    <div class="form-row"><label class="form-label">{$_('tasks.priority')}</label><select class="form-select w-full" bind:value={taskPriority}><option value="normal">{$_('tasks.priority_normal')}</option><option value="low">{$_('tasks.priority_low')}</option><option value="high">{$_('tasks.priority_high')}</option><option value="urgent">{$_('tasks.priority_urgent')}</option></select></div>
                    <div class="form-row"><label class="form-label">{$_('tasks.assign_to')}</label><select class="form-select w-full" bind:value={taskAgent}><option value="">{$_('tasks.unassigned')}</option>{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
                </div>
                <div class="form-row-inline">
                    <div class="form-row"><label class="form-label">{$_('tasks.project')}</label><select class="form-select w-full" bind:value={taskProject} on:change={() => { taskMilestoneId = '0'; taskSprintId = '0'; loadTaskMilestones(taskProject); loadTaskSprints(taskProject); }}><option value="0">{$_('common.none')}</option>{#each projectsList as p}<option value={p.id}>{p.name}</option>{/each}</select></div>
                    <div class="form-row"><label class="form-label">{$_('tasks.due_date')}</label><input type="date" class="form-input w-full" bind:value={taskDueDate}></div>
                </div>
                <div class="form-row-inline">
                    <div class="form-row"><label class="form-label">{$_('tasks.milestone')}</label><select class="form-select w-full" bind:value={taskMilestoneId}><option value="0">{$_('tasks.no_milestone')}</option>{#each taskMilestones as m}<option value={String(m.id)}>{m.name}{m.due_date ? ' (' + m.due_date + ')' : ''}</option>{/each}</select></div>
                    <div class="form-row"><label class="form-label">{$_('tasks.sprint')}</label><select class="form-select w-full" bind:value={taskSprintId}><option value="0">{$_('tasks.no_sprint')}</option>{#each taskSprints as s}<option value={String(s.id)}>{s.name} ({s.status})</option>{/each}</select></div>
                </div>
                <div class="form-row">
                    <label class="form-label">{$_('tasks.tags')}</label><input type="text" class="form-input w-full" bind:value={taskTags} placeholder={$_('tasks.tags_placeholder')}>
                </div>
                {#if editTaskId}
                    <div class="surface-panel">
                        <label class="form-label">{$_('tasks.activity')}</label>
                        {#each comments as c}
                            <div class="comment-item"><div class="comment-header"><strong>{c.author || $_('tasks.unknown')}</strong> &middot; {timeAgo(c.created_at)}</div><div class="comment-body">{c.content}</div></div>
                        {:else}
                            <div class="muted-note" style="padding:0.5rem 0">{$_('tasks.no_activity')}</div>
                        {/each}
                        <div class="inline-spread" style="margin-top:0.75rem">
                            <input type="text" class="form-input grow" bind:value={newComment} placeholder={$_('tasks.add_comment')}>
                            <button class="btn btn-sm btn-primary" on:click={addComment}>{$_('tasks.post')}</button>
                        </div>
                    </div>
                {/if}
            </div>
        {/if}
    </div>
    <div slot="footer" class="inline-spread grow">
        <div class="inline-spread">
            {#if showDelete}<button class="btn btn-danger btn-sm" on:click={deleteTask}>{$_('common.delete')}</button>{/if}
        </div>
        <div class="inline-spread">
            <button class="btn" on:click={() => { taskModalOpen = false; taskFullscreen = false; }}>{$_('common.cancel')}</button>
            <button class="btn btn-primary" on:click={saveTask}>{$_('common.save')}</button>
        </div>
    </div>
</Modal>

<Modal bind:show={cronModalOpen} title={$_('tasks.new_cron_schedule')} width="550px">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">{$_('dashboard.col_agent')}</label><select class="form-select w-full" bind:value={cronAgent}>{#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}</select></div>
        <div class="form-row"><label class="form-label">{$_('tasks.col_name')}</label><input type="text" class="form-input w-full" bind:value={cronName}></div>
        <div class="form-row"><label class="form-label">{$_('tasks.cron_expression')}</label>
            <select class="form-select w-full" bind:value={cronPreset} on:change={applyCronPreset} style="margin-bottom:0.5rem">
                <option value="">{$_('tasks.cron_custom')}</option>
                <option value="*/5 * * * *">{$_('tasks.cron_every_5min')}</option><option value="0 * * * *">{$_('tasks.cron_every_hour')}</option>
                <option value="0 8 * * *">{$_('tasks.cron_daily_8am')}</option><option value="0 9 * * 1-5">{$_('tasks.cron_weekdays_9am')}</option>
            </select>
            <input type="text" class="form-input w-full" bind:value={cronExpr} placeholder={$_('tasks.cron_expr_placeholder')}>
        </div>
        <div class="form-row"><label class="form-label">{$_('tasks.col_prompt')}</label><textarea class="form-input" bind:value={cronPrompt} rows="2"></textarea></div>
        <div class="form-row"><label class="form-label">{$_('tasks.col_timezone')}</label><select class="form-select w-full" bind:value={cronTimezone}>
            <option value="America/Los_Angeles">Pacific</option><option value="America/New_York">Eastern</option><option value="UTC">UTC</option>
        </select></div>
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => cronModalOpen = false}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={saveCronJob}>{$_('common.create')}</button>
    </div>
</Modal>

<Modal bind:show={bulkModalOpen} title={$_('tasks.bulk_import')} width="560px">
    <div class="modal-form">
        <div class="form-row-inline">
            <div class="form-row">
                <label class="form-label">{$_('tasks.project')}</label>
                <select class="form-select w-full" bind:value={bulkProject} on:change={() => { bulkSprintId='0'; bulkMilestoneId='0'; loadTaskMilestones(bulkProject); loadTaskSprints(bulkProject); }}>
                    <option value="0">{$_('common.none')}</option>
                    {#each projectsList as p}<option value={p.id}>{p.name}</option>{/each}
                </select>
            </div>
            <div class="form-row">
                <label class="form-label">{$_('tasks.assign_to')}</label>
                <select class="form-select w-full" bind:value={bulkAgent}>
                    <option value="">{$_('tasks.unassigned')}</option>
                    {#each agentsList as a}<option value={a.name}>{a.display_name || a.name}</option>{/each}
                </select>
            </div>
        </div>
        <div class="form-row-inline">
            <div class="form-row">
                <label class="form-label">{$_('tasks.priority')}</label>
                <select class="form-select w-full" bind:value={bulkPriority}>
                    <option value="normal">{$_('tasks.priority_normal')}</option><option value="low">{$_('tasks.priority_low')}</option>
                    <option value="high">{$_('tasks.priority_high')}</option><option value="urgent">{$_('tasks.priority_urgent')}</option>
                </select>
            </div>
            <div class="form-row">
                <label class="form-label">{$_('tasks.sprint')}</label>
                <select class="form-select w-full" bind:value={bulkSprintId}>
                    <option value="0">{$_('tasks.no_sprint')}</option>
                    {#each taskSprints as s}<option value={String(s.id)}>{s.name} ({s.status})</option>{/each}
                </select>
            </div>
            <div class="form-row">
                <label class="form-label">{$_('tasks.milestone')}</label>
                <select class="form-select w-full" bind:value={bulkMilestoneId}>
                    <option value="0">{$_('tasks.no_milestone')}</option>
                    {#each taskMilestones as m}<option value={String(m.id)}>{m.name}</option>{/each}
                </select>
            </div>
        </div>
        <div class="form-row">
            <label class="form-label">{$_('tasks.bulk_tasks_label')}</label>
            <textarea class="form-input bulk-textarea" bind:value={bulkText} rows="10" placeholder="Set up CI/CD pipeline&#10;Write unit tests :: Cover core business logic&#10;Deploy to staging&#10;- Review PRD and acceptance criteria"></textarea>
        </div>
        {#if bulkText.trim()}
            {@const parsed = parseBulkTasks(bulkText)}
            <div class="bulk-preview">
                <div class="bulk-preview-label">{parsed.length} {$_('tasks.tasks_to_create', { values: { count: parsed.length } })}</div>
                {#each parsed.slice(0, 6) as t}
                    <div class="bulk-preview-row">
                        <span class="bulk-preview-title">{t.title}</span>
                        {#if t.description}<span class="bulk-preview-desc">{t.description}</span>{/if}
                    </div>
                {/each}
                {#if parsed.length > 6}<div class="bulk-preview-more">+{parsed.length - 6} more…</div>{/if}
            </div>
        {/if}
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => bulkModalOpen = false}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={createBulkTasks} disabled={bulkCreating || !bulkText.trim()}>
            {bulkCreating ? $_('tasks.creating') : $_('tasks.create_n_tasks', { values: { count: parseBulkTasks(bulkText).length } })}
        </button>
    </div>
</Modal>

<Modal bind:show={projectModalOpen} title={projectModalTitle} width="500px">
    <div class="modal-form">
        <div class="form-row"><label class="form-label">{$_('tasks.name')}</label><input type="text" class="form-input w-full" bind:value={projectName}></div>
        <div class="form-row"><label class="form-label">{$_('tasks.description')}</label><textarea class="form-input" bind:value={projectDesc} rows="3"></textarea></div>
        <div class="form-row"><label class="form-label">{$_('tasks.due_date')}</label><input type="date" class="form-input w-full" bind:value={projectDueDate}></div>
        {#if showProjectStatus}<div class="form-row"><label class="form-label">{$_('dashboard.col_status')}</label><select class="form-select w-full" bind:value={projectStatus}><option value="active">{$_('tasks.status_active')}</option><option value="completed">{$_('tasks.stat_completed')}</option><option value="archived">{$_('tasks.status_archived')}</option></select></div>{/if}
    </div>
    <div slot="footer" class="inline-spread">
        <button class="btn" on:click={() => projectModalOpen = false}>{$_('common.cancel')}</button>
        <button class="btn btn-primary" on:click={saveProject}>{$_('common.save')}</button>
    </div>
</Modal>

<style>
    .tab-bar { display: flex; gap: 0.4rem; margin-bottom: 1.5rem; flex-wrap: wrap; }
    .tab-btn { padding: 0.4rem 1rem; font-size: 0.85rem; font-weight: 600; font-family: var(--font-grotesk); background: none; border: none; border-radius: 4px; color: var(--text-primary, #111); cursor: pointer; letter-spacing: 0.02em; transition: background 0.12s; }
    .tab-btn:hover { background: rgba(0,0,0,0.06); }
    .task-inner-tabs { display: flex; gap: 0.3rem; margin-bottom: 1rem; border-bottom: 1px solid var(--surface-3); padding-bottom: 0.5rem; }
    .task-tab-content { display: flex; flex-direction: column; gap: 0.9rem; flex: 1; }
    .task-desc-textarea { flex: 1; resize: vertical; min-height: 200px; }
    .tab-btn.active { background: var(--accent, #f5c842); color: #000; }

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
    .project-card-header { display: flex; justify-content: space-between; align-items: flex-start; }
    .project-name-lg { font-family: var(--font-grotesk); font-size: 1rem; font-weight: 700; margin-bottom: 0.3rem; }
    .project-desc-lg { font-size: 0.85rem; color: var(--text-muted); margin-bottom: 0.8rem; }
    .project-stats-lg { display: flex; gap: 1rem; font-family: var(--font-grotesk); font-size: 0.7rem; color: var(--text-muted); margin-bottom: 0.5rem; }
    .project-due { font-family: var(--font-grotesk); font-size: 0.75rem; color: var(--text-muted); margin-bottom: 0.4rem; }
    .project-due.overdue { color: var(--red); font-weight: 700; }
    .project-actions { margin-top: 0.8rem; display: flex; gap: 0.3rem; flex-wrap: wrap; }

    /* Milestones */
    .milestones-section { margin-top: 1rem; background: var(--surface-2); border-radius: var(--radius-lg); padding: 0.7rem 0.9rem; }
    .milestones-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
    .milestones-title { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); }
    .milestone-form { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.5rem; }
    .milestone-form .form-input { font-size: 0.75rem; padding: 0.25rem 0.5rem; }
    .milestone-form .form-select { font-size: 0.75rem; padding: 0.25rem 0.5rem; }
    .milestone-row { display: flex; align-items: center; gap: 0.4rem; padding: 0.25rem 0; font-size: 0.75rem; flex-wrap: wrap; }
    .milestone-name { font-family: var(--font-grotesk); font-weight: 600; cursor: pointer; flex: 1; min-width: 80px; }
    .milestone-name:hover { text-decoration: underline; }
    .milestone-due { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); }
    .milestone-tasks { font-size: 0.65rem; color: var(--text-muted); }
    .milestones-empty { font-size: 0.72rem; color: var(--text-muted); font-style: italic; padding: 0.2rem 0; }
    .milestone-status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .milestone-status-dot.status-open { background: var(--text-muted); }
    .milestone-status-dot.status-reached { background: var(--green); }
    .milestone-status-dot.status-missed { background: var(--red); }
    .badge-milestone-open { background: var(--tone-neutral-bg); color: var(--tone-neutral-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .badge-milestone-reached { background: var(--tone-success-bg); color: var(--tone-success-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .badge-milestone-missed { background: var(--tone-error-bg); color: var(--tone-error-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }

    /* Sprints */
    .sprints-section { margin-top: 0.5rem; background: var(--surface-2); border-radius: var(--radius-lg); padding: 0.7rem 0.9rem; }
    .sprints-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem; }
    .sprints-title { font-family: var(--font-grotesk); font-size: 0.7rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); }
    .sprint-form { display: flex; gap: 0.4rem; align-items: center; flex-wrap: wrap; margin-bottom: 0.5rem; }
    .sprint-form .form-input { font-size: 0.75rem; padding: 0.25rem 0.5rem; }
    .sprint-row { display: flex; align-items: center; gap: 0.4rem; padding: 0.25rem 0; font-size: 0.75rem; flex-wrap: wrap; }
    .sprint-name { font-family: var(--font-grotesk); font-weight: 600; flex: 1; min-width: 80px; }
    .sprint-dates { font-family: var(--font-grotesk); font-size: 0.65rem; color: var(--text-muted); }
    .sprint-tasks { font-size: 0.65rem; color: var(--text-muted); background: var(--surface-3); padding: 0.1rem 0.35rem; border-radius: var(--radius); }
    .sprints-empty { font-size: 0.72rem; color: var(--text-muted); font-style: italic; padding: 0.2rem 0; }
    .sprint-status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }
    .sprint-status-dot.status-sprint-planned { background: var(--text-muted); }
    .sprint-status-dot.status-sprint-active { background: var(--warn-outline); }
    .sprint-status-dot.status-sprint-completed { background: var(--green); }
    .badge-sprint-planned { background: var(--tone-neutral-bg); color: var(--tone-neutral-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .badge-sprint-active { background: var(--tone-warning-bg); color: var(--tone-warning-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .badge-sprint-completed { background: var(--tone-success-bg); color: var(--tone-success-text); font-size: 0.6rem; padding: 0.1rem 0.35rem; border-radius: var(--radius); font-family: var(--font-grotesk); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; }
    .sprint-badge { font-family: var(--font-grotesk); font-size: 0.6rem; background: var(--tone-warning-bg); color: var(--tone-warning-text); padding: 0.1rem 0.35rem; border-radius: var(--radius); font-weight: 700; text-transform: uppercase; letter-spacing: 0.03em; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 90px; }

    /* Sprint progress bar */
    .sprint-progress-wrap { flex: 0 0 100%; height: 3px; background: var(--surface-3); border-radius: 2px; margin-top: 0.2rem; }
    .sprint-progress-fill { height: 100%; background: var(--warn-outline); border-radius: 2px; transition: width 0.3s ease; }
    .sprint-progress-fill.sprint-progress-done { background: var(--green); }

    /* Bulk import */
    .bulk-textarea { font-family: var(--font-mono, monospace); font-size: 0.8rem; resize: vertical; }
    .bulk-hint { font-weight: 400; color: var(--text-muted); font-size: 0.68rem; margin-left: 0.3rem; }
    .bulk-preview { margin-top: 0.75rem; background: var(--surface-2); border-radius: var(--radius-lg); padding: 0.75rem 1rem; }
    .bulk-preview-label { font-family: var(--font-grotesk); font-size: 0.65rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-bottom: 0.5rem; }
    .bulk-preview-row { display: flex; gap: 0.5rem; align-items: baseline; padding: 0.2rem 0; border-bottom: 1px solid var(--border-subtle, var(--surface-3)); }
    .bulk-preview-row:last-child { border-bottom: none; }
    .bulk-preview-title { font-family: var(--font-grotesk); font-size: 0.78rem; font-weight: 600; }
    .bulk-preview-desc { font-size: 0.72rem; color: var(--text-muted); }
    .bulk-preview-more { font-family: var(--font-grotesk); font-size: 0.68rem; color: var(--text-muted); margin-top: 0.4rem; }

    /* Board sidebar context panel */
    .ctx-panel { margin: 0.5rem 0.5rem 0.5rem; padding: 0.6rem 0.75rem; background: var(--surface-2); border-radius: var(--radius-lg); display: flex; flex-direction: column; gap: 0.25rem; }
    .ctx-section-label { font-family: var(--font-grotesk); font-size: 0.62rem; font-weight: 700; text-transform: uppercase; color: var(--text-muted); margin-top: 0.4rem; letter-spacing: 0.04em; }
    .ctx-section-label:first-child { margin-top: 0; }

    .ctx-sprint-banner { background: var(--tone-warning-bg); border-radius: var(--radius); padding: 0.5rem 0.6rem; margin-bottom: 0.1rem; }
    .ctx-sprint-name { font-family: var(--font-grotesk); font-size: 0.75rem; font-weight: 700; color: var(--tone-warning-text); }
    .ctx-sprint-goal { font-size: 0.68rem; color: var(--tone-warning-text); opacity: 0.8; margin-top: 0.1rem; line-height: 1.3; }
    .ctx-sprint-dates { font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--tone-warning-text); opacity: 0.7; margin-top: 0.15rem; }
    .ctx-sprint-meta { display: flex; justify-content: space-between; font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--tone-warning-text); opacity: 0.75; }

    .ctx-milestone-row { display: flex; align-items: center; gap: 0.3rem; padding: 0.2rem 0; }
    .ctx-milestone-name { font-family: var(--font-grotesk); font-size: 0.72rem; font-weight: 600; flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .ctx-milestone-meta { font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--text-muted); white-space: nowrap; }
    .ctx-milestone-tasks { font-family: var(--font-grotesk); font-size: 0.6rem; color: var(--text-muted); background: var(--surface-3); padding: 0.05rem 0.3rem; border-radius: var(--radius); white-space: nowrap; }
    .ctx-overdue { color: var(--red) !important; }
    .ctx-see-all { font-family: var(--font-grotesk); font-size: 0.62rem; color: var(--text-muted); cursor: pointer; text-align: right; margin-top: 0.1rem; }
    .ctx-see-all:hover { color: var(--text-primary); }

    .ctx-stats { display: grid; grid-template-columns: repeat(4, 1fr); gap: 0.2rem; margin-top: 0.15rem; }
    .ctx-stat { background: var(--surface-3); border-radius: var(--radius); padding: 0.3rem 0.2rem; text-align: center; }
    .ctx-stat-val { font-family: var(--font-grotesk); font-size: 0.9rem; font-weight: 700; line-height: 1; }
    .ctx-stat-lbl { font-family: var(--font-grotesk); font-size: 0.55rem; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.03em; margin-top: 0.1rem; }

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
