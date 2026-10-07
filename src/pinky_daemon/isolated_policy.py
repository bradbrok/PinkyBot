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


def isolated_caller_policy(request):
    """Return a verified isolated caller and mode, with uncertain flags closed."""
    caller = getattr(request.state, "internal_caller", "")
    mode = policy_mode()
    if (
        not caller
        or mode == "off"
        or isolation_flag(getattr(request.app.state, "agents", None), caller) is False
    ):
        return "", mode
    return caller, mode


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


# Tool visibility and exact mutation grants share these reviewed registrations.
# Empty tuples are read-only or have no daemon route; handlers retain ownership checks.
ISOLATED_TOOL_ROUTES = {
    ("self", "agent_status"): (),
    ("self", "check_inbox"): (),
    ("self", "check_my_health"): (),
    ("self", "context_status"): (),
    ("self", "get_agent_card"): (),
    ("self", "get_attribution"): (),
    ("self", "get_next_task"): (),
    ("self", "get_owner_profile"): (),
    ("self", "get_schedule"): (),
    ("self", "get_task"): (),
    ("self", "kb_get_wiki"): (),
    ("self", "kb_search"): (),
    ("self", "kb_stats"): (),
    ("self", "list_agents"): (),
    ("self", "list_call_requests"): (),
    ("self", "list_my_schedules"): (),
    ("self", "list_my_skills"): (),
    ("self", "list_triggers"): (),
    ("self", "list_voice_calls"): (),
    ("self", "load_my_context"): (),
    ("self", "load_skill"): (),
    ("self", "mcp_probe"): (),
    ("self", "search_history"): (),
    ("self", "who_am_i"): (),
    ("self", "block_task"): (("POST", "/tasks/block/{task_id}"),),
    ("self", "claim_task"): (("POST", "/tasks/claim/{task_id}"),),
    ("self", "complete_task"): (("POST", "/tasks/complete/{task_id}"),),
    ("self", "context_restart"): (("POST", "/agents/{name}/streaming/restart"),),
    ("self", "create_task"): (("POST", "/tasks"),),
    ("self", "delete_trigger"): (("DELETE", "/agents/{agent_name}/triggers/{trigger_id}"),),
    ("self", "discard_pending_schedule_wake"): (
        ("DELETE", "/agents/{agent_name}/pending-schedule-wakes/{pending_id}"),
    ),
    ("self", "mesh_remote_send"): (("POST", "/agents/{name}/mesh/send"),),
    ("self", "propose_call"): (("POST", "/api/voice/request"),),
    ("self", "remove_wake_schedule"): (
        ("DELETE", "/agents/{agent_name}/schedules/{schedule_id}"),  # Owned schedule lookup
    ),
    ("self", "save_my_context"): (("PUT", "/agents/{agent_name}/context"),),
    ("self", "send_heartbeat"): (("POST", "/agents/{agent_name}/heartbeat"),),
    ("self", "send_to_agent"): (("POST", "/agents/{name}/message"),),
    ("self", "set_thinking_effort"): (("POST", "/agents/{name}/sessions/{session_label}/effort"),),
    ("self", "set_wake_schedule"): (("POST", "/agents/{agent_name}/schedules"),),
    ("self", "test_trigger"): (("POST", "/agents/{agent_name}/triggers/{trigger_id}/test"),),
    ("self", "update_wake_schedule"): (("PATCH", "/agents/{agent_name}/schedules/{schedule_id}"),),
    ("messaging", "broadcast"): (("POST", "/broker/broadcast"),),
    ("messaging", "react"): (("POST", "/broker/react"),),
    ("messaging", "send"): (("POST", "/broker/send"),),
    ("messaging", "send_document"): (("POST", "/broker/send-document"),),
    ("messaging", "send_gif"): (("POST", "/broker/send-gif"),),
    ("messaging", "send_photo"): (("POST", "/broker/send-photo"),),
    ("messaging", "send_video"): (("POST", "/broker/send-video"),),
    ("messaging", "send_voice"): (("POST", "/broker/send-voice"),),
    ("messaging", "thread"): (("POST", "/broker/thread"),),
}

ISOLATED_NON_TOOL_ROUTES = frozenset(
    {
        ("POST", "/agents/{name}/effort-drift"),  # Own drift telemetry
        ("POST", "/agents/{name}/transport/wake"),  # Own transport notification
        (
            "POST",
            "/agents/{name}/transport/transcript-path",
        ),  # Own transport notification; own project dir
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

ISOLATED_MUTATION_ALLOW = ISOLATED_NON_TOOL_ROUTES | frozenset(
    route for routes in ISOLATED_TOOL_ROUTES.values() for route in routes
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
