"""Keep independently named launch and delivery boundaries under one policy."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from pinky_daemon import api

# Configuration changes and explicit administrative teardown stay available.
BLOCKED = (
    "_prepare_streaming_session", "_connect_prepared_session",
    "_connect_prepared_session_unchecked", "_start_streaming_session",
    "_start_streaming_session_unchecked", "_replace_session_locked",
    "_connect_retained_session", "_restart_compatible", "_ensure_streaming_session",
    "_deliver_streaming", "_guarded_handle_inbound", "_guarded_inject_agent_message",
    "fork_session", "clone_worker", "send_message", "restart_session", "refresh_session",
    "refresh_agent_sessions", "send_agent_message", "spawn_agent_session",
    "create_streaming_session", "restart_streaming_session",
    "_restart_streaming_session_after_response", "compact_streaming_session",
    "send_agent_message_direct", "chat_with_agent", "upload_file_to_agent",
    "send_tmux_pane_keys", "forward_to_agent", "wake_agent", "_wake_callback",
    "_dream_callback", "_librarian_callback", "_trigger_librarian", "_heartbeat_resurrect",
    "_is_resurrectable", "_notify_scheduler_agent", "_watchdog_recover",
    "_watchdog_mcp_notice", "_watchdog_mcp_recover", "admin_force_restart_agent",
    "_container_start_main", "containerize_agent", "decontainerize_agent",
    "start_agent_loop", "push_agent_event", "on_startup", "_launch_boot_session", "on_shutdown",
)
CONFIGURATION = ("update_agent", "apply_agent_skills", "set_streaming_model", "set_agent_provider")
ADMINISTRATION = ("stop_agent", "stop_all_agents", "delete_streaming_session",
                  "rename_streaming_session", "archive_streaming_session", "transport_wake")


def functions():
    return {node.name: node for node in ast.walk(ast.parse(Path(api.__file__).read_text()))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def direct_nodes(node):
    for child in ast.iter_child_nodes(node):
        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            yield child
            yield from direct_nodes(child)


@pytest.mark.parametrize("name", BLOCKED + CONFIGURATION)
def test_each_launch_delivery_or_configuration_boundary_reads_the_one_policy(name):
    found = functions()
    assert name in found, f"Missing inventoried boundary: {name}"
    calls = [node for node in direct_nodes(found[name]) if isinstance(node, ast.Call)]
    assert any(isinstance(node.func, ast.Name) and node.func.id == "_storage_hold_for"
               for node in calls), f"{name} bypasses the storage-hold reader"


def test_effective_hold_state_has_one_reader():
    found = functions()
    assert "_storage_hold_for" in found
    for name, fn in found.items():
        for node in direct_nodes(fn):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            base = node.func.value
            if isinstance(base, ast.Attribute) and base.attr in {"storage_holds", "storage_hold_overrides"}:
                if node.func.attr not in {"pop", "add", "discard", "clear", "update"}:
                    assert name == "_storage_hold_for", f"{name} reads raw hold state"


def test_operator_controls_keep_their_explicit_disposition():
    found = functions()
    assert set(ADMINISTRATION).issubset(found)
    assert not set(ADMINISTRATION).intersection(BLOCKED)
    assert "archive_streaming_session" in found and "compact_streaming_session" in BLOCKED
