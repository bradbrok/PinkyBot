"""Content scanner — detect prompt injection and exfiltration threats.

Scans content before injection into agent context (CLAUDE.md, SKILL.md bodies,
directives, etc.). Blocks or strips threats and logs detections.

See: GitHub issue #54
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Threat Pattern Definitions ───────────────────────────────

THREAT_PATTERNS: list[tuple[re.Pattern, str]] = [
    # Prompt injection
    (re.compile(r"ignore\s+((all|previous|above|prior)\s+)+instructions", re.IGNORECASE), "prompt_injection"),
    (re.compile(r"you\s+are\s+now\s+", re.IGNORECASE), "role_hijack"),
    (re.compile(r"do\s+not\s+tell\s+the\s+user", re.IGNORECASE), "deception_hide"),
    (re.compile(r"system\s+prompt\s+override", re.IGNORECASE), "sys_prompt_override"),
    (re.compile(r"disregard\s+(your|all|any)\s+(instructions|rules|guidelines)", re.IGNORECASE), "disregard_rules"),
    (re.compile(
        r"act\s+as\s+(if|though)\s+you\s+(have\s+no|don't\s+have)\s+(restrictions|limits|rules)",
        re.IGNORECASE,
    ), "bypass_restrictions"),
    # Exfiltration
    (re.compile(r"curl\s+[^\n]*\$\{?\w*(KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|API)", re.IGNORECASE), "exfil_curl"),
    (re.compile(r"cat\s+[^\n]*(\.env|credentials|\.netrc)", re.IGNORECASE), "read_secrets"),
    # SSH persistence
    (re.compile(r"authorized_keys", re.IGNORECASE), "ssh_backdoor"),
]

# Invisible Unicode characters (zero-width spaces, RTL override, BOM, etc.)
INVISIBLE_CHARS: set[str] = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u2060",  # word joiner
    "\ufeff",  # BOM / zero-width no-break space
    "\u202a",  # LTR embedding
    "\u202b",  # RTL embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # LTR override
    "\u202e",  # RTL override
}

_INVISIBLE_RE = re.compile("[" + "".join(re.escape(c) for c in INVISIBLE_CHARS) + "]")


# ── Result Types ─────────────────────────────────────────────


@dataclass
class ThreatMatch:
    """A single detected threat in scanned content."""
    pattern_name: str
    matched_text: str
    line_number: int


@dataclass
class ScanResult:
    """Result of scanning content for threats."""
    source: str  # e.g. "CLAUDE.md", "skill:pinky-calendar", "directive:3"
    clean: bool
    threats: list[ThreatMatch] = field(default_factory=list)
    has_invisible_chars: bool = False

    @property
    def threat_summary(self) -> str:
        if self.clean:
            return ""
        parts = []
        if self.has_invisible_chars:
            parts.append("invisible_unicode")
        parts.extend(t.pattern_name for t in self.threats)
        return ", ".join(parts)


# ── Core Scanner ─────────────────────────────────────────────


def scan_content(content: str, source: str) -> ScanResult:
    """Scan content for prompt injection / exfiltration threats.

    Args:
        content: The text to scan.
        source: Human-readable label for logging (e.g. "CLAUDE.md", "skill:foo").

    Returns:
        ScanResult with detected threats.
    """
    threats: list[ThreatMatch] = []
    has_invisible = bool(_INVISIBLE_RE.search(content))

    lines = content.split("\n")
    for line_num, line in enumerate(lines, start=1):
        for pattern, name in THREAT_PATTERNS:
            match = pattern.search(line)
            if match:
                threats.append(ThreatMatch(
                    pattern_name=name,
                    matched_text=match.group(0),
                    line_number=line_num,
                ))

    clean = len(threats) == 0 and not has_invisible
    return ScanResult(
        source=source,
        clean=clean,
        threats=threats,
        has_invisible_chars=has_invisible,
    )


def strip_invisible_chars(content: str) -> str:
    """Remove invisible Unicode characters from content."""
    return _INVISIBLE_RE.sub("", content)


def scan_and_log(content: str, source: str) -> ScanResult:
    """Scan content and log any threats found.

    Returns the ScanResult for the caller to decide what to do.
    """
    result = scan_content(content, source)
    if not result.clean:
        _log(f"content_scanner: THREAT DETECTED in {source}: {result.threat_summary}")
        for t in result.threats:
            _log(f"  line {t.line_number}: [{t.pattern_name}] \"{t.matched_text}\"")
        if result.has_invisible_chars:
            _log("  invisible Unicode characters found")
    return result


def sanitize(content: str, source: str) -> tuple[str, ScanResult]:
    """Scan content. Strip invisible chars if found but allow content through.

    Returns (possibly-cleaned content, scan result).
    Callers should check result.threats to decide whether to block.
    """
    result = scan_and_log(content, source)
    cleaned = content
    if result.has_invisible_chars:
        cleaned = strip_invisible_chars(content)
        _log(f"content_scanner: stripped invisible chars from {source}")
    return cleaned, result
