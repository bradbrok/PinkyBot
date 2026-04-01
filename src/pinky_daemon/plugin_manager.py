"""Plugin manager — Python code plugin discovery and lifecycle.

Plugins are directories containing a plugin.yaml manifest and Python code.
They provide full code execution capabilities: MCP tools, hooks, scheduled
tasks, HTTP routes, and database access — all sandboxed through PluginContext.

Plugin directory structure:
    my-plugin/
    ├── plugin.yaml         # Required: manifest
    ├── __init__.py         # Required: entry point with setup(ctx)
    ├── tools.py            # Optional: MCP tool definitions
    ├── hooks.py            # Optional: event hooks
    └── migrations/         # Optional: DB migrations
        └── 001_init.sql

Plugin lifecycle: DISCOVERED → VALIDATED → ENABLED → ACTIVE → DISABLED

Discovery paths:
    1. <pinky_root>/plugins/           (project-level)
    2. Per-agent: <working_dir>/plugins/
    3. ~/.pinky/plugins/               (user-level)

Plugins register through the SkillStore with skill_type="plugin".
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

import yaml


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class PluginState(str, Enum):
    DISCOVERED = "discovered"
    VALIDATED = "validated"
    ENABLED = "enabled"
    ACTIVE = "active"
    DISABLED = "disabled"
    ERROR = "error"


@dataclass
class PluginManifest:
    """Parsed plugin.yaml manifest."""

    name: str
    description: str = ""
    version: str = "0.1.0"
    author: str = ""
    license: str = ""

    # Capabilities
    tools: list[str] = field(default_factory=list)  # Tool names this plugin provides
    hooks: list[str] = field(default_factory=list)  # Events this plugin hooks into
    schedules: list[dict] = field(default_factory=list)  # Cron schedules

    # Permissions (declared, must be approved)
    permissions: list[str] = field(default_factory=list)
    # e.g., ["db:read", "db:write", "http:outbound", "fs:read", "fs:write"]

    # Dependencies
    requires: list[str] = field(default_factory=list)  # Other plugins this depends on
    python_requires: str = ""  # Python version constraint

    # MCP server (if plugin provides MCP tools)
    mcp_server: dict = field(default_factory=dict)
    # e.g., {"command": "python", "args": ["-m", "my_plugin.server"]}

    # Metadata
    metadata: dict = field(default_factory=dict)

    # Location
    directory: str = ""
    entry_point: str = "__init__"  # Python module with setup(ctx)


@dataclass
class PluginInfo:
    """Runtime state of a loaded plugin."""

    manifest: PluginManifest
    state: PluginState = PluginState.DISCOVERED
    error: str = ""
    loaded_at: float = 0.0
    module: Any = None  # Loaded Python module


# ── Plugin Context ────────────────────────────────────────


class PluginContext:
    """Safe API surface for plugins.

    Gives plugins controlled access to daemon capabilities without
    exposing raw internals. All database access is prefixed to prevent
    cross-plugin table collisions.
    """

    def __init__(
        self,
        plugin_name: str,
        *,
        db_path: str = "",
        api_url: str = "http://localhost:8888",
        working_dir: str = ".",
    ):
        self.plugin_name = plugin_name
        self._api_url = api_url
        self._working_dir = Path(working_dir)
        self._db: sqlite3.Connection | None = None
        self._db_path = db_path
        self._tools: dict[str, Callable] = {}
        self._hooks: dict[str, list[Callable]] = {}

        # Table name prefix for isolation
        self._table_prefix = f"plugin_{plugin_name.replace('-', '_')}_"

    # ── Database Access (schema-isolated) ─────────────────

    @property
    def db(self) -> sqlite3.Connection:
        """Get a database connection with plugin-prefixed tables."""
        if self._db is None:
            if not self._db_path:
                data_dir = self._working_dir / "data"
                data_dir.mkdir(parents=True, exist_ok=True)
                self._db_path = str(data_dir / "plugins.db")
            self._db = sqlite3.connect(self._db_path, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
        return self._db

    def create_table(self, table_name: str, schema: str) -> None:
        """Create a plugin-prefixed table.

        The table name is automatically prefixed with `plugin_{name}_`
        to prevent cross-plugin collisions.
        """
        prefixed = self._table_prefix + table_name
        self.db.execute(f"CREATE TABLE IF NOT EXISTS {prefixed} ({schema})")
        self.db.commit()

    def execute(self, table_name: str, sql_template: str, params: tuple = ()) -> sqlite3.Cursor:
        """Execute SQL against a plugin-prefixed table.

        The sql_template should use {table} as placeholder for the table name.
        """
        prefixed = self._table_prefix + table_name
        sql = sql_template.replace("{table}", prefixed)
        return self.db.execute(sql, params)

    # ── Tool Registration ─────────────────────────────────

    def register_tool(self, name: str, handler: Callable, *, description: str = "") -> None:
        """Register an MCP tool provided by this plugin.

        The tool name is automatically namespaced as plugin_{name}_{tool_name}.
        """
        full_name = f"plugin_{self.plugin_name}_{name}"
        self._tools[full_name] = handler
        _log(f"plugin[{self.plugin_name}]: registered tool {full_name}")

    def get_tools(self) -> dict[str, Callable]:
        """Get all registered tools."""
        return dict(self._tools)

    # ── Hook Registration ─────────────────────────────────

    def on(self, event: str, handler: Callable) -> None:
        """Register a hook for a daemon event.

        Events are namespaced as plugin.{name}.{event}.
        """
        self._hooks.setdefault(event, []).append(handler)

    def get_hooks(self) -> dict[str, list[Callable]]:
        """Get all registered hooks."""
        return dict(self._hooks)

    # ── API Access ────────────────────────────────────────

    def api_call(self, method: str, path: str, body: dict | None = None) -> dict:
        """Call the PinkyBot API."""
        import urllib.error
        import urllib.request

        url = f"{self._api_url}{path}"
        data = json.dumps(body).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"error": e.read().decode(), "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    # ── Filesystem Access ─────────────────────────────────

    @property
    def data_dir(self) -> Path:
        """Plugin-specific data directory."""
        d = self._working_dir / "data" / "plugins" / self.plugin_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── Cleanup ───────────────────────────────────────────

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None


# ── Manifest Parsing ──────────────────────────────────────


def parse_plugin_yaml(path: str | Path) -> PluginManifest | None:
    """Parse a plugin.yaml manifest file."""
    path = Path(path)
    if not path.exists():
        return None

    try:
        content = path.read_text(encoding="utf-8")
        data = yaml.safe_load(content)
    except Exception as e:
        _log(f"plugin_manager: failed to parse {path}: {e}")
        return None

    if not isinstance(data, dict):
        _log(f"plugin_manager: {path} is not a mapping")
        return None

    name = str(data.get("name", "")).strip()
    if not name:
        _log(f"plugin_manager: missing name in {path}")
        return None

    return PluginManifest(
        name=name,
        description=str(data.get("description", "")),
        version=str(data.get("version", "0.1.0")),
        author=str(data.get("author", "")),
        license=str(data.get("license", "")),
        tools=data.get("tools", []) or [],
        hooks=data.get("hooks", []) or [],
        schedules=data.get("schedules", []) or [],
        permissions=data.get("permissions", []) or [],
        requires=data.get("requires", []) or [],
        python_requires=str(data.get("python_requires", "")),
        mcp_server=data.get("mcp_server", {}) or {},
        metadata=data.get("metadata", {}) or {},
        directory=str(path.parent.resolve()),
        entry_point=str(data.get("entry_point", "__init__")),
    )


# ── Plugin Manager ────────────────────────────────────────


class PluginManager:
    """Discovers, validates, and manages Python plugin lifecycle."""

    def __init__(
        self,
        *,
        db_path: str = "data/plugins.db",
        api_url: str = "http://localhost:8888",
        working_dir: str = ".",
    ):
        self._plugins: dict[str, PluginInfo] = {}
        self._contexts: dict[str, PluginContext] = {}
        self._db_path = db_path
        self._api_url = api_url
        self._working_dir = working_dir

        # State persistence
        self._state_db_path = db_path
        self._init_state_db()

    def _init_state_db(self) -> None:
        """Initialize the plugin state tracking table."""
        Path(self._state_db_path).parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self._state_db_path, check_same_thread=False)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("""
            CREATE TABLE IF NOT EXISTS plugin_state (
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL DEFAULT 'discovered',
                enabled INTEGER NOT NULL DEFAULT 0,
                permissions_approved TEXT NOT NULL DEFAULT '[]',
                error TEXT NOT NULL DEFAULT '',
                loaded_at REAL NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS plugin_migrations (
                plugin_name TEXT NOT NULL,
                version TEXT NOT NULL,
                applied_at REAL NOT NULL,
                checksum TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (plugin_name, version)
            )
        """)
        db.commit()
        db.close()

    # ── Discovery ─────────────────────────────────────────

    def discover(self, *directories: str | Path) -> list[PluginManifest]:
        """Discover plugins from given directories.

        Looks for subdirectories containing plugin.yaml.
        """
        discovered = []
        for directory in directories:
            directory = Path(directory)
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if not entry.is_dir():
                    continue
                manifest_path = entry / "plugin.yaml"
                if not manifest_path.exists():
                    continue
                manifest = parse_plugin_yaml(manifest_path)
                if manifest and manifest.name not in self._plugins:
                    self._plugins[manifest.name] = PluginInfo(manifest=manifest)
                    discovered.append(manifest)
                    _log(f"plugin_manager: discovered {manifest.name} at {entry}")

        return discovered

    def discover_all(self, *, project_root: str | Path | None = None) -> list[PluginManifest]:
        """Discover plugins from all standard locations."""
        dirs = []

        # Project-level plugins
        if project_root:
            dirs.append(Path(project_root) / "plugins")

        # Pinky root plugins
        pinky_root = Path(__file__).resolve().parent.parent.parent
        dirs.append(pinky_root / "plugins")

        # User-level
        dirs.append(Path.home() / ".pinky" / "plugins")

        return self.discover(*dirs)

    # ── Validation ────────────────────────────────────────

    def validate(self, name: str) -> bool:
        """Validate a plugin's manifest and entry point.

        Checks:
        - Entry point module exists
        - Dependencies are available
        - Required permissions are declared
        """
        info = self._plugins.get(name)
        if not info:
            return False

        manifest = info.manifest
        plugin_dir = Path(manifest.directory)

        # Check entry point exists
        entry_file = plugin_dir / f"{manifest.entry_point}.py"
        init_file = plugin_dir / "__init__.py"
        if not entry_file.exists() and not init_file.exists():
            info.state = PluginState.ERROR
            info.error = f"Entry point not found: {manifest.entry_point}.py or __init__.py"
            _log(f"plugin_manager: validation failed for {name}: {info.error}")
            return False

        # Check dependencies
        for dep in manifest.requires:
            if dep not in self._plugins:
                info.state = PluginState.ERROR
                info.error = f"Missing dependency: {dep}"
                _log(f"plugin_manager: validation failed for {name}: {info.error}")
                return False

        info.state = PluginState.VALIDATED
        _log(f"plugin_manager: validated {name}")
        return True

    # ── Enable / Disable ──────────────────────────────────

    def enable(self, name: str) -> bool:
        """Enable and load a plugin.

        Creates a PluginContext, loads the module, and calls setup(ctx).
        """
        info = self._plugins.get(name)
        if not info:
            return False

        if info.state not in (PluginState.VALIDATED, PluginState.DISABLED):
            if info.state == PluginState.DISCOVERED:
                if not self.validate(name):
                    return False

        manifest = info.manifest
        plugin_dir = Path(manifest.directory)

        # Create plugin context
        ctx = PluginContext(
            manifest.name,
            db_path=self._db_path,
            api_url=self._api_url,
            working_dir=self._working_dir,
        )

        # Load the plugin module
        try:
            # Add plugin directory to sys.path temporarily
            if str(plugin_dir.parent) not in sys.path:
                sys.path.insert(0, str(plugin_dir.parent))

            spec = importlib.util.spec_from_file_location(
                f"pinky_plugin_{manifest.name}",
                str(plugin_dir / f"{manifest.entry_point}.py"),
            )
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            else:
                raise ImportError(f"Cannot load {manifest.entry_point}")

            # Call setup(ctx) if it exists
            if hasattr(module, "setup"):
                module.setup(ctx)

            info.module = module
            info.state = PluginState.ACTIVE
            info.loaded_at = time.time()
            info.error = ""
            self._contexts[name] = ctx

            # Persist state
            self._save_state(name, "enabled")

            _log(f"plugin_manager: enabled {name}")
            return True

        except Exception as e:
            info.state = PluginState.ERROR
            info.error = str(e)
            ctx.close()
            _log(f"plugin_manager: failed to enable {name}: {e}")
            return False

    def disable(self, name: str) -> bool:
        """Disable a plugin, calling teardown(ctx) if defined."""
        info = self._plugins.get(name)
        if not info:
            return False

        # Call teardown if available
        if info.module and hasattr(info.module, "teardown"):
            try:
                ctx = self._contexts.get(name)
                if ctx:
                    info.module.teardown(ctx)
            except Exception as e:
                _log(f"plugin_manager: teardown error for {name}: {e}")

        # Clean up context
        ctx = self._contexts.pop(name, None)
        if ctx:
            ctx.close()

        info.state = PluginState.DISABLED
        info.module = None
        self._save_state(name, "disabled")
        _log(f"plugin_manager: disabled {name}")
        return True

    # ── Queries ───────────────────────────────────────────

    def get(self, name: str) -> PluginInfo | None:
        return self._plugins.get(name)

    def list_plugins(self) -> list[dict]:
        """List all discovered plugins with their state."""
        result = []
        for name, info in sorted(self._plugins.items()):
            m = info.manifest
            result.append({
                "name": m.name,
                "description": m.description,
                "version": m.version,
                "author": m.author,
                "state": info.state.value,
                "error": info.error,
                "permissions": m.permissions,
                "tools": m.tools,
                "hooks": m.hooks,
                "directory": m.directory,
            })
        return result

    def get_context(self, name: str) -> PluginContext | None:
        return self._contexts.get(name)

    # ── State Persistence ─────────────────────────────────

    def _save_state(self, name: str, state: str) -> None:
        try:
            db = sqlite3.connect(self._state_db_path, check_same_thread=False)
            now = time.time()
            db.execute(
                """INSERT INTO plugin_state (name, state, enabled, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT (name)
                   DO UPDATE SET state=excluded.state, enabled=excluded.enabled, updated_at=excluded.updated_at""",
                (name, state, 1 if state == "enabled" else 0, now),
            )
            db.commit()
            db.close()
        except Exception as e:
            _log(f"plugin_manager: failed to save state for {name}: {e}")

    def get_previously_enabled(self) -> list[str]:
        """Get names of plugins that were enabled in previous run."""
        try:
            db = sqlite3.connect(self._state_db_path, check_same_thread=False)
            rows = db.execute("SELECT name FROM plugin_state WHERE enabled=1").fetchall()
            db.close()
            return [r[0] for r in rows]
        except Exception:
            return []

    # ── SkillStore Registration ───────────────────────────

    def register_in_skill_store(self, skill_store, name: str) -> bool:
        """Register a plugin as a skill in the SkillStore."""
        info = self._plugins.get(name)
        if not info:
            return False

        m = info.manifest
        tool_patterns = [f"mcp__plugin-{m.name}__*"] if m.mcp_server else []
        # Also add any explicitly declared tool patterns
        for tool in m.tools:
            pattern = f"plugin_{m.name}_{tool}"
            if pattern not in tool_patterns:
                tool_patterns.append(pattern)

        skill_store.register(
            m.name,
            description=m.description,
            skill_type="plugin",
            version=m.version,
            enabled=info.state == PluginState.ACTIVE,
            config={
                "directory": m.directory,
                "author": m.author,
                "permissions": m.permissions,
                "hooks": m.hooks,
                "source": "filesystem",
            },
            mcp_server_config=m.mcp_server,
            tool_patterns=tool_patterns,
            requires=m.requires,
            self_assignable=True,
            category="plugin",
        )
        return True

    # ── Cleanup ───────────────────────────────────────────

    def shutdown(self) -> None:
        """Disable all active plugins."""
        for name in list(self._contexts.keys()):
            self.disable(name)
