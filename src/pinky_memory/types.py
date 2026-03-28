from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field


class ReflectionType(str, Enum):
    insight = "insight"
    project_state = "project_state"
    interaction_pattern = "interaction_pattern"
    continuation = "continuation"
    fact = "fact"


class Reflection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = ""
    type: ReflectionType = ReflectionType.fact
    content: str = ""
    context: str = ""
    project: str = ""
    salience: int = Field(default=3, ge=1, le=5)
    active: bool = True
    no_recall: bool = False
    supersedes: str = ""
    superseded_by: str = ""
    event_date: Optional[str] = None  # ISO date (YYYY-MM-DD) when the described event occurred
    entities: list[str] = Field(default_factory=list)  # Person names tagged on this reflection
    source_session_id: Optional[str] = None   # e.g. "telegram:6770805286"
    source_channel: Optional[str] = None      # e.g. "telegram"
    source_message_ids: list[str] = Field(default_factory=list)  # message IDs that produced this reflection
    embedding: list[float] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    accessed_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    access_count: int = 0
    weight: float = 1.0
    next_review_date: Optional[str] = None  # ISO date (YYYY-MM-DD) for spaced review
    review_interval_days: int = 7


class ReflectInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    type: ReflectionType = ReflectionType.fact
    content: str
    context: str = ""
    project: str = ""
    salience: int = Field(default=3, ge=1, le=5)
    supersedes: str = ""
    entities: list[str] = Field(default_factory=list)
    source_session_id: Optional[str] = None
    source_channel: Optional[str] = None
    source_message_ids: list[str] = Field(default_factory=list)


class RecallInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    query: str = ""
    type: Optional[ReflectionType] = None
    project: str = ""
    entity: str = ""
    min_weight: float = 0.0
    limit: int = Field(default=10, ge=1, le=50)
    active_only: bool = True


class ReflectionLink(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: int = 0
    source_id: str = ""
    target_id: str = ""
    similarity: float = 0.0
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class MemoryQueryFilters(BaseModel):
    """Structured filter DSL for querying the reflections store.

    No raw SQL — this typed interface is the only way to query.
    """
    model_config = ConfigDict(extra="ignore")

    type: Optional[str] = None
    project: Optional[str] = None
    entity: Optional[str] = None
    salience_min: Optional[int] = None
    salience_max: Optional[int] = None
    active: bool = True
    created_after: Optional[str] = None    # ISO date string
    created_before: Optional[str] = None   # ISO date string
    accessed_after: Optional[str] = None   # ISO date string
    accessed_before: Optional[str] = None  # ISO date string (for stale queries)
    due_for_review: bool = False
    has_links: Optional[bool] = None       # True/False/None=any
    # Orphan mode: access_count=0, no entities
    orphan_mode: bool = False
    sort_by: str = Field(default="created_at", pattern=r"^(created_at|accessed_at|salience|access_count)$")
    sort_dir: str = Field(default="desc", pattern=r"^(asc|desc)$")
    limit: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    # Preset name (if using a named shortcut)
    preset: Optional[str] = None


PRESET_NAMES = frozenset({
    "recent_insights", "stale_projects", "high_value",
    "orphans", "due_review", "by_project",
})


def resolve_preset(filters: "MemoryQueryFilters") -> "MemoryQueryFilters":
    """Resolve a preset name into concrete filter values, merging caller overrides."""
    from datetime import datetime, timedelta, timezone

    preset_name = filters.preset
    if not preset_name or preset_name not in PRESET_NAMES:
        return filters

    now = datetime.now(timezone.utc)
    caller = filters.model_dump(exclude_defaults=True)
    caller.pop("preset", None)

    if preset_name == "recent_insights":
        base = {
            "type": "insight",
            "created_after": (now - timedelta(days=30)).isoformat(),
            "sort_by": "salience",
            "sort_dir": "desc",
            "limit": 20,
        }
    elif preset_name == "stale_projects":
        base = {
            "type": "project_state",
            "accessed_before": (now - timedelta(days=60)).isoformat(),
            "sort_by": "accessed_at",
            "sort_dir": "asc",
            "limit": 20,
        }
    elif preset_name == "high_value":
        base = {
            "salience_min": 4,
            "active": True,
            "sort_by": "access_count",
            "sort_dir": "desc",
            "limit": 20,
        }
    elif preset_name == "orphans":
        base = {
            "orphan_mode": True,
            "created_before": (now - timedelta(days=30)).isoformat(),
            "sort_by": "created_at",
            "sort_dir": "asc",
            "limit": 20,
        }
    elif preset_name == "due_review":
        base = {
            "due_for_review": True,
            "sort_by": "created_at",
            "sort_dir": "asc",
            "limit": 20,
        }
    elif preset_name == "by_project":
        base = {
            "sort_by": "created_at",
            "sort_dir": "desc",
            "limit": 50,
        }
    else:
        return filters

    base.update(caller)
    return MemoryQueryFilters(**base)


class IntrospectInput(BaseModel):
    model_config = ConfigDict(extra="ignore")

    timeframe: str = Field(default="all", pattern=r"^(day|week|month|all)$")
    type: Optional[ReflectionType] = None
    project: str = ""
