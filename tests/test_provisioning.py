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
    ContainerNames,
    ContainerProvisioner,
    LocalProvisioner,
    ProvisionError,
    ProvisionResult,
    SystemContainerOps,
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

    def test_container_stays_fail_closed(self, monkeypatch):
        # Dormancy guarantee: with the runtime gate OFF (default),
        # ContainerProvisioner exists but the factory raises so the #642 respawn
        # guard keeps blocking container until an operator opts in.
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with pytest.raises(NotImplementedError) as exc:
            get_provisioner("container")
        assert "fail-closed" in str(exc.value)

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValueError):
            get_provisioner("qemu_vm")

    def test_known_modes_constant(self):
        assert KNOWN_MODES == frozenset({"local", "unix_user", "container"})


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


# Every path a fully-provisioned "tenant" agent owns — the set is_provisioned()
# requires before it reports ready (user + these paths).
FULLY_PROVISIONED_PATHS = {
    "/home/pinky-tenant/workdir",
    "/home/pinky-tenant/data",
    "/home/pinky-tenant/.claude",
    "/home/pinky-tenant/data/agent_keys.db",
    "/home/pinky-tenant/workdir/.mcp.json",
}


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

    def test_idempotent_when_fully_provisioned(self, unix_agent):
        # user + ALL resources present → no-op, no commands at all
        ops = RecordingOps(users={"pinky-tenant"}, paths=set(FULLY_PROVISIONED_PATHS))
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is True
        assert "already provisioned" in result.message
        assert ops.commands == []  # nothing touched

    def test_reconciles_partial_tenant(self, unix_agent):
        # User + keystore exist but dirs + .mcp.json are missing (a half-build).
        # Strengthened is_provisioned() reports NOT ready, so provision() must
        # reconcile — fill the gaps WITHOUT re-running useradd, and track only
        # the newly-built resources for rollback.
        ops = RecordingOps(
            users={"pinky-tenant"},
            paths={"/home/pinky-tenant/data", "/home/pinky-tenant/data/agent_keys.db"},
        )
        result = _provisioner(ops).provision(unix_agent)
        assert result.ok is True
        # did NOT recreate the existing user or keystore
        assert ops.cmds_starting("useradd") == []
        assert ops.keystores == []  # keystore already existed → not rewritten
        # DID build the missing pieces
        made = {c[-1] for c in ops.cmds_starting("mkdir")}
        assert made == {"/home/pinky-tenant/workdir", "/home/pinky-tenant/.claude"}
        assert ops.secret_files and ops.secret_files[0][0] == "/home/pinky-tenant/workdir/.mcp.json"
        # created tracks only this call's work — never the pre-existing user
        assert "user:pinky-tenant" not in result.created
        assert "path:/home/pinky-tenant/workdir" in result.created
        assert "path:/home/pinky-tenant/workdir/.mcp.json" in result.created
        # and the tenant is now fully provisioned
        assert _provisioner(ops).is_provisioned(unix_agent) is True


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
    def test_requires_every_resource(self, unix_agent):
        # user alone → not ready
        assert _provisioner(RecordingOps(users={"pinky-tenant"})).is_provisioned(unix_agent) is False
        # user + keystore but missing dirs/.mcp.json → still not ready (the
        # weak-check bug @murzik flagged)
        partial = RecordingOps(
            users={"pinky-tenant"}, paths={"/home/pinky-tenant/data/agent_keys.db"}
        )
        assert _provisioner(partial).is_provisioned(unix_agent) is False
        # user + the full resource set → ready
        full = RecordingOps(users={"pinky-tenant"}, paths=set(FULLY_PROVISIONED_PATHS))
        assert _provisioner(full).is_provisioned(unix_agent) is True

    def test_missing_one_dir_is_not_ready(self, unix_agent):
        # Drop just .claude → not ready (exercises the all() over dirs).
        paths = set(FULLY_PROVISIONED_PATHS) - {"/home/pinky-tenant/.claude"}
        ops = RecordingOps(users={"pinky-tenant"}, paths=paths)
        assert _provisioner(ops).is_provisioned(unix_agent) is False


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


# --------------------------------------------------------------------------- #
# container provisioning (container isolation_mode)
# --------------------------------------------------------------------------- #


class RecordingContainerOps:
    """In-memory ContainerOps double: records every podman command + tracks
    image/volume/secret/container state so idempotency + rollback paths are
    exercised without a real container runtime.

    ``fail_predicate(argv) -> bool`` injects a mid-provision command failure.
    """

    def __init__(
        self, *, images=None, volumes=None, secrets=None, containers=None, fail_predicate=None
    ):
        self.commands: list[list[str]] = []
        self.secrets_written: list[tuple[str, str]] = []
        self._images = set(images or [])
        self._volumes = set(volumes or [])
        self._secrets = set(secrets or [])
        self._containers = set(containers or [])
        self._fail = fail_predicate

    def run(self, argv):
        self.commands.append(list(argv))
        if self._fail and self._fail(list(argv)):
            raise ProvisionError(f"injected failure: {' '.join(argv)}")
        # Reflect state mutations (argv[0] is the podman binary).
        if len(argv) >= 4 and argv[1] == "volume" and argv[2] == "create":
            self._volumes.add(argv[3])
        elif len(argv) >= 3 and argv[1] == "pull":
            self._images.add(argv[2])
        elif len(argv) >= 4 and argv[1] == "create" and argv[2] == "--name":
            self._containers.add(argv[3])
        elif len(argv) >= 4 and argv[1] == "rm" and argv[2] == "-f":
            self._containers.discard(argv[3])
        elif len(argv) >= 4 and argv[1] == "secret" and argv[2] == "rm":
            self._secrets.discard(argv[3])
        elif len(argv) >= 4 and argv[1] == "volume" and argv[2] == "rm":
            self._volumes.discard(argv[3])

    def image_exists(self, ref):
        return ref in self._images

    def volume_exists(self, name):
        return name in self._volumes

    def secret_exists(self, name):
        return name in self._secrets

    def container_exists(self, name):
        return name in self._containers

    def write_secret(self, name, content):
        self.secrets_written.append((name, content))
        self._secrets.add(name)


@pytest.fixture
def container_agent():
    return Agent(name="tenant", model="opus", isolated=True, isolation_mode="container")


def _cprov(ops, **kw):
    kw.setdefault("image_provider", lambda a: "myco/agent:1")
    kw.setdefault("signing_key_provider", lambda n: f"key-{n}")
    return ContainerProvisioner(ops=ops, **kw)


class TestContainerNames:
    def test_layout(self):
        n = ContainerNames.for_agent("tenant")
        assert n.container == "pinky-tenant"
        assert n.volume == "pinky-tenant-home"
        assert n.secret == "pinky-tenant-key"
        assert n.home == "/home/agent"
        assert n.workdir == "/home/agent/workdir"
        assert n.config_dir == "/home/agent/.claude"

    def test_custom_prefix_and_home(self):
        n = ContainerNames.for_agent("x", prefix="bot-", home="/srv/x")
        assert n.container == "bot-x"
        assert n.volume == "bot-x-home"
        assert n.secret == "bot-x-key"
        assert n.home == "/srv/x"
        assert n.config_dir == "/srv/x/.claude"


class TestContainerProvisionerContract:
    def test_is_an_agent_provisioner(self):
        assert isinstance(_cprov(RecordingContainerOps()), AgentProvisioner)
        assert _cprov(RecordingContainerOps()).mode == "container"


class TestContainerProvision:
    def test_command_shapes(self, container_agent):
        ops = RecordingContainerOps()
        result = _cprov(ops).provision(container_agent)
        assert result.ok is True
        assert result.mode == "container"
        assert ["podman", "volume", "create", "pinky-tenant-home"] in ops.commands
        assert ["podman", "pull", "myco/agent:1"] in ops.commands
        # the container is CREATED (stopped), never `run` — start is activation's job
        creates = [c for c in ops.commands if len(c) > 1 and c[1] == "create"]
        assert len(creates) == 1
        cc = creates[0]
        assert cc[:4] == ["podman", "create", "--name", "pinky-tenant"]
        assert "pinky-tenant-home:/home/agent" in cc  # home volume mount
        assert "pinky-tenant-key,type=mount" in cc  # key secret mount
        assert "HOME=/home/agent" in cc
        assert cc[-4:] == ["--entrypoint", "sleep", "myco/agent:1", "infinity"]

    def test_create_includes_engaged_path_flags(self, container_agent):
        # Rootless uid mapping (so claude runs non-root + bind files stay
        # writable) and host reachability for the daemon API + shared MCP.
        ops = RecordingContainerOps()
        _cprov(ops).provision(container_agent)
        cc = next(c for c in ops.commands if len(c) > 1 and c[1] == "create")
        assert "--userns=keep-id" in cc
        assert "--add-host=host.containers.internal:host-gateway" in cc

    def test_create_binds_working_dir_at_same_absolute_path(self):
        # The host working_dir must be mounted at the SAME absolute path so the
        # absolute hook commands in .claude/settings.json resolve in-container.
        ops = RecordingContainerOps()
        agent = Agent(
            name="tenant", model="opus", isolated=True,
            isolation_mode="container", working_dir="/srv/data/agents/tenant",
        )
        _cprov(ops).provision(agent)
        cc = next(c for c in ops.commands if len(c) > 1 and c[1] == "create")
        assert "/srv/data/agents/tenant:/srv/data/agents/tenant" in cc

    def test_no_working_dir_omits_bind(self, container_agent):
        # container_agent fixture has no working_dir → no workdir bind emitted.
        ops = RecordingContainerOps()
        _cprov(ops).provision(container_agent)
        cc = next(c for c in ops.commands if len(c) > 1 and c[1] == "create")
        assert not any(tok.count(":") and tok.split(":")[0] == tok.split(":")[1]
                       for tok in cc if "/" in tok and tok != "pinky-tenant-home:/home/agent")

    def test_docker_runtime_omits_keep_id(self, container_agent):
        # keep-id is Podman-specific; rootless Docker maps to the host user already.
        ops = RecordingContainerOps()
        _cprov(ops, binary="docker").provision(container_agent)
        cc = next(c for c in ops.commands if len(c) > 1 and c[1] == "create")
        assert cc[0] == "docker"
        assert "--userns=keep-id" not in cc
        assert "--add-host=host.containers.internal:host-gateway" in cc

    def test_signing_key_never_on_an_argv(self, container_agent):
        ops = RecordingContainerOps()
        _cprov(ops).provision(container_agent)
        # the key VALUE went via write_secret (stdin), not any podman argv
        assert ops.secrets_written == [("pinky-tenant-key", "key-tenant")]
        assert not any("key-tenant" in tok for c in ops.commands for tok in c)

    def test_created_tokens_recorded_in_order(self, container_agent):
        ops = RecordingContainerOps()
        result = _cprov(ops).provision(container_agent)
        # image pull is shared infra → NOT a tracked per-agent resource
        assert result.created == [
            "volume:pinky-tenant-home",
            "secret:pinky-tenant-key",
            "container:pinky-tenant",
        ]

    def test_no_image_fails_closed_before_touching_anything(self, container_agent):
        ops = RecordingContainerOps()
        p = ContainerProvisioner(
            ops=ops, image_provider=lambda a: "", signing_key_provider=lambda n: "k"
        )
        result = p.provision(container_agent)
        assert result.ok is False
        assert "container_image" in result.message
        assert ops.commands == []  # nothing created without an image

    def test_missing_signing_key_fails_and_rolls_back(self, container_agent):
        ops = RecordingContainerOps()
        p = ContainerProvisioner(
            ops=ops, image_provider=lambda a: "img:1", signing_key_provider=lambda n: ""
        )
        result = p.provision(container_agent)
        assert result.ok is False
        assert "signing key" in result.message.lower()
        # the volume created before the failure was rolled back
        assert not ops.volume_exists("pinky-tenant-home")
        assert any(c[:3] == ["podman", "volume", "rm"] for c in ops.commands)

    def test_idempotent_when_fully_provisioned(self, container_agent):
        ops = RecordingContainerOps(
            volumes={"pinky-tenant-home"},
            secrets={"pinky-tenant-key"},
            containers={"pinky-tenant"},
        )
        result = _cprov(ops).provision(container_agent)
        assert result.ok is True
        assert "already provisioned" in result.message
        assert ops.commands == []

    def test_reconciles_partial_tenant(self, container_agent):
        # volume exists; secret + container missing → build only the gaps,
        # don't recreate the volume, and track only this call's work.
        ops = RecordingContainerOps(volumes={"pinky-tenant-home"})
        result = _cprov(ops).provision(container_agent)
        assert result.ok is True
        assert not any(c[:3] == ["podman", "volume", "create"] for c in ops.commands)
        assert ops.secrets_written == [("pinky-tenant-key", "key-tenant")]
        assert any(len(c) > 3 and c[1] == "create" and c[3] == "pinky-tenant" for c in ops.commands)
        assert result.created == ["secret:pinky-tenant-key", "container:pinky-tenant"]

    def test_present_image_is_not_pulled(self, container_agent):
        ops = RecordingContainerOps(images={"myco/agent:1"})
        _cprov(ops).provision(container_agent)
        assert not any(len(c) > 1 and c[1] == "pull" for c in ops.commands)


class TestContainerRollback:
    def test_failure_on_container_create_undoes_in_reverse(self, container_agent):
        # Fail the container `create`; volume + secret were built first.
        ops = RecordingContainerOps(fail_predicate=lambda a: len(a) > 1 and a[1] == "create")
        result = _cprov(ops).provision(container_agent)
        assert result.ok is False
        assert result.created == ["volume:pinky-tenant-home", "secret:pinky-tenant-key"]
        # undone in reverse: secret rm'd, then volume rm'd
        assert result.removed == ["secret:pinky-tenant-key", "volume:pinky-tenant-home"]
        assert not ops.secret_exists("pinky-tenant-key")
        assert not ops.volume_exists("pinky-tenant-home")


class TestContainerDeprovision:
    def test_default_keeps_home_volume(self, container_agent):
        ops = RecordingContainerOps(
            volumes={"pinky-tenant-home"},
            secrets={"pinky-tenant-key"},
            containers={"pinky-tenant"},
        )
        result = _cprov(ops).deprovision(container_agent)
        assert result.ok is True
        assert result.removed == ["container:pinky-tenant", "secret:pinky-tenant-key"]
        assert "preserved" in result.message
        assert ops.volume_exists("pinky-tenant-home")  # the durable login survives
        assert not ops.container_exists("pinky-tenant")
        assert not ops.secret_exists("pinky-tenant-key")

    def test_remove_volume_purges_everything(self, container_agent):
        ops = RecordingContainerOps(
            volumes={"pinky-tenant-home"},
            secrets={"pinky-tenant-key"},
            containers={"pinky-tenant"},
        )
        result = _cprov(ops).deprovision(container_agent, remove_volume=True)
        assert result.removed == [
            "container:pinky-tenant",
            "secret:pinky-tenant-key",
            "volume:pinky-tenant-home",
        ]
        assert not ops.volume_exists("pinky-tenant-home")

    def test_absent_is_noop(self, container_agent):
        ops = RecordingContainerOps()
        result = _cprov(ops).deprovision(container_agent)
        assert result.ok is True
        assert result.removed == []
        assert ops.commands == []


class TestContainerIsProvisioned:
    def test_requires_volume_secret_and_container(self, container_agent):
        full = RecordingContainerOps(
            volumes={"pinky-tenant-home"},
            secrets={"pinky-tenant-key"},
            containers={"pinky-tenant"},
        )
        assert _cprov(full).is_provisioned(container_agent) is True
        no_container = RecordingContainerOps(
            volumes={"pinky-tenant-home"}, secrets={"pinky-tenant-key"}
        )
        assert _cprov(no_container).is_provisioned(container_agent) is False
        no_secret = RecordingContainerOps(
            volumes={"pinky-tenant-home"}, containers={"pinky-tenant"}
        )
        assert _cprov(no_secret).is_provisioned(container_agent) is False


class TestContainerRuntimeEnv:
    def test_in_container_paths(self, container_agent):
        env = _cprov(RecordingContainerOps()).runtime_env(container_agent)
        assert env["HOME"] == "/home/agent"
        assert env["CLAUDE_CONFIG_DIR"] == "/home/agent/.claude"
        assert env["PINKY_AGENT_NAME"] == "tenant"


class TestSystemContainerOps:
    """The real ops shell out to podman, so they can't run in CI — but their
    argv shapes (and the no-argv-leak secret guarantee) are worth pinning via a
    monkeypatched subprocess.run."""

    def test_exists_uses_inspect(self, monkeypatch):
        import subprocess

        calls: list[list[str]] = []

        class _R:
            returncode = 0

        monkeypatch.setattr(subprocess, "run", lambda argv, **kw: calls.append(argv) or _R())
        ops = SystemContainerOps()
        assert ops.image_exists("img:1") is True
        assert calls[-1] == ["podman", "image", "inspect", "img:1"]
        ops.container_exists("c")
        assert calls[-1] == ["podman", "container", "inspect", "c"]
        ops.volume_exists("v")
        assert calls[-1] == ["podman", "volume", "inspect", "v"]
        ops.secret_exists("s")
        assert calls[-1] == ["podman", "secret", "inspect", "s"]

    def test_write_secret_feeds_stdin_never_argv(self, monkeypatch):
        import subprocess

        captured: dict = {}

        class _R:
            returncode = 0
            stderr = b""

        def fake_run(argv, **kw):
            captured["argv"] = argv
            captured["input"] = kw.get("input")
            return _R()

        monkeypatch.setattr(subprocess, "run", fake_run)
        SystemContainerOps().write_secret("pinky-x-key", "s3cret")
        assert captured["argv"] == ["podman", "secret", "create", "pinky-x-key", "-"]
        assert captured["input"] == b"s3cret"
        assert "s3cret" not in " ".join(captured["argv"])


class TestContainerRuntimeGate:
    """The PINKY_CONTAINER_RUNTIME opt-in gate: container stays fail-closed by
    default and only get_provisioner-flips to a real ContainerProvisioner when
    an operator sets the env on the host."""

    def test_fail_closed_when_gate_off(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        with pytest.raises(NotImplementedError) as exc:
            get_provisioner("container")
        assert "not enabled" in str(exc.value)
        assert "PINKY_CONTAINER_RUNTIME" in str(exc.value)

    def test_returns_container_provisioner_when_gate_on(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        p = get_provisioner("container")
        assert isinstance(p, ContainerProvisioner)
        assert p.mode == "container"
        assert p._binary == "podman"

    def test_docker_binary_selected(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "docker")
        assert get_provisioner("container")._binary == "docker"

    def test_truthy_value_defaults_to_podman(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "1")
        assert get_provisioner("container")._binary == "podman"

    def test_signing_key_provider_is_threaded(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        p = get_provisioner("container", signing_key_provider=lambda n: f"k-{n}")
        assert p._signing_key_provider("x") == "k-x"

    def test_local_and_unix_user_unaffected_by_gate(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        assert isinstance(get_provisioner("local"), LocalProvisioner)
        with pytest.raises(NotImplementedError):
            get_provisioner("unix_user")  # still fail-closed; its own increment


class TestContainerStartStop:
    def test_start_command_shape(self, container_agent):
        ops = RecordingContainerOps()
        _cprov(ops).start(container_agent)
        assert ops.commands == [["podman", "start", "pinky-tenant"]]

    def test_stop_command_shape(self, container_agent):
        ops = RecordingContainerOps()
        _cprov(ops).stop(container_agent)
        assert ops.commands == [["podman", "stop", "pinky-tenant"]]

    def test_ensure_started_provisions_then_starts_when_absent(self, container_agent):
        ops = RecordingContainerOps()
        _cprov(ops).ensure_started(container_agent)
        assert any(c[:3] == ["podman", "volume", "create"] for c in ops.commands)
        assert any(len(c) > 3 and c[1] == "create" and c[3] == "pinky-tenant" for c in ops.commands)
        assert ops.commands[-1] == ["podman", "start", "pinky-tenant"]  # start is last

    def test_ensure_started_only_starts_when_already_provisioned(self, container_agent):
        ops = RecordingContainerOps(
            volumes={"pinky-tenant-home"},
            secrets={"pinky-tenant-key"},
            containers={"pinky-tenant"},
        )
        _cprov(ops).ensure_started(container_agent)
        assert ops.commands == [["podman", "start", "pinky-tenant"]]  # no re-provision

    def test_ensure_started_raises_on_provision_failure(self, container_agent):
        ops = RecordingContainerOps()
        p = ContainerProvisioner(
            ops=ops, image_provider=lambda a: "", signing_key_provider=lambda n: "k"
        )
        with pytest.raises(ProvisionError):
            p.ensure_started(container_agent)  # no image → provision fails → raises


class TestContainerDefaultImageProvider:
    """With no image_provider injected, ContainerProvisioner resolves the image
    from the agent's persisted ``container_image`` field (activation prep)."""

    def test_reads_agent_container_image(self):
        ops = RecordingContainerOps()
        agent = Agent(
            name="tenant", model="opus", isolated=True,
            isolation_mode="container", container_image="myco/agent:1",
        )
        result = ContainerProvisioner(ops=ops, signing_key_provider=lambda n: "k").provision(agent)
        assert result.ok is True
        assert ["podman", "pull", "myco/agent:1"] in ops.commands

    def test_fails_closed_when_container_image_unset(self):
        ops = RecordingContainerOps()
        agent = Agent(name="tenant", model="opus", isolation_mode="container")  # no image
        result = ContainerProvisioner(ops=ops, signing_key_provider=lambda n: "k").provision(agent)
        assert result.ok is False
        assert "container_image" in result.message
        assert ops.commands == []
