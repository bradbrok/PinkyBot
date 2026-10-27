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
    "_deliver_streaming", "_guarded_handle_inbound", "_guarded_inject_agent_message", "_guarded_route_streaming",
    "fork_session", "clone_worker", "send_message", "restart_session", "refresh_session",
    "refresh_agent_sessions", "send_agent_message", "spawn_agent_session",
    "create_streaming_session", "restart_streaming_session",
    "_restart_streaming_session_after_response", "compact_streaming_session",
    "send_agent_message_direct", "chat_with_agent", "upload_file_to_agent",
    "send_tmux_pane_keys", "forward_to_agent", "wake_agent", "_wake_callback",
    "_dream_callback", "_librarian_callback", "_trigger_librarian", "_heartbeat_resurrect",
    "_is_resurrectable", "_notify_scheduler_agent", "_watchdog_recover",
    "_watchdog_mcp_notice", "_watchdog_mcp_recover", "admin_force_restart_agent",
    "_get_watchdog_config", "_watchdog_tmux_liveness", "_watchdog_login_wall_probe", "_watchdog_login_wall_freeze",
    "_storage_cleanup_control",
    "_container_start_main", "containerize_agent", "decontainerize_agent",
    "start_agent_loop", "push_agent_event", "on_startup", "_launch_boot_session", "on_shutdown",
)
CONFIGURATION = ("register_agent", "update_agent", "apply_agent_skills", "set_streaming_model", "set_agent_provider", "approve_user")
ADMINISTRATION = ("stop_agent", "stop_all_agents", "delete_streaming_session",
                  "rename_streaming_session", "archive_streaming_session", "transport_wake")

# Each real effect is exercised through its owning API entry, with a held/clean
# pair and an in-memory container-operations recorder at the external boundary.
CONTAINER_RUNTIME_CALLERS = (
    ("src/pinky_daemon/api.py", "create_api.register_agent", "provision", "registration"),
    ("src/pinky_daemon/api.py", "create_api.containerize_agent", "containerize", "containerize"),
    ("src/pinky_daemon/api.py", "create_api._container_start_main", "_start_streaming_session", "cold"),
    ("src/pinky_daemon/container_ops.py", "ContainerLifecycle._containerize", "provision", "containerize"),
    ("src/pinky_daemon/container_ops.py", "ContainerLifecycle._containerize", "start", "containerize"),
    ("src/pinky_daemon/tmux_session.py", "TmuxSession._ensure_container_started", "ensure_started", "cold"),
    ("src/pinky_daemon/provisioning.py", "ContainerProvisioner.ensure_started", "provision", "cold"),
    ("src/pinky_daemon/provisioning.py", "ContainerProvisioner.ensure_started", "start", "cold"),
    ("src/pinky_daemon/provisioning.py", "ContainerProvisioner._recreate_if_image_changed", "provision", "image-recreate"),
    ("src/pinky_daemon/provisioning.py", "ContainerProvisioner.provision", "_create_argv", "registration"),
    ("src/pinky_daemon/provisioning.py", "ContainerProvisioner.start", "container-start-command", "cold"),
)


def source_sites(path):
    tree = ast.parse(path.read_text())
    result = []

    def walk(node, owner=""):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            owner = owner + "." + node.name if owner else node.name
        if isinstance(node, ast.Attribute) and node.attr in {"provision", "start", "ensure_started", "containerize", "_create_argv"}:
            result.append((owner, node.attr, node.lineno))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_start_streaming_session":
            result.append((owner, node.func.id, node.lineno))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run" and node.args and isinstance(node.args[0], ast.List)
                and any(isinstance(value, ast.Constant) and value.value == "start" for value in node.args[0].elts)):
            result.append((owner, "container-start-command", node.lineno))
        for child in ast.iter_child_nodes(node):
            walk(child, owner)

    walk(tree)
    return result


def test_container_effect_inventory_covers_every_provision_reference():
    root = Path(api.__file__).resolve().parents[2]
    found = {
        (str(path.relative_to(root)), owner, effect)
        for path in (root / "src").rglob("*.py")
        for owner, effect, _ in source_sites(path) if effect == "provision"
    }
    expected = {(path, owner, effect) for path, owner, effect, _ in CONTAINER_RUNTIME_CALLERS if effect == "provision"}
    assert found == expected


def test_container_effect_inventory_links_all_create_and_start_paths_to_twins():
    root = Path(api.__file__).resolve().parents[2]
    for path, owner, effect, scenario in CONTAINER_RUNTIME_CALLERS:
        assert scenario in {"registration", "containerize", "cold", "image-recreate"}
        assert sum(candidate == owner and call == effect for candidate, call, _ in source_sites(root / path)) == 1
    found = {
        (path, owner, effect)
        for path in {row[0] for row in CONTAINER_RUNTIME_CALLERS}
        for owner, effect, _ in source_sites(root / path)
        if effect in {"ensure_started", "container-start-command", "_create_argv"}
        or (effect == "start" and owner in {"ContainerProvisioner.ensure_started", "ContainerLifecycle._containerize"})
    }
    expected = {(path, owner, effect) for path, owner, effect, _ in CONTAINER_RUNTIME_CALLERS if (path, owner, effect) in found}
    assert found == expected


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
    tree = ast.parse(Path(api.__file__).read_text())
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in {"storage_holds", "storage_hold_overrides"}:
            continue
        if isinstance(node.ctx, ast.Store):
            continue
        parent = parents[node]
        if isinstance(parent, ast.Subscript) and isinstance(parent.ctx, ast.Store):
            continue
        if isinstance(parent, ast.Attribute) and parent.attr in {"pop", "add", "discard", "clear", "update"}:
            call = parents[parent]
            if isinstance(call, ast.Call) and isinstance(parents[call], ast.Expr):
                continue
        owner = parent
        while owner in parents and not isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)):
            owner = parents[owner]
        assert isinstance(owner, (ast.FunctionDef, ast.AsyncFunctionDef)) and owner.name == "_storage_hold_for"
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
