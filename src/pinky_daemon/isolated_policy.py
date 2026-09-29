"""Shared, import-safe isolation policy and tool entitlement data."""

import logging
import os
from contextlib import nullcontext


def policy_mode(log=None):
    value = os.environ.get("PINKY_ISOLATED_POLICY_MODE", "off").strip().lower()
    if value not in {"off", "shadow", "enforce"}:
        message = "isolation: invalid policy mode; using enforce"
        logging.getLogger(__name__).error(message)
        if log:
            log(message)
        return "enforce"
    return value


def isolation_flag(registry, name):
    """Only a literal bool from an existing row is a policy decision.

    AgentRegistry supports cross-thread access and creates a cursor per get.
    Serialize with its RMW lock so policy reads don't split guarded updates.
    Missing dependencies/rows/attributes and corrupt flags fail closed.
    """
    try:
        with getattr(registry, "_rmw_lock", nullcontext()):
            row = registry.get(name)
        value = getattr(row, "isolated", None)
        return value if type(value) is bool else None
    except Exception:
        return None


SKILL_TO_GATES: dict[str, list[str]] = {
    "pinky-self": [
        "schedule",
        "admin",
        "skill-admin",
        "triggers",
        "extras",
        "tasks-admin",
        "voice",
        "apps",
    ],
    "pinky-memory": ["kb"],
    "research": ["research"],
    "presentations": ["presentations"],
}

# All valid gate names for reference
ALL_TOOL_GATES = [
    "extras",
    "kb",
    "research",
    "presentations",
    "triggers",
    "schedule",
    "skill-admin",
    "admin",
    "tasks-admin",
    "voice",
    "apps",
]

# Gate → pinky-self tool names registered under that gate.
# Used to compute disallowed_tools for SDK-side gating in shared MCP mode
# (where the shared server runs ALL gates and filtering happens client-side).
GATE_TOOL_NAMES: dict[str, list[str]] = {
    "extras": [
        "get_attribution",
        "render_pdf",
        "spawn_clone",
        "get_agent_card",
    ],
    "schedule": [
        "set_wake_schedule",
        "update_wake_schedule",
        "list_my_schedules",
        "get_schedule",
        "remove_wake_schedule",
        "discard_pending_schedule_wake",
    ],
    "tasks-admin": [
        "decompose_project",
        "bulk_create_tasks",
    ],
    "presentations": [
        "get_presentation_template",
        "create_presentation",
        "update_presentation",
        "list_presentations",
    ],
    "research": [
        "submit_research_brief",
        "submit_research_review",
        "get_my_research_assignments",
        "claim_research_topic",
        "create_research_topic",
        "publish_research",
        "list_research_topics",
        "get_research_detail",
        "export_research_pdf",
    ],
    "skill-admin": [
        "list_available_skills",
        "add_skill",
        "remove_skill",
        "discover_skills",
        "install_skill",
        "create_skill",
        "propose_skill",
    ],
    "admin": [
        "check_for_updates",
        "update_and_restart",
        "restart_daemon",
        "register_agent",
    ],
    "triggers": [
        "create_trigger",
        "list_triggers",
        "delete_trigger",
        "test_trigger",
    ],
    "kb": [
        "kb_ingest",
        "kb_search",
        "kb_get_wiki",
        "kb_stats",
        "kb_run_librarian",
        "kb_save_wiki",
        "kb_delete_wiki",
        "kb_delete_raw",
        "kb_update_raw",
    ],
    "voice": [
        "propose_call",
        "list_voice_calls",
        "list_call_requests",
    ],
    "apps": [
        "create_app",
        "deploy_app",
        "update_app",
        "get_app_source",
        "list_apps",
        "delete_app",
        "app_url",
    ],
}

# Pre-compute the full set of all gated tool names (MCP-prefixed)
ALL_GATED_TOOL_NAMES: set[str] = set()
for _gate_tools in GATE_TOOL_NAMES.values():
    for _tool in _gate_tools:
        ALL_GATED_TOOL_NAMES.add(f"mcp__pinky-self__{_tool}")


def agent_tool_gates(agent_name, skill_store=None, agent_registry=None):
    if skill_store is None:
        return []
    try:
        assigned = skill_store.get_agent_skills(agent_name, enabled_only=True)
        gates = {gate for skill in assigned for gate in SKILL_TO_GATES.get(skill.get("name"), [])}
    except Exception:
        return []
    if isolation_flag(agent_registry, agent_name) is not False:
        gates.difference_update({"admin", "skill-admin"})
    return sorted(gates)


def launch_denies(agent_name, skill_store=None, agent_registry=None, stored=()):
    gates = set(agent_tool_gates(agent_name, skill_store, agent_registry))
    denied = {
        f"mcp__pinky-self__{tool}"
        for gate, tools in GATE_TOOL_NAMES.items()
        if gate not in gates
        for tool in tools
    }
    return sorted(set(stored or ()) | denied)


# Exact method/template grants; handlers retain body and object ownership checks.
ISOLATED_MUTATION_ALLOW = frozenset(
    {
        ("POST", "/broker/thread"),  # Existing thread context and verified body sender
        ("POST", "/broker/send"),  # Verified body sender
        ("POST", "/broker/react"),  # Verified body sender
        ("POST", "/broker/send-voice"),  # Verified body sender
        ("POST", "/broker/send-gif"),  # Server-selected GIF; verified body sender
        ("POST", "/broker/broadcast"),  # Approved recipients and verified body sender
        ("POST", "/agents/{agent_name}/schedules"),  # New row bound to path agent
        ("PATCH", "/agents/{agent_name}/schedules/{schedule_id}"),  # Owned schedule lookup
        (
            "DELETE",
            "/agents/{agent_name}/pending-schedule-wakes/{pending_id}",
        ),  # Owned pending wake lookup
        ("PUT", "/agents/{agent_name}/context"),  # Own context
        (
            "POST",
            "/agents/{name}/streaming/restart",
        ),  # Own streaming session; saved-context guard
        ("POST", "/agents/{agent_name}/heartbeat"),  # Own heartbeat
        (
            "POST",
            "/agents/{name}/sessions/{session_label}/effort",
        ),  # Session lookup nested under path agent
        ("POST", "/agents/{name}/message"),  # Existing group exception and body sender check
        (
            "POST",
            "/agents/{name}/mesh/send",
        ),  # Own sender and outbound destination allowlist
        ("DELETE", "/agents/{agent_name}/triggers/{trigger_id}"),  # Owned trigger lookup
        (
            "POST",
            "/agents/{agent_name}/triggers/{trigger_id}/test",
        ),  # Owned trigger lookup before wake
        (
            "POST",
            "/api/voice/request",
        ),  # Requester derived from verified caller
        ("POST", "/agents/{name}/effort-drift"),  # Own drift telemetry
        ("POST", "/agents/{name}/transport/wake"),  # Own transport notification
        ("POST", "/agents/{name}/transport/tool-use"),  # Own transport notification
        (
            "POST",
            "/agents/{name}/transport/tool-result",
        ),  # Own transport notification
        (
            "POST",
            "/agents/{name}/transport/stop-failure",
        ),  # Own transport notification
        ("POST", "/agents/{name}/status"),  # Own working status
        ("POST", "/agents/{name}/policy/evaluate"),  # Own tool-policy evaluation
    }
)


def resolve_mutation_route(app, scope):
    """Mirror first-FULL dispatch; PARTIAL never grants mutation authority."""
    from fastapi.routing import APIRoute
    from starlette.routing import Match

    try:
        for route in app.routes:
            match, child = route.matches(scope)
            if match == Match.FULL:
                if isinstance(route, APIRoute):
                    return route.path, child.get("path_params", {})
                return None, {}
    except Exception:
        pass
    return None, {}


# Explicit core names: new registrations require a policy review.
CORE_TOOLS = {
    "self": frozenset(
        [
            "agent_status",
            "block_task",
            "check_inbox",
            "check_my_health",
            "claim_task",
            "complete_task",
            "context_restart",
            "context_status",
            "create_task",
            "get_next_task",
            "get_owner_profile",
            "get_task",
            "list_agents",
            "list_my_skills",
            "load_my_context",
            "load_skill",
            "mcp_probe",
            "mesh_remote_send",
            "save_my_context",
            "search_history",
            "send_file_to_agent",
            "send_heartbeat",
            "send_to_agent",
            "set_thinking_effort",
            "who_am_i",
        ]
    ),
    "memory": frozenset(
        [
            "introspect",
            "kg_add",
            "kg_connections",
            "kg_find_contradictions",
            "kg_invalidate",
            "kg_query",
            "kg_stats",
            "kg_sweep_ephemeral",
            "kg_timeline",
            "kg_what_changed",
            "memory_links",
            "memory_query",
            "recall",
            "reflect",
        ]
    ),
    "messaging": frozenset(
        [
            "broadcast",
            "react",
            "send",
            "send_document",
            "send_gif",
            "send_photo",
            "send_video",
            "send_voice",
            "thread",
        ]
    ),
}
