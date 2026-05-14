"""Tests for the Transport protocol.

PR2 is typing-only; this test module verifies:

1. The Protocol imports cleanly.
2. A minimal hand-rolled class satisfies `isinstance(obj, Transport)`
   structurally (since the Protocol is ``runtime_checkable``).
3. A class missing required attributes does NOT satisfy isinstance.
4. The Protocol surface matches what we documented — attribute / method
   names won't silently drift without a test failure.

PR3 (StreamingSession adopts) carries the behavior tests; this module
just locks in the contract shape.
"""

from __future__ import annotations

import inspect

import pytest

from pinky_daemon.transport import Transport
from pinky_daemon.transport_state import SessionState


# Hand-rolled stub that intentionally implements the full Transport
# surface with no-op bodies. Used to verify the Protocol's structural
# isinstance behavior and to lock in the surface shape.
class _StubTransport:
    """Stub satisfying Transport. Not a real backend; just structural."""

    agent_name = "stub"

    @property
    def id(self) -> str:
        return "stub-main"

    @property
    def state(self) -> SessionState:
        return SessionState.UNINITIALIZED

    @property
    def is_connected(self) -> bool:
        return False

    @property
    def is_idle_sleeping(self) -> bool:
        return False

    @property
    def session_id(self) -> str:
        return ""

    @property
    def stats(self) -> dict:
        return {}

    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    async def send(
        self,
        prompt: str,
        *,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> None: ...

    async def force_restart(self) -> bool:
        return True

    async def idle_sleep(self) -> bool:
        return True

    async def attempt_reconnect(self) -> None: ...

    @property
    def effective_effort(self) -> str:
        return "medium"

    def set_effort(self, level: str) -> None: ...
    def clear_effort_override(self) -> None: ...


class TestProtocolImport:
    def test_protocol_imports(self):
        """Sanity: the Protocol module imports without errors."""
        assert Transport is not None
        assert hasattr(Transport, "__protocol_attrs__") or hasattr(
            Transport, "_is_protocol"
        )

    def test_protocol_uses_session_state_enum(self):
        """The ``state`` property's annotation refers to ``SessionState``
        from ``transport_state``. PR1's enum is the source of truth; this
        test fails loudly if someone defines a parallel enum in transport.py."""
        # Inspect the Protocol's class body for the SessionState symbol.
        # If the import was removed or shadowed, the import would fail at
        # module load; this is just a belt-and-suspenders.
        from pinky_daemon import transport as transport_mod
        assert transport_mod.SessionState is SessionState


class TestRuntimeCheckable:
    def test_stub_satisfies_transport_protocol(self):
        """The structural ``isinstance`` check passes for a class that
        implements the documented surface."""
        stub = _StubTransport()
        assert isinstance(stub, Transport)

    def test_incomplete_class_does_not_satisfy_protocol(self):
        """A class missing required attributes fails isinstance.

        ``runtime_checkable`` checks attribute names exist, not their
        types or signatures — so this test only catches structural gaps,
        not signature drift. Signature contracts are enforced at type-
        check time (mypy / pyright); see PR3 for adoption tests.
        """
        class _MissingState:
            agent_name = "missing"

            @property
            def id(self) -> str:
                return ""

            # Missing: state, is_connected, is_idle_sleeping, session_id,
            # stats, connect, disconnect, send, force_restart,
            # idle_sleep, attempt_reconnect, effective_effort,
            # set_effort, clear_effort_override.

        assert not isinstance(_MissingState(), Transport)


class TestSurfaceShape:
    """Lock in the public surface so silent name drift fails a test.

    These tests are intentionally redundant with the Protocol definition
    itself — the point is that renaming a method or removing a property
    requires explicit acknowledgment in this test file rather than slipping
    through.
    """

    REQUIRED_ATTRIBUTES = {"agent_name"}
    REQUIRED_PROPERTIES = {
        "id",
        "state",
        "is_connected",
        "is_idle_sleeping",
        "session_id",
        "stats",
        "effective_effort",
    }
    REQUIRED_ASYNC_METHODS = {
        "connect",
        "disconnect",
        "send",
        "force_restart",
        "idle_sleep",
        "attempt_reconnect",
    }
    REQUIRED_SYNC_METHODS = {"set_effort", "clear_effort_override"}

    @pytest.mark.parametrize("name", sorted(REQUIRED_PROPERTIES))
    def test_protocol_declares_property(self, name):
        """Each documented property must appear on the Protocol class."""
        assert hasattr(Transport, name), (
            f"Transport Protocol missing property {name!r}"
        )

    @pytest.mark.parametrize("name", sorted(REQUIRED_ASYNC_METHODS))
    def test_protocol_declares_async_method(self, name):
        """Each documented async method must appear on the Protocol class
        AND be detectable as a coroutine on a stub implementation."""
        assert hasattr(Transport, name), (
            f"Transport Protocol missing async method {name!r}"
        )
        # Verify the stub's implementation is actually async — catches
        # accidental sync/async mismatch when we add new methods.
        method = getattr(_StubTransport, name)
        assert inspect.iscoroutinefunction(method), (
            f"Stub {name!r} is not async — Protocol contract drift"
        )

    @pytest.mark.parametrize("name", sorted(REQUIRED_SYNC_METHODS))
    def test_protocol_declares_sync_method(self, name):
        """Each documented sync method must appear on the Protocol class."""
        assert hasattr(Transport, name), (
            f"Transport Protocol missing sync method {name!r}"
        )

    def test_send_signature_keyword_only(self):
        """``send`` takes ``prompt`` positionally then keyword-only args.

        Locks in the calling convention used across the daemon
        (``broker.py`` calls ``streaming.send(prompt, platform=..., ...)``).
        Catches accidental signature changes during PR3.
        """
        sig = inspect.signature(_StubTransport.send)
        params = list(sig.parameters.values())
        # self, prompt (positional), then keyword-only.
        assert params[0].name == "self"
        assert params[1].name == "prompt"
        assert params[1].kind == inspect.Parameter.POSITIONAL_OR_KEYWORD
        for p in params[2:]:
            assert p.kind == inspect.Parameter.KEYWORD_ONLY, (
                f"send() arg {p.name!r} should be keyword-only "
                f"(got {p.kind})"
            )


class TestProtocolDoesNotImportStreamingSession:
    """Side-by-side architecture: the Transport Protocol must NOT import
    or reference StreamingSession directly. The Protocol is the contract
    each backend honors; concrete backends import the Protocol, not the
    other way around. Otherwise we get an import cycle and StreamingSession
    becomes load-bearing for TmuxSession's type definitions.
    """

    def test_transport_module_does_not_import_streaming_session(self):
        from pinky_daemon import transport as transport_mod
        src = inspect.getsource(transport_mod)
        # The Protocol may MENTION StreamingSession in docstrings (it does)
        # — but it must not IMPORT the class. Distinguish via import lines.
        import_lines = [
            ln for ln in src.splitlines()
            if ln.lstrip().startswith(("import ", "from "))
        ]
        for ln in import_lines:
            assert "streaming_session" not in ln.lower(), (
                f"transport.py imports streaming_session — breaks the "
                f"side-by-side contract. Offending line: {ln!r}"
            )

    def test_transport_module_does_not_import_tmux(self):
        """Mirror constraint: tmux backend doesn't get import precedence
        either. Protocol is the abstraction; both backends import it."""
        from pinky_daemon import transport as transport_mod
        src = inspect.getsource(transport_mod)
        import_lines = [
            ln for ln in src.splitlines()
            if ln.lstrip().startswith(("import ", "from "))
        ]
        for ln in import_lines:
            assert "tmux" not in ln.lower(), (
                f"transport.py imports tmux backend — breaks the "
                f"side-by-side contract. Offending line: {ln!r}"
            )
