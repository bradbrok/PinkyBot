"""Project / sprint / milestone / task management routes.

Extracted from api.py. Covers the full project hub surface plus task
self-service (claim / complete / block) and task comments.

Deliberately left in api.py:
- /tasks-ui  (the SPA HTML page; lives with other UI routes)
- /agents/{name}/health  (sandwiched between task routes; uses
  streaming-session machinery and audit/autonomy state directly)
"""

from __future__ import annotations

from typing import Any, Callable

from fastapi import APIRouter, HTTPException

from pinky_daemon.api_models import (
    AddCommentRequest,
    AddLinkedAssetRequest,
    AddTeamMemberRequest,
    CreateMilestoneRequest,
    CreateProjectRequest,
    CreateSprintRequest,
    CreateTaskRequest,
    UpdateMilestoneRequest,
    UpdateProjectRequest,
    UpdateSprintRequest,
    UpdateTaskRequest,
)
from pinky_daemon.autonomy import AgentEvent, EventType

router = APIRouter(tags=["projects-tasks"])


# ── Shared dependency state ───────────────────────────────────────────────────

_tasks: Any = None
_research: Any = None
_presentations: Any = None
_activity: Any = None
_autonomy: Any = None
_kb_auto_ingest: Callable[..., bool] | None = None


def set_dependencies(
    *,
    tasks,
    research,
    presentations,
    activity,
    autonomy,
    kb_auto_ingest: Callable[..., bool],
) -> None:
    """Wire shared instances and helpers for the projects/tasks router."""
    global _tasks, _research, _presentations, _activity, _autonomy, _kb_auto_ingest
    _tasks = tasks
    _research = research
    _presentations = presentations
    _activity = activity
    _autonomy = autonomy
    _kb_auto_ingest = kb_auto_ingest


# ── Projects ──────────────────────────────────────────────────────────────────


@router.post("/projects")
async def create_project(req: CreateProjectRequest):
    project = _tasks.create_project(
        name=req.name,
        description=req.description,
        repo_url=req.repo_url,
        team_members=req.team_members,
        linked_assets=req.linked_assets,
    )
    return project.to_dict()


@router.get("/projects")
async def list_projects():
    projects = _tasks.list_projects()
    return {"projects": [p.to_dict() for p in projects], "count": len(projects)}


@router.get("/projects/{project_id}")
async def get_project(project_id: int):
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    project_tasks = _tasks.list(project_id=project_id, include_completed=True)
    return {
        "project": project.to_dict(),
        "tasks": [t.to_dict() for t in project_tasks],
        "task_count": len(project_tasks),
    }


@router.put("/projects/{project_id}")
async def update_project(project_id: int, req: UpdateProjectRequest):
    updates: dict = {}
    if req.name:
        updates["name"] = req.name
    if req.description:
        updates["description"] = req.description
    if req.status:
        updates["status"] = req.status
    if req.due_date:
        updates["due_date"] = req.due_date
    if req.repo_url is not None:
        updates["repo_url"] = req.repo_url
    if req.team_members is not None:
        updates["team_members"] = req.team_members
    if req.linked_assets is not None:
        updates["linked_assets"] = req.linked_assets
    project = _tasks.update_project(project_id, **updates)
    if not project:
        raise HTTPException(404, "Project not found")
    return project.to_dict()


@router.get("/projects/{project_id}/hub")
async def get_project_hub(project_id: int):
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")

    # Active sprint with progress
    sprints = _tasks.list_sprints(project_id, include_completed=False)
    active_sprint = None
    for s in sprints:
        if s.status == "active":
            d = s.to_dict()
            sprint_counts = _tasks.count_tasks_by_sprint(s.id)
            total = sprint_counts.get("total", 0)
            completed = sprint_counts.get("completed", 0)
            d["progress_pct"] = round(completed / total * 100) if total else 0
            d["tasks_total"] = total
            d["tasks_completed"] = completed
            active_sprint = d
            break

    # Milestones with progress
    milestones = _tasks.list_milestones(project_id)
    milestone_task_counts = _tasks.count_tasks_by_milestone(project_id)
    milestone_completed_counts = _tasks.count_completed_tasks_by_milestone(project_id)
    milestones_data = []
    for m in milestones:
        d = m.to_dict()
        total = milestone_task_counts.get(m.id, 0)
        completed = milestone_completed_counts.get(m.id, 0)
        d["task_count"] = total
        d["tasks_completed"] = completed
        d["progress_pct"] = round(completed / total * 100) if total else 0
        milestones_data.append(d)

    # Recent tasks (last 10 by updated_at)
    all_tasks = _tasks.list(project_id=project_id, include_completed=True, limit=1000)
    all_tasks_sorted = sorted(all_tasks, key=lambda t: t.updated_at, reverse=True)
    recent_tasks = [t.to_dict() for t in all_tasks_sorted[:10]]

    # Task stats by status
    status_counts: dict[str, int] = {}
    for t in all_tasks:
        status_counts[t.status] = status_counts.get(t.status, 0) + 1

    # Enrich linked assets: research assets get topic title/status;
    # presentation assets get share_token
    enriched_assets = []
    for asset in project.linked_assets:
        a = dict(asset)
        asset_type = a.get("type", "")
        asset_id = a.get("id")
        if asset_type == "research" and asset_id is not None:
            try:
                topic = _research.get_topic(int(asset_id))
                if topic:
                    a["research_title"] = topic.title
                    a["research_status"] = topic.status
            except Exception:
                pass
        elif asset_type == "presentation" and asset_id is not None:
            try:
                pres = _presentations.get(int(asset_id))
                if pres:
                    a["share_token"] = pres.share_token
                    a["presentation_title"] = pres.title
            except Exception:
                pass
        enriched_assets.append(a)

    # Linked presentations: match any linked research asset IDs
    linked_research_ids = [
        a.get("id") or a.get("research_id")
        for a in project.linked_assets
        if a.get("type") == "research"
    ]
    linked_presentations = []
    for rid in linked_research_ids:
        if rid is not None:
            pres_list = _presentations.list(research_topic_id=int(rid))
            linked_presentations.extend(p.to_dict() for p in pres_list)

    # Recent activity: fetch last 10 events mentioning this project
    all_activity = _activity.list(limit=200)
    project_activity = []
    for ev in all_activity:
        meta = ev.get("metadata") or {}
        if meta.get("project_id") == project_id or meta.get("project_name") == project.name:
            project_activity.append(ev)
            if len(project_activity) >= 10:
                break

    project_dict = project.to_dict()
    project_dict["linked_assets"] = enriched_assets

    return {
        "project": project_dict,
        "active_sprint": active_sprint,
        "milestones": milestones_data,
        "recent_tasks": recent_tasks,
        "task_stats": status_counts,
        "linked_presentations": linked_presentations,
        "recent_activity": project_activity,
    }


@router.delete("/projects/{project_id}")
async def delete_project(project_id: int):
    if not _tasks.delete_project(project_id):
        raise HTTPException(404, "Project not found")
    return {"deleted": True}


# ── Team member management ────────────────────────────────────────────────────


@router.post("/projects/{project_id}/team")
async def add_team_member(project_id: int, req: AddTeamMemberRequest):
    """Append a team member to the project without replacing the whole list."""
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    member = {"name": req.name, "role": req.role, "contact": req.contact}
    updated = _tasks.update_project(project_id, team_members=project.team_members + [member])
    return {"team_members": updated.team_members}  # type: ignore[union-attr]


@router.delete("/projects/{project_id}/team/{index}")
async def remove_team_member(project_id: int, index: int):
    """Remove a team member by list index."""
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    members = list(project.team_members)
    if index < 0 or index >= len(members):
        raise HTTPException(
            400, f"Index {index} out of range (list has {len(members)} members)"
        )
    members.pop(index)
    updated = _tasks.update_project(project_id, team_members=members)
    return {"team_members": updated.team_members}  # type: ignore[union-attr]


# ── Linked asset management ───────────────────────────────────────────────────


@router.post("/projects/{project_id}/assets")
async def add_linked_asset(project_id: int, req: AddLinkedAssetRequest):
    """Append a linked asset to the project without replacing the whole list."""
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    asset: dict = {
        "type": req.type,
        "title": req.title,
        "url": req.url,
        "description": req.description,
    }
    if req.id is not None:
        asset["id"] = req.id
    updated = _tasks.update_project(project_id, linked_assets=project.linked_assets + [asset])
    return {"linked_assets": updated.linked_assets}  # type: ignore[union-attr]


@router.delete("/projects/{project_id}/assets/{index}")
async def remove_linked_asset(project_id: int, index: int):
    """Remove a linked asset by list index."""
    project = _tasks.get_project(project_id)
    if not project:
        raise HTTPException(404, "Project not found")
    assets = list(project.linked_assets)
    if index < 0 or index >= len(assets):
        raise HTTPException(
            400, f"Index {index} out of range (list has {len(assets)} assets)"
        )
    assets.pop(index)
    updated = _tasks.update_project(project_id, linked_assets=assets)
    return {"linked_assets": updated.linked_assets}  # type: ignore[union-attr]


# ── Milestones ────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/milestones")
async def create_milestone(project_id: int, req: CreateMilestoneRequest):
    if not _tasks.get_project(project_id):
        raise HTTPException(404, "Project not found")
    milestone = _tasks.create_milestone(
        project_id, req.name, description=req.description, due_date=req.due_date
    )
    return milestone.to_dict()


@router.get("/projects/{project_id}/milestones")
async def list_milestones(project_id: int):
    if not _tasks.get_project(project_id):
        raise HTTPException(404, "Project not found")
    milestones = _tasks.list_milestones(project_id)
    task_counts = _tasks.count_tasks_by_milestone(project_id)
    result = []
    for m in milestones:
        d = m.to_dict()
        d["task_count"] = task_counts.get(m.id, 0)
        result.append(d)
    return {"milestones": result, "count": len(result)}


@router.put("/milestones/{milestone_id}")
async def update_milestone(milestone_id: int, req: UpdateMilestoneRequest):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    milestone = _tasks.update_milestone(milestone_id, **kwargs)
    if not milestone:
        raise HTTPException(404, "Milestone not found")
    return milestone.to_dict()


@router.delete("/milestones/{milestone_id}")
async def delete_milestone(milestone_id: int):
    if not _tasks.delete_milestone(milestone_id):
        raise HTTPException(404, "Milestone not found")
    return {"deleted": True}


# ── Sprints ───────────────────────────────────────────────────────────────────


@router.post("/projects/{project_id}/sprints")
async def create_sprint(project_id: int, req: CreateSprintRequest):
    if not _tasks.get_project(project_id):
        raise HTTPException(404, "Project not found")
    sprint = _tasks.create_sprint(
        project_id, req.name, goal=req.goal, start_date=req.start_date, end_date=req.end_date
    )
    return sprint.to_dict()


@router.get("/projects/{project_id}/sprints")
async def list_sprints(project_id: int, include_completed: bool = False):
    if not _tasks.get_project(project_id):
        raise HTTPException(404, "Project not found")
    sprints = _tasks.list_sprints(project_id, include_completed=include_completed)
    result = []
    for s in sprints:
        d = s.to_dict()
        d["task_counts"] = _tasks.count_tasks_by_sprint(s.id)
        result.append(d)
    return {"sprints": result, "count": len(result)}


@router.get("/sprints/{sprint_id}")
async def get_sprint(sprint_id: int):
    sprint = _tasks.get_sprint(sprint_id)
    if not sprint:
        raise HTTPException(404, "Sprint not found")
    d = sprint.to_dict()
    d["task_counts"] = _tasks.count_tasks_by_sprint(sprint_id)
    return d


@router.get("/sprints/{sprint_id}/burndown")
async def get_sprint_burndown(sprint_id: int):
    if not _tasks.get_sprint(sprint_id):
        raise HTTPException(404, "Sprint not found")
    series = _tasks.get_sprint_burndown(sprint_id)
    return {"sprint_id": sprint_id, "series": series}


@router.put("/sprints/{sprint_id}")
async def update_sprint(sprint_id: int, req: UpdateSprintRequest):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    sprint = _tasks.update_sprint(sprint_id, **kwargs)
    if not sprint:
        raise HTTPException(404, "Sprint not found")
    return sprint.to_dict()


@router.delete("/sprints/{sprint_id}")
async def delete_sprint(sprint_id: int):
    if not _tasks.delete_sprint(sprint_id):
        raise HTTPException(404, "Sprint not found")
    return {"deleted": True}


@router.post("/sprints/{sprint_id}/start")
async def start_sprint(sprint_id: int):
    sprint = _tasks.start_sprint(sprint_id)
    if not sprint:
        raise HTTPException(404, "Sprint not found")
    return sprint.to_dict()


@router.post("/sprints/{sprint_id}/complete")
async def complete_sprint(sprint_id: int):
    sprint = _tasks.complete_sprint(sprint_id)
    if not sprint:
        raise HTTPException(404, "Sprint not found")
    return sprint.to_dict()


# ── Tasks ─────────────────────────────────────────────────────────────────────


@router.post("/tasks")
async def create_task(req: CreateTaskRequest):
    task = _tasks.create(
        req.title,
        project_id=req.project_id,
        milestone_id=req.milestone_id,
        sprint_id=req.sprint_id,
        description=req.description,
        status=req.status,
        priority=req.priority,
        assigned_agent=req.assigned_agent,
        created_by=req.created_by,
        tags=req.tags,
        due_date=req.due_date,
        parent_id=req.parent_id,
        blocked_by=req.blocked_by,
    )

    # Push event to assigned agent's autonomy loop
    if task.assigned_agent:
        priority = 2 if task.priority == "urgent" else 1 if task.priority == "high" else 0
        await _autonomy.push_event(AgentEvent(
            type=EventType.task_assigned,
            agent_name=task.assigned_agent,
            data={"task_id": task.id, "title": task.title, "priority": task.priority},
            priority=priority,
        ))

    _activity.log(
        agent_name=task.assigned_agent or task.created_by or "",
        event_type="task_created",
        title=f"Task created: {task.title}",
        description=task.description or "",
        metadata={"task_id": task.id, "priority": task.priority, "assigned_agent": task.assigned_agent or ""},
    )

    return task.to_dict()


@router.get("/tasks")
async def list_tasks(
    status: str = "",
    assigned_agent: str = "",
    priority: str = "",
    tag: str = "",
    project_id: int | None = None,
    include_completed: bool = False,
    limit: int = 100,
):
    task_list = _tasks.list(
        status=status, assigned_agent=assigned_agent,
        priority=priority, tag=tag, project_id=project_id,
        include_completed=include_completed, limit=limit,
    )
    return {"tasks": [t.to_dict() for t in task_list], "count": len(task_list)}


@router.get("/tasks/stats")
async def task_stats():
    by_status = _tasks.count_by_status()
    by_agent = _tasks.count_by_agent()
    return {"by_status": by_status, "by_agent": by_agent}


# Task self-service — must be before /tasks/{task_id} to avoid route conflict


@router.get("/tasks/next")
async def next_task(agent_name: str = "", priority: str = ""):
    """Get the next available task for an agent to work on."""
    if agent_name:
        my_tasks = _tasks.list(assigned_agent=agent_name, status="pending")
        if my_tasks:
            return {"task": my_tasks[0].to_dict(), "source": "assigned"}
        my_tasks = _tasks.list(assigned_agent=agent_name, status="in_progress")
        if my_tasks:
            return {"task": my_tasks[0].to_dict(), "source": "in_progress"}
    unassigned = _tasks.list(assigned_agent="")
    unassigned = [t for t in unassigned if not t.assigned_agent]
    if unassigned:
        return {"task": unassigned[0].to_dict(), "source": "unassigned"}
    return {"task": None, "source": "none"}


@router.get("/tasks/{task_id}")
async def get_task(task_id: int):
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    subtasks = _tasks.get_subtasks(task_id)
    comments = _tasks.get_comments(task_id)
    return {
        "task": task.to_dict(),
        "subtasks": [s.to_dict() for s in subtasks],
        "comments": [c.to_dict() for c in comments],
    }


@router.put("/tasks/{task_id}")
async def update_task(task_id: int, req: UpdateTaskRequest):
    kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
    task = _tasks.update(task_id, **kwargs)
    if not task:
        raise HTTPException(404, "Task not found")
    return task.to_dict()


@router.delete("/tasks/{task_id}")
async def delete_task(task_id: int):
    if not _tasks.delete(task_id):
        raise HTTPException(404, "Task not found")
    return {"deleted": True}


@router.post("/tasks/claim/{task_id}")
async def claim_task(task_id: int, agent_name: str = ""):
    """Agent claims an unassigned task. Sets status to in_progress."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    if task.assigned_agent and task.assigned_agent != agent_name:
        raise HTTPException(409, f"Task already assigned to {task.assigned_agent}")

    updated = _tasks.update(task_id, assigned_agent=agent_name, status="in_progress")
    _tasks.add_comment(task_id, agent_name, "Claimed and started work")
    return updated.to_dict()


@router.post("/tasks/complete/{task_id}")
async def complete_task(task_id: int, agent_name: str = "", summary: str = ""):
    """Agent marks a task as completed with an optional summary."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updated = _tasks.update(task_id, status="completed")
    comment = summary or "Task completed"
    _tasks.add_comment(task_id, agent_name or task.assigned_agent, comment)

    # Push worker_report event to parent agent if task has a creator
    if task.created_by and task.created_by != agent_name:
        await _autonomy.push_event(AgentEvent(
            type=EventType.worker_report,
            agent_name=task.created_by,
            data={
                "task_id": task.id,
                "title": task.title,
                "worker": agent_name or task.assigned_agent,
                "result": summary,
            },
            priority=1,
        ))

    _activity.log(
        agent_name=agent_name or task.assigned_agent or "",
        event_type="task_completed",
        title=f"Task completed: {task.title}",
        description=summary or "",
        metadata={"task_id": task.id},
    )

    # Auto-ingest substantive task completions into KB (skip trivial summaries)
    if summary and len(summary) > 50:
        project_name = ""
        if task.project_id:
            proj = _tasks.get_project(task.project_id)
            project_name = proj.name if proj else ""
        tags = ["tasks", "project-state"]
        if project_name:
            tags.append(project_name.lower().replace(" ", "-"))
        _kb_auto_ingest(
            title=f"Task completed: {task.title}",
            content=(
                f"# {task.title}\n\n"
                f"- Project: {project_name or 'none'}\n"
                f"- Completed by: {agent_name or task.assigned_agent or 'unknown'}\n"
                f"- Priority: {task.priority}\n\n"
                f"## Summary\n\n{summary}"
            ),
            source_type="note",
            filed_by=agent_name or task.assigned_agent or "system",
            tags=tags,
        )

    return updated.to_dict()


@router.post("/tasks/block/{task_id}")
async def block_task(task_id: int, agent_name: str = "", reason: str = ""):
    """Agent marks a task as blocked with a reason."""
    task = _tasks.get(task_id)
    if not task:
        raise HTTPException(404, "Task not found")

    updated = _tasks.update(task_id, status="blocked")
    _tasks.add_comment(task_id, agent_name or task.assigned_agent, f"Blocked: {reason}")
    return updated.to_dict()


# ── Task comments ─────────────────────────────────────────────────────────────


@router.post("/tasks/{task_id}/comments")
async def add_comment(task_id: int, req: AddCommentRequest):
    if not _tasks.get(task_id):
        raise HTTPException(404, "Task not found")
    comment = _tasks.add_comment(task_id, req.author, req.content)
    return comment.to_dict()


@router.get("/tasks/{task_id}/comments")
async def list_comments(task_id: int, limit: int = 50):
    comments = _tasks.get_comments(task_id, limit=limit)
    return {"comments": [c.to_dict() for c in comments], "count": len(comments)}


@router.delete("/tasks/{task_id}/comments/{comment_id}")
async def delete_comment(task_id: int, comment_id: int):
    if not _tasks.delete_comment(comment_id):
        raise HTTPException(404, "Comment not found")
    return {"deleted": True}
