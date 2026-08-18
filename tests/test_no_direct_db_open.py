"""Keep direct daemon SQLite opens behind an explicit storage authority.

This is a syntactic guard, not semantic or data-flow analysis. Assigned aliases such as
``open_db = sqlite3.connect``, ``getattr`` calls, star imports, and
``sqlite3.dbapi2.connect`` are intentionally out of scope.
"""

from __future__ import annotations

import ast
from collections import Counter
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


@dataclass(frozen=True, order=True)
class DirectOpenIdentity:
    relpath: str
    qualname: str


@dataclass(frozen=True)
class AllowlistEntry:
    expected_count: int
    reason: str


@dataclass(frozen=True, order=True)
class DirectOpenSite:
    relpath: str
    lineno: int
    qualname: str
    call: str

    @property
    def identity(self) -> DirectOpenIdentity:
        return DirectOpenIdentity(relpath=self.relpath, qualname=self.qualname)


# Existing non-owner connector sites only. Identity prevents a new function in an
# allowlisted module from inheriting approval; count catches another call in the same function.
DIRECT_OPEN_ALLOWLIST = {
    DirectOpenIdentity(
        "src/pinky_daemon/auth.py",
        "make_db_signing_key_resolver._resolve",
    ): AllowlistEntry(
        expected_count=1,
        reason=(
            "Reads signing keys from the authoritative agent registry DB; "
            "P1 routes behind the seam."
        ),
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/dream_runner.py",
        "DreamRunner._db",
    ): AllowlistEntry(
        expected_count=1,
        reason="Owns dream state; P1 routes behind the seam.",
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/dream_runner.py",
        "DreamRunner._reflection_ids_for_attempt",
    ): AllowlistEntry(
        expected_count=1,
        reason=("Verifies writes in per-agent memory DBs; P1 routes behind the seam."),
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/hooks.py",
        "AuditStore._db",
    ): AllowlistEntry(
        expected_count=1,
        reason="Opens the legacy hook audit store; P1 routes behind the seam.",
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/provisioning.py",
        "SystemProvisionOps.write_keystore",
    ): AllowlistEntry(
        expected_count=1,
        reason=(
            "Creates per-agent signing-key DBs during provisioning; P1 routes behind the seam."
        ),
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/store_catalog.py",
        "StoreCatalog.preflight_integrity",
    ): AllowlistEntry(
        expected_count=1,
        reason=(
            "Daemon-internal storage authority opens each manifest-selected existing store "
            "read-only for boot-time PRAGMA quick_check (#1114)."
        ),
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/store_snapshot.py",
        "StoreSnapshotService._backup_and_verify",
    ): AllowlistEntry(
        expected_count=2,
        reason=(
            "Daemon-internal storage authority opens one catalog-selected source and one "
            "fresh destination for SQLite Online Backup API snapshots (#1112)."
        ),
    ),
    DirectOpenIdentity(
        "src/pinky_daemon/store_restore.py",
        "_verify_static_store",
    ): AllowlistEntry(
        expected_count=1,
        reason=(
            "Attended daemon-down restore opens only static snapshot, temp, and installed "
            "files read-only with immutable=1 for quick_check and schema verification (#1116); "
            "it never opens a live P0.4 preflight store."
        ),
    ),
}


class _DirectOpenVisitor(ast.NodeVisitor):
    def __init__(
        self,
        *,
        relpath: str,
        module_aliases: Set[str],
        imported_call_aliases: Set[str],
    ) -> None:
        self._relpath = relpath
        self._module_aliases = module_aliases
        self._imported_call_aliases = imported_call_aliases
        self._scope: list[str] = []
        self.sites: list[DirectOpenSite] = []

    def _visit_named_scope(
        self,
        node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        self._scope.append(node.name)
        self.generic_visit(node)
        self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_named_scope(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_named_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_named_scope(node)

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in self._module_aliases
            and func.attr in _SQLITE_OPEN_CALLS
        ):
            call_name = f"{func.value.id}.{func.attr}"
        elif isinstance(func, ast.Name) and func.id in self._imported_call_aliases:
            call_name = func.id
        else:
            self.generic_visit(node)
            return

        self.sites.append(
            DirectOpenSite(
                relpath=self._relpath,
                lineno=node.lineno,
                qualname=".".join(self._scope) or "<module>",
                call=call_name,
            )
        )
        self.generic_visit(node)


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

    visitor = _DirectOpenVisitor(
        relpath=relpath,
        module_aliases=module_aliases,
        imported_call_aliases=imported_call_aliases,
    )
    visitor.visit(tree)
    return sorted(visitor.sites)


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
    allowlist: Mapping[DirectOpenIdentity, AllowlistEntry],
) -> None:
    site_list = list(sites)
    unapproved = [
        site
        for site in site_list
        if site.relpath not in owner_modules and site.identity not in allowlist
    ]
    actual_counts = Counter(site.identity for site in site_list)
    dead_entries = sorted(identity for identity in allowlist if not actual_counts[identity])
    count_mismatches = sorted(
        (
            identity,
            entry.expected_count,
            actual_counts[identity],
        )
        for identity, entry in allowlist.items()
        if actual_counts[identity] and actual_counts[identity] != entry.expected_count
    )
    missing_reasons = sorted(
        identity for identity, entry in allowlist.items() if not entry.reason.strip()
    )

    failures: list[str] = []
    if unapproved:
        rendered_sites = "\n".join(
            f"- {site.relpath}:{site.lineno} [{site.qualname}] ({site.call})" for site in unapproved
        )
        failures.append(
            "Unapproved direct SQLite opens:\n"
            f"{rendered_sites}\n"
            "Route each call through a registered storage owner or add its exact "
            "(relpath, qualname) identity to DIRECT_OPEN_ALLOWLIST with a count and reason."
        )
    if dead_entries:
        failures.append(
            "Dead DIRECT_OPEN_ALLOWLIST entries (remove them):\n"
            + "\n".join(f"- {identity.relpath} :: {identity.qualname}" for identity in dead_entries)
        )
    if count_mismatches:
        failures.append(
            "DIRECT_OPEN_ALLOWLIST count mismatches:\n"
            + "\n".join(
                f"- {identity.relpath} :: {identity.qualname}: expected {expected}, found {actual}"
                for identity, expected, actual in count_mismatches
            )
        )
    if missing_reasons:
        failures.append(
            "DIRECT_OPEN_ALLOWLIST entries without reasons:\n"
            + "\n".join(
                f"- {identity.relpath} :: {identity.qualname}" for identity in missing_reasons
            )
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

    assert [(site.lineno, site.qualname, site.call) for site in sites] == [
        (6, "<module>", "sqlite3.connect"),
        (7, "<module>", "sqlite3.Connection"),
        (8, "<module>", "database.connect"),
        (9, "<module>", "database.Connection"),
        (10, "<module>", "Connection"),
        (11, "<module>", "open_database"),
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
            allowlist={},
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
    identity = DirectOpenIdentity(
        "src/pinky_daemon/auth.py",
        "make_db_signing_key_resolver._resolve",
    )

    with pytest.raises(AssertionError, match="newly_added_probe"):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={identity: DIRECT_OPEN_ALLOWLIST[identity]},
        )


def test_extra_direct_open_in_allowlisted_function_is_rejected() -> None:
    source = """\
import sqlite3

def make_db_signing_key_resolver():
    def _resolve():
        sqlite3.connect("sanctioned.db")
        sqlite3.connect("unexpected.db")
    return _resolve
"""
    sites = _find_direct_opens(source, relpath="src/pinky_daemon/auth.py")
    identity = DirectOpenIdentity(
        "src/pinky_daemon/auth.py",
        "make_db_signing_key_resolver._resolve",
    )

    with pytest.raises(AssertionError, match="expected 1, found 2"):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={identity: DIRECT_OPEN_ALLOWLIST[identity]},
        )


def test_dead_allowlist_entry_is_rejected() -> None:
    identity = DirectOpenIdentity(
        "src/pinky_daemon/removed_connector.py",
        "removed_connector",
    )
    with pytest.raises(AssertionError, match="Dead DIRECT_OPEN_ALLOWLIST entries"):
        _assert_direct_opens_allowed(
            [],
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={identity: AllowlistEntry(expected_count=1, reason="Remove me.")},
        )


def test_blank_allowlist_reason_is_rejected() -> None:
    sites = _find_direct_opens(
        'import sqlite3\nsqlite3.connect("legacy.db")\n',
        relpath="src/pinky_daemon/legacy_connector.py",
    )
    identity = DirectOpenIdentity(
        "src/pinky_daemon/legacy_connector.py",
        "<module>",
    )

    with pytest.raises(AssertionError, match="without reasons"):
        _assert_direct_opens_allowed(
            sites,
            owner_modules=APPROVED_STORAGE_OWNER_MODULES,
            allowlist={identity: AllowlistEntry(expected_count=1, reason="   ")},
        )
