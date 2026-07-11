"""Tests for agent registry."""

from __future__ import annotations

import os
import tempfile

import pytest

from pinky_daemon.agent_registry import AgentContext, AgentRegistry


@pytest.fixture
def registry():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    r = AgentRegistry(db_path=path)
    yield r
    r.close()
    os.unlink(path)


class TestOwnerNotificationDestinations:
    def test_explicit_migration_seeds_legacy_primary_chat_id(self, registry):
        registry.set_primary_user("6770805286", "Brad")

        destinations = registry.migrate_primary_user_notification_destination(
            platform="telegram",
            account_id="primary-telegram-bot",
        )

        assert destinations == [
            {
                "platform": "telegram",
                "account_id": "primary-telegram-bot",
                "conversation_id": "6770805286",
            }
        ]

    def test_migration_never_infers_platform_or_account(self, registry):
        registry.set_primary_user("6770805286", "Brad")

        with pytest.raises(ValueError, match="requires platform"):
            registry.migrate_primary_user_notification_destination(
                platform="", account_id="",
            )

        assert registry.get_owner_notification_destinations() == []

    def test_primary_and_ordered_fallbacks_round_trip(self, registry):
        destinations = registry.set_owner_notification_destinations([
            {
                "platform": "telegram",
                "account_id": "tg-owner",
                "conversation_id": "6770805286",
            },
            {
                "platform": "slack",
                "team_id": "T_FALLBACK",
                "conversation_id": "D_FALLBACK",
            },
        ])

        assert destinations[1]["account_id"] == "T_FALLBACK"
        assert registry.get_owner_notification_destinations() == destinations


class TestSigningKeys:
    """Per-agent signing keys (#623)."""

    def test_register_generates_signing_key(self, registry):
        registry.register("oleg", model="opus")
        key = registry.get_signing_key("oleg")
        assert key
        assert len(key) >= 32  # 256-bit urlsafe token

    def test_get_or_create_is_idempotent(self, registry):
        registry.register("leo", model="opus")
        k1 = registry.get_or_create_signing_key("leo")
        k2 = registry.get_or_create_signing_key("leo")
        assert k1 == k2

    def test_get_signing_key_unknown_agent_is_none(self, registry):
        assert registry.get_signing_key("ghost") is None

    def test_keys_are_distinct_per_agent(self, registry):
        registry.register("a", model="opus")
        registry.register("b", model="opus")
        assert registry.get_signing_key("a") != registry.get_signing_key("b")

    def test_reregister_preserves_signing_key(self, registry):
        registry.register("kai", model="opus")
        before = registry.get_signing_key("kai")
        registry.register("kai", model="sonnet")  # update path
        assert registry.get_signing_key("kai") == before

    def test_signing_key_not_in_to_dict(self, registry):
        agent = registry.register("nova", model="opus")
        assert "signing_key" not in agent.to_dict()

    def test_delete_purges_signing_key_no_stale_on_recreate(self, registry):
        """#623 pre-cutover hardening: hard delete() purges agent_signing_keys
        so a re-registered name mints a FRESH key, not the stale one."""
        registry.register("alice", model="opus")
        first_key = registry.get_signing_key("alice")
        assert first_key

        # Split the side-effecting call out of the assert (CodeQL: asserts are
        # stripped under python -O, which would skip the delete).
        deleted = registry.delete("alice")
        assert deleted is True
        # Key is gone — not orphaned in agent_signing_keys.
        assert registry.get_signing_key("alice") is None

        # Re-register the same name → brand-new key, not the old one.
        registry.register("alice", model="opus")
        second_key = registry.get_signing_key("alice")
        assert second_key
        assert second_key != first_key

    def test_hook_templates_prefer_per_agent_key(self, tmp_path):
        """#623 increment 2: every signed tmux hook prefers PINKY_AGENT_KEY
        over the global PINKY_SESSION_SECRET, so hooks running in an agent's
        tmux session sign with a non-forgeable identity (daemon dual-accepts).
        """
        from pinky_daemon import agent_registry as ar

        prefer_line = (
            'secret = os.environ.get("PINKY_AGENT_KEY", "").strip() '
            'or os.environ.get("PINKY_SESSION_SECRET", "").strip()'
        )
        old_line = 'secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()\n'

        # The 5 named hook-source templates.
        sources = [
            ar._tmux_wake_hook_source("dymok"),
            ar._tmux_pre_tool_hook_source("dymok"),
            ar._tmux_post_tool_hook_source("dymok"),
            ar._tmux_stop_failure_hook_source("dymok"),
            ar._tmux_session_start_hook_source("dymok"),
        ]
        # The 6th template is inline in _setup_hooks (status hooks); cover it
        # through the real write path so a future edit can't silently revert it.
        AgentRegistry._setup_hooks(tmp_path, "dymok")
        claude_dir = tmp_path / ".claude"
        sources.append((claude_dir / "hook_idle.py").read_text())
        sources.append((claude_dir / "hook_working.py").read_text())

        for src in sources:
            assert prefer_line in src
            # The old single-source line must be gone (no global-only signer).
            assert old_line not in src
            # Agent name still bound into the signed request.
            assert 'x-pinky-agent' in src

    def test_hook_templates_honor_daemon_url_env(self, tmp_path):
        """#638: every generated tmux hook script resolves the daemon URL from
        PINKY_DAEMON_URL (default http://localhost:8888) instead of hardcoding
        it inline — a hardcoded localhost is dead inside a container netns;
        container sessions inject PINKY_DAEMON_URL=http://host.containers.internal:8888.
        """
        from pinky_daemon import agent_registry as ar

        env_line = 'os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888")'

        # The 5 named hook-source templates.
        sources = [
            ar._tmux_wake_hook_source("dymok"),
            ar._tmux_pre_tool_hook_source("dymok"),
            ar._tmux_post_tool_hook_source("dymok"),
            ar._tmux_stop_failure_hook_source("dymok"),
            ar._tmux_session_start_hook_source("dymok"),
        ]
        # The 6th template is inline in _setup_hooks (status hooks); cover it
        # through the real write path so a future edit can't silently revert it.
        AgentRegistry._setup_hooks(tmp_path, "dymok")
        claude_dir = tmp_path / ".claude"
        sources.append((claude_dir / "hook_idle.py").read_text())
        sources.append((claude_dir / "hook_working.py").read_text())

        for src in sources:
            # Env-resolved URL with the loopback default still present.
            assert env_line in src
            # The old inline f-string shape (f"http://localhost:8888{path}")
            # must be gone — that URL can never be overridden at runtime.
            assert 'f"http://localhost:8888' not in src

    def test_stale_status_hooks_rewritten_in_place(self, tmp_path):
        """#638 review fix: hook_working.py / hook_idle.py were historically
        write-once, stranding fleet agents on stale sources (e.g. the
        hardcoded f"http://localhost:8888" that is dead inside a container
        netns). They are fully PinkyBot-managed, so _setup_hooks now rewrites
        them via _write_hook_if_changed whenever the on-disk content drifted
        from the current template."""
        claude_dir = tmp_path / ".claude"
        claude_dir.mkdir()
        stale = (
            "#!/usr/bin/env python3\n"
            "import urllib.request\n"
            'path = "/agents/dymok/status"\n'
            'req = urllib.request.Request(f"http://localhost:8888{path}")\n'
        )
        (claude_dir / "hook_working.py").write_text(stale)
        (claude_dir / "hook_idle.py").write_text(stale)

        AgentRegistry._setup_hooks(tmp_path, "dymok")

        for fname in ("hook_working.py", "hook_idle.py"):
            src = (claude_dir / fname).read_text()
            assert src != stale  # rewritten, not left alone
            # Current template: env-resolved daemon URL, no hardcoded inline
            # f-string that a container session could never override.
            assert "PINKY_DAEMON_URL" in src
            assert 'f"http://localhost:8888' not in src
        # The pair stays distinct — each posts its own status payload.
        assert '"status": "working"' in (claude_dir / "hook_working.py").read_text()
        assert '"status": "idle"' in (claude_dir / "hook_idle.py").read_text()

    def test_current_status_hooks_left_untouched(self, tmp_path):
        """The rewrite is content-gated: a second _setup_hooks run over
        already-current sources must not touch the files (no churn on every
        registration / workspace sync)."""
        AgentRegistry._setup_hooks(tmp_path, "dymok")
        claude_dir = tmp_path / ".claude"
        working = claude_dir / "hook_working.py"
        idle = claude_dir / "hook_idle.py"
        before = (working.read_text(), idle.read_text())
        before_mtimes = (working.stat().st_mtime_ns, idle.stat().st_mtime_ns)

        AgentRegistry._setup_hooks(tmp_path, "dymok")

        assert (working.read_text(), idle.read_text()) == before
        assert (working.stat().st_mtime_ns, idle.stat().st_mtime_ns) == before_mtimes

    def test_backfill_skips_malformed_name_without_bricking(self, registry):
        # A legacy/non-conforming agent name must not brick boot: the per-row
        # get_or_create -> _validate_agent_name raises, but backfill log+skips
        # and still keys the conforming agents.
        registry.register("good", model="opus")
        registry._db.execute(
            "INSERT INTO agents (name, model, created_at, updated_at) VALUES (?,?,?,?)",
            ("BAD NAME!", "opus", 1.0, 1.0),
        )
        registry._db.commit()
        # Must not raise even though "BAD NAME!" fails validation.
        registry._backfill_signing_keys()
        assert registry.get_signing_key("good")
        assert registry.get_signing_key("BAD NAME!") is None


class TestAgentCRUD:
    def test_register(self, registry):
        agent = registry.register("oleg", display_name="Oleg", model="opus")
        assert agent.name == "oleg"
        assert agent.display_name == "Oleg"
        assert agent.model == "opus"
        assert agent.runtime == "claude_sdk"
        assert agent.transport == "sdk"
        assert agent.enabled is True
        assert agent.created_at > 0

    def test_isolated_flag_round_trip(self, registry):
        """#149: the isolated flag persists through insert/get/to_dict, defaults
        off, and is settable via the update path."""
        # Default off.
        plain = registry.register("plain", model="opus")
        assert plain.isolated is False
        assert registry.get("plain").isolated is False
        assert registry.get("plain").to_dict()["isolated"] is False

        # Insert with isolated=True.
        iso = registry.register("tenant", model="opus", isolated=True)
        assert iso.isolated is True
        assert registry.get("tenant").isolated is True

        # Update path flips it on for an existing agent.
        registry.register("plain", isolated=True)
        assert registry.get("plain").isolated is True

    def test_isolation_mode_round_trip(self, registry):
        """#149 phase-3: isolation_mode persists through insert/get/to_dict,
        defaults to 'local', and is settable via the update path."""
        # Default 'local'.
        plain = registry.register("plain", model="opus")
        assert plain.isolation_mode == "local"
        assert registry.get("plain").isolation_mode == "local"
        assert registry.get("plain").to_dict()["isolation_mode"] == "local"

        # Insert with an explicit mode.
        iso = registry.register("tenant", model="opus", isolated=True,
                                isolation_mode="unix_user")
        assert iso.isolation_mode == "unix_user"
        assert registry.get("tenant").isolation_mode == "unix_user"
        assert registry.get("tenant").to_dict()["isolation_mode"] == "unix_user"

        # Update path changes it for an existing agent.
        registry.register("plain", isolation_mode="unix_user")
        assert registry.get("plain").isolation_mode == "unix_user"

        # isolation_mode is orthogonal to the isolated flag.
        assert registry.get("tenant").isolated is True
        assert registry.get("plain").isolated is False

    def test_register_with_full_config(self, registry, tmp_path):
        agent = registry.register(
            "leo",
            display_name="Leo",
            model="sonnet",
            soul="# Leo the Worker",
            system_prompt="You are a code worker.",
            working_dir=str(tmp_path / "workspace"),
            permission_mode="auto",
            allowed_tools=["Read", "Glob", "Grep", "Edit"],
            max_turns=50,
            timeout=600.0,
            parent="oleg",
            groups=["butter-team"],
            max_sessions=3,
            runtime="codex_cli",
            transport="sdk",
        )
        assert agent.model == "sonnet"
        assert agent.soul == "# Leo the Worker"
        assert agent.allowed_tools == ["Read", "Glob", "Grep", "Edit"]
        assert agent.parent == "oleg"
        assert agent.groups == ["butter-team"]
        assert agent.max_sessions == 3
        assert agent.runtime == "codex_cli"
        assert agent.transport == "sdk"

    def test_register_update(self, registry):
        registry.register("oleg", model="sonnet")
        agent = registry.register("oleg", model="opus")
        assert agent.model == "opus"

    def test_update_is_partial_and_preserves_unset_fields(self, registry):
        registry.register(
            "murzik",
            display_name="Murzik",
            model="gpt-5.6-sol",
            thinking_effort="high",
            runtime="codex_cli",
            transport="tmux",
            groups=["reviewers"],
            allowed_tools=["Read", "Grep"],
        )

        updated = registry.update("murzik", thinking_effort="xhigh")

        assert updated.thinking_effort == "xhigh"
        assert updated.display_name == "Murzik"
        assert updated.model == "gpt-5.6-sol"
        assert updated.runtime == "codex_cli"
        assert updated.transport == "tmux"
        assert updated.groups == ["reviewers"]
        assert updated.allowed_tools == ["Read", "Grep"]

    def test_update_never_creates_a_missing_agent(self, registry):
        with pytest.raises(KeyError, match="not found"):
            registry.update("ghost", thinking_effort="high")
        assert registry.get("ghost") is None

    def test_update_rejects_working_dir_path_mutation(self, registry, tmp_path):
        original = tmp_path / "original"
        registry.register("murzik", working_dir=str(original))

        with pytest.raises(ValueError, match="working_dir"):
            registry.update("murzik", working_dir=str(tmp_path / "other"))

        assert registry.get("murzik").working_dir == str(original)
        assert not (tmp_path / "other").exists()

    def test_get(self, registry):
        registry.register("test")
        agent = registry.get("test")
        assert agent is not None
        assert agent.name == "test"

    def test_get_missing(self, registry):
        assert registry.get("nope") is None

    def test_list(self, registry):
        registry.register("a")
        registry.register("b")
        registry.register("c")
        agents = registry.list()
        assert len(agents) == 3
        assert [a.name for a in agents] == ["a", "b", "c"]

    def test_list_by_parent(self, registry):
        registry.register("lead")
        registry.register("worker1", parent="lead")
        registry.register("worker2", parent="lead")
        registry.register("solo")
        children = registry.list(parent="lead")
        assert len(children) == 2

    def test_list_by_group(self, registry):
        registry.register("a", groups=["team-1"])
        registry.register("b", groups=["team-1", "team-2"])
        registry.register("c", groups=["team-2"])
        team1 = registry.list(group="team-1")
        assert len(team1) == 2

    def test_list_enabled_only(self, registry):
        registry.register("on", enabled=True)
        registry.register("off", enabled=False)
        active = registry.list(enabled_only=True)
        assert len(active) == 1

    def test_delete(self, registry):
        registry.register("doomed")
        assert registry.delete("doomed") is True
        assert registry.get("doomed") is None

    def test_delete_missing(self, registry):
        assert registry.delete("nope") is False

    def test_get_children(self, registry):
        registry.register("boss")
        registry.register("w1", parent="boss")
        registry.register("w2", parent="boss")
        children = registry.get_children("boss")
        assert len(children) == 2

    def test_hierarchy(self, registry):
        registry.register("boss")
        registry.register("lead", parent="boss")
        registry.register("worker", parent="lead")
        tree = registry.get_hierarchy("boss")
        assert tree["agent"]["name"] == "boss"
        assert len(tree["children"]) == 1
        assert tree["children"][0]["agent"]["name"] == "lead"
        assert len(tree["children"][0]["children"]) == 1

    def test_to_dict(self, registry):
        agent = registry.register("test", display_name="Test Agent", model="opus")
        d = agent.to_dict()
        assert d["name"] == "test"
        assert d["display_name"] == "Test Agent"
        assert d["model"] == "opus"
        assert d["runtime"] == "claude_sdk"
        assert d["transport"] == "sdk"
        assert d["enabled"] is True

    def test_runtime_column_exists_with_default(self, registry):
        columns = {
            row[1]: row
            for row in registry._db.execute("PRAGMA table_info(agents)").fetchall()
        }
        assert "runtime" in columns
        assert columns["runtime"][4] == "'claude_sdk'"

        agent = registry.register("runtime-default")
        assert agent.runtime == "claude_sdk"

    def test_transport_column_exists_with_default(self, registry):
        columns = {
            row[1]: row
            for row in registry._db.execute("PRAGMA table_info(agents)").fetchall()
        }
        assert "transport" in columns
        assert columns["transport"][4] == "'sdk'"

        agent = registry.register("transport-default")
        assert agent.transport == "sdk"

    def test_first_register_with_all_kwargs_persists_every_field(self, registry):
        # Regression: pre-#358 INSERT omitted provider_url/key/model/ref,
        # thinking_effort, runtime-adjacent fields.
        # A single register() call (no follow-up UPDATE) must persist all of them.
        agent = registry.register(
            "full-kwargs-agent",
            provider_url="https://api.openai.com/v1",
            provider_key="sk-test-key",
            provider_model="gpt-5",
            provider_ref="some-provider-id",
            thinking_effort="high",
            runtime="claude_sdk",
            transport="tmux",
        )
        # Verify via the returned object (built from INSERT path)
        assert agent.provider_url == "https://api.openai.com/v1"
        assert agent.provider_key == "sk-test-key"
        assert agent.provider_model == "gpt-5"
        assert agent.provider_ref == "some-provider-id"
        assert agent.thinking_effort == "high"
        assert agent.runtime == "claude_sdk"
        assert agent.transport == "tmux"

        # Verify via a fresh get() to confirm DB round-trip, not just in-memory object
        fetched = registry.get("full-kwargs-agent")
        assert fetched.provider_url == "https://api.openai.com/v1"
        assert fetched.provider_key == "sk-test-key"
        assert fetched.provider_model == "gpt-5"
        assert fetched.provider_ref == "some-provider-id"
        assert fetched.thinking_effort == "high"
        assert fetched.runtime == "claude_sdk"
        assert fetched.transport == "tmux"

    def test_runtime_codex_cli_backfill_is_one_shot_and_idempotent(self, registry):
        registry.register(
            "legacy-codex",
            provider_url="codex_cli",
            runtime="claude_sdk",
            transport="tmux",
        )
        registry.register("explicit-claude", provider_url="codex_cli", runtime="claude_sdk")
        registry.register("already-codex", provider_url="codex_cli", runtime="codex_cli")
        registry.register("opencode-agent", provider_url="codex_cli", runtime="opencode")

        marker = "migration:agents_runtime_codex_cli_backfill"
        registry.set_setting(marker, "")
        registry._backfill_runtime_from_provider_url()
        assert registry.get("legacy-codex").runtime == "codex_cli"
        assert registry.get("legacy-codex").transport == "sdk"
        assert registry.get("explicit-claude").runtime == "codex_cli"
        assert registry.get("already-codex").runtime == "codex_cli"
        assert registry.get("opencode-agent").runtime == "opencode"
        assert registry.get_setting(marker) == "1"

        registry.set_setting(marker, "")
        registry._backfill_runtime_from_provider_url()
        assert registry.get("legacy-codex").runtime == "codex_cli"
        assert registry.get("already-codex").runtime == "codex_cli"
        assert registry.get("opencode-agent").runtime == "opencode"

        registry.register("explicit-claude", runtime="claude_sdk")
        registry._backfill_runtime_from_provider_url()
        assert registry.get("explicit-claude").runtime == "claude_sdk"

    def test_warn_codex_runtime_mismatch_after_one_shot_backfill(self, registry, capsys):
        registry.register("late-codex", provider_url="codex_cli", runtime="claude_sdk")

        registry._warn_codex_runtime_mismatches()

        err = capsys.readouterr().err
        assert "warning" in err
        assert "late-codex" in err
        assert "runtime=claude_sdk" in err

    def test_stamp_last_seen_updates_column(self, registry):
        registry.register("seen")
        agent = registry.get("seen")
        assert agent.last_seen_at == 0.0

        registry.stamp_last_seen("seen", ts=1234567.0)
        updated = registry.get("seen")
        assert updated.last_seen_at == 1234567.0

    def test_stamp_last_seen_default_ts_is_now(self, registry):
        import time as _time
        registry.register("seen2")
        before = _time.time()
        registry.stamp_last_seen("seen2")
        after = _time.time()
        updated = registry.get("seen2")
        assert before <= updated.last_seen_at <= after

    def test_stamp_last_seen_missing_agent_is_noop(self, registry):
        # Should not raise — just affects zero rows.
        registry.stamp_last_seen("ghost", ts=42.0)


class TestMainAgentAutoAssign:
    """First-run convenience: a fresh install must not silently end up with no
    main agent (no main_agent ⇒ daemon starts no autonomy loop ⇒ created agent
    never wakes). Creating the first enabled agent adopts it as main."""

    def test_first_agent_becomes_main(self, registry):
        assert registry.get_main_agent() == ""
        registry.register("alpha")
        assert registry.get_main_agent() == "alpha"

    def test_second_agent_does_not_override_main(self, registry):
        registry.register("alpha")
        registry.register("beta")
        assert registry.get_main_agent() == "alpha"

    def test_explicit_main_is_not_overridden(self, registry):
        registry.register("alpha")
        registry.set_main_agent("alpha")
        registry.register("beta")
        assert registry.get_main_agent() == "alpha"

    def test_disabled_first_agent_not_auto_assigned(self, registry):
        registry.register("alpha", enabled=False)
        assert registry.get_main_agent() == ""

    def test_updating_existing_agent_does_not_assign_main(self, registry):
        # Seed main, clear it, then re-register (update path) — update must not
        # auto-assign; only creation does.
        registry.register("alpha")
        registry.set_setting("main_agent", "")
        registry.register("alpha", model="opus")
        assert registry.get_main_agent() == ""


class TestAgentNameValidation:
    """Path-traversal defense: agent names must match the safe-char allowlist.

    Names flow into filesystem paths (working_dir, .claude/ hook scripts,
    settings.json) downstream. Anything outside ``^[a-z0-9][a-z0-9_-]{0,62}$``
    is rejected at ``register()``. CodeQL flagged the path-construction
    sites in agent_registry on PR #510; this validator is the source-side
    sanitizer that closes those alerts.
    """

    @pytest.mark.parametrize(
        "bad_name",
        [
            "../etc/passwd",         # explicit traversal
            "..",                    # implicit traversal
            "foo/bar",               # path separator
            "foo\\bar",              # windows path separator
            "foo bar",               # whitespace
            "Foo",                   # uppercase (path collision on case-insensitive FS)
            "foo.bar",               # dot
            "foo@host",              # at-sign
            "foo$bar",               # shell metachar
            "foo`cmd`",              # shell backticks
            "foo;rm",                # shell separator
            "foo|pipe",              # shell pipe
            "-leading-hyphen",       # leads with hyphen (arg-injection in CLI hooks)
            "_leading-underscore",   # leads with underscore (reserved-shape)
            "",                      # empty
            "a" * 64,                # over length
            "café",                  # non-ASCII
            "foo\x00bar",            # null byte
        ],
    )
    def test_register_rejects_unsafe_name(self, registry, bad_name):
        with pytest.raises(ValueError, match="invalid agent name"):
            registry.register(bad_name)

    @pytest.mark.parametrize(
        "good_name",
        [
            "a",                     # single char minimum
            "dymok",                 # the new tmux-native agent
            "barsik",                # canonical existing
            "agent_one",             # underscore
            "agent-one",             # hyphen
            "0bot",                  # leading digit OK
            "x" * 63,                # max length
            "a1b2c3-d4_e5",          # mixed
        ],
    )
    def test_register_accepts_safe_name(self, registry, good_name):
        agent = registry.register(good_name)
        assert agent.name == good_name


class TestDirectives:
    def test_add_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Always write tests")
        assert d.directive == "Always write tests"
        assert d.active is True
        assert d.id > 0

    def test_add_with_priority(self, registry):
        registry.register("oleg")
        registry.add_directive("oleg", "Low priority", priority=0)
        registry.add_directive("oleg", "High priority", priority=10)
        directives = registry.get_directives("oleg")
        assert directives[0].directive == "High priority"
        assert directives[1].directive == "Low priority"

    def test_get_directives(self, registry):
        registry.register("oleg")
        registry.add_directive("oleg", "Rule 1")
        registry.add_directive("oleg", "Rule 2")
        directives = registry.get_directives("oleg")
        assert len(directives) == 2

    def test_get_directives_active_only(self, registry):
        registry.register("oleg")
        _d1 = registry.add_directive("oleg", "Active")
        d2 = registry.add_directive("oleg", "Inactive")
        registry.toggle_directive(d2.id, False)
        active = registry.get_directives("oleg", active_only=True)
        assert len(active) == 1
        all_d = registry.get_directives("oleg", active_only=False)
        assert len(all_d) == 2

    def test_remove_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Temp rule")
        assert registry.remove_directive(d.id) is True
        assert len(registry.get_directives("oleg")) == 0

    def test_toggle_directive(self, registry):
        registry.register("oleg")
        d = registry.add_directive("oleg", "Toggle me")
        registry.toggle_directive(d.id, False)
        directives = registry.get_directives("oleg", active_only=False)
        assert directives[0].active is False

    def test_build_system_prompt(self, registry):
        registry.register("oleg", soul="# Oleg Soul", system_prompt="Be helpful")
        registry.add_directive("oleg", "Write tests for every PR", priority=10)
        registry.add_directive("oleg", "Use Python 3.11+", priority=5)
        prompt = registry.build_system_prompt("oleg")
        assert "# Oleg Soul" in prompt
        assert "Be helpful" in prompt
        assert "Write tests for every PR" in prompt
        assert "Use Python 3.11+" in prompt

    def test_build_system_prompt_missing_agent(self, registry):
        assert registry.build_system_prompt("nope") == ""

    def test_cascade_delete(self, registry):
        registry.register("temp")
        registry.add_directive("temp", "Will be deleted")
        registry.delete("temp")
        # Directives should be gone too
        assert len(registry.get_directives("temp")) == 0


class TestTokens:
    def test_set_token(self, registry):
        registry.register("oleg")
        token = registry.set_token("oleg", "telegram", "bot123:secret")
        assert token.agent_name == "oleg"
        assert token.platform == "telegram"
        assert token.token_set is True
        assert token.enabled is True

    def test_token_not_exposed(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "super-secret")
        token = registry.get_token("oleg", "telegram")
        d = token.to_dict()
        assert "super-secret" not in str(d)
        assert d["token_set"] is True

    def test_get_raw_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "bot123:raw")
        assert registry.get_raw_token("oleg", "telegram") == "bot123:raw"

    def test_get_raw_token_missing(self, registry):
        assert registry.get_raw_token("nope", "telegram") == ""

    def test_update_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "old-token")
        registry.set_token("oleg", "telegram", "new-token")
        assert registry.get_raw_token("oleg", "telegram") == "new-token"

    def test_list_tokens(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1")
        registry.set_token("oleg", "discord", "d1")
        tokens = registry.list_tokens("oleg")
        assert len(tokens) == 2
        platforms = {t.platform for t in tokens}
        assert "telegram" in platforms
        assert "discord" in platforms

    def test_remove_token(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1")
        assert registry.remove_token("oleg", "telegram") is True
        assert registry.get_token("oleg", "telegram") is None

    def test_token_with_settings(self, registry):
        registry.register("oleg")
        registry.set_token("oleg", "telegram", "t1", settings={"allowed_chats": ["123"]})
        token = registry.get_token("oleg", "telegram")
        assert token.settings == {"allowed_chats": ["123"]}

    def test_cascade_delete_tokens(self, registry):
        registry.register("temp")
        registry.set_token("temp", "telegram", "t1")
        registry.delete("temp")
        assert registry.get_token("temp", "telegram") is None


class TestOwnerProfile:
    def test_get_defaults(self, registry):
        profile = registry.get_owner_profile()
        assert profile["name"] == ""
        assert profile["pronouns"] == ""
        assert profile["role"] == ""
        assert profile["comm_style"] == ""
        assert profile["languages"] == ""
        assert profile["code_word"] == ""
        # timezone falls back to detected/UTC
        assert profile["timezone"] != ""

    def test_set_and_get(self, registry):
        result = registry.set_owner_profile({
            "name": "Brad",
            "role": "solo dev building PinkyBot",
            "comm_style": "terse, direct",
            "code_word": "pineapple",
        })
        assert result["name"] == "Brad"
        assert result["role"] == "solo dev building PinkyBot"
        assert result["comm_style"] == "terse, direct"
        assert result["code_word"] == "pineapple"

        # Verify via separate get
        profile = registry.get_owner_profile()
        assert profile["name"] == "Brad"
        assert profile["code_word"] == "pineapple"

    def test_partial_update(self, registry):
        registry.set_owner_profile({"name": "Brad"})
        registry.set_owner_profile({"pronouns": "he/him"})
        profile = registry.get_owner_profile()
        assert profile["name"] == "Brad"
        assert profile["pronouns"] == "he/him"

    def test_ignores_unknown_fields(self, registry):
        result = registry.set_owner_profile({
            "name": "Brad",
            "favorite_color": "blue",
        })
        assert result["name"] == "Brad"
        assert "favorite_color" not in result

    def test_build_system_prompt_includes_profile(self, registry):
        registry.register("oleg", soul="# Oleg")
        registry.set_owner_profile({
            "name": "Brad",
            "timezone": "America/Denver",
            "code_word": "pineapple",
        })
        prompt = registry.build_system_prompt("oleg")
        assert "## Users" in prompt
        assert "### Owner" in prompt
        assert "Brad" in prompt
        assert "America/Denver" in prompt
        assert "pineapple" in prompt
        assert "never share" in prompt.lower() or "Never share" in prompt

    def test_build_system_prompt_omits_empty_profile(self, registry):
        registry.register("oleg", soul="# Oleg")
        prompt = registry.build_system_prompt("oleg")
        # timezone always has a fallback, so Users section will appear
        # unless we clear it — but with only timezone, it should still show
        # Let's just check the structure is sensible
        assert "# Oleg" in prompt
        assert "## Memory" in prompt


class TestAgentContextToPrompt:
    """#591 — ``to_prompt(resume_mode=True)`` gates manifest rendering for
    warm ``--continue`` resumes. The bulk manifest is redundant when the
    prior conversation is already loaded; only the ``wake_action``
    directive must survive because it represents intent set by the prior
    session ("do this FIRST"), not history.
    """

    def _full_ctx(self) -> AgentContext:
        return AgentContext(
            agent_name="dymok",
            task="Phase 2 of tmux watchdog fix",
            context="Detailed context body",
            notes="Some scratch notes",
            blockers=["upstream PR pending"],
            priority_items=["check daemon log", "ping barsik"],
            wake_action="Grep daemon log for verdict_wedged_inputs lines",
            updated_at=1_700_000_000.0,
        )

    def test_default_emits_full_manifest(self):
        """Pins pre-#591 behavior for non-resume wakes (CONTEXT_RESTART /
        AUTO_RESTART / NEW_SESSION / IDLE_WAKE). All sections render."""
        out = self._full_ctx().to_prompt()
        assert "## ⚡ Wake Action (do this FIRST)" in out
        assert "Grep daemon log for verdict_wedged_inputs lines" in out
        assert "## Continuation" in out
        assert "Phase 2 of tmux watchdog fix" in out
        assert "### Context" in out
        assert "Detailed context body" in out
        assert "### Notes" in out
        assert "Some scratch notes" in out
        assert "### Blockers" in out
        assert "upstream PR pending" in out
        assert "### Priority Items" in out
        assert "check daemon log" in out

    def test_resume_mode_emits_only_wake_action(self):
        """RESUME mode (warm ``--continue``): only the directive renders.
        Continuation/Context/Notes/Blockers/Priority are dropped because
        the resumed conversation already carries that history."""
        out = self._full_ctx().to_prompt(resume_mode=True)
        # Directive survives.
        assert "## ⚡ Wake Action (do this FIRST)" in out
        assert "Grep daemon log for verdict_wedged_inputs lines" in out
        # Bulk does NOT.
        assert "## Continuation" not in out
        assert "Phase 2 of tmux watchdog fix" not in out
        assert "### Context" not in out
        assert "Detailed context body" not in out
        assert "### Notes" not in out
        assert "Some scratch notes" not in out
        assert "### Blockers" not in out
        assert "upstream PR pending" not in out
        assert "### Priority Items" not in out

    def test_resume_mode_returns_empty_when_no_wake_action(self):
        """RESUME mode with manifest set but no wake_action: nothing to
        render. Caller (``_build_streaming_wake_context``) treats empty
        return as "no saved-state contribution to wake prompt."""
        ctx = self._full_ctx()
        ctx.wake_action = ""
        out = ctx.to_prompt(resume_mode=True)
        assert out == ""

    def test_resume_mode_empty_when_wake_action_only_field_empty(self):
        """RESUME mode with ONLY wake_action set (no other fields): the
        directive renders by itself. Confirms the gate doesn't require
        the bulk fields to be populated."""
        ctx = AgentContext(
            agent_name="dymok",
            wake_action="Ping Barsik with verdict data",
        )
        out = ctx.to_prompt(resume_mode=True)
        assert out == "## ⚡ Wake Action (do this FIRST)\nPing Barsik with verdict data"

    def test_default_empty_context_returns_empty_string(self):
        """Regression: empty manifest → empty string, both modes."""
        empty = AgentContext(agent_name="dymok")
        assert empty.to_prompt() == ""
        assert empty.to_prompt(resume_mode=True) == ""


class TestModelSeeds:
    """Model registry seeding — Claude Fable 5 / Mythos 5 + idempotency."""

    def test_fable_and_mythos_seeded(self, registry):
        models = {
            m["model_id"]: m
            for m in registry.list_models(provider="anthropic", active_only=False)
        }
        assert "claude-fable-5" in models
        assert "claude-mythos-5" in models
        fable = models["claude-fable-5"]
        assert fable["input_price"] == 10.0
        assert fable["output_price"] == 50.0
        assert fable["context_window"] == 1_000_000
        assert fable["is_1m"] == 1
        assert fable["supports_thinking"] == 1

    def test_seed_idempotent_and_propagates(self, registry):
        n = len(registry.list_models(active_only=False))
        # Re-seeding a populated DB must not duplicate.
        registry._seed_models()
        assert len(registry.list_models(active_only=False)) == n
        # A model missing from an existing DB is (re-)added on the next seed —
        # the mechanism by which a newly-added model reaches running installs
        # rather than only fresh databases.
        registry._db.execute("DELETE FROM models WHERE model_id='claude-fable-5'")
        registry._db.commit()
        assert all(
            m["model_id"] != "claude-fable-5"
            for m in registry.list_models(active_only=False)
        )
        registry._seed_models()
        assert any(
            m["model_id"] == "claude-fable-5"
            for m in registry.list_models(active_only=False)
        )
        assert len(registry.list_models(active_only=False)) == n

    def test_anthropic_seed_prices_match_pricing_rate_table(self, registry):
        """#741 invariant: the registry's display prices must agree with
        pricing.py (the actual cost engine) for every Anthropic model both
        tables know. Catches the next list-price change that lands in one
        place but not the other."""
        from pinky_daemon.pricing import RATE_TABLE

        for m in registry.list_models(provider="anthropic", active_only=False):
            rate = RATE_TABLE.get(m["model_id"])
            if rate is None:
                continue
            assert m["input_price"] == rate["input"], m["model_id"]
            assert m["output_price"] == rate["output"], m["model_id"]
            assert m["cached_input_price"] == rate["cache_read"], m["model_id"]

    def test_1m_models_set_matches_registry_is_1m(self, registry):
        """#839 invariant: streaming_session._1M_MODELS (the hand-maintained set
        that corrects the SDK's 200k report on the StreamingSession path) must
        contain every model the registry flags is_1m=1. PR #837 added
        claude-sonnet-5 to the three cost/registry tables but not to this set,
        so a sonnet-5 SDK session capped context at 200k and compacted early.
        Pin them together so the next 1M model add can't silently drift."""
        from pinky_daemon.streaming_session import _1M_MODELS

        registry_1m = {
            m["model_id"]
            for m in registry.list_models(provider="anthropic", active_only=False)
            if m["is_1m"] == 1
        }
        missing = registry_1m - _1M_MODELS
        assert not missing, (
            f"models flagged is_1m=1 but absent from streaming_session._1M_MODELS "
            f"(would cap context at 200k on the SDK path): {sorted(missing)}"
        )

    def test_stale_prices_corrected_on_existing_rows(self, registry):
        """#741: rows already seeded with the stale tier are rewritten on the
        next seed pass (INSERT OR IGNORE alone never reaches existing DBs)."""
        registry._db.execute(
            "UPDATE models SET input_price=15.0, output_price=75.0,"
            " cached_input_price=1.5 WHERE id='anthropic/claude-opus-4-8'"
        )
        registry._db.execute(
            "UPDATE models SET input_price=0.8, output_price=4.0,"
            " cached_input_price=0.08 WHERE id='anthropic/claude-haiku-4-5'"
        )
        registry._db.commit()
        registry._seed_models()
        models = {
            m["id"]: m
            for m in registry.list_models(provider="anthropic", active_only=False)
        }
        opus = models["anthropic/claude-opus-4-8"]
        assert (opus["input_price"], opus["output_price"],
                opus["cached_input_price"]) == (5.0, 25.0, 0.5)
        haiku = models["anthropic/claude-haiku-4-5"]
        assert (haiku["input_price"], haiku["output_price"],
                haiku["cached_input_price"]) == (1.0, 5.0, 0.1)

    def test_operator_customized_prices_survive_reseed(self, registry):
        """The correction is gated on the exact stale triple — a price an
        operator changed by hand must not be clobbered."""
        registry._db.execute(
            "UPDATE models SET input_price=12.34 WHERE id='anthropic/claude-opus-4-8'"
        )
        registry._db.commit()
        registry._seed_models()
        opus = next(
            m for m in registry.list_models(provider="anthropic", active_only=False)
            if m["id"] == "anthropic/claude-opus-4-8"
        )
        assert opus["input_price"] == 12.34

    def test_gpt_56_sol_seeded_at_frontier_rates(self, registry):
        """#860: gpt-5.6-sol (the codex fleet model since 2026-07) must be in
        the catalog at the official $5/$30 (cached $0.50)."""
        models = {
            m["model_id"]: m
            for m in registry.list_models(provider="openai", active_only=False)
        }
        assert "gpt-5.6-sol" in models
        sol = models["gpt-5.6-sol"]
        assert (sol["input_price"], sol["output_price"],
                sol["cached_input_price"]) == (5.0, 30.0, 0.5)
        assert sol["tier"] == "flagship"

    def test_openai_seed_prices_match_pricing_rate_table(self, registry):
        """#860 extends the #741 invariant to the OpenAI family: catalog
        display prices must agree with pricing.py (the actual cost engine)
        for every openai model both tables know."""
        from pinky_daemon.pricing import RATE_TABLE

        checked = 0
        for m in registry.list_models(provider="openai", active_only=False):
            rate = RATE_TABLE.get(m["model_id"])
            if rate is None:
                continue
            assert m["input_price"] == rate["input"], m["model_id"]
            assert m["output_price"] == rate["output"], m["model_id"]
            assert m["cached_input_price"] == rate["cache_read"], m["model_id"]
            checked += 1
        # Guard the guard: gpt-5.6-sol + gpt-5.5 must both be intersecting.
        assert checked >= 2

    def test_stale_gpt55_price_corrected_on_existing_rows(self, registry):
        """#860: deployed DBs seeded gpt-5.5 at the gpt-5.2-tier $1.75/$14;
        the next seed pass realigns existing rows to the official $5/$30
        (INSERT OR IGNORE alone never reaches them)."""
        registry._db.execute(
            "UPDATE models SET input_price=1.75, output_price=14.0,"
            " cached_input_price=0.175 WHERE id='openai/gpt-5.5'"
        )
        registry._db.commit()
        registry._seed_models()
        gpt = next(
            m for m in registry.list_models(provider="openai", active_only=False)
            if m["id"] == "openai/gpt-5.5"
        )
        assert (gpt["input_price"], gpt["output_price"],
                gpt["cached_input_price"]) == (5.0, 30.0, 0.5)

    def test_operator_customized_gpt55_price_survives_reseed(self, registry):
        """Same exact-stale-triple gate as the Anthropic corrections — an
        operator-priced gpt-5.5 row must not be clobbered."""
        registry._db.execute(
            "UPDATE models SET input_price=9.99 WHERE id='openai/gpt-5.5'"
        )
        registry._db.commit()
        registry._seed_models()
        gpt = next(
            m for m in registry.list_models(provider="openai", active_only=False)
            if m["id"] == "openai/gpt-5.5"
        )
        assert gpt["input_price"] == 9.99
