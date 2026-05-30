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
    AgentProvisioner,
    LocalProvisioner,
    ProvisionResult,
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

    def test_unix_user_not_yet_implemented(self):
        # Fail-closed: the recognized-but-unbuilt path raises rather than
        # silently handing back a no-op LocalProvisioner.
        with pytest.raises(NotImplementedError) as exc:
            get_provisioner("unix_user")
        assert "inc3b" in str(exc.value)

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
