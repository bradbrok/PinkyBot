"""Regression tests for agent-registry sweep fixes.

Covers: provider_key redaction in serialized output, null/absent-secret
"unchanged" vs explicit-empty "clear" semantics on update paths,
working_dir update invariants, token_ref-aware list_all_tokens,
_cron_next_run/scheduler agreement, hook command quoting, and
approve_user display_name preservation.
"""

from __future__ import annotations

import datetime as dt
import os
import shlex
import tempfile
import time
import zoneinfo
from pathlib import Path

import pytest

import pinky_daemon.routes.providers as providers_routes
from pinky_daemon.agent_registry import AgentRegistry, _cron_next_run
from pinky_daemon.scheduler import cron_matches


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    yield r
    r.close()
    os.unlink(path)


class TestProviderKeyRedaction:
    def test_to_dict_exposes_boolean_not_raw_key(self, registry, tmp_path):
        agent = registry.register(
            "keyed", working_dir=str(tmp_path / "ws"), provider_key="sk-secret"
        )
        d = agent.to_dict()
        assert "provider_key" not in d
        assert d["provider_key_set"] is True

        bare = registry.register("bare", working_dir=str(tmp_path / "ws2"))
        assert bare.to_dict()["provider_key_set"] is False

    def test_update_with_empty_provider_key_is_unchanged(self, registry, tmp_path):
        registry.register(
            "keyed", working_dir=str(tmp_path / "ws"), provider_key="sk-secret"
        )
        registry.register("keyed", provider_key="")
        assert registry.get("keyed").provider_key == "sk-secret"

        registry.register("keyed", provider_key="sk-new")
        assert registry.get("keyed").provider_key == "sk-new"

    def test_clear_provider_key_flag_clears(self, registry, tmp_path):
        registry.register(
            "keyed", working_dir=str(tmp_path / "ws"), provider_key="sk-secret"
        )
        registry.register("keyed", clear_provider_key=True)
        agent = registry.get("keyed")
        assert agent.provider_key == ""
        assert agent.to_dict()["provider_key_set"] is False

    async def test_provider_routes_redact_and_preserve_key(self, registry):
        providers_routes.set_dependencies(agents=registry)
        created = await providers_routes.create_provider(
            {"name": "p1", "provider_url": "https://x.example", "provider_key": "sk-glob"}
        )
        assert "provider_key" not in created
        assert created["provider_key_set"] is True

        listed = await providers_routes.list_providers()
        assert all("provider_key" not in p for p in listed)

        updated = await providers_routes.update_provider(
            created["id"], {"provider_url": "https://y.example", "provider_key": None}
        )
        assert updated["provider_key_set"] is True
        row = registry._db.execute(
            "SELECT provider_key FROM providers WHERE id=?", (created["id"],)
        ).fetchone()
        assert row[0] == "sk-glob"

    async def test_provider_route_explicit_empty_key_clears(self, registry):
        providers_routes.set_dependencies(agents=registry)
        created = await providers_routes.create_provider(
            {"name": "p2", "provider_url": "https://x.example", "provider_key": "sk-glob"}
        )
        assert created["provider_key_set"] is True

        updated = await providers_routes.update_provider(
            created["id"], {"name": "p2-renamed"}
        )
        assert updated["provider_key_set"] is True

        cleared = await providers_routes.update_provider(
            created["id"], {"provider_key": ""}
        )
        assert cleared["provider_key_set"] is False
        row = registry._db.execute(
            "SELECT provider_key FROM providers WHERE id=?", (created["id"],)
        ).fetchone()
        assert row[0] == ""


class TestAgentProviderEndpoint:
    def test_null_absent_unchanged_explicit_empty_clears(self, tmp_path):
        from fastapi.testclient import TestClient

        from pinky_daemon.api import create_api

        app = create_api(
            max_sessions=5,
            default_working_dir=str(tmp_path),
            db_path=str(tmp_path / "test.db"),
        )
        with TestClient(app) as client:
            client.post("/agents", json={"name": "prov", "model": "sonnet"})
            agents = app.state.agents

            r = client.put("/agents/prov/provider", json={"provider_key": "sk-secret"})
            assert r.status_code == 200, r.text
            assert agents.get("prov").provider_key == "sk-secret"

            r = client.put("/agents/prov/provider", json={"provider_key": None})
            assert r.status_code == 200, r.text
            assert agents.get("prov").provider_key == "sk-secret"

            r = client.put(
                "/agents/prov/provider", json={"provider_url": "https://x.example"}
            )
            assert r.status_code == 200, r.text
            assert agents.get("prov").provider_key == "sk-secret"

            r = client.put("/agents/prov/provider", json={"provider_key": ""})
            assert r.status_code == 200, r.text
            assert agents.get("prov").provider_key == ""
            assert agents.get("prov").to_dict()["provider_key_set"] is False


class TestWorkingDirUpdate:
    def test_empty_working_dir_on_update_is_unchanged(self, registry, tmp_path):
        ws = tmp_path / "ws"
        registry.register("wd", working_dir=str(ws))
        registry.register("wd", working_dir="", model="sonnet")
        agent = registry.get("wd")
        assert agent.working_dir == str(ws)
        assert agent.model == "sonnet"

    def test_relative_working_dir_on_update_is_absolutized(
        self, registry, tmp_path, monkeypatch
    ):
        registry.register("wd", working_dir=str(tmp_path / "ws"))
        monkeypatch.chdir(tmp_path)
        registry.register("wd", working_dir="relws")
        agent = registry.get("wd")
        assert Path(agent.working_dir).is_absolute()
        assert agent.working_dir == str((tmp_path / "relws").resolve())

    def test_new_working_dir_on_update_gets_workspace_init(self, registry, tmp_path):
        registry.register("wd", working_dir=str(tmp_path / "ws"))
        new_dir = tmp_path / "ws2"
        registry.register("wd", working_dir=str(new_dir))
        assert (new_dir / "data").is_dir()
        assert (new_dir / "output").is_dir()
        assert (new_dir / ".claude" / "settings.json").is_file()


class TestListAllTokens:
    def test_token_ref_counts_as_set(self, registry, tmp_path):
        registry.register("reffed", working_dir=str(tmp_path / "a"))
        registry.register("inline", working_dir=str(tmp_path / "b"))
        registry.register("none", working_dir=str(tmp_path / "c"))
        registry.set_token("reffed", "telegram", "", token_ref="global-tok-1")
        registry.set_token("inline", "telegram", "123:abc")
        registry.set_token("none", "telegram", "")

        by_agent = {t["agent_name"]: t for t in registry.list_all_tokens()}
        assert by_agent["reffed"]["token_set"] is True
        assert by_agent["inline"]["token_set"] is True
        assert by_agent["none"]["token_set"] is False


class TestCronNextRun:
    def test_stepped_dom_agrees_with_scheduler(self):
        cron = "0 0 */10 * *"
        ts = _cron_next_run(cron, "UTC")
        assert ts is not None
        when = dt.datetime.fromtimestamp(ts, tz=zoneinfo.ZoneInfo("UTC"))
        assert cron_matches(cron, when)
        assert when.day in {10, 20, 30}

    def test_range_with_step_agrees_with_scheduler(self):
        # Range-with-step support ('1-5/2') depends on the scheduler matcher.
        # Whatever it says, next_run must agree: either no next run at all, or
        # a timestamp the scheduler would actually fire on.
        cron = "0 0 * * 1-5/2"
        ts = _cron_next_run(cron, "UTC")
        if ts is not None:
            when = dt.datetime.fromtimestamp(ts, tz=zoneinfo.ZoneInfo("UTC"))
            assert cron_matches(cron, when)

    def test_never_matching_cron_returns_none_fast(self):
        start = time.monotonic()
        assert _cron_next_run("0 0 30 2 *", "UTC") is None  # Feb 30
        assert _cron_next_run("0 0 * * 7", "UTC") is None  # dow out of range
        assert time.monotonic() - start < 1.0

    def test_simple_cron_still_resolves(self):
        ts = _cron_next_run("*/5 * * * *", "UTC")
        assert ts is not None
        when = dt.datetime.fromtimestamp(ts, tz=zoneinfo.ZoneInfo("UTC"))
        assert cron_matches("*/5 * * * *", when)
        assert ts > time.time()


class TestHookCommandQuoting:
    def test_paths_with_spaces_are_quoted(self, tmp_path):
        work_dir = tmp_path / "dir with space"
        AgentRegistry._init_workspace(work_dir, agent_name="spacey")
        import json

        settings = json.loads((work_dir / ".claude" / "settings.json").read_text())
        commands = [
            h["command"]
            for buckets in settings["hooks"].values()
            for bucket in buckets
            for h in bucket["hooks"]
        ]
        assert commands
        for cmd in commands:
            tokens = shlex.split(cmd)
            assert tokens[0] == "python3"
            script = Path(tokens[1])
            assert script.is_file(), f"hook script path mangled in: {cmd}"


class TestApproveUserDisplayName:
    def test_blank_reapproval_preserves_display_name(self, registry, tmp_path):
        registry.register("appr", working_dir=str(tmp_path / "ws"))
        registry.approve_user("appr", "12345", "Alice", "owner")
        user = registry.approve_user("appr", "12345", "", "api")
        assert user.display_name == "Alice"
        assert user.approved_by == "api"

        user = registry.approve_user("appr", "12345", "Bob", "owner")
        assert user.display_name == "Bob"


class TestRuntimeModelSync:
    @pytest.mark.asyncio
    async def test_sync_adds_only_complete_fallbacks_and_preserves_operator_rates(
        self,
        registry,
        monkeypatch,
    ):
        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": [
                        {
                            "id": "claude-opus-4-8",
                            "display_name": "Existing",
                        },
                        {
                            "id": "claude-opus-4-1",
                            "display_name": "Complete fallback",
                        },
                        {
                            "id": "claude-unpriced-new",
                            "display_name": "Needs pricing",
                        },
                    ]
                }

        class Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, *_args, **_kwargs):
                return Response()

        columns = {
            row[1]
            for row in registry._db.execute("PRAGMA table_info(models)").fetchall()
        }
        for field in ("cache_write_5m_price", "cache_write_1h_price"):
            if field not in columns:
                registry._db.execute(f"ALTER TABLE models ADD COLUMN {field} REAL")
        registry._db.execute(
            "UPDATE models SET input_price=91.0, output_price=92.0, "
            "cached_input_price=93.0, cache_write_5m_price=94.0, "
            "cache_write_1h_price=95.0 WHERE id='anthropic/claude-opus-4-8'"
        )
        registry._db.commit()

        monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
        monkeypatch.setattr("httpx.AsyncClient", Client)
        providers_routes.set_dependencies(agents=registry)

        result = await providers_routes.sync_models()

        assert result == {
            "synced": 1,
            "new_models": ["claude-opus-4-1"],
            "requires_pricing": ["claude-unpriced-new"],
        }
        complete = registry.get_model("claude-opus-4-1")
        assert complete is not None
        assert (
            complete["input_price"],
            complete["output_price"],
            complete["cached_input_price"],
            complete["cache_write_5m_price"],
            complete["cache_write_1h_price"],
        ) == (15.0, 75.0, 1.5, 18.75, 30.0)
        assert registry.get_model("claude-unpriced-new") is None

        existing = registry.get_model("claude-opus-4-8")
        assert existing is not None
        assert (
            existing["input_price"],
            existing["output_price"],
            existing["cached_input_price"],
            existing["cache_write_5m_price"],
            existing["cache_write_1h_price"],
        ) == (91.0, 92.0, 93.0, 94.0, 95.0)
