"""Tests for the #149 phase-3 agent OS-provisioning seam.

inc3a ships only the interface + the no-op LocalProvisioner + the factory.
These tests pin that contract: the no-op is genuinely inert, the factory
maps modes correctly, and the unimplemented unix_user path fails closed
(raises) rather than silently degrading to local.
"""

from __future__ import annotations

import pytest

from pinky_daemon.agent_registry import Agent
from pinky_daemon.provisioning import (
    KNOWN_MODES,
    SECRET_MODE,
    AgentProvisioner,
    LocalProvisioner,
    ProvisionError,
    ProvisionResult,
    SystemProvisionOps,
    UnixUserPaths,
    UnixUserProvisioner,
    get_provisioner,
)


@pytest.fixture
def local_agent():
    return Agent(name="tenant", model="opus", isolated=True, isolation_mode="local")


class TestLocalProvisioner:
    def test_is_an_agent_provisioner(self):
        assert isinstance(LocalProvisioner(), AgentProvisioner)
        assert LocalProvisioner().mode == "local"

    def test_provision_is_a_successful_noop(self, local_agent):
        result = LocalProvisioner().provision(local_agent)
        assert isinstance(result, ProvisionResult)
        assert result.ok is True
        assert result.mode == "local"
        assert result.created == []  # nothing created
        assert result.removed == []

    def test_deprovision_is_a_successful_noop(self, local_agent):
        result = LocalProvisioner().deprovision(local_agent)
        assert result.ok is True
        assert result.created == []
        assert result.removed == []

    def test_always_provisioned(self, local_agent):
        # No OS resources to set up → always considered ready.
        assert LocalProvisioner().is_provisioned(local_agent) is True

    def test_contributes_no_runtime_env(self, local_agent):
        assert LocalProvisioner().runtime_env(local_agent) == {}

    def test_idempotent(self, local_agent):
        p = LocalProvisioner()
        # Repeated calls stay successful no-ops.
        assert p.provision(local_agent).ok
        assert p.provision(local_agent).ok
        assert p.deprovision(local_agent).ok
        assert p.deprovision(local_agent).ok


class TestGetProvisioner:
    def test_local_returns_local_provisioner(self):
        p = get_provisioner("local")
        assert isinstance(p, LocalProvisioner)
        assert p.mode == "local"

    def test_unix_user_stays_fail_closed(self):
        # Dormancy guarantee: even though UnixUserProvisioner now exists, the
        # factory still raises for unix_user so the #642 respawn guard keeps
        # blocking it until the activation increment wires the lifecycle.
        with pytest.raises(NotImplementedError) as exc:
            get_provisioner("unix_user")
        assert "fail-closed" in str(exc.value)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            get_provisioner("container")

    def test_known_modes_constant(self):
        assert KNOWN_MODES == frozenset({"local", "unix_user"})


class TestProvisionResult:
    def test_defaults(self):
        r = ProvisionResult()
        assert r.ok is True
        assert r.mode == "local"
        assert r.created == []
        assert r.removed == []
        assert r.message == ""

    def test_created_lists_are_independent(self):
        # default_factory must not share a single list across instances.
        a, b = ProvisionResult(), ProvisionResult()
        a.created.append("user:pinky-x")
        assert b.created == []


# --------------------------------------------------------------------------- #
# unix_user provisioning (#149 inc3c)
# --------------------------------------------------------------------------- #


class RecordingOps:
    """In-memory ProvisionOps double: records every mutation, tracks user/path
    state so idempotency + rollback paths are exercised without touching the OS.

    ``fail_predicate(argv) -> bool`` injects a mid-provision command failure to
    drive rollback tests.
    """

    def __init__(self, *, users=None, paths=None, fail_predicate=None):
        self.commands: list[list[str]] = []
        self.secret_files: list[tuple[str, str]] = []
        self.keystores: list[tuple[str, str, str]] = []
        self._users = set(users or [])
        self._paths = set(paths or [])
        self._fail = fail_predicate

    def run(self, argv):
        self.commands.append(list(argv))
        if self._fail and self._fail(list(argv)):
            raise ProvisionError(f"injected failure: {' '.join(argv)}")
        cmd = argv[0]
        if cmd == "useradd":
            self._users.add(argv[-1])
        elif cmd == "userdel":
            user = argv[-1]
            self._users.discard(user)
            # --remove deletes the home subtree → drop paths under it.
            self._paths = {p for p in self._paths if f"/{user}" not in p}
        elif cmd == "mkdir":
            self._paths.add(argv[-1])
        elif cmd == "rm":
            self._paths.discard(argv[-1])

    def user_exists(self, username):
        return username in self._users

    def path_exists(self, path):
        return path in self._paths

    def write_secret_file(self, path, content):
        self.secret_files.append((path, content))
        self._paths.add(path)

    def write_keystore(self, path, agent_name, signing_key):
        self.keystores.append((path, agent_name, signing_key))
        self._paths.add(path)

    # helpers
    def cmds_starting(self, prog):
        return [c for c in self.commands if c and c[0] == prog]


@pytest.fixture
def unix_agent():
    return Agent(name="tenant", model="opus", isolated=True, isolation_mode="unix_user")


def _provisioner(ops, **kw):
    return UnixUserProvisioner(ops=ops, signing_key_provider=lambda n: f"key-{n}", **kw)


class TestUnixUserPaths:
    def test_layout_under_home(self):
        p = UnixUserPaths.for_agent("tenant")
        assert p.username == "pinky-tenant"
        assert p.home == "/home/pinky-tenant"
        assert p.workdir == "/home/pinky-tenant/workdir"
        assert p.data_dir == "/home/pinky-tenant/data"
        assert p.config_dir == "/home/pinky-tenant/.claude"
        assert p.keystore == "/home/pinky-tenant/data/agent_keys.db"
        assert p.mcp_json == "/home/pinky-tenant/workdir/.mcp.json"
        # Everything is under the home so userdel --remove cleans it all.
        for path in (p.workdir, p.data_dir, p.config_dir, p.keystore, p.mcp_json):
            assert path.startswith(p.home + "/")

    def test_custom_home_root(self):
        p = UnixUserPaths.for_agent("x", home_root="/srv/agents", prefix="bot-")
        assert p.username == "bot-x"
        assert p.home == "/srv/agents/bot-x"


class TestUnixUserProvisionerContract:
    def test_is_an_agent_provisioner(self):
        assert isinstance(_provisioner(RecordingOps()), AgentProvisioner)
        assert _provisioner(RecordingOps()).mode == "unix_user"


class TestUnixUserProvision:
    def test_provision_command_shapes(self, unix_agent):
        ops = RecordingOps()
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is True
        assert result.mode == "unix_user"

        # useradd: own home + nologin shell + correct username
        useradd = ops.cmds_starting("useradd")
        assert len(useradd) == 1
        assert useradd[0] == [
            "useradd", "--create-home", "--home-dir", "/home/pinky-tenant",
            "--shell", "/usr/sbin/nologin", "pinky-tenant",
        ]
        # the three private dirs each get mkdir
        mkdirs = {c[-1] for c in ops.cmds_starting("mkdir")}
        assert mkdirs == {
            "/home/pinky-tenant/workdir",
            "/home/pinky-tenant/data",
            "/home/pinky-tenant/.claude",
        }

    def test_dir_perms_are_0700(self, unix_agent):
        ops = RecordingOps()
        _provisioner(ops).provision(unix_agent)
        # home + 3 dirs all chmod 0700
        chmods = ops.cmds_starting("chmod")
        modes = {c[1] for c in chmods}
        assert modes == {"0700"}
        chmod_targets = {c[2] for c in chmods}
        assert "/home/pinky-tenant" in chmod_targets  # home tightened from useradd default

    def test_everything_chowned_to_agent_user(self, unix_agent):
        ops = RecordingOps()
        _provisioner(ops).provision(unix_agent)
        chowns = ops.cmds_starting("chown")
        # every chown targets pinky-tenant:pinky-tenant
        assert all(c[1] == "pinky-tenant:pinky-tenant" for c in chowns)
        targets = {c[2] for c in chowns}
        # home, 3 dirs, keystore, mcp.json
        assert "/home/pinky-tenant/data/agent_keys.db" in targets
        assert "/home/pinky-tenant/workdir/.mcp.json" in targets

    def test_single_agent_keystore_written(self, unix_agent):
        ops = RecordingOps()
        _provisioner(ops).provision(unix_agent)
        assert len(ops.keystores) == 1
        path, agent_name, key = ops.keystores[0]
        assert path == "/home/pinky-tenant/data/agent_keys.db"
        assert agent_name == "tenant"
        assert key == "key-tenant"  # from the injected provider

    def test_mcp_json_written_as_secret(self, unix_agent):
        ops = RecordingOps()
        _provisioner(ops).provision(unix_agent)
        assert len(ops.secret_files) == 1
        path, content = ops.secret_files[0]
        assert path == "/home/pinky-tenant/workdir/.mcp.json"
        assert "mcpServers" in content

    def test_created_tokens_recorded_in_order(self, unix_agent):
        ops = RecordingOps()
        result = _provisioner(ops).provision(unix_agent)
        assert result.created[0] == "user:pinky-tenant"
        assert "path:/home/pinky-tenant/data/agent_keys.db" in result.created
        assert "path:/home/pinky-tenant/workdir/.mcp.json" in result.created

    def test_missing_signing_key_fails_and_rolls_back(self, unix_agent):
        ops = RecordingOps()
        # default provider raises -> provision must fail closed + roll back user
        p = UnixUserProvisioner(ops=ops, signing_key_provider=lambda n: "")
        result = p.provision(unix_agent)
        assert result.ok is False
        assert "signing key" in result.message.lower()
        assert not ops.user_exists("pinky-tenant")  # rolled back

    def test_idempotent_when_already_provisioned(self, unix_agent):
        # user + keystore already present → no-op, no useradd
        ops = RecordingOps(
            users={"pinky-tenant"},
            paths={"/home/pinky-tenant/data/agent_keys.db"},
        )
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is True
        assert "already provisioned" in result.message
        assert ops.cmds_starting("useradd") == []


class TestUnixUserRollback:
    def test_failure_after_user_rolls_back_user(self, unix_agent):
        # Fail on the very first mkdir → only the user was created so far.
        ops = RecordingOps(fail_predicate=lambda a: a[0] == "mkdir")
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is False
        assert result.created == ["user:pinky-tenant"]
        assert "user:pinky-tenant" in result.removed
        assert not ops.user_exists("pinky-tenant")
        # rollback issued a userdel --remove
        assert any(c[:2] == ["userdel", "--remove"] for c in ops.commands)

    def test_failure_midway_undoes_in_reverse(self, unix_agent):
        # Fail creating the 2nd dir (data): user + workdir already built.
        def fail(argv):
            return argv[0] == "mkdir" and argv[-1].endswith("/data")

        ops = RecordingOps(fail_predicate=fail)
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is False
        assert result.created == ["user:pinky-tenant", "path:/home/pinky-tenant/workdir"]
        # undone in reverse: workdir rm'd, then user removed
        assert result.removed == ["path:/home/pinky-tenant/workdir", "user:pinky-tenant"]
        assert not ops.user_exists("pinky-tenant")
        assert not ops.path_exists("/home/pinky-tenant/workdir")


class TestUnixUserDeprovision:
    def test_deprovision_removes_user_and_home(self, unix_agent):
        ops = RecordingOps(users={"pinky-tenant"})
        result = _provisioner(ops).deprovision(unix_agent)
        assert result.ok is True
        assert result.removed == ["user:pinky-tenant"]
        assert ops.commands[-1] == ["userdel", "--remove", "pinky-tenant"]
        assert not ops.user_exists("pinky-tenant")

    def test_deprovision_absent_is_noop(self, unix_agent):
        ops = RecordingOps()  # no such user
        result = _provisioner(ops).deprovision(unix_agent)
        assert result.ok is True
        assert "already absent" in result.message
        assert ops.commands == []


class TestUnixUserIsProvisioned:
    def test_needs_both_user_and_keystore(self, unix_agent):
        p = _provisioner(RecordingOps(users={"pinky-tenant"}))  # user but no keystore
        assert p.is_provisioned(unix_agent) is False

        ops = RecordingOps(
            users={"pinky-tenant"}, paths={"/home/pinky-tenant/data/agent_keys.db"}
        )
        assert _provisioner(ops).is_provisioned(unix_agent) is True


class TestUnixUserRuntimeEnv:
    def test_points_at_single_agent_keystore_not_fleet_db(self, unix_agent):
        env = _provisioner(RecordingOps()).runtime_env(unix_agent)
        # The critical isolation property: PINKY_AGENTS_DB is the per-agent
        # keystore under the tenant's own home, NEVER the fleet DB.
        assert env["PINKY_AGENTS_DB"] == "/home/pinky-tenant/data/agent_keys.db"
        assert env["HOME"] == "/home/pinky-tenant"
        assert env["CLAUDE_CONFIG_DIR"] == "/home/pinky-tenant/.claude"
        assert env["PINKY_AGENT_USER"] == "pinky-tenant"
        assert "conversations_agents" not in env["PINKY_AGENTS_DB"]


class TestSystemProvisionOps:
    """The real ops double can't run useradd in CI, but its file-writing half
    is testable on macOS — and the 0600 secret guarantee is worth pinning."""

    def test_write_secret_file_is_0600(self, tmp_path):
        target = tmp_path / "secret.txt"
        SystemProvisionOps().write_secret_file(str(target), "hunter2")
        assert target.read_text() == "hunter2"
        assert (target.stat().st_mode & 0o777) == SECRET_MODE

    def test_write_keystore_single_row_0600(self, tmp_path):
        import sqlite3

        target = tmp_path / "agent_keys.db"
        SystemProvisionOps().write_keystore(str(target), "tenant", "sekret")
        assert (target.stat().st_mode & 0o777) == SECRET_MODE
        conn = sqlite3.connect(str(target))
        try:
            rows = conn.execute(
                "SELECT agent_name, signing_key FROM agent_signing_keys"
            ).fetchall()
        finally:
            conn.close()
        assert rows == [("tenant", "sekret")]

    def test_keystore_is_resolver_compatible(self, tmp_path):
        # End-to-end with the #641 resolver: a tenant pointed at this keystore
        # resolves ITS key and nothing else.
        from pinky_daemon.auth import make_db_signing_key_resolver

        target = tmp_path / "agent_keys.db"
        SystemProvisionOps().write_keystore(str(target), "tenant", "sekret")
        resolve = make_db_signing_key_resolver(str(target))
        assert resolve("tenant") == "sekret"
        assert resolve("someone-else") is None
