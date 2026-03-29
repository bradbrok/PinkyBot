# Research Pipeline — Multi-Agent Peer Review

> Spec v1.0 — 2026-03-29

## Overview

A structured research pipeline where users submit topics, agents investigate them, and peer agents review the findings before publication. Replaces ad-hoc brief submission with a full lifecycle: intake, assignment, research, draft, review, revision, publish.

This integrates with PinkyBot's existing task system, agent registry, autonomy engine, and memory MCP. No new infrastructure required — just new tables, endpoints, a store class, and a frontend page.

---

## Architecture

```
User submits topic
       │
       ▼
┌─────────────┐     ┌──────────────┐     ┌──────────────┐
│   Intake     │────▶│  Assignment  │────▶│  Research     │
│  (open)      │     │  (assigned)  │     │  (researching)│
└─────────────┘     └──────────────┘     └──────┬───────┘
                                                │
                                           Agent submits
                                           draft brief
                                                │
                                                ▼
                                        ┌──────────────┐
                                        │  Peer Review  │
                                        │  (in_review)  │
                                        └──────┬───────┘
                                               │
                                          2+ agents
                                          review draft
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │   Revision    │
                                        │  (revising)   │
                                        └──────┬───────┘
                                               │
                                          Original agent
                                          incorporates feedback
                                               │
                                               ▼
                                        ┌──────────────┐
                                        │  Published    │
                                        │  (published)  │
                                        └──────────────┘
```

---

## Data Model

### ResearchTopic

The top-level entity. One topic per research question.

```python
@dataclass
class ResearchTopic:
    id: int = 0
    title: str = ""                    # Short title (e.g., "MCP Server Hot-Reload Patterns")
    description: str = ""              # Full research question / context
    submitted_by: str = ""             # Username or agent name
    status: str = "open"               # open, assigned, researching, in_review, revising, published, cancelled
    assigned_agent: str = ""           # Agent doing the research
    reviewer_agents: list[str] = []    # Agents assigned to review
    priority: str = "normal"           # low, normal, high, urgent
    tags: list[str] = []               # Topic tags for categorization
    scope: str = ""                    # Optional constraints / focus areas
    created_at: float = 0.0
    updated_at: float = 0.0
```

**Status transitions:**
- `open` -> `assigned` (agent picked or auto-assigned)
- `assigned` -> `researching` (agent starts work)
- `researching` -> `in_review` (agent submits draft brief)
- `in_review` -> `revising` (all reviews received)
- `revising` -> `published` (revised brief submitted and approved)
- Any status -> `cancelled` (user cancels)

### ResearchBrief

The deliverable. Attached to a topic. Versioned — v1 is the draft, v2+ are revisions.

```python
@dataclass
class ResearchBrief:
    id: int = 0
    topic_id: int = 0                 # FK to ResearchTopic
    author_agent: str = ""            # Agent that wrote this version
    version: int = 1                  # 1 = draft, 2+ = revisions
    content: str = ""                 # Full brief in markdown
    summary: str = ""                 # TL;DR (2-3 sentences)
    sources: list[str] = []           # URLs, paper titles, repo paths
    key_findings: list[str] = []      # Bulleted takeaways
    status: str = "draft"             # draft, in_review, revised, published
    created_at: float = 0.0
    published_at: float = 0.0        # Set when status -> published
```

### PeerReview

One per reviewer per brief version. Each reviewer submits independently.

```python
@dataclass
class PeerReview:
    id: int = 0
    brief_id: int = 0                 # FK to ResearchBrief
    topic_id: int = 0                 # FK to ResearchTopic (denormalized for queries)
    reviewer_agent: str = ""           # Who reviewed
    verdict: str = ""                  # approve, request_changes, reject
    comments: str = ""                 # Markdown review comments
    suggested_additions: list[str] = [] # Things the reviewer thinks are missing
    corrections: list[str] = []        # Factual errors or misinterpretations
    confidence: int = 3                # 1-5, reviewer's self-assessed confidence
    created_at: float = 0.0
```

**Verdict meanings:**
- `approve`: Brief is good, publish as-is or with minor edits
- `request_changes`: Needs revision — corrections or additions required
- `reject`: Fundamentally flawed, needs complete rework

---

## SQLite Schema

New file: `src/pinky_daemon/research_store.py`

Follows the same pattern as `task_store.py` — dataclass models, SQLite with WAL, JSON-serialized list fields.

```sql
CREATE TABLE IF NOT EXISTS research_topics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',
    assigned_agent TEXT NOT NULL DEFAULT '',
    reviewer_agents TEXT NOT NULL DEFAULT '[]',  -- JSON array
    priority TEXT NOT NULL DEFAULT 'normal',
    tags TEXT NOT NULL DEFAULT '[]',              -- JSON array
    scope TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS research_briefs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    topic_id INTEGER NOT NULL,
    author_agent TEXT NOT NULL DEFAULT '',
    version INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL DEFAULT '',
    sources TEXT NOT NULL DEFAULT '[]',           -- JSON array
    key_findings TEXT NOT NULL DEFAULT '[]',      -- JSON array
    status TEXT NOT NULL DEFAULT 'draft',
    created_at REAL NOT NULL,
    published_at REAL NOT NULL DEFAULT 0.0,
    FOREIGN KEY (topic_id) REFERENCES research_topics(id)
);

CREATE TABLE IF NOT EXISTS peer_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    brief_id INTEGER NOT NULL,
    topic_id INTEGER NOT NULL,
    reviewer_agent TEXT NOT NULL DEFAULT '',
    verdict TEXT NOT NULL DEFAULT '',
    comments TEXT NOT NULL DEFAULT '',
    suggested_additions TEXT NOT NULL DEFAULT '[]',  -- JSON array
    corrections TEXT NOT NULL DEFAULT '[]',           -- JSON array
    confidence INTEGER NOT NULL DEFAULT 3,
    created_at REAL NOT NULL,
    FOREIGN KEY (brief_id) REFERENCES research_briefs(id),
    FOREIGN KEY (topic_id) REFERENCES research_topics(id)
);

CREATE INDEX IF NOT EXISTS idx_topics_status ON research_topics(status);
CREATE INDEX IF NOT EXISTS idx_topics_agent ON research_topics(assigned_agent);
CREATE INDEX IF NOT EXISTS idx_briefs_topic ON research_briefs(topic_id);
CREATE INDEX IF NOT EXISTS idx_reviews_brief ON peer_reviews(brief_id);
CREATE INDEX IF NOT EXISTS idx_reviews_topic ON peer_reviews(topic_id);
```

---

## Store Class

`ResearchStore` in `src/pinky_daemon/research_store.py`. Same structure as `TaskStore`.

### Methods

```python
class ResearchStore:
    def __init__(self, db_path: str = "data/research.db") -> None: ...

    # Topics
    def create_topic(self, title: str, *, description="", submitted_by="",
                     priority="normal", tags=None, scope="") -> ResearchTopic: ...
    def get_topic(self, topic_id: int) -> ResearchTopic | None: ...
    def update_topic(self, topic_id: int, **kwargs) -> ResearchTopic | None: ...
    def list_topics(self, *, status="", assigned_agent="", tag="",
                    include_cancelled=False, limit=100) -> list[ResearchTopic]: ...
    def count_by_status(self) -> dict[str, int]: ...

    # Briefs
    def submit_brief(self, topic_id: int, author_agent: str, *,
                     content: str, summary: str, sources=None,
                     key_findings=None) -> ResearchBrief: ...
    def get_brief(self, brief_id: int) -> ResearchBrief | None: ...
    def get_latest_brief(self, topic_id: int) -> ResearchBrief | None: ...
    def list_briefs(self, topic_id: int) -> list[ResearchBrief]: ...
    def update_brief(self, brief_id: int, **kwargs) -> ResearchBrief | None: ...

    # Reviews
    def submit_review(self, brief_id: int, reviewer_agent: str, *,
                      verdict: str, comments: str, suggested_additions=None,
                      corrections=None, confidence=3) -> PeerReview: ...
    def get_reviews(self, brief_id: int) -> list[PeerReview]: ...
    def get_reviews_for_topic(self, topic_id: int) -> list[PeerReview]: ...

    # Queries
    def get_full_topic(self, topic_id: int) -> dict: ...
        """Returns topic + all briefs + all reviews in one call."""
```

---

## API Endpoints

Added to `api.py` in the `create_api()` function, following the existing pattern (Pydantic request models, direct store calls, autonomy event pushes).

### Request Models

```python
class CreateResearchTopicRequest(BaseModel):
    title: str
    description: str = ""
    submitted_by: str = ""
    priority: str = "normal"
    tags: list[str] = Field(default_factory=list)
    scope: str = ""

class UpdateResearchTopicRequest(BaseModel):
    title: str | None = None
    description: str | None = None
    priority: str | None = None
    tags: list[str] | None = None
    scope: str | None = None
    status: str | None = None

class AssignResearchRequest(BaseModel):
    agent_name: str = ""    # Empty = auto-assign

class SubmitBriefRequest(BaseModel):
    author_agent: str
    content: str
    summary: str = ""
    sources: list[str] = Field(default_factory=list)
    key_findings: list[str] = Field(default_factory=list)

class SubmitReviewRequest(BaseModel):
    reviewer_agent: str
    verdict: str            # approve, request_changes, reject
    comments: str = ""
    suggested_additions: list[str] = Field(default_factory=list)
    corrections: list[str] = Field(default_factory=list)
    confidence: int = 3     # 1-5
```

### Routes

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/research` | Create a new research topic |
| `GET` | `/research` | List topics (filter by status, agent, tag) |
| `GET` | `/research/stats` | Count by status |
| `GET` | `/research/{id}` | Full topic + briefs + reviews |
| `PUT` | `/research/{id}` | Update topic fields |
| `DELETE` | `/research/{id}` | Cancel/delete topic |
| `POST` | `/research/{id}/assign` | Assign agent (manual or auto) |
| `POST` | `/research/{id}/brief` | Submit draft or revised brief |
| `GET` | `/research/{id}/briefs` | List all brief versions |
| `POST` | `/research/{id}/reviews` | Submit peer review |
| `GET` | `/research/{id}/reviews` | List all reviews |
| `POST` | `/research/{id}/publish` | Publish final brief |
| `GET` | `/research-ui` | Serve Research frontend page |

### Endpoint Details

#### POST /research

Create a new research topic. Returns the created topic.

```python
@app.post("/research")
async def create_research_topic(req: CreateResearchTopicRequest):
    topic = research.create_topic(
        req.title,
        description=req.description,
        submitted_by=req.submitted_by,
        priority=req.priority,
        tags=req.tags,
        scope=req.scope,
    )
    return topic.to_dict()
```

#### GET /research

List topics with optional filters.

Query params: `status`, `assigned_agent`, `tag`, `include_cancelled` (bool), `limit` (int).

#### GET /research/{id}

Returns the full research package: topic, all brief versions, all reviews.

```python
@app.get("/research/{topic_id}")
async def get_research_topic(topic_id: int):
    topic = research.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Research topic not found")
    briefs = research.list_briefs(topic_id)
    reviews = research.get_reviews_for_topic(topic_id)
    return {
        "topic": topic.to_dict(),
        "briefs": [b.to_dict() for b in briefs],
        "reviews": [r.to_dict() for r in reviews],
    }
```

#### POST /research/{id}/assign

Assign a researcher. If `agent_name` is empty, run auto-delegation (see below). Sets status to `assigned`.

```python
@app.post("/research/{topic_id}/assign")
async def assign_research(topic_id: int, req: AssignResearchRequest):
    topic = research.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Research topic not found")
    if topic.status not in ("open",):
        raise HTTPException(400, f"Cannot assign topic in status '{topic.status}'")

    agent_name = req.agent_name or await _auto_assign_researcher(topic)
    if not agent_name:
        raise HTTPException(503, "No agents available for assignment")

    research.update_topic(topic_id, assigned_agent=agent_name, status="assigned")

    # Push autonomy event to wake the agent
    await autonomy.push_event(AgentEvent(
        type=EventType.task_assigned,
        agent_name=agent_name,
        data={
            "research_topic_id": topic.id,
            "title": topic.title,
            "type": "research_assignment",
        },
        priority=1 if topic.priority in ("high", "urgent") else 0,
    ))

    return research.get_topic(topic_id).to_dict()
```

#### POST /research/{id}/brief

Submit a draft or revised brief. If this is the first brief, sets topic status to `in_review` and triggers reviewer assignment. If this is a revision (version > 1), sets status based on review state.

```python
@app.post("/research/{topic_id}/brief")
async def submit_brief(topic_id: int, req: SubmitBriefRequest):
    topic = research.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Research topic not found")
    if topic.status not in ("assigned", "researching", "revising"):
        raise HTTPException(400, f"Cannot submit brief in status '{topic.status}'")

    brief = research.submit_brief(
        topic_id, req.author_agent,
        content=req.content, summary=req.summary,
        sources=req.sources, key_findings=req.key_findings,
    )

    if brief.version == 1:
        # First draft — move to review, assign reviewers
        reviewer_agents = await _auto_assign_reviewers(topic, exclude=req.author_agent)
        research.update_topic(topic_id,
            status="in_review",
            reviewer_agents=reviewer_agents,
        )
        # Notify reviewers
        for reviewer in reviewer_agents:
            await autonomy.push_event(AgentEvent(
                type=EventType.task_assigned,
                agent_name=reviewer,
                data={
                    "research_topic_id": topic.id,
                    "brief_id": brief.id,
                    "title": topic.title,
                    "type": "peer_review_request",
                },
                priority=0,
            ))
    else:
        # Revision submitted — ready for re-review or publish
        research.update_topic(topic_id, status="in_review")

    return brief.to_dict()
```

#### POST /research/{id}/reviews

Submit a peer review.

```python
@app.post("/research/{topic_id}/reviews")
async def submit_review(topic_id: int, req: SubmitReviewRequest):
    topic = research.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Research topic not found")
    if topic.status != "in_review":
        raise HTTPException(400, f"Cannot review topic in status '{topic.status}'")

    latest_brief = research.get_latest_brief(topic_id)
    if not latest_brief:
        raise HTTPException(400, "No brief to review")

    review = research.submit_review(
        latest_brief.id, req.reviewer_agent,
        verdict=req.verdict, comments=req.comments,
        suggested_additions=req.suggested_additions,
        corrections=req.corrections, confidence=req.confidence,
    )

    # Check if all reviewers have submitted
    all_reviews = research.get_reviews(latest_brief.id)
    reviewer_names = {r.reviewer_agent for r in all_reviews}
    expected = set(topic.reviewer_agents)

    if reviewer_names >= expected:
        # All reviews in — check verdicts
        verdicts = [r.verdict for r in all_reviews if r.reviewer_agent in expected]
        if all(v == "approve" for v in verdicts):
            # All approve — auto-publish
            research.update_brief(latest_brief.id, status="published",
                                  published_at=time.time())
            research.update_topic(topic_id, status="published")
        elif any(v == "reject" for v in verdicts):
            # Any reject — back to researching
            research.update_topic(topic_id, status="researching")
            await _notify_agent(topic.assigned_agent, topic, "review_rejected")
        else:
            # At least one request_changes — needs revision
            research.update_topic(topic_id, status="revising")
            await _notify_agent(topic.assigned_agent, topic, "revision_requested")

    return review.to_dict()
```

#### POST /research/{id}/publish

Manual publish override. For cases where a human wants to force-publish regardless of review state.

```python
@app.post("/research/{topic_id}/publish")
async def publish_research(topic_id: int):
    topic = research.get_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Research topic not found")

    latest_brief = research.get_latest_brief(topic_id)
    if not latest_brief:
        raise HTTPException(400, "No brief to publish")

    research.update_brief(latest_brief.id, status="published",
                          published_at=time.time())
    research.update_topic(topic_id, status="published")

    # Store in memory for future recall
    await _persist_to_memory(topic, latest_brief)

    return {
        "topic": research.get_topic(topic_id).to_dict(),
        "brief": research.get_latest_brief(topic_id).to_dict(),
    }
```

---

## Auto-Delegation Logic

Two functions handle automatic agent selection.

### Researcher Assignment

```python
async def _auto_assign_researcher(topic: ResearchTopic) -> str | None:
    """Pick the best available agent for a research topic.

    Selection criteria (in priority order):
    1. Agent is enabled and has a running session
    2. Agent context usage < 60% (has room to work)
    3. Agent role matches topic (e.g., 'researcher' role preferred)
    4. Agent has lowest active task count (least busy)
    5. If no agent is available, return None (topic stays queued)
    """
    all_agents = agents.list()
    candidates = []

    for agent in all_agents:
        if not agent.enabled:
            continue

        # Check session health
        session = manager.get(f"{agent.name}-main")
        context_pct = session.context_used_pct if session else 100.0
        if context_pct > 60.0:
            continue

        # Count active research assignments
        active_topics = research.list_topics(assigned_agent=agent.name, status="researching")
        active_tasks = len(tasks.list(assigned_agent=agent.name, status="in_progress"))

        # Role bonus: agents with 'researcher' or 'worker' role preferred
        role_score = 0
        if agent.role in ("researcher", "specialist"):
            role_score = 2
        elif agent.role in ("worker",):
            role_score = 1

        # Tag match bonus
        tag_score = 0
        agent_tags = set(agent.groups)  # Use groups as skill tags
        topic_tags = set(topic.tags)
        if agent_tags & topic_tags:
            tag_score = len(agent_tags & topic_tags)

        candidates.append({
            "name": agent.name,
            "load": len(active_topics) + active_tasks,
            "context_pct": context_pct,
            "role_score": role_score,
            "tag_score": tag_score,
        })

    if not candidates:
        return None

    # Sort: highest role_score, then highest tag_score, then lowest load, then lowest context
    candidates.sort(key=lambda c: (-c["role_score"], -c["tag_score"], c["load"], c["context_pct"]))
    return candidates[0]["name"]
```

### Reviewer Assignment

```python
async def _auto_assign_reviewers(
    topic: ResearchTopic,
    exclude: str,
    min_reviewers: int = 2,
) -> list[str]:
    """Pick 2+ agents to review a draft, excluding the original researcher.

    Selection criteria:
    1. Agent is enabled and online
    2. Agent is NOT the original researcher
    3. Prefer agents with low workload
    4. Prefer agents whose role/groups overlap with topic tags
    5. Return at least min_reviewers, or all available if fewer
    """
    all_agents = agents.list()
    candidates = []

    for agent in all_agents:
        if not agent.enabled or agent.name == exclude:
            continue

        session = manager.get(f"{agent.name}-main")
        context_pct = session.context_used_pct if session else 100.0
        if context_pct > 80.0:
            continue

        active_reviews = len([
            t for t in research.list_topics(status="in_review")
            if agent.name in t.reviewer_agents
        ])

        candidates.append({
            "name": agent.name,
            "review_load": active_reviews,
            "context_pct": context_pct,
        })

    # Sort by lowest review load, then lowest context
    candidates.sort(key=lambda c: (c["review_load"], c["context_pct"]))
    selected = [c["name"] for c in candidates[:max(min_reviewers, 2)]]
    return selected
```

### Fallback: Queued Topics

If no agent is available at assignment time, the topic stays in `open` status. The autonomy engine should periodically check for unassigned topics:

```python
# In the idle_check handler or a scheduled task:
async def _check_queued_research():
    """Attempt to assign any open research topics that are waiting."""
    open_topics = research.list_topics(status="open")
    for topic in open_topics:
        agent = await _auto_assign_researcher(topic)
        if agent:
            research.update_topic(topic.id, assigned_agent=agent, status="assigned")
            await autonomy.push_event(AgentEvent(
                type=EventType.task_assigned,
                agent_name=agent,
                data={
                    "research_topic_id": topic.id,
                    "title": topic.title,
                    "type": "research_assignment",
                },
            ))
```

---

## MCP Tool Integration (pinky-self)

Add research tools to `src/pinky_self/server.py` so agents can interact with the pipeline from their sessions.

```python
@mcp.tool()
def research_start(topic_id: int) -> str:
    """Mark a research topic as 'researching' — I'm starting work on this."""
    result = _api("PUT", f"/research/{topic_id}", {"status": "researching"})
    return json.dumps(result)

@mcp.tool()
def research_submit_brief(
    topic_id: int,
    content: str,
    summary: str = "",
    sources: list[str] = [],
    key_findings: list[str] = [],
) -> str:
    """Submit my research draft for this topic."""
    result = _api("POST", f"/research/{topic_id}/brief", {
        "author_agent": agent_name,
        "content": content,
        "summary": summary,
        "sources": sources,
        "key_findings": key_findings,
    })
    return json.dumps(result)

@mcp.tool()
def research_submit_review(
    topic_id: int,
    verdict: str,
    comments: str = "",
    suggested_additions: list[str] = [],
    corrections: list[str] = [],
    confidence: int = 3,
) -> str:
    """Submit my peer review for a research topic's draft brief.

    Args:
        topic_id: The research topic to review.
        verdict: approve, request_changes, or reject.
        comments: Detailed review in markdown.
        suggested_additions: Things missing from the brief.
        corrections: Factual errors found.
        confidence: 1-5, how confident I am in this review.
    """
    result = _api("POST", f"/research/{topic_id}/reviews", {
        "reviewer_agent": agent_name,
        "verdict": verdict,
        "comments": comments,
        "suggested_additions": suggested_additions,
        "corrections": corrections,
        "confidence": confidence,
    })
    return json.dumps(result)

@mcp.tool()
def research_get_topic(topic_id: int) -> str:
    """Get full research topic with brief and reviews."""
    result = _api("GET", f"/research/{topic_id}")
    return json.dumps(result)

@mcp.tool()
def research_list(status: str = "", limit: int = 20) -> str:
    """List research topics, optionally filtered by status."""
    qs = f"?limit={limit}"
    if status:
        qs += f"&status={status}"
    result = _api("GET", f"/research{qs}")
    return json.dumps(result)
```

---

## Memory Integration

When a brief is published, persist it to the agent's long-term memory for future recall.

```python
async def _persist_to_memory(topic: ResearchTopic, brief: ResearchBrief):
    """Store published research in the memory system.

    Creates a 'research' type reflection with high salience so it persists
    across sessions and can be recalled via semantic search.
    """
    memory_content = f"""Research: {topic.title}

Summary: {brief.summary}

Key Findings:
{chr(10).join(f'- {f}' for f in brief.key_findings)}

Sources:
{chr(10).join(f'- {s}' for s in brief.sources)}

Tags: {', '.join(topic.tags)}
Researcher: {brief.author_agent}
Reviewers: {', '.join(topic.reviewer_agents)}
Published: {brief.published_at}"""

    # Call the memory MCP or store directly
    # This depends on whether we're calling from the daemon (direct DB access)
    # or from an agent session (MCP tool call)
    # For daemon: direct store access is simplest
    from pinky_memory.store import ReflectionStore
    store = ReflectionStore(db_path=f"data/agents/{brief.author_agent}/memory.db")
    store.create(
        content=memory_content,
        context=f"research_topic_{topic.id}",
        reflection_type="insight",
        salience=4,  # High salience — persists through decay
        entities=[brief.author_agent] + topic.reviewer_agents,
        project="research",
    )
```

---

## Notification Integration

Use the existing outreach MCP / autonomy event system for notifications.

### When to notify:

| Event | Who | Method |
|-------|-----|--------|
| Topic assigned | Assigned agent | Autonomy event (task_assigned) |
| Review requested | Reviewer agents | Autonomy event (task_assigned) |
| All reviews in (approve) | Author agent | Autonomy event + outreach (Telegram) |
| Changes requested | Author agent | Autonomy event (task_updated) |
| Brief rejected | Author agent + submitted_by | Autonomy event + outreach |
| Brief published | submitted_by | Outreach (Telegram) |

The autonomy events are already handled — agents wake up and check their events. For user-facing notifications (Telegram), use the outreach send_message tool from the endpoint handler:

```python
async def _notify_user(topic: ResearchTopic, event: str):
    """Notify the topic submitter about status changes."""
    if not topic.submitted_by:
        return
    # If submitted_by is a known user with a chat_id, send via outreach
    # This is optional — depends on whether users have Telegram configured
```

---

## Frontend: Research Page

New file: `frontend-svelte/src/pages/Research.svelte`
New HTML fallback: `frontend/research.html`

### Layout

The Research page follows the existing Tasks page pattern (kanban board with modals).

#### Header Stats Bar
- Total topics | Open | In Review | Published

#### View Toggle
- **Pipeline** (default): Kanban columns — Open, Assigned/Researching, In Review, Revising, Published
- **List**: Table view with all topics, sortable by date/status/priority

#### Topic Cards (in pipeline/list)
Each card shows:
- Title
- Priority badge (urgent/high/normal/low)
- Status badge
- Assigned agent (if any)
- Tags
- Time since created
- Reviewer count / status (e.g., "2/2 reviewed")

#### Topic Detail Modal
When a topic card is clicked:

**Header:**
- Title (editable)
- Status badge
- Priority selector
- Assigned agent
- Tags (editable)

**Tabs:**
1. **Brief** — Rendered markdown of the latest brief version. Version selector if multiple.
2. **Reviews** — List of peer reviews with verdict badges, comments, corrections. Each review is a card.
3. **Timeline** — Chronological activity log: created, assigned, brief submitted, reviews submitted, published.

**Actions (bottom bar):**
- Assign Agent (if status = open)
- Publish (if at least one brief exists)
- Cancel Topic
- Delete

#### New Topic Modal
- Title (required)
- Description (textarea, markdown supported)
- Priority selector
- Tags (comma-separated)
- Scope / constraints (textarea)
- Auto-assign toggle (default: on)

### App Router Update

In `frontend-svelte/src/App.svelte`:

```javascript
import Research from './pages/Research.svelte';

const routes = {
    // ... existing routes
    '/research': Research,
};
```

In `api.py`:

```python
@app.get("/research-ui", response_class=HTMLResponse)
async def research_ui():
    return _serve_spa_or_html("research.html")
```

### Navigation

Add "Research" to the nav bar between "Tasks" and "Memories" in `frontend-svelte/src/components/Layout.svelte`.

---

## Implementation Plan

### Phase 1: Data Layer (research_store.py)
- [ ] Create `src/pinky_daemon/research_store.py` with SQLite schema and CRUD methods
- [ ] Add dataclass models: `ResearchTopic`, `ResearchBrief`, `PeerReview`
- [ ] Write tests in `tests/test_research_store.py` (follow `tests/test_task_store.py` pattern)
- [ ] Ensure JSON list serialization matches task_store pattern

### Phase 2: API Endpoints
- [ ] Add Pydantic request models to `api.py`
- [ ] Add all `/research` routes
- [ ] Initialize `ResearchStore` in `create_api()` alongside `TaskStore`
- [ ] Wire auto-delegation functions
- [ ] Add `research-ui` HTML route

### Phase 3: MCP Tools (pinky-self)
- [ ] Add research tools to `src/pinky_self/server.py`
- [ ] Test from a live agent session — submit topic, submit brief, submit review

### Phase 4: Auto-Delegation
- [ ] Implement `_auto_assign_researcher()` and `_auto_assign_reviewers()`
- [ ] Add `_check_queued_research()` to the idle_check handler
- [ ] Test with multiple agents in the fleet

### Phase 5: Frontend
- [ ] Create `Research.svelte` page (pipeline + list views)
- [ ] Create topic detail modal with brief/reviews/timeline tabs
- [ ] Add new topic modal
- [ ] Add to App.svelte routes and Layout.svelte navigation
- [ ] Create `frontend/research.html` fallback for non-SPA mode

### Phase 6: Memory + Notifications
- [ ] Implement `_persist_to_memory()` for published briefs
- [ ] Wire outreach notifications for key status transitions
- [ ] Test end-to-end: submit topic -> assign -> draft -> review -> revise -> publish

---

## Edge Cases and Design Decisions

### What if a reviewer goes offline mid-review?
The system waits for all assigned reviewers. If a reviewer's session dies or goes stale (heartbeat timeout), the endpoint handler should allow proceeding with available reviews after a configurable timeout (default: 24h). Alternatively, the user can manually publish via `POST /research/{id}/publish`.

### What if the researcher's context fills up during research?
The agent should save a continuation to wake context before restarting. The research topic stays in `researching` status. On restart, the agent picks up where it left off using the continuation data. The pinky-self tools provide `save_continuation()` for this.

### Can a topic have multiple researchers?
Not in v1. One assigned_agent per topic. If needed later, extend to a `researcher_agents: list[str]` field.

### Can the same agent research and review?
No. The `_auto_assign_reviewers()` function explicitly excludes the original researcher. The `submit_review` endpoint should also validate that `reviewer_agent != brief.author_agent`.

### What about topics that never get assigned?
The `_check_queued_research()` function runs periodically. Topics that sit in `open` for too long (> 48h) should surface in the dashboard as a warning. The frontend should show a "No agents available" indicator on stale open topics.

### Concurrent brief submissions?
The `version` field auto-increments. If somehow two briefs arrive for the same topic, both are stored — the latest version wins. The `get_latest_brief()` method sorts by version descending.

### Review confidence scoring
Confidence is informational only in v1. It helps humans gauge review quality. A future enhancement could weight review verdicts by confidence (e.g., a confidence-5 "approve" overrides a confidence-2 "request_changes").

---

## Future Enhancements (Not in v1)

1. **Research templates**: Pre-defined topic structures for common research types (competitive analysis, tech evaluation, architecture review)
2. **Citation verification**: Agents validate each other's source URLs are accessible and relevant
3. **Research graph**: Link related topics — "this builds on Topic #12"
4. **Quality metrics**: Track approval rate, revision count, reviewer agreement per agent
5. **Public feed**: RSS/Atom feed of published briefs for external consumption
6. **Collaborative editing**: Multiple agents contribute sections to a single brief
7. **Research scheduling**: Auto-submit topics on a cron (e.g., "weekly industry scan")
