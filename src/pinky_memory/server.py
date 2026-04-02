from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

from pinky_memory.types import (
    PRESET_NAMES,
    IntrospectInput,
    MemoryQueryFilters,
    RecallInput,
    ReflectInput,
    Reflection,
    ReflectionType,
)

if TYPE_CHECKING:
    from pinky_memory.embeddings import EmbeddingClient, NoOpEmbeddingClient
    from pinky_memory.store import ReflectionStore


def _log(msg: str) -> None:
    """Log to stderr (stdout is MCP protocol)."""
    print(msg, file=sys.stderr, flush=True)


def create_server(
    store: ReflectionStore,
    embedder: EmbeddingClient | NoOpEmbeddingClient,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
) -> FastMCP:
    mcp = FastMCP("pinky-memory", host=host, port=port)

    @mcp.tool()
    def reflect(
        content: str,
        type: str = "fact",
        context: str = "",
        project: str = "",
        salience: int = 3,
        supersedes: str = "",
        entities: list[str] | None = None,
        source_session_id: str | None = None,
        source_channel: str | None = None,
        source_message_ids: list[str] | None = None,
    ) -> str:
        """Persist a cross-session insight to long-term vector memory.

        WHEN TO USE: You learned something worth remembering beyond this session —
        owner preferences, project patterns, key decisions, interaction styles,
        or facts about people. Call this as soon as you notice the insight.
        NOT FOR: Active task state (use save_my_context), things derivable from
        code or git history, or ephemeral conversation details.

        Args:
            content: The reflection content to store.
            type: One of: insight, project_state, interaction_pattern, continuation, fact.
            context: Additional context about when/why this was noted.
            project: Project name this relates to (empty for general).
            salience: Importance 1-5 (1=low, 5=critical). Default 3.
            supersedes: ID of a previous reflection this replaces.
            entities: Person names to tag this memory with (e.g. ["terry", "kyle"]).
            source_session_id: Session that produced this reflection (e.g. "telegram:6770805286").
            source_channel: Channel name (e.g. "telegram").
            source_message_ids: Message IDs that produced this reflection.
        """
        input_data = ReflectInput(
            content=content,
            type=ReflectionType(type),
            context=context,
            project=project,
            salience=salience,
            supersedes=supersedes,
            entities=[e.lower() for e in entities] if entities else [],
            source_session_id=source_session_id,
            source_channel=source_channel,
            source_message_ids=source_message_ids or [],
        )

        # Generate embedding
        embedding = embedder.embed(input_data.content)

        # Build reflection
        ref = Reflection(
            type=input_data.type,
            content=input_data.content,
            context=input_data.context,
            project=input_data.project,
            salience=input_data.salience,
            supersedes=input_data.supersedes,
            entities=input_data.entities,
            source_session_id=input_data.source_session_id,
            source_channel=input_data.source_channel,
            source_message_ids=input_data.source_message_ids,
            embedding=embedding,
        )

        # Insert
        ref = store.insert(ref)

        # Handle supersession (after insert so we have ref.id)
        if input_data.supersedes:
            store.deactivate_superseded(input_data.supersedes, superseded_by=ref.id)
        _log(f"reflect: stored {ref.id} type={ref.type.value}")

        return json.dumps({
            "id": ref.id,
            "type": ref.type.value,
            "salience": ref.salience,
            "stored": True,
        })

    @mcp.tool()
    def recall(
        query: str = "",
        type: str = "",
        project: str = "",
        entity: str = "",
        min_weight: float = 0.0,
        limit: int = 10,
        active_only: bool = True,
    ) -> str:
        """Search long-term memory by meaning or filters.

        WHEN TO USE: You need to remember something from a past session — what
        you know about a person, a project decision, an owner preference, or any
        previously stored insight. Semantic search (query) finds related memories
        even with different wording; filters narrow by type/project/entity.
        NOT FOR: Conversation history from this session (use search_history),
        current task state (use load_my_context), or memory statistics (use introspect).

        Args:
            query: Natural language search query. Leave empty to browse by filters.
            type: Filter by type: insight, project_state, interaction_pattern, continuation, fact.
            project: Filter by project name.
            entity: Filter by person name (e.g. "terry"). Returns only reflections tagged with this entity.
            min_weight: Minimum weight threshold (0.0-1.0).
            limit: Maximum results to return (1-50, default 10).
            active_only: Only return active (non-superseded) reflections.
        """
        input_data = RecallInput(
            query=query,
            type=ReflectionType(type) if type else None,
            project=project,
            entity=entity.lower() if entity else "",
            min_weight=min_weight,
            limit=limit,
            active_only=active_only,
        )

        results: list[Reflection] = []

        if input_data.query:
            # Try vector search first
            query_embedding = embedder.embed(input_data.query)
            if query_embedding:
                results = store.search_by_embedding(
                    query_embedding=query_embedding,
                    limit=input_data.limit,
                    active_only=input_data.active_only,
                    type_filter=input_data.type,
                    project_filter=input_data.project,
                    min_weight=input_data.min_weight,
                    entity_filter=input_data.entity,
                )

            # Fall back to keyword search if no vector results
            if not results:
                results = store.search_by_keyword(
                    query=input_data.query,
                    limit=input_data.limit,
                    active_only=input_data.active_only,
                    type_filter=input_data.type,
                    project_filter=input_data.project,
                    min_weight=input_data.min_weight,
                    entity_filter=input_data.entity,
                )
        else:
            # No query — browse by filters using keyword search with empty query
            results = store.search_by_keyword(
                query="",
                limit=input_data.limit,
                active_only=input_data.active_only,
                type_filter=input_data.type,
                project_filter=input_data.project,
                min_weight=input_data.min_weight,
                entity_filter=input_data.entity,
            )

        _log(f"recall: found {len(results)} results for query={input_data.query!r}")

        return json.dumps({
            "count": len(results),
            "reflections": [
                {
                    "id": r.id,
                    "type": r.type.value,
                    "content": r.content,
                    "context": r.context,
                    "project": r.project,
                    "salience": r.salience,
                    "weight": r.weight,
                    "entities": r.entities,
                    "source_session_id": r.source_session_id,
                    "source_channel": r.source_channel,
                    "source_message_ids": r.source_message_ids,
                    "created_at": r.created_at.isoformat(),
                    "access_count": r.access_count,
                }
                for r in results
            ],
        })

    @mcp.tool()
    def introspect(
        timeframe: str = "all",
        type: str = "",
        project: str = "",
    ) -> str:
        """Get memory statistics and pattern overview.

        WHEN TO USE: You want to understand your memory landscape — how many
        memories by type/project, salience distribution, growth over time.
        Good for memory health checks and understanding knowledge coverage.
        NOT FOR: Finding a specific memory (use recall), or exact filtering (use memory_query).

        Args:
            timeframe: Time window: day, week, month, or all.
            type: Filter by reflection type.
            project: Filter by project name.
        """
        input_data = IntrospectInput(
            timeframe=timeframe,
            type=ReflectionType(type) if type else None,
            project=project,
        )

        stats = store.introspect(
            timeframe=input_data.timeframe,
            type_filter=input_data.type,
            project_filter=input_data.project,
        )

        _log(f"introspect: {stats['total_reflections']} reflections in timeframe={input_data.timeframe}")

        return json.dumps(stats)

    @mcp.tool()
    def memory_links(reflection_id: str) -> str:
        """Explore the memory graph around a specific reflection.

        WHEN TO USE: You found a relevant memory via recall and want to see
        what's connected to it — related insights, follow-up facts, or
        contradicting observations. Useful for building a fuller picture.
        NOT FOR: Initial memory search (start with recall).

        Args:
            reflection_id: The ID of the reflection to get links for.
        """
        links = store.get_links(reflection_id)
        if not links:
            return json.dumps({"count": 0, "links": []})

        result_links = []
        for link in links:
            neighbor = store.get(link.target_id)
            result_links.append({
                "id": link.target_id,
                "similarity": round(link.similarity, 4),
                "content": neighbor.content[:200] if neighbor else "(deleted)",
                "type": neighbor.type.value if neighbor else "unknown",
                "salience": neighbor.salience if neighbor else 0,
                "active": neighbor.active if neighbor else False,
            })

        _log(f"memory_links: {len(result_links)} links for {reflection_id}")
        return json.dumps({"count": len(result_links), "links": result_links})

    @mcp.tool()
    def memory_query(
        preset: str = "",
        type: str = "",
        project: str = "",
        entity: str = "",
        salience_min: int | None = None,
        salience_max: int | None = None,
        active: bool = True,
        created_after: str = "",
        created_before: str = "",
        accessed_after: str = "",
        due_for_review: bool = False,
        has_links: bool | None = None,
        sort_by: str = "created_at",
        sort_dir: str = "desc",
        limit: int = 20,
        offset: int = 0,
    ) -> str:
        """Run exact-match queries against memory with structured filters.

        WHEN TO USE: You need precise filtering that semantic search can't do —
        memories by date range, salience threshold, review status, or using
        presets like "stale_projects" or "high_value". Good for memory
        maintenance, audits, and browsing by criteria.
        NOT FOR: Finding memories by meaning or topic (use recall).

        Args:
            preset: Named shortcut (recent_insights, stale_projects, high_value, orphans, due_review, by_project).
            type: Filter by type: fact, insight, project_state, interaction_pattern, continuation.
            project: Filter by project name (partial match).
            entity: Filter by entity/person name.
            salience_min: Minimum salience (1-5).
            salience_max: Maximum salience (1-5).
            active: Only active memories (default True).
            created_after: ISO date: only memories created after this date.
            created_before: ISO date: only memories created before this date.
            accessed_after: ISO date: only memories accessed after this date.
            due_for_review: Only memories due for spaced review.
            has_links: Filter by whether memory has links (True/False/None=any).
            sort_by: Sort field: created_at, accessed_at, salience, access_count.
            sort_dir: Sort direction: asc or desc.
            limit: Max results (1-100, default 20).
            offset: Skip first N results (for pagination).
        """
        if preset and preset not in PRESET_NAMES:
            return json.dumps({"error": f"Unknown preset '{preset}'. Valid: {', '.join(sorted(PRESET_NAMES))}"})

        filter_kwargs: dict = {}
        if preset:
            filter_kwargs["preset"] = preset
        if type:
            filter_kwargs["type"] = type
        if project:
            filter_kwargs["project"] = project
        if entity:
            filter_kwargs["entity"] = entity
        if salience_min is not None:
            filter_kwargs["salience_min"] = salience_min
        if salience_max is not None:
            filter_kwargs["salience_max"] = salience_max
        filter_kwargs["active"] = active
        if created_after:
            filter_kwargs["created_after"] = created_after
        if created_before:
            filter_kwargs["created_before"] = created_before
        if accessed_after:
            filter_kwargs["accessed_after"] = accessed_after
        if due_for_review:
            filter_kwargs["due_for_review"] = True
        if has_links is not None:
            filter_kwargs["has_links"] = has_links
        filter_kwargs["sort_by"] = sort_by
        filter_kwargs["sort_dir"] = sort_dir
        filter_kwargs["limit"] = limit
        filter_kwargs["offset"] = offset

        filters = MemoryQueryFilters(**filter_kwargs)
        results, total = store.query(filters)

        _log(f"memory_query: {total} total, returning {len(results)}")

        return json.dumps({
            "total": total,
            "count": len(results),
            "offset": offset,
            "reflections": [
                {
                    "id": r.id,
                    "type": r.type.value,
                    "content": r.content,
                    "project": r.project,
                    "salience": r.salience,
                    "active": r.active,
                    "entities": r.entities,
                    "access_count": r.access_count,
                    "created_at": r.created_at.isoformat(),
                    "accessed_at": r.accessed_at.isoformat(),
                    "next_review_date": r.next_review_date,
                    "event_date": r.event_date,
                }
                for r in results
            ],
        })

    return mcp
