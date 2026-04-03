"""Task Store — SQLite-backed project management for agents.

Projects > Milestones > Tasks > Subtasks hierarchy with comments/activity log.
Designed for fleet coordination: agents pick up work, report progress,
and coordinate through task assignments and status updates.

Schema:
    projects(id, name, description, status, created_at, updated_at)
    milestones(id, project_id, name, description, due_date, status, created_at, updated_at)
    tasks(id, project_id, milestone_id, title, description, status, priority, assigned_agent,
          created_by, tags, due_date, parent_id, blocked_by, created_at, updated_at)
    task_comments(id, task_id, author, content, created_at)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


VALID_STATUSES = ("pending", "in_progress", "completed", "blocked", "cancelled")
VALID_PRIORITIES = ("low", "normal", "high", "urgent")
PROJECT_STATUSES = ("active", "completed", "archived")
MILESTONE_STATUSES = ("open", "reached", "missed")


@dataclass
class Project:
    """A project that groups related tasks."""

    id: int = 0
    name: str = ""
    description: str = ""
    status: str = "active"  # active, completed, archived
    due_date: str = ""  # ISO date string or empty
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "status": self.status,
            "due_date": self.due_date,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Milestone:
    """A milestone within a project."""

    id: int = 0
    project_id: int = 0
    name: str = ""
    description: str = ""
    due_date: str = ""  # ISO date string or empty
    status: str = "open"  # open, reached, missed
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "name": self.name,
            "description": self.description,
            "due_date": self.due_date,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class TaskComment:
    """A comment or status update on a task."""

    id: int = 0
    task_id: int = 0
    author: str = ""  # Agent name or "user"
    content: str = ""
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "author": self.author,
            "content": self.content,
            "created_at": self.created_at,
        }


@dataclass
class Task:
    """A managed task."""

    id: int = 0
    project_id: int = 0  # Project this task belongs to (0 = unassigned)
    milestone_id: int = 0  # Milestone this task belongs to (0 = none)
    title: str = ""
    description: str = ""
    status: str = "pending"  # pending, in_progress, completed, blocked, cancelled
    priority: str = "normal"  # low, normal, high, urgent
    assigned_agent: str = ""  # Agent name or empty
    created_by: str = ""  # Who created it (agent name or "user")
    tags: list[str] = field(default_factory=list)
    due_date: str = ""  # ISO date string or empty
    parent_id: int = 0  # Parent task ID for subtasks (0 = top-level)
    blocked_by: list[int] = field(default_factory=list)  # Task IDs that block this
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "project_id": self.project_id,
            "milestone_id": self.milestone_id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "assigned_agent": self.assigned_agent,
            "created_by": self.created_by,
            "tags": self.tags,
            "due_date": self.due_date,
            "parent_id": self.parent_id,
            "blocked_by": self.blocked_by,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class TaskStore:
    """SQLite-backed task management."""

    def __init__(self, db_path: str = "data/tasks.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'active',
                due_date TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS milestones (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                due_date TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL DEFAULT 0,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',
                priority TEXT NOT NULL DEFAULT 'normal',
                assigned_agent TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL DEFAULT '',
                tags TEXT NOT NULL DEFAULT '[]',
                due_date TEXT NOT NULL DEFAULT '',
                parent_id INTEGER NOT NULL DEFAULT 0,
                blocked_by TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                milestone_id INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS task_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                author TEXT NOT NULL DEFAULT '',
                content TEXT NOT NULL,
                created_at REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
            CREATE INDEX IF NOT EXISTS idx_tasks_agent ON tasks(assigned_agent);
            CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_id);
            CREATE INDEX IF NOT EXISTS idx_tasks_priority ON tasks(priority);
            CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
            CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id);
            CREATE INDEX IF NOT EXISTS idx_comments_task ON task_comments(task_id);
        """)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing databases."""
        proj_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(projects)").fetchall()
        }
        proj_migrations = [
            ("due_date", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in proj_migrations:
            if col not in proj_existing:
                self._db.execute(f"ALTER TABLE projects ADD COLUMN {col} {typedef}")
                _log(f"task_store: migrated — added {col} to projects")

        task_existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(tasks)").fetchall()
        }
        task_migrations = [
            ("milestone_id", "INTEGER NOT NULL DEFAULT 0"),
        ]
        for col, typedef in task_migrations:
            if col not in task_existing:
                self._db.execute(f"ALTER TABLE tasks ADD COLUMN {col} {typedef}")
                _log(f"task_store: migrated — added {col} to tasks")

        self._db.commit()

    # ── CRUD ───────────────────────────────────────────────

    def create(
        self,
        title: str,
        *,
        project_id: int = 0,
        milestone_id: int = 0,
        description: str = "",
        status: str = "pending",
        priority: str = "normal",
        assigned_agent: str = "",
        created_by: str = "",
        tags: list[str] | None = None,
        due_date: str = "",
        parent_id: int = 0,
        blocked_by: list[int] | None = None,
    ) -> Task:
        """Create a new task."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO tasks
               (project_id, title, description, status, priority, assigned_agent,
                created_by, tags, due_date, parent_id, blocked_by, created_at, updated_at,
                milestone_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (project_id, title, description, status, priority, assigned_agent,
             created_by, json.dumps(tags or []), due_date, parent_id,
             json.dumps(blocked_by or []), now, now, milestone_id),
        )
        self._db.commit()
        _log(f"tasks: created #{cursor.lastrowid} '{title}'")
        return self.get(cursor.lastrowid)  # type: ignore

    def get(self, task_id: int) -> Task | None:
        """Get a task by ID."""
        row = self._db.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_task(row)

    def update(self, task_id: int, **kwargs) -> Task | None:
        """Update task fields. Only provided kwargs are changed."""
        task = self.get(task_id)
        if not task:
            return None

        updates = {}
        for key in ("title", "description", "status", "priority",
                     "assigned_agent", "due_date", "parent_id", "project_id", "milestone_id"):
            if key in kwargs:
                updates[key] = kwargs[key]

        if "tags" in kwargs:
            updates["tags"] = json.dumps(kwargs["tags"])
        if "blocked_by" in kwargs:
            updates["blocked_by"] = json.dumps(kwargs["blocked_by"])

        if not updates:
            return task

        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE tasks SET {set_clause} WHERE id=?",
            list(updates.values()) + [task_id],
        )
        self._db.commit()
        return self.get(task_id)

    def delete(self, task_id: int) -> bool:
        """Delete a task and its subtasks."""
        # Delete subtasks first
        self._db.execute("DELETE FROM tasks WHERE parent_id=?", (task_id,))
        cursor = self._db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
        self._db.commit()
        return cursor.rowcount > 0

    # ── Queries ────────────────────────────────────────────

    def list(
        self,
        *,
        status: str = "",
        assigned_agent: str = "",
        priority: str = "",
        tag: str = "",
        project_id: int | None = None,
        parent_id: int | None = None,
        include_completed: bool = False,
        limit: int = 100,
    ) -> list[Task]:
        """List tasks with optional filters."""
        conditions = []
        params: list = []

        if status:
            conditions.append("status=?")
            params.append(status)
        elif not include_completed:
            conditions.append("status NOT IN ('completed', 'cancelled')")

        if assigned_agent:
            conditions.append("assigned_agent=?")
            params.append(assigned_agent)

        if project_id is not None:
            conditions.append("project_id=?")
            params.append(project_id)

        if priority:
            conditions.append("priority=?")
            params.append(priority)

        if parent_id is not None:
            conditions.append("parent_id=?")
            params.append(parent_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self._db.execute(
            f"""SELECT * FROM tasks {where}
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                  WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END,
                    created_at DESC
                LIMIT ?""",
            params,
        ).fetchall()

        tasks = [self._row_to_task(r) for r in rows]

        # Filter by tag in Python (JSON array)
        if tag:
            tasks = [t for t in tasks if tag in t.tags]

        return tasks

    def get_subtasks(self, parent_id: int) -> list[Task]:
        """Get all subtasks of a task."""
        return self.list(parent_id=parent_id, include_completed=True)

    def count_by_status(self) -> dict[str, int]:
        """Get task counts grouped by status."""
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM tasks GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def count_by_agent(self) -> dict[str, int]:
        """Get active task counts grouped by assigned agent."""
        rows = self._db.execute(
            """SELECT assigned_agent, COUNT(*) FROM tasks
               WHERE status NOT IN ('completed', 'cancelled') AND assigned_agent != ''
               GROUP BY assigned_agent"""
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── Helpers ────────────────────────────────────────────

    def _row_to_task(self, row: tuple) -> Task:
        return Task(
            id=row[0],
            project_id=row[1],
            title=row[2],
            description=row[3],
            status=row[4],
            priority=row[5],
            assigned_agent=row[6],
            created_by=row[7],
            tags=json.loads(row[8]),
            due_date=row[9],
            parent_id=row[10],
            blocked_by=json.loads(row[11]),
            created_at=row[12],
            updated_at=row[13],
            milestone_id=row[14] if len(row) > 14 else 0,
        )

    # ── Projects ───────────────────────────────────────────

    def create_project(self, name: str, *, description: str = "") -> Project:
        """Create a new project."""
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO projects (name, description, status, created_at, updated_at) VALUES (?, ?, 'active', ?, ?)",
            (name, description, now, now),
        )
        self._db.commit()
        _log(f"projects: created #{cursor.lastrowid} '{name}'")
        return self.get_project(cursor.lastrowid)  # type: ignore

    def get_project(self, project_id: int) -> Project | None:
        """Get a project by ID."""
        row = self._db.execute(
            "SELECT id, name, description, status, due_date, created_at, updated_at FROM projects WHERE id=?",
            (project_id,),
        ).fetchone()
        if not row:
            return None
        return Project(id=row[0], name=row[1], description=row[2],
                       status=row[3], due_date=row[4], created_at=row[5], updated_at=row[6])

    def list_projects(self, *, include_archived: bool = False) -> list[Project]:
        """List all projects."""
        cols = "id, name, description, status, due_date, created_at, updated_at"
        if include_archived:
            rows = self._db.execute(
                f"SELECT {cols} FROM projects ORDER BY updated_at DESC"
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {cols} FROM projects WHERE status != 'archived' ORDER BY updated_at DESC"
            ).fetchall()
        return [
            Project(id=r[0], name=r[1], description=r[2],
                    status=r[3], due_date=r[4], created_at=r[5], updated_at=r[6])
            for r in rows
        ]

    def update_project(self, project_id: int, **kwargs) -> Project | None:
        """Update project fields."""
        project = self.get_project(project_id)
        if not project:
            return None
        updates = {}
        for key in ("name", "description", "status", "due_date"):
            if key in kwargs:
                updates[key] = kwargs[key]
        if not updates:
            return project
        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE projects SET {set_clause} WHERE id=?",
            list(updates.values()) + [project_id],
        )
        self._db.commit()
        return self.get_project(project_id)

    def delete_project(self, project_id: int) -> bool:
        """Delete a project (tasks are unlinked, not deleted)."""
        # Unlink tasks from this project and clear milestone references
        self._db.execute("UPDATE tasks SET project_id=0, milestone_id=0 WHERE project_id=?", (project_id,))
        self._db.execute("DELETE FROM milestones WHERE project_id=?", (project_id,))
        cursor = self._db.execute("DELETE FROM projects WHERE id=?", (project_id,))
        self._db.commit()
        return cursor.rowcount > 0

    # ── Milestones ─────────────────────────────────────────

    def create_milestone(
        self,
        project_id: int,
        name: str,
        *,
        description: str = "",
        due_date: str = "",
    ) -> Milestone:
        """Create a new milestone for a project."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO milestones
               (project_id, name, description, due_date, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?)""",
            (project_id, name, description, due_date, now, now),
        )
        self._db.commit()
        _log(f"milestones: created #{cursor.lastrowid} '{name}' for project {project_id}")
        return self.get_milestone(cursor.lastrowid)  # type: ignore

    def get_milestone(self, milestone_id: int) -> Milestone | None:
        """Get a milestone by ID."""
        row = self._db.execute(
            "SELECT id, project_id, name, description, due_date, status, created_at, updated_at"
            " FROM milestones WHERE id=?",
            (milestone_id,),
        ).fetchone()
        if not row:
            return None
        return Milestone(
            id=row[0], project_id=row[1], name=row[2], description=row[3],
            due_date=row[4], status=row[5], created_at=row[6], updated_at=row[7],
        )

    def list_milestones(self, project_id: int) -> list[Milestone]:
        """List all milestones for a project."""
        rows = self._db.execute(
            "SELECT id, project_id, name, description, due_date, status, created_at, updated_at"
            " FROM milestones WHERE project_id=? ORDER BY due_date ASC, created_at ASC",
            (project_id,),
        ).fetchall()
        return [
            Milestone(
                id=r[0], project_id=r[1], name=r[2], description=r[3],
                due_date=r[4], status=r[5], created_at=r[6], updated_at=r[7],
            )
            for r in rows
        ]

    def update_milestone(self, milestone_id: int, **kwargs) -> Milestone | None:
        """Update milestone fields. Supports: name, description, due_date, status."""
        milestone = self.get_milestone(milestone_id)
        if not milestone:
            return None
        updates = {}
        for key in ("name", "description", "due_date", "status"):
            if key in kwargs:
                updates[key] = kwargs[key]
        if not updates:
            return milestone
        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE milestones SET {set_clause} WHERE id=?",
            list(updates.values()) + [milestone_id],
        )
        self._db.commit()
        return self.get_milestone(milestone_id)

    def delete_milestone(self, milestone_id: int) -> bool:
        """Delete a milestone (tasks with this milestone are unlinked)."""
        self._db.execute(
            "UPDATE tasks SET milestone_id=0 WHERE milestone_id=?", (milestone_id,)
        )
        cursor = self._db.execute("DELETE FROM milestones WHERE id=?", (milestone_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def count_tasks_by_milestone(self, project_id: int) -> dict[int, int]:
        """Return task counts keyed by milestone_id for a project."""
        rows = self._db.execute(
            "SELECT milestone_id, COUNT(*) FROM tasks WHERE project_id=? GROUP BY milestone_id",
            (project_id,),
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    # ── Comments ───────────────────────────────────────────

    def add_comment(self, task_id: int, author: str, content: str) -> TaskComment:
        """Add a comment to a task."""
        now = time.time()
        cursor = self._db.execute(
            "INSERT INTO task_comments (task_id, author, content, created_at) VALUES (?, ?, ?, ?)",
            (task_id, author, content, now),
        )
        self._db.commit()
        return TaskComment(id=cursor.lastrowid, task_id=task_id,
                          author=author, content=content, created_at=now)

    def get_comments(self, task_id: int, *, limit: int = 50) -> list[TaskComment]:
        """Get comments for a task, newest first."""
        rows = self._db.execute(
            "SELECT id, task_id, author, content, created_at FROM task_comments WHERE task_id=? ORDER BY created_at DESC LIMIT ?",
            (task_id, limit),
        ).fetchall()
        return [
            TaskComment(id=r[0], task_id=r[1], author=r[2], content=r[3], created_at=r[4])
            for r in rows
        ]

    def delete_comment(self, comment_id: int) -> bool:
        """Delete a comment."""
        cursor = self._db.execute("DELETE FROM task_comments WHERE id=?", (comment_id,))
        self._db.commit()
        return cursor.rowcount > 0

    def close(self) -> None:
        self._db.close()
