"""P1 repository seams that close the storage-authority connector exceptions."""

from __future__ import annotations

import ast
import errno
import importlib
import importlib.util
import inspect
import os
import sqlite3
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.store_catalog import (
    StoreCatalog,
    StoreCatalogError,
    StoreIntegrityTarget,
)


def _load_required_module(name: str, missing_message: str) -> Any:
    spec = importlib.util.find_spec(name)
    assert spec is not None, missing_message
    return importlib.import_module(name)


def _signing_store_module() -> Any:
    return _load_required_module(
        "pinky_daemon.agent_signing_key_store",
        "AgentSigningKeyStore owner is not implemented",
    )


def _manifest_module() -> Any:
    return _load_required_module(
        "pinky_daemon.store_manifest",
        "explicit fleet/standalone manifest providers are not implemented",
    )


def _seed_signing_key_db(
    path: Path,
    *,
    signing_key: str = "tenant-secret",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE agent_signing_keys ("
            "agent_name TEXT PRIMARY KEY, signing_key TEXT NOT NULL, created_at REAL)"
        )
        connection.execute(
            "INSERT INTO agent_signing_keys VALUES (?, ?, ?)",
            ("tenant", signing_key, 1.0),
        )
        connection.commit()
    finally:
        connection.close()


def _seed_persistent_wal_signing_key_db(
    path: Path,
    *,
    signing_key: str = "tenant-secret",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        connection.execute(
            "CREATE TABLE agent_signing_keys ("
            "agent_name TEXT PRIMARY KEY, signing_key TEXT NOT NULL, created_at REAL)"
        )
        connection.execute(
            "INSERT INTO agent_signing_keys VALUES (?, ?, ?)",
            ("tenant", signing_key, 1.0),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(os.fspath(path) + suffix)
        if sidecar.exists():
            sidecar.unlink()
    assert path.read_bytes()[18:20] == b"\x02\x02"


def _seed_unix_user_registry(base: Path, work_dir: Path) -> None:
    from pinky_daemon import api as api_module

    agents_path = Path(api_module._derive_api_store_manifest(base)["agents"].path)
    registry = AgentRegistry(db_path=os.fspath(agents_path))
    try:
        registry.register(
            "tenant",
            working_dir=os.fspath(work_dir),
            isolated=True,
            isolation_mode="unix_user",
        )
    finally:
        registry.close()


def test_signing_key_reader_is_typed_read_only_validated_and_fail_soft(
    tmp_path: Path,
) -> None:
    module = _signing_store_module()
    store_type = getattr(module, "AgentSigningKeyStore", None)
    reader_protocol = getattr(module, "SigningKeyReader", None)
    assert store_type is not None
    assert reader_protocol is not None

    missing = tmp_path / "missing.db"
    missing_reader = store_type.read_only(os.fspath(missing))
    assert isinstance(missing_reader, reader_protocol)
    assert missing_reader.get_signing_key("tenant") is None
    assert not missing.exists(), "a read-only lookup must not create an absent DB"

    path = tmp_path / "agents.db"
    _seed_signing_key_db(path)
    reader = store_type.read_only(os.fspath(path))
    assert reader.get_signing_key("tenant") == "tenant-secret"
    assert reader.get_signing_key("missing") is None
    assert reader.get_signing_key("BAD NAME!") is None

    path.write_bytes(b"not sqlite")
    assert reader.get_signing_key("tenant") is None


def test_auth_resolver_delegates_to_one_typed_reader(monkeypatch: pytest.MonkeyPatch) -> None:
    from pinky_daemon import auth

    observations: list[tuple[str, str]] = []

    class FakeReader:
        @classmethod
        def read_only(cls, db_path: str) -> "FakeReader":
            observations.append(("construct", db_path))
            return cls()

        def get_signing_key(self, agent_name: str) -> str | None:
            observations.append(("read", agent_name))
            return "typed-secret" if agent_name == "tenant" else None

    monkeypatch.setattr(auth, "AgentSigningKeyStore", FakeReader, raising=False)

    resolver = auth.make_db_signing_key_resolver("/authority/agents.db")

    assert resolver("tenant") == "typed-secret"
    assert resolver("other") is None
    assert observations == [
        ("construct", "/authority/agents.db"),
        ("read", "tenant"),
        ("read", "other"),
    ]


def test_agent_registry_signing_methods_delegate_to_typed_owner(tmp_path: Path) -> None:
    registry = AgentRegistry(db_path=os.fspath(tmp_path / "agents.db"))

    class FakeSigningKeys:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def get_signing_key(self, name: str) -> str | None:
            self.calls.append(("get", name))
            return "existing-secret"

        def get_or_create_signing_key(self, name: str) -> str:
            self.calls.append(("get_or_create", name))
            return "created-secret"

    fake = FakeSigningKeys()
    registry._signing_keys = fake
    try:
        assert registry.get_signing_key("tenant") == "existing-secret"
        assert registry.get_or_create_signing_key("tenant") == "created-secret"
        assert fake.calls == [("get", "tenant"), ("get_or_create", "tenant")]
    finally:
        registry.close()


def test_provisioned_signing_store_is_reserved_before_open_and_committed_atomically(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    store_type = module.AgentSigningKeyStore
    target = tmp_path / "agent_keys.db"
    staging_root = tmp_path / "daemon-staging"
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    real_link = module.os.link
    boundary_records: list[Any] = []

    def observing_link(source: Any, destination: Any, **kwargs: Any) -> None:
        if os.fspath(destination) == target.name:
            boundary_records.extend(catalog.snapshot())
            assert not target.exists()
        real_link(source, destination, **kwargs)

    monkeypatch.setattr(module.os, "link", observing_link)

    store_type.provision_single_agent(
        os.fspath(target),
        "tenant",
        "tenant-secret",
        catalog=catalog,
        staging_root=staging_root,
    )

    assert [record.logical_name for record in boundary_records] == ["agent_signing_keys"]
    assert boundary_records[0].dev_ino is None
    [record] = catalog.snapshot()
    assert record.logical_name == "agent_signing_keys"
    assert record.owner == "AgentSigningKeyStore"
    assert record.journal_mode == "delete"
    assert record.dev_ino == (target.stat().st_dev, target.stat().st_ino)
    assert (target.stat().st_mode & 0o777) == 0o600
    with sqlite3.connect(target) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
        assert connection.execute(
            "SELECT agent_name, signing_key FROM agent_signing_keys"
        ).fetchall() == [("tenant", "tenant-secret")]
    assert not Path(os.fspath(target) + "-wal").exists()
    assert not Path(os.fspath(target) + "-shm").exists()


def test_provisioned_signing_store_failure_cleans_staging_and_reservation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    staging_root = tmp_path / "daemon-staging"
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    opened: list[str] = []

    def fail_after_create(database: Any, *_args: Any, **_kwargs: Any) -> Any:
        opened.append(os.fspath(database))
        raise sqlite3.OperationalError("injected create failure")

    monkeypatch.setattr(module.sqlite3, "connect", fail_after_create)

    with pytest.raises(sqlite3.OperationalError, match="injected create failure"):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=staging_root,
        )

    assert opened and all(os.fspath(target) not in database for database in opened)
    assert catalog.snapshot() == []
    assert not target.exists()
    assert list(staging_root.iterdir()) == []


def test_provisioned_signing_store_never_follows_a_dangling_symlink(
    tmp_path: Path,
) -> None:
    module = _signing_store_module()
    victim = tmp_path / "victim.db"
    target = tmp_path / "agent_keys.db"
    target.symlink_to(victim)
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})

    with pytest.raises(OSError):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=tmp_path / "daemon-staging",
        )

    assert target.is_symlink()
    assert not victim.exists()
    assert catalog.snapshot() == []


def test_provision_never_opens_sqlite_through_tenant_target_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    victim = tmp_path / "victim.db"
    real_connect = module.sqlite3.connect
    with real_connect(victim) as connection:
        connection.execute("CREATE TABLE victim_sentinel (value TEXT)")
        connection.execute("INSERT INTO victim_sentinel VALUES ('keep')")
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    attack_reached_target = False

    def swap_to_victim(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal attack_reached_target
        if os.fspath(database) != os.fspath(target):
            return real_connect(database, *args, **kwargs)
        attack_reached_target = True
        target.symlink_to(victim)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(module.sqlite3, "connect", swap_to_victim)

    module.AgentSigningKeyStore.provision_single_agent(
        os.fspath(target),
        "tenant",
        "must-not-escape",
        catalog=catalog,
        staging_root=tmp_path / "daemon-staging",
    )

    assert not attack_reached_target
    with real_connect(victim) as connection:
        assert connection.execute("SELECT value FROM victim_sentinel").fetchall() == [("keep",)]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='agent_signing_keys'"
            ).fetchall()
            == []
        )
    with real_connect(target) as connection:
        assert connection.execute(
            "SELECT signing_key FROM agent_signing_keys WHERE agent_name='tenant'"
        ).fetchone() == ("must-not-escape",)


def test_provision_sqlite_open_cannot_be_redirected_by_swap_open_restore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    exclusive_create = tmp_path / "exclusive-create-moved-aside.db"
    victim = tmp_path / "victim.db"
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    real_connect = module.sqlite3.connect
    with real_connect(victim) as connection:
        connection.execute("CREATE TABLE victim_sentinel (value TEXT)")
        connection.execute("INSERT INTO victim_sentinel VALUES ('keep')")

    def swap_open_restore(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if os.fspath(database) != os.fspath(target):
            return real_connect(database, *args, **kwargs)
        target.rename(exclusive_create)
        target.symlink_to(victim)
        connection = real_connect(database, *args, **kwargs)
        target.unlink()
        exclusive_create.rename(target)
        return connection

    monkeypatch.setattr(module.sqlite3, "connect", swap_open_restore)

    module.AgentSigningKeyStore.provision_single_agent(
        os.fspath(target),
        "tenant",
        "must-not-escape",
        catalog=catalog,
        staging_root=tmp_path / "daemon-staging",
    )

    with real_connect(victim) as connection:
        assert connection.execute("SELECT value FROM victim_sentinel").fetchall() == [("keep",)]
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE name='agent_signing_keys'"
            ).fetchall()
            == []
        )
    with real_connect(target) as connection:
        assert connection.execute(
            "SELECT signing_key FROM agent_signing_keys WHERE agent_name='tenant'"
        ).fetchone() == ("must-not-escape",)


def test_provision_preserves_every_preexisting_sidecar_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    sidecars = {
        Path(os.fspath(target) + suffix): f"sentinel:{suffix}".encode()
        for suffix in ("-wal", "-shm", "-journal")
    }
    for path, content in sidecars.items():
        path.write_bytes(content)
    identities = {path: (path.stat().st_dev, path.stat().st_ino) for path in sidecars}
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    connect_calls = 0

    def forbidden_connect(*_args: Any, **_kwargs: Any) -> Any:
        nonlocal connect_calls
        connect_calls += 1
        raise AssertionError("pre-existing sidecars must fail before SQLite open")

    monkeypatch.setattr(module.sqlite3, "connect", forbidden_connect)

    with pytest.raises(FileExistsError, match="sidecar"):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=tmp_path / "daemon-staging",
        )

    assert connect_calls == 0
    assert not target.exists()
    for path, content in sidecars.items():
        assert path.read_bytes() == content
        assert (path.stat().st_dev, path.stat().st_ino) == identities[path]
    assert catalog.snapshot() == []


def test_provision_cleanup_preserves_inode_swapped_in_after_create(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    created_moved_aside = tmp_path / "created-moved-aside.db"
    replacement_content = b"replacement inode must survive"
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    real_fsync = module.os.fsync
    replacement_written = False

    def swap_during_file_fsync(descriptor: int) -> None:
        nonlocal replacement_written
        descriptor_stat = os.fstat(descriptor)
        if stat.S_ISREG(descriptor_stat.st_mode) and not replacement_written:
            if target.exists():
                target.rename(created_moved_aside)
            target.write_bytes(replacement_content)
            replacement_written = True
        real_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", swap_during_file_fsync)

    with pytest.raises(OSError):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=tmp_path / "daemon-staging",
        )

    assert replacement_written
    assert target.read_bytes() == replacement_content
    assert catalog.snapshot() == []


def test_provision_cleanup_never_trusts_linux_reused_dev_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    target = tmp_path / "agent_keys.db"
    created_moved_aside = tmp_path / "created-moved-aside.db"
    replacement_content = b"replacement with simulated reused Linux inode"
    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    real_fsync = module.os.fsync
    real_lstat = module.os.lstat
    created_identity: tuple[int, int] | None = None
    replacement_written = False

    def swap_during_file_fsync(descriptor: int) -> None:
        nonlocal created_identity, replacement_written
        descriptor_stat = os.fstat(descriptor)
        if stat.S_ISREG(descriptor_stat.st_mode) and not replacement_written:
            created_identity = (descriptor_stat.st_dev, descriptor_stat.st_ino)
            if target.exists():
                target.rename(created_moved_aside)
            target.write_bytes(replacement_content)
            replacement_written = True
        real_fsync(descriptor)

    def report_linux_reused_inode(path: Any) -> Any:
        observed = real_lstat(path)
        if (
            replacement_written
            and created_identity is not None
            and os.fspath(path) == os.fspath(target)
        ):
            return SimpleNamespace(
                st_dev=created_identity[0],
                st_ino=created_identity[1],
                st_mode=observed.st_mode,
            )
        return observed

    monkeypatch.setattr(module.os, "fsync", swap_during_file_fsync)
    monkeypatch.setattr(module.os, "lstat", report_linux_reused_inode)

    with pytest.raises(OSError):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=tmp_path / "daemon-staging",
        )

    assert replacement_written
    assert target.read_bytes() == replacement_content
    assert catalog.snapshot() == []


def test_provision_requires_a_private_parent_directory(tmp_path: Path) -> None:
    module = _signing_store_module()
    parent = tmp_path / "tenant-data"
    parent.mkdir(mode=0o755)
    parent.chmod(0o755)
    target = parent / "agent_keys.db"
    catalog = StoreCatalog(expected_root=parent, silence_allowlist={})

    with pytest.raises(PermissionError, match="0700"):
        module.AgentSigningKeyStore.provision_single_agent(
            os.fspath(target),
            "tenant",
            "tenant-secret",
            catalog=catalog,
            staging_root=tmp_path / "daemon-staging",
        )

    assert not target.exists()
    assert catalog.snapshot() == []


def test_system_provision_ops_delegates_with_a_scoped_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon import provisioning

    target = tmp_path / "tenant" / "data" / "agent_keys.db"
    target.parent.mkdir(parents=True)
    observations: list[tuple[str, str, str, StoreCatalog, Path]] = []

    class FakeSigningKeyStore:
        @classmethod
        def provision_single_agent(
            cls,
            path: str,
            agent_name: str,
            signing_key: str,
            *,
            catalog: StoreCatalog,
            staging_root: str | os.PathLike[str],
        ) -> None:
            observations.append((path, agent_name, signing_key, catalog, Path(staging_root)))

    monkeypatch.setattr(
        provisioning,
        "AgentSigningKeyStore",
        FakeSigningKeyStore,
        raising=False,
    )

    staging_root = tmp_path / "daemon-staging"
    provisioning.SystemProvisionOps(staging_root=staging_root).write_keystore(
        os.fspath(target),
        "tenant",
        "tenant-secret",
    )

    assert len(observations) == 1
    path, agent_name, signing_key, catalog, observed_staging_root = observations[0]
    assert (path, agent_name, signing_key) == (
        os.fspath(target),
        "tenant",
        "tenant-secret",
    )
    assert catalog.expected_root == os.path.realpath(target.parent)
    assert observed_staging_root == staging_root


def test_reflection_store_read_only_correlation_accessor_preserves_absence_and_schema(
    tmp_path: Path,
) -> None:
    from pinky_memory.store import ReflectionStore

    accessor = getattr(ReflectionStore, "reflection_ids_for_source_session", None)
    assert accessor is not None, "typed correlated-reflection accessor is not implemented"

    absent = tmp_path / "absent-memory.db"
    assert accessor(os.fspath(absent), "receipt") == set()
    assert not absent.exists()

    legacy = tmp_path / "legacy-memory.db"
    with sqlite3.connect(legacy) as connection:
        connection.execute("CREATE TABLE reflections (id TEXT PRIMARY KEY)")
    assert accessor(os.fspath(legacy), "receipt") == set()
    with sqlite3.connect(legacy) as connection:
        assert [row[1] for row in connection.execute("PRAGMA table_info(reflections)")] == ["id"]

    memory = tmp_path / "memory.db"
    with sqlite3.connect(memory) as connection:
        connection.execute(
            "CREATE TABLE reflections ("
            "id TEXT PRIMARY KEY, source_session_id TEXT, active INTEGER, embedding TEXT)"
        )
        connection.executemany(
            "INSERT INTO reflections VALUES (?, ?, ?, ?)",
            [
                ("embedded", "receipt", 1, "[1.0]"),
                ("empty", "receipt", 1, "[]"),
                ("inactive", "receipt", 0, "[1.0]"),
                ("other", "other-receipt", 1, "[1.0]"),
            ],
        )
    assert accessor(os.fspath(memory), "receipt") == {
        "embedded",
        "empty",
        "inactive",
    }
    assert accessor(os.fspath(memory), "receipt", embedded_only=True) == {"embedded"}


def test_dream_runner_delegates_correlation_lookup_to_reflection_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon.dream_runner import DreamRunner
    from pinky_memory.store import ReflectionStore

    accessor = getattr(ReflectionStore, "reflection_ids_for_source_session", None)
    assert accessor is not None, "typed correlated-reflection accessor is not implemented"
    observations: list[tuple[str, str, bool]] = []

    def fake_accessor(
        db_path: str,
        source_session_id: str,
        *,
        embedded_only: bool = False,
    ) -> set[str]:
        observations.append((db_path, source_session_id, embedded_only))
        return {"typed-reflection"}

    monkeypatch.setattr(
        ReflectionStore,
        "reflection_ids_for_source_session",
        staticmethod(fake_accessor),
    )
    runner = object.__new__(DreamRunner)
    attempt = SimpleNamespace(correlation_id="receipt-id")
    agent_config = SimpleNamespace(working_dir=os.fspath(tmp_path))

    assert runner._reflection_ids_for_attempt(
        attempt,
        agent_config,
        embedded_only=True,
    ) == {"typed-reflection"}
    assert observations == [(os.fspath(tmp_path / "data" / "memory.db"), "receipt-id", True)]


def test_hooks_live_audit_path_traverses_dedicated_audit_store(tmp_path: Path) -> None:
    audit_module = _load_required_module(
        "pinky_daemon.audit_store",
        "dedicated AuditStore owner module is not implemented",
    )
    hooks_module = importlib.import_module("pinky_daemon.hooks")
    source = Path(hooks_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    sqlite_imports = [
        node
        for node in ast.walk(tree)
        if (isinstance(node, ast.Import) and any(alias.name == "sqlite3" for alias in node.names))
        or (isinstance(node, ast.ImportFrom) and node.module == "sqlite3")
    ]
    assert sqlite_imports == []
    assert hooks_module.AuditStore is audit_module.AuditStore

    store = audit_module.AuditStore(db_path=os.fspath(tmp_path / "audit.db"))
    manager = hooks_module.HookManager(audit_store=store)
    context = hooks_module.HookContext(
        event=hooks_module.HookEvent.post_tool_use,
        agent_name="tenant",
        session_id="session-1",
        data={"tool": {"tool_name": "Read", "tool_input": {"path": "safe"}}},
    )
    try:
        import asyncio

        asyncio.run(manager.fire(context))
        [entry] = store.get_log(agent_name="tenant", session_id="session-1")
        assert entry.event == "post_tool_use"
        assert entry.tool_name == "Read"
    finally:
        store.close()


def test_fleet_and_standalone_manifest_providers_are_explicit_and_non_guessing(
    tmp_path: Path,
) -> None:
    module = _manifest_module()
    provider_for_kind = getattr(module, "manifest_provider_for_kind", None)
    assert provider_for_kind is not None
    fleet = provider_for_kind("fleet")
    standalone = provider_for_kind("standalone-tenant")
    base = tmp_path / "fleet" / "conversations.db"
    keystore = tmp_path / "tenant" / "data" / "agent_keys.db"

    fleet_manifest = fleet(base)
    tenant_manifest = standalone(keystore)

    assert Path(fleet_manifest["agent_signing_keys"].path) == Path(fleet_manifest["agents"].path)
    assert tenant_manifest == {
        "agent_signing_keys": StoreIntegrityTarget(
            logical_name="agent_signing_keys",
            path=os.fspath(keystore),
            criticality="authoritative",
            recovery="snapshot",
            journal_mode="delete",
        )
    }
    with pytest.raises(ValueError, match="manifest kind"):
        provider_for_kind("guessed-from-path")


def test_agent_signing_keys_join_fleet_preflight_and_restore_schema_cohort(
    tmp_path: Path,
) -> None:
    from pinky_daemon import api as api_module
    from pinky_daemon.store_restore import STORE_SCHEMA_SENTINELS

    manifest = api_module._derive_api_store_manifest(tmp_path / "conversations.db")

    assert "agent_signing_keys" in manifest
    assert manifest["agent_signing_keys"].path == manifest["agents"].path
    assert STORE_SCHEMA_SENTINELS["agent_signing_keys"] == ("agent_signing_keys",)


def test_agent_signing_keys_join_agents_snapshot_physical_cohort(tmp_path: Path) -> None:
    from pinky_daemon.store_snapshot import StoreSnapshotService

    catalog = StoreCatalog(expected_root=tmp_path, silence_allowlist={})
    registry = AgentRegistry(
        db_path=os.fspath(tmp_path / "agents.db"),
        catalog=catalog,
    )
    try:
        registry.register("tenant", working_dir=os.fspath(tmp_path / "work"))
        [result] = StoreSnapshotService(catalog).create_snapshots("agents")
        assert result.verification == "ok"
        assert result.logical_names == ("agents", "agent_signing_keys")
    finally:
        registry.close()


def test_restore_selector_requires_and_uses_the_explicit_manifest_provider(
    tmp_path: Path,
) -> None:
    manifest_module = _manifest_module()
    restore_module = importlib.import_module("pinky_daemon.store_restore")
    provider = manifest_module.manifest_provider_for_kind("standalone-tenant")
    target = tmp_path / "tenant" / "data" / "agent_keys.db"
    _seed_signing_key_db(target)
    parameter = inspect.signature(restore_module.restore_store).parameters.get("manifest_provider")
    assert parameter is not None
    assert parameter.default is inspect.Parameter.empty

    selected = restore_module._select_target(
        target,
        "agent_signing_keys",
        manifest_provider=provider,
    )
    assert selected.target == target
    assert selected.logical_names == ("agent_signing_keys",)


def test_restore_cli_requires_an_explicit_manifest_kind() -> None:
    script = Path(__file__).resolve().parents[1] / "scripts" / "store_restore.py"
    source = script.read_text(encoding="utf-8")
    tree = ast.parse(source)
    manifest_kind_calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_argument"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "--manifest-kind"
    ]
    assert len(manifest_kind_calls) == 1
    keywords = {keyword.arg: keyword.value for keyword in manifest_kind_calls[0].keywords}
    assert isinstance(keywords.get("required"), ast.Constant)
    assert keywords["required"].value is True
    assert "manifest_provider_for_kind" in source


def test_boot_preflights_scoped_tenant_keystores_after_registry_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon import api as api_module

    base = tmp_path / "fleet" / "conversations.db"
    _seed_unix_user_registry(base, tmp_path / "tenant-work")
    tenant_db = tmp_path / "homes" / "pinky-tenant" / "data" / "agent_keys.db"
    derived: list[str] = []

    def derive(agent_name: str) -> dict[str, StoreIntegrityTarget]:
        derived.append(agent_name)
        assert Path(api_module._derive_api_store_manifest(base)["agents"].path).is_file()
        return {
            "agent_signing_keys": StoreIntegrityTarget(
                logical_name="agent_signing_keys",
                path=os.fspath(tenant_db),
            )
        }

    monkeypatch.setattr(
        api_module,
        "_derive_standalone_tenant_store_manifest",
        derive,
        raising=False,
    )
    calls: list[tuple[str, tuple[str, ...]]] = []
    real_preflight = StoreCatalog.preflight_integrity

    def preflight_spy(
        catalog: StoreCatalog,
        targets: Any,
        **kwargs: Any,
    ) -> dict[str, str]:
        target_list = list(targets)
        calls.append(
            (
                catalog.expected_root,
                tuple(target.logical_name for target in target_list),
            )
        )
        return real_preflight(catalog, target_list, **kwargs)

    monkeypatch.setattr(StoreCatalog, "preflight_integrity", preflight_spy)

    app = api_module.create_api(db_path=os.fspath(base))

    assert derived == ["tenant"]
    assert calls[0][0] == os.path.realpath(base.parent)
    assert "agents" in calls[0][1]
    assert "agent_signing_keys" in calls[0][1]
    assert calls[1] == (
        os.path.realpath(tenant_db.parent),
        ("agent_signing_keys",),
    )
    assert set(app.state.tenant_store_catalogs) == {"tenant"}
    assert app.state.tenant_store_catalogs["tenant"].expected_root == os.path.realpath(
        tenant_db.parent
    )


def test_boot_fails_closed_on_corrupt_scoped_tenant_keystore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon import api as api_module

    base = tmp_path / "fleet" / "conversations.db"
    _seed_unix_user_registry(base, tmp_path / "tenant-work")
    tenant_db = tmp_path / "homes" / "pinky-tenant" / "data" / "agent_keys.db"
    tenant_db.parent.mkdir(parents=True)
    tenant_db.write_bytes(b"not a sqlite database")

    monkeypatch.setattr(
        api_module,
        "_derive_standalone_tenant_store_manifest",
        lambda agent_name: {
            "agent_signing_keys": StoreIntegrityTarget(
                logical_name="agent_signing_keys",
                path=os.fspath(tenant_db),
            )
        },
        raising=False,
    )

    with pytest.raises(StoreCatalogError, match="agent_signing_keys"):
        api_module.create_api(db_path=os.fspath(base))


def test_boot_loudly_rejects_cross_device_signing_key_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon import api as api_module

    base = tmp_path / "fleet" / "conversations.db"
    _seed_unix_user_registry(base, tmp_path / "tenant-work")
    tenant_db = tmp_path / "homes" / "pinky-tenant" / "data" / "agent_keys.db"
    monkeypatch.setattr(
        api_module,
        "_derive_standalone_tenant_store_manifest",
        lambda _agent_name: {
            "agent_signing_keys": StoreIntegrityTarget(
                logical_name="agent_signing_keys",
                path=os.fspath(tenant_db),
                journal_mode="delete",
            )
        },
        raising=False,
    )

    def reject_cross_device(*_args: Any, **_kwargs: Any) -> None:
        raise OSError(
            errno.EXDEV,
            "configure PINKY_SIGNING_KEY_STAGING_ROOT on the tenant filesystem",
        )

    monkeypatch.setattr(api_module, "validate_signing_key_staging_root", reject_cross_device)
    closed_roots: list[str] = []
    real_close = StoreCatalog.close

    def close_spy(catalog: StoreCatalog) -> None:
        closed_roots.append(catalog.expected_root)
        real_close(catalog)

    monkeypatch.setattr(StoreCatalog, "close", close_spy)

    with pytest.raises(
        StoreCatalogError,
        match="staging configuration.*PINKY_SIGNING_KEY_STAGING_ROOT",
    ):
        api_module.create_api(db_path=os.fspath(base))

    assert os.path.realpath(base.parent) in closed_roots


def test_boot_never_attaches_preflight_mode_to_replacement_inode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pinky_daemon import api as api_module

    base = tmp_path / "fleet" / "conversations.db"
    _seed_unix_user_registry(base, tmp_path / "tenant-work")
    tenant_db = tmp_path / "homes" / "pinky-tenant" / "data" / "agent_keys.db"
    replacement = tmp_path / "replacement-wal.db"
    preflight_original = tmp_path / "preflight-original.db"
    _seed_signing_key_db(tenant_db, signing_key="preflight-secret")
    _seed_persistent_wal_signing_key_db(replacement, signing_key="replacement-secret")
    swapped = False

    monkeypatch.setattr(
        api_module,
        "_derive_standalone_tenant_store_manifest",
        lambda _agent_name: {
            "agent_signing_keys": StoreIntegrityTarget(
                logical_name="agent_signing_keys",
                path=os.fspath(tenant_db),
                journal_mode="delete",
            )
        },
        raising=False,
    )
    real_preflight = StoreCatalog.preflight_integrity

    def swap_after_tenant_preflight(
        catalog: StoreCatalog,
        targets: Any,
        **kwargs: Any,
    ) -> dict[str, str]:
        nonlocal swapped
        target_list = list(targets)
        outcomes = real_preflight(catalog, target_list, **kwargs)
        if catalog.expected_root == os.path.realpath(tenant_db.parent):
            tenant_db.replace(preflight_original)
            replacement.replace(tenant_db)
            swapped = True
        return outcomes

    monkeypatch.setattr(StoreCatalog, "preflight_integrity", swap_after_tenant_preflight)

    with pytest.raises(StoreCatalogError, match="changed|identity|observation"):
        api_module.create_api(db_path=os.fspath(base))

    assert swapped
    assert not Path(os.fspath(tenant_db) + "-wal").exists()
    assert not Path(os.fspath(tenant_db) + "-shm").exists()


def test_all_externally_read_signing_databases_are_rollback_journal(
    tmp_path: Path,
) -> None:
    module = _signing_store_module()
    fleet_path = tmp_path / "fleet-agents.db"
    registry = AgentRegistry(db_path=os.fspath(fleet_path))
    try:
        registry.register("tenant", working_dir=os.fspath(tmp_path / "work"))
        assert registry._db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "truncate"
        assert module.AgentSigningKeyStore.read_only(os.fspath(fleet_path)).get_signing_key(
            "tenant"
        )
    finally:
        registry.close()
    assert not Path(os.fspath(fleet_path) + "-wal").exists()
    assert not Path(os.fspath(fleet_path) + "-shm").exists()

    tenant_path = tmp_path / "tenant" / "agent_keys.db"
    tenant_path.parent.mkdir()
    tenant_path.parent.chmod(0o700)
    catalog = StoreCatalog(expected_root=tenant_path.parent, silence_allowlist={})
    module.AgentSigningKeyStore.provision_single_agent(
        os.fspath(tenant_path),
        "tenant",
        "tenant-secret",
        catalog=catalog,
        staging_root=tmp_path / "daemon-staging",
    )
    with sqlite3.connect(tenant_path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "delete"
    assert (
        module.AgentSigningKeyStore.read_only(os.fspath(tenant_path)).get_signing_key("tenant")
        == "tenant-secret"
    )
    assert not Path(os.fspath(tenant_path) + "-wal").exists()
    assert not Path(os.fspath(tenant_path) + "-shm").exists()


def test_persistent_wal_keystore_preflight_fails_before_sidecar_recreation(
    tmp_path: Path,
) -> None:
    tenant_path = tmp_path / "tenant" / "agent_keys.db"
    _seed_persistent_wal_signing_key_db(tenant_path)
    catalog = StoreCatalog(expected_root=tenant_path.parent, silence_allowlist={})
    target = StoreIntegrityTarget(
        logical_name="agent_signing_keys",
        path=os.fspath(tenant_path),
        journal_mode="delete",
    )

    with pytest.raises(StoreCatalogError, match="WAL|rollback"):
        catalog.preflight_integrity([target])

    assert catalog.snapshot() == [], "preflight must not falsely register DELETE"
    assert not Path(os.fspath(tenant_path) + "-wal").exists()
    assert not Path(os.fspath(tenant_path) + "-shm").exists()


def test_persistent_wal_keystore_reader_fails_soft_without_creating_sidecars(
    tmp_path: Path,
) -> None:
    module = _signing_store_module()
    tenant_path = tmp_path / "tenant" / "agent_keys.db"
    _seed_persistent_wal_signing_key_db(tenant_path)

    result = module.AgentSigningKeyStore.read_only(os.fspath(tenant_path)).get_signing_key("tenant")
    assert not Path(os.fspath(tenant_path) + "-wal").exists()
    assert not Path(os.fspath(tenant_path) + "-shm").exists()
    assert result is None


def test_reader_and_preflight_pin_one_fd_across_header_path_swap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _signing_store_module()
    reader_path = tmp_path / "reader" / "agent_keys.db"
    reader_victim = tmp_path / "reader-victim-wal.db"
    reader_original = tmp_path / "reader-original.db"
    preflight_path = tmp_path / "preflight" / "agent_keys.db"
    preflight_victim = tmp_path / "preflight-victim-wal.db"
    preflight_original = tmp_path / "preflight-original.db"
    _seed_signing_key_db(reader_path, signing_key="reader-original-secret")
    _seed_persistent_wal_signing_key_db(
        reader_victim,
        signing_key="reader-victim-secret",
    )
    _seed_signing_key_db(preflight_path, signing_key="preflight-original-secret")
    _seed_persistent_wal_signing_key_db(
        preflight_victim,
        signing_key="preflight-victim-secret",
    )
    races = {
        (reader_path.stat().st_dev, reader_path.stat().st_ino): (
            reader_path,
            reader_original,
            reader_victim,
        ),
        (preflight_path.stat().st_dev, preflight_path.stat().st_ino): (
            preflight_path,
            preflight_original,
            preflight_victim,
        ),
    }
    real_pread = os.pread

    def swap_after_header_read(descriptor: int, length: int, offset: int) -> bytes:
        header = real_pread(descriptor, length, offset)
        descriptor_stat = os.fstat(descriptor)
        race = races.pop((descriptor_stat.st_dev, descriptor_stat.st_ino), None)
        if race is not None:
            target, original, victim = race
            target.replace(original)
            target.symlink_to(victim)
        return header

    monkeypatch.setattr(module.os, "pread", swap_after_header_read)

    reader_result = module.AgentSigningKeyStore.read_only(os.fspath(reader_path)).get_signing_key(
        "tenant"
    )
    catalog = StoreCatalog(expected_root=preflight_path.parent, silence_allowlist={})
    target = StoreIntegrityTarget(
        logical_name="agent_signing_keys",
        path=os.fspath(preflight_path),
        journal_mode="delete",
    )
    preflight_error: StoreCatalogError | None = None
    try:
        catalog.preflight_integrity([target])
    except StoreCatalogError as exc:
        preflight_error = exc

    assert races == {}
    assert reader_result is None
    assert preflight_error is not None
    assert "changed" in str(preflight_error).lower() or "identity" in str(preflight_error).lower()
    for path in (reader_path, reader_victim, preflight_path, preflight_victim):
        assert not Path(os.fspath(path) + "-wal").exists()
        assert not Path(os.fspath(path) + "-shm").exists()
