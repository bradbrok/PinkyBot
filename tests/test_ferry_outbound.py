"""Tests for @ferry/host-pinky outbound side — envelope build + publish.

Companion to test_ferry_host_pinky.py (inbound). Covers:
- address parsing (canonical ferry:// + at-form, edge cases)
- subject derivation (token sanitization)
- allowlist matching (exact, wildcards, default-deny)
- envelope builder (kind enforcement, ts/id stamping)
- envelope-to-wire serialization (key rename, optional-field elision)
- MeshSender (configured/diagnostics/send happy + failure paths)

The MeshSender tests mock subprocess.run rather than spawning the real
``nats`` CLI — the contract under test is "we shell out the right thing
and surface the right result", not "the nats CLI works" (separately
proven by the smoke fired 2026-05-10 12:14 PDT).

PinkyBot issue: #418.
"""

from __future__ import annotations

import json
import subprocess
from unittest.mock import patch

import pytest

from pinky_daemon.ferry.outbound import (
    MeshSender,
    SendResult,
    allowlist_matches,
    build_envelope,
    derive_subject,
    envelope_to_wire,
    parse_address,
)

# -- parse_address -------------------------------------------------------------


class TestParseAddress:
    def test_canonical_ferry_scheme(self):
        assert parse_address("ferry://pulse/pulse") == ("pulse", "pulse")

    def test_at_form(self):
        assert parse_address("pulse@pulse") == ("pulse", "pulse")

    def test_at_form_with_scope(self):
        # Rightmost @ is the fleet boundary; scope segments stay in the slug.
        assert parse_address("pulse@studio@sigil") == ("sigil", "pulse@studio")

    def test_canonical_with_complex_slug(self):
        assert parse_address("ferry://sigil/pulse@studio") == ("sigil", "pulse@studio")

    @pytest.mark.parametrize("addr", [
        "",
        "   ",
        "no-fleet",
        "ferry://",
        "ferry://onlyfleet",
        "ferry://onlyfleet/",
        "ferry:///onlyagent",
        "@nofleet",
        "noslug@",
        None,
    ])
    def test_invalid_returns_none(self, addr):
        assert parse_address(addr) is None  # type: ignore[arg-type]

    def test_strips_whitespace(self):
        assert parse_address("  pulse@pulse  ") == ("pulse", "pulse")


# -- derive_subject ------------------------------------------------------------


class TestDeriveSubject:
    def test_simple(self):
        assert derive_subject("pulse", "pulse") == "ferry.pulse.pulse.inbox"

    def test_at_in_slug_sanitized(self):
        assert derive_subject("sigil", "pulse@studio") == "ferry.sigil.pulse-studio.inbox"

    def test_dot_in_slug_sanitized(self):
        # dots are NATS subject separators — must not appear in tokens
        assert derive_subject("pinky.local", "misha") == "ferry.pinky-local.misha.inbox"


# -- allowlist_matches ---------------------------------------------------------


class TestAllowlistMatches:
    def test_empty_allowlist_denies_all(self):
        assert allowlist_matches("pulse@pulse", []) is False

    def test_exact_match(self):
        assert allowlist_matches("pulse@pulse", ["pulse@pulse"]) is True

    def test_no_match(self):
        assert allowlist_matches("pulse@pulse", ["misha@pulse"]) is False

    def test_wildcard_agent_for_fleet(self):
        assert allowlist_matches("pulse@pulse", ["*@pulse"]) is True
        assert allowlist_matches("misha@pulse", ["*@pulse"]) is True
        assert allowlist_matches("pulse@sigil", ["*@pulse"]) is False

    def test_wildcard_fleet_for_agent(self):
        assert allowlist_matches("pulse@pulse", ["pulse@*"]) is True
        assert allowlist_matches("pulse@sigil", ["pulse@*"]) is True
        assert allowlist_matches("misha@pulse", ["pulse@*"]) is False

    def test_global_wildcard(self):
        assert allowlist_matches("pulse@pulse", ["*"]) is True
        assert allowlist_matches("anything@anything", ["*@*"]) is True

    def test_unparseable_target_denied(self):
        assert allowlist_matches("not-an-address", ["*"]) is False

    def test_invalid_pattern_skipped_not_matched(self):
        # Bad pattern shouldn't crash; good pattern still matches.
        assert allowlist_matches(
            "pulse@pulse",
            ["", "garbage", "pulse@pulse"],
        ) is True


# -- build_envelope ------------------------------------------------------------


class TestBuildEnvelope:
    def _body(self, **extras):
        return {"kind": "msg", "text": "hi", **extras}

    def test_basic(self):
        env = build_envelope(
            from_="ferry://pinkybot/barsik",
            to="pulse@pulse",
            body=self._body(),
        )
        assert env.v == "0.1"
        assert env.from_ == "ferry://pinkybot/barsik"
        assert env.to == "pulse@pulse"
        assert env.body == {"kind": "msg", "text": "hi"}
        assert env.id  # uuid stamped
        assert env.ts > 0

    def test_kind_enforced(self):
        with pytest.raises(ValueError, match="must be a dict containing a 'kind' field"):
            build_envelope(from_="x", to="y", body={"text": "no kind"})

    def test_body_must_be_dict(self):
        with pytest.raises(ValueError):
            build_envelope(from_="x", to="y", body="not-a-dict")  # type: ignore[arg-type]

    def test_correlation_id_passthrough(self):
        env = build_envelope(
            from_="x", to="y", body=self._body(),
            correlation_id="cid-123",
        )
        assert env.correlation_id == "cid-123"

    def test_priority_validated(self):
        for p in ("low", "normal", "high", "urgent"):
            env = build_envelope(from_="x", to="y", body=self._body(), priority=p)
            assert env.priority == p
        # Bogus priority falls back to "normal" (don't crash on caller typo).
        env = build_envelope(from_="x", to="y", body=self._body(), priority="bogus")
        assert env.priority == "normal"


# -- envelope_to_wire ----------------------------------------------------------


class TestEnvelopeToWire:
    def _env(self, **kw):
        return build_envelope(
            from_="ferry://pinkybot/barsik",
            to="pulse@pulse",
            body={"kind": "msg", "text": "hi"},
            **kw,
        )

    def test_renames_from(self):
        wire = json.loads(envelope_to_wire(self._env()))
        assert "from" in wire
        assert "from_" not in wire
        assert wire["from"] == "ferry://pinkybot/barsik"

    def test_required_fields(self):
        wire = json.loads(envelope_to_wire(self._env()))
        for field in ("v", "id", "from", "to", "ts", "body"):
            assert field in wire

    def test_optional_fields_elided_at_default(self):
        wire = json.loads(envelope_to_wire(self._env()))
        # Defaults shouldn't bloat the wire.
        assert "priority" not in wire   # default "normal"
        assert "wake" not in wire       # default "if-idle"
        assert "ack" not in wire        # default "delivery"
        assert "ttl_ms" not in wire
        assert "correlation_id" not in wire
        assert "reply_to" not in wire
        assert "subject" not in wire
        assert "traversal" not in wire
        assert "headers" not in wire

    def test_optional_fields_emitted_when_set(self):
        env = self._env(correlation_id="cid-1", reply_to="cid-0", priority="high")
        wire = json.loads(envelope_to_wire(env))
        assert wire["correlation_id"] == "cid-1"
        assert wire["reply_to"] == "cid-0"
        assert wire["priority"] == "high"


# -- MeshSender ----------------------------------------------------------------


class TestMeshSenderConfig:
    def test_unconfigured_when_no_env(self):
        s = MeshSender(env={}, bin_path="")
        assert s.configured is False
        diag = s.diagnostics()
        assert diag["configured"] is False
        assert diag["user_set"] is False
        assert diag["password_set"] is False
        # Password value never in diagnostics.
        assert "password" not in diag or diag.get("password") in (None, "")

    def test_configured_with_full_env(self, tmp_path):
        # Synthesize a fake nats binary that exists, so configured=True.
        fake_bin = tmp_path / "nats"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        s = MeshSender(
            env={
                "PINKYBOT_FLEET_USER": "pinkybot_fleet",
                "PINKYBOT_FLEET_PASSWORD": "secret",
                "PINKYBOT_FLEET_NATS_URL": "wss://example.test",
            },
            bin_path=str(fake_bin),
        )
        assert s.configured is True
        diag = s.diagnostics()
        assert diag["user_set"] is True
        assert diag["password_set"] is True
        assert diag["nats_url"] == "wss://example.test"
        # Diagnostics MUST NOT leak the password value itself.
        for v in diag.values():
            assert v != "secret"


class TestMeshSenderSend:
    def _sender(self, tmp_path):
        fake_bin = tmp_path / "nats"
        fake_bin.write_text("#!/bin/sh\nexit 0\n")
        fake_bin.chmod(0o755)
        return MeshSender(
            env={
                "PINKYBOT_FLEET_USER": "pinkybot_fleet",
                "PINKYBOT_FLEET_PASSWORD": "secret",
                "PINKYBOT_FLEET_NATS_URL": "wss://example.test",
            },
            bin_path=str(fake_bin),
        )

    def _envelope(self, **kw):
        return build_envelope(
            from_="ferry://pinkybot/barsik",
            to="pulse@pulse",
            body={"kind": "smoke", "text": "hi"},
            **kw,
        )

    def test_unconfigured_returns_error(self):
        s = MeshSender(env={}, bin_path="")
        result = s.send(self._envelope(correlation_id="cid-x"))
        assert result.sent is False
        assert "not configured" in (result.error or "")
        assert result.correlation_id == "cid-x"

    def test_unparseable_target(self, tmp_path):
        env = self._envelope()
        env.to = "not-an-address"
        result = self._sender(tmp_path).send(env)
        assert result.sent is False
        assert "unparseable target" in (result.error or "")

    def test_happy_path(self, tmp_path):
        sender = self._sender(tmp_path)
        env = self._envelope(correlation_id="cid-happy")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="Published 165 bytes\n", stderr="",
            )
            result = sender.send(env)

        assert result.sent is True
        assert result.error is None
        assert result.correlation_id == "cid-happy"
        assert result.subject == "ferry.pulse.pulse.inbox"

        # Verify the subprocess call had the right shape.
        call = mock_run.call_args
        cmd = call.args[0]
        assert cmd[0].endswith("nats")
        assert "--user" in cmd
        assert "pinkybot_fleet" in cmd
        assert "--password" in cmd
        assert "secret" in cmd
        assert "pub" in cmd
        assert "ferry.pulse.pulse.inbox" in cmd
        # Wire payload is the last arg; verify it's the envelope JSON.
        payload = json.loads(cmd[-1])
        assert payload["from"] == "ferry://pinkybot/barsik"
        assert payload["to"] == "pulse@pulse"
        assert payload["correlation_id"] == "cid-happy"

    def test_nats_failure_surfaces_error(self, tmp_path):
        sender = self._sender(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="",
                stderr="nats: error: auth failed\n",
            )
            result = sender.send(self._envelope())

        assert result.sent is False
        assert result.error and "auth failed" in result.error

    def test_timeout_surfaces_error(self, tmp_path):
        sender = self._sender(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="nats", timeout=10.0)
            result = sender.send(self._envelope())

        assert result.sent is False
        assert "timed out" in (result.error or "")

    def test_password_not_in_error_message(self, tmp_path):
        """Defense in depth — if a failure surfaces stderr, password must
        not appear (we don't put it in stderr by default, but assert it)."""
        sender = self._sender(tmp_path)
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=1, stdout="",
                stderr="nats: error: connection refused\n",
            )
            result = sender.send(self._envelope())
        assert result.sent is False
        assert "secret" not in (result.error or "")


class TestSendResultShape:
    def test_smoke(self):
        # Quick proof of the dataclass shape — used by API serializer.
        r = SendResult(sent=True, correlation_id="cid", subject="s", ts=1, error=None)
        assert r.sent and r.correlation_id == "cid"
