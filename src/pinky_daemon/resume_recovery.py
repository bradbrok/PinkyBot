"""Positive startup evidence and a single recovery budget per logical operation."""

from __future__ import annotations

import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Literal


def enabled() -> bool:
    return os.environ.get("PINKY_RESUME_FAILSAFE", "0") == "1"


def is_uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value.lower()
    except (ValueError, AttributeError, TypeError):
        return False


@dataclass(frozen=True)
class ResumeEvidence:
    backend: Literal["claude_sdk", "codex_app_server"]
    phase: Literal["initialize", "thread/resume"]
    reason: Literal["missing_conversation", "missing_rollout"]
    generation: int
    execution: Literal["not_started", "possible"] = "not_started"


@dataclass
class RecoveryOperation:
    deadline: float = field(default_factory=lambda: time.monotonic() + 600)
    operation_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    fresh_used: bool = False
    cleanup_failed: bool = False
    generation: int = 0

    def claim(self, evidence: ResumeEvidence | None) -> bool:
        # Claim synchronously, before cleanup/persistence/spawn can yield.
        if (not enabled() or evidence is None or self.fresh_used
                or evidence.execution != "not_started"
                or evidence.generation != self.generation or time.monotonic() >= self.deadline):
            return False
        self.fresh_used = True
        self.generation += 1
        return True

    def event(self, kind: str, evidence: ResumeEvidence, agent: str, label: str) -> dict:
        return {"type": kind, "agent": agent, "label": label,
                "backend": evidence.backend, "phase": evidence.phase,
                "reason": evidence.reason, "operation_id": self.operation_id}


def sdk_rejection(error: BaseException, requested_id: str, generation: int) -> ResumeEvidence | None:
    from claude_agent_sdk._errors import ProcessError

    # Pinned SDK 0.2.138 / bundled CLI: observed during initialize, without a query.
    expected = ("Claude Code returned an error result: No conversation found with session ID: "
                f"{requested_id} (exit code: 1)")
    if (is_uuid(requested_id) and isinstance(error, ProcessError)
            and error.exit_code == 1 and str(error) == expected):
        return ResumeEvidence("claude_sdk", "initialize", "missing_conversation", generation)
    return None


def appserver_rejection(error: BaseException, requested_id: str,
                        generation: int) -> ResumeEvidence | None:
    from pinky_daemon.codex_app_server import CodexAppServerError

    # rust-v0.154.0 thread_processor.rs:5819. Other -32600 errors are not evidence.
    if (is_uuid(requested_id) and isinstance(error, CodexAppServerError)
            and error.code == -32600 and error.data is None
            and str(error) == f"no rollout found for thread id {requested_id}"):
        return ResumeEvidence("codex_app_server", "thread/resume", "missing_rollout", generation)
    return None


def sdk_contract() -> tuple[str, bool]:
    """Characterize metadata and exception rendering without launching a CLI."""
    from importlib.metadata import PackageNotFoundError, version

    from claude_agent_sdk._errors import ProcessError

    try:
        installed = version("claude-agent-sdk")
    except PackageNotFoundError:
        installed = "unknown"
    display = installed if len(installed) <= 64 and re.fullmatch(r"\d+\.\d+\.\d+(?:[a-z0-9.+-]{0,20})?", installed) else "unknown"
    compatible = installed == "0.2.138" and str(ProcessError("canary", exit_code=1)) == "canary (exit code: 1)"
    return display, compatible


_sdk_drift_events: OrderedDict = OrderedDict()


def report_sdk_drift(log, version: str, reason: str) -> None:
    """Bound both event size and repeated emission, including cache retention."""
    import json

    key = (log, version, reason)
    now = time.monotonic()
    if now - _sdk_drift_events.get(key, float("-inf")) < 300:
        return
    _sdk_drift_events[key] = now
    _sdk_drift_events.move_to_end(key)
    while len(_sdk_drift_events) > 64:
        _sdk_drift_events.popitem(last=False)
    log(json.dumps({"type": "resume_signature_contract_drift",
                    "sdk_version": version, "reason": reason}))


def sanitized_diagnostic(value: str) -> str:
    """Sanitize a bounded raw prefix, including incomplete credential values."""
    value = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", value[:4096])
    # Quoted fields can contain spaces, escaped quotes, or end at the input
    # cap. Consume an unfinished value too; never retain its raw prefix.
    value = re.sub(
        r'''(?i)(["'](?:api[_-]?key|token|password|secret)["']\s*[:=]\s*)'''
        r'''(?:"(?:\\.|[^"\\])*(?:"|\\?$)|'(?:\\.|[^'\\])*(?:'|\\?$))''',
        r'\1"[redacted]"', value,
    )
    value = re.sub(r"(?i)((?:basic|bearer)\s+|(?:api[_-]?key|token|password|secret)\s*[=:]\s*)\S+",
                   r"\1[redacted]", value)
    value = re.sub(r"(?i)\b(https?://)[^\s/?#]*@", r"\1[redacted]@", value)
    # At EOF/cap an authority may end before its @. Withhold ambiguous
    # user:password fragments even when the password could look like a port.
    value = re.sub(
        r"(?i)\b(https?://)([^\s/?#:@]+):([^\s/?#@]+)(?=$|\s)",
        lambda m: m[1] + "[redacted]", value,
    )
    value = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", value)
    value = re.sub(r"\beyJ[A-Za-z0-9_-]*(?:\.[A-Za-z0-9_-]*){0,2}", "[redacted]", value)
    return " ".join(value.split())[:1024]


async def drain_diagnostic(stream) -> str:
    retained = bytearray()
    while chunk := await stream.read(4096):
        retained.extend(chunk[:max(0, 4096 - len(retained))])
    return sanitized_diagnostic(retained.decode(errors="replace"))
