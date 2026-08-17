"""Keep direct daemon SQLite opens behind an explicit storage authority.

This is a syntactic guard, not semantic or data-flow analysis. Assigned aliases such as
``open_db = sqlite3.connect``, ``getattr`` calls, star imports, and
``sqlite3.dbapi2.connect`` are intentionally out of scope.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Set
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DAEMON_ROOT = _REPO_ROOT / "src" / "pinky_daemon"
_SQLITE_OPEN_CALLS = frozenset({"connect", "Connection"})

# These modules own stores registered with StoreCatalog. A new owner must register its
# database there and update this catalog-bound set in the same change.
APPROVED_STORAGE_OWNER_MODULES = frozenset(
    {
        "src/pinky_daemon/activity_store.py",
        "src/pinky_daemon/agent_comms.py",
        "src/pinky_daemon/agent_registry.py",
        "src/pinky_daemon/analytics_store.py",
        "src/pinky_daemon/app_store.py",
        "src/pinky_daemon/conversation_store.py",
        "src/pinky_daemon/kb_store.py",
        "src/pinky_daemon/librarian_runner.py",
        "src/pinky_daemon/mesh_store.py",
        "src/pinky_daemon/message_context_store.py",
        "src/pinky_daemon/outreach_config.py",
        "src/pinky_daemon/plugin_manager.py",
        "src/pinky_daemon/presentation_store.py",
        "src/pinky_daemon/research_store.py",
        "src/pinky_daemon/session_store.py",
        "src/pinky_daemon/skill_store.py",
        "src/pinky_daemon/task_store.py",
        "src/pinky_daemon/trigger_store.py",
        "src/pinky_daemon/user_profile_store.py",
        "src/pinky_daemon/voice_store.py",
    }
)

# Existing non-owner connectors only. Each exception needs a reason and a removal plan.
DIRECT_OPEN_ALLOWLIST = {
    "src/pinky_daemon/auth.py": (
        "Reads signing keys from the authoritative agent registry DB; P1 routes behind the seam."
    ),
    "src/pinky_daemon/dream_runner.py": (
        "Owns dream state and verifies writes in per-agent memory DBs; P1 routes behind the seam."
    ),
    "src/pinky_daemon/hooks.py": ("Opens the legacy hook audit store; P1 routes behind the seam."),
    "src/pinky_daemon/provisioning.py": (
        "Creates per-agent signing-key DBs during provisioning; P1 routes behind the seam."
    ),
}


@dataclass(frozen=True, order=True)
class DirectOpenSite:
    relpath: str
    lineno: int
    call: str


def _find_direct_opens(source: str, *, relpath: str) -> list[DirectOpenSite]:
    tree = ast.parse(source, filename=relpath)
    module_aliases: set[str] = set()
    imported_call_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlite3":
                    module_aliases.add(alias.asname or alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module == "sqlite3":
            for alias in node.names:
                if alias.name in _SQLITE_OPEN_CALLS:
                    imported_call_aliases.add(alias.asname or alias.name)

    sites: list[DirectOpenSite] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in module_aliases
            and func.attr in _SQLITE_OPEN_CALLS
        ):
            call_name = f"{func.value.id}.{func.attr}"
        elif isinstance(func, ast.Name) and func.id in imported_call_aliases:
            call_name = func.id
        else:
            continue

        sites.append(DirectOpenSite(relpath=relpath, lineno=node.lineno, call=call_name))

    return sorted(sites)


def _scan_daemon_sources() -> list[DirectOpenSite]:
    sites: list[DirectOpenSite] = []
    for path in sorted(_DAEMON_ROOT.rglob("*.py")):
        relpath = path.relative_to(_REPO_ROOT).as_posix()
        sites.extend(_find_direct_opens(path.read_text(encoding="utf-8"), relpath=relpath))
    return sorted(sites)


def _assert_direct_opens_allowed(
    sites: Iterable[DirectOpenSite],
    *,
    owner_modules: Set[str],
    allowlist: Mapping[str, str],
) -> None:
    site_list = list(sites)
    unapproved = [
        site
        for site in site_list
        if site.relpath not in owner_modules and site.relpath not in allowlist
    ]
    actual_paths = {site.relpath for site in site_list}
    dead_entries = sorted(set(allowlist) - actual_paths)
    missing_reasons = sorted(path for path, reason in allowlist.items() if not reason.strip())

    failures: list[str] = []
    if unapproved:
        rendered_sites = "\n".join(
            f"- {site.relpath}:{site.lineno} ({site.call})" for site in unapproved
        )
        failures.append(
            "Unapproved direct SQLite opens:\n"
            f"{rendered_sites}\n"
            "Route each call through a registered storage owner or add its module to "
            "DIRECT_OPEN_ALLOWLIST with a reason."
        )
    if dead_entries:
        failures.append(
            "Dead DIRECT_OPEN_ALLOWLIST entries (remove them):\n"
            + "\n".join(f"- {path}" for path in dead_entries)
        )
    if missing_reasons:
        failures.append(
            "DIRECT_OPEN_ALLOWLIST entries without reasons:\n"
            + "\n".join(f"- {path}" for path in missing_reasons)
        )

    assert not failures, "\n\n".join(failures)


def test_daemon_direct_sqlite_opens_are_owned_or_allowlisted() -> None:
    _assert_direct_opens_allowed(
        _scan_daemon_sources(),
        owner_modules=APPROVED_STORAGE_OWNER_MODULES,
        allowlist=DIRECT_OPEN_ALLOWLIST,
    )


def test_scanner_resolves_sqlite_import_aliases() -> None:
    source = """\
import sqlite3
import sqlite3 as database
from sqlite3 import Connection
from sqlite3 import connect as open_database

sqlite3.connect("qualified.db")
sqlite3.Connection("qualified-constructor.db")
database.connect("module-alias.db")
database.Connection("module-alias-constructor.db")
Connection("imported-constructor.db")
open_database("imported-alias.db")
"""

    sites = _find_direct_opens(source, relpath="src/pinky_daemon/import_forms.py")

    assert [(site.lineno, site.call) for site in sites] == [
        (6, "sqlite3.connect"),
        (7, "sqlite3.Connection"),
        (8, "database.connect"),
        (9, "database.Connection"),
        (10, "Connection"),
        (11, "open_database"),
    ]


def test_planted_unapproved_direct_open_is_rejected() -> None:
    sites = _find_direct_opens(
        'import sqlite3\nsqlite3.connect("planted.db")\n',
        relpath="src/pinky_daemon/unowned_connector.py",
    )

    with pytest.raises(
        AssertionError,
        match=r"Unapproved direct SQLite opens:.*unowned_connector\.py:2",
    ):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist=DIRECT_OPEN_ALLOWLIST,
        )


def test_new_direct_open_in_allowlisted_module_is_rejected() -> None:
    source = """\
import sqlite3

def make_db_signing_key_resolver():
    def _resolve():
        sqlite3.connect("sanctioned.db")
    return _resolve

def newly_added_probe():
    sqlite3.connect("unexpected.db")
"""
    sites = _find_direct_opens(source, relpath="src/pinky_daemon/auth.py")

    with pytest.raises(AssertionError, match="newly_added_probe"):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={
                "src/pinky_daemon/auth.py": DIRECT_OPEN_ALLOWLIST["src/pinky_daemon/auth.py"]
            },
        )


def test_dead_allowlist_entry_is_rejected() -> None:
    with pytest.raises(AssertionError, match="Dead DIRECT_OPEN_ALLOWLIST entries"):
        _assert_direct_opens_allowed(
            [],
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={"src/pinky_daemon/removed_connector.py": "Remove me."},
        )


def test_blank_allowlist_reason_is_rejected() -> None:
    sites = _find_direct_opens(
        'import sqlite3\nsqlite3.connect("legacy.db")\n',
        relpath="src/pinky_daemon/legacy_connector.py",
    )

    with pytest.raises(AssertionError, match="without reasons"):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={"src/pinky_daemon/legacy_connector.py": "   "},
        )
