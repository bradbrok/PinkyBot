#!/usr/bin/env python3
"""Read-only RingCentral voicemail triage from a Zoho Desk ticket.

The voicemail audio is fetched through the Desk ticket-thread attachment
chain. RingCentral credentials are intentionally neither accepted nor used.
The external ``whisper_transcribe.py`` helper is invoked without a prompt,
and the result is combined with Desk contact candidates and read-only ledger
matches into one JSON object on stdout.

Authentication environment:

* ``RC_VOICEMAIL_AUTH_MODE`` (optional: ``gateway`` or ``direct``)
* gateway: ``ZOHO_API_HOST``, ``ZOHO_API_SECRET``, and ``ZOHO_AGENT_NAME``
* direct: ``ZOHO_DESK_ACCESS_TOKEN`` or ``ZOHO_DESK_TOKEN_FILE``

Common install-time environment:

* ``ZOHO_DESK_ORG_ID`` (optional for organization-bound OAuth tokens)
* ``ZOHO_DESK_BASE_URL`` (default: https://desk.zoho.com/api/v1)
* ``WHISPER_TRANSCRIBE_PATH`` (default: sibling whisper_transcribe.py)
* ``RC_VOICEMAIL_LEDGER_ROOT`` (default: current directory)

Each ledger can also be pinned independently with
``RC_VOICEMAIL_LEDGER_<NAME>`` where NAME is ACTIVE_RMAS, LABEL_REQUESTS,
ACTIVE_CC, ACTIVE_JAMF, or INTEGRATION_REQUESTS.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urljoin, urlparse
from urllib.request import Request, urlopen

NOTIFY_ADDRESS = "notify@ringcentral.com"
DEFAULT_DESK_BASE_URL = "https://desk.zoho.com/api/v1"
DEFAULT_TIMEOUT_S = 30.0
DEFAULT_TRANSCRIBE_TIMEOUT_S = 300.0
AUTH_MODES = {"direct", "gateway"}
GATEWAY_ENV_GROUP = ("ZOHO_API_HOST", "ZOHO_API_SECRET", "ZOHO_AGENT_NAME")
DIRECT_ENV_GROUP = ("ZOHO_DESK_ACCESS_TOKEN", "ZOHO_DESK_TOKEN_FILE")

EXIT_CONFIG = 2
EXIT_FETCH = 3
EXIT_TRANSCRIBE = 4
EXIT_RESOLVE = 5
EXIT_IN_FLIGHT = 6
EXIT_INTERNAL = 70

LEDGER_ALIASES: dict[str, tuple[str, ...]] = {
    "active_rmas": ("active_rmas",),
    "label_requests": ("label_requests",),
    "active_cc": ("active_cc",),
    "active_jamf": ("active_jamf",),
    "integration_requests": ("integration_requests", "ubereats"),
}
LEDGER_SUFFIXES = ("", ".json", ".jsonl", ".csv", ".txt", ".md", ".yaml", ".yml")
LEDGER_SUBDIRS = ("", "data", "ledgers", "state")
NO_SPEECH_MARKERS = {
    "no speech",
    "no_speech",
    "[no speech]",
    "[no_speech]",
    "no speech detected",
    "no_speech_detected",
    "[no speech detected]",
    "[no_speech_detected]",
    "<no speech>",
    "<no_speech>",
}

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?1[\s.\-]*)?\(?\d{3}\)?[\s.\-]*\d{3}[\s.\-]*\d{4}(?!\d)")
_DURATION_RE = re.compile(r"\bLength:\s*(\d{1,3}):(\d{2})(?::(\d{2}))?\b", re.I)
_CITY_HINT_RE = re.compile(r"\bin\s+([A-Za-z][A-Za-z .'-]{1,60})\s*$", re.I)
_SAFE_TICKET_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_SAFE_GATEWAY_HOST_RE = re.compile(r"^[A-Za-z0-9.-]+$")


class TriageError(Exception):
    """A user-visible failure in one pipeline stage."""

    def __init__(self, stage: str, message: str, exit_code: int):
        super().__init__(message)
        self.stage = stage
        self.message = message
        self.exit_code = exit_code


class DeskAPIError(Exception):
    """A Zoho Desk request or response failure."""


def _sign(secret: str, agent: str, method: str, path: str, ts: int) -> dict[str, str]:
    """Build HMAC-signed headers."""
    normalized = path.split("?", 1)[0]
    payload = f"{agent}\n{method.upper()}\n{normalized}\n{ts}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return {
        "x-pinky-agent": agent,
        "x-pinky-timestamp": str(ts),
        "x-pinky-signature": sig,
    }


def digits_only(value: object) -> str:
    if value is None:
        return ""
    return re.sub(r"\D", "", str(value))


def format_phone(number: str) -> str:
    digits = digits_only(number)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) == 10:
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    return digits


def _duration_seconds(match: re.Match[str] | None) -> int | None:
    if match is None:
        return None
    first, second, third = match.groups()
    if third is None:
        minutes, seconds = int(first), int(second)
        return minutes * 60 + seconds if seconds < 60 else None
    hours, minutes, seconds = int(first), int(second), int(third)
    if minutes >= 60 or seconds >= 60:
        return None
    return hours * 3600 + minutes * 60 + seconds


def parse_summary_line(summary: object) -> dict[str, str | int | None]:
    """Parse nullable caller fields from a RingCentral Desk thread summary."""

    if not isinstance(summary, str):
        return {"callerid_name": None, "number": None, "duration_s": None}

    duration_match = _DURATION_RE.search(summary)
    duration_s = _duration_seconds(duration_match)
    before_length = summary[: duration_match.start()] if duration_match else summary
    phone_match = _PHONE_RE.search(before_length)
    number = digits_only(phone_match.group(0)) if phone_match else None
    if number and len(number) == 11 and number.startswith("1"):
        number = number[1:]

    callerid_name: str | None = None
    from_match = re.search(r"\bFrom:\s*(.*)", before_length, re.I | re.S)
    if from_match:
        name_block = from_match.group(1)
        if " - " in name_block:
            name_block = name_block.split(" - ", 1)[1]
        phone_in_name = _PHONE_RE.search(name_block)
        if phone_in_name:
            name_block = name_block[: phone_in_name.start()]
        name_block = re.sub(r"[\s,;:.\-]+$", "", name_block).strip()
        if name_block.casefold() not in {"", "unknown", "anonymous", "null", "none"}:
            callerid_name = name_block

    return {
        "callerid_name": callerid_name,
        "number": number,
        "duration_s": duration_s,
    }


def extract_city_hint(callerid_name: object) -> str | None:
    if not isinstance(callerid_name, str):
        return None
    match = _CITY_HINT_RE.search(callerid_name.strip())
    if not match:
        return None
    city = match.group(1).strip(" ,.-")
    return city or None


def _token_from_json(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    for key in ("access_token", "accessToken", "token"):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
        nested = _token_from_json(candidate)
        if nested:
            return nested
    for candidate in value.values():
        nested = _token_from_json(candidate)
        if nested:
            return nested
    return None


def load_access_token(env: Mapping[str, str] | None = None) -> str:
    values = os.environ if env is None else env
    direct = values.get("ZOHO_DESK_ACCESS_TOKEN", "").strip()
    if direct:
        return direct.removeprefix("Zoho-oauthtoken ").strip()

    token_file_value = values.get("ZOHO_DESK_TOKEN_FILE", "").strip()
    if not token_file_value:
        raise TriageError(
            "config",
            "set ZOHO_DESK_ACCESS_TOKEN or ZOHO_DESK_TOKEN_FILE",
            EXIT_CONFIG,
        )
    token_file = Path(token_file_value).expanduser()
    try:
        raw = token_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TriageError(
            "config", f"cannot read Desk token file {token_file}: {exc}", EXIT_CONFIG
        ) from exc
    if not raw:
        raise TriageError("config", f"Desk token file {token_file} is empty", EXIT_CONFIG)

    token = None
    try:
        token = _token_from_json(json.loads(raw))
    except json.JSONDecodeError:
        token = raw
    if not token:
        raise TriageError(
            "config",
            f"Desk token file {token_file} has no access_token",
            EXIT_CONFIG,
        )
    return token.removeprefix("Zoho-oauthtoken ").strip()


def _present_env(values: Mapping[str, str], name: str) -> bool:
    return bool(values.get(name, "").strip())


def select_auth_mode(env: Mapping[str, str] | None = None) -> str:
    """Select an explicitly usable auth mode, or fail with missing credential names."""

    values = os.environ if env is None else env
    requested = values.get("RC_VOICEMAIL_AUTH_MODE", "").strip().casefold()
    if requested and requested not in AUTH_MODES:
        raise TriageError(
            "config",
            "RC_VOICEMAIL_AUTH_MODE must be gateway or direct",
            EXIT_CONFIG,
        )

    gateway_missing = [name for name in GATEWAY_ENV_GROUP if not _present_env(values, name)]
    direct_available = any(_present_env(values, name) for name in DIRECT_ENV_GROUP)

    if requested == "gateway":
        if gateway_missing:
            raise TriageError(
                "config",
                f"gateway auth requires {', '.join(gateway_missing)}",
                EXIT_CONFIG,
            )
        return "gateway"
    if requested == "direct":
        if not direct_available:
            raise TriageError(
                "config",
                "direct auth requires ZOHO_DESK_ACCESS_TOKEN or ZOHO_DESK_TOKEN_FILE",
                EXIT_CONFIG,
            )
        return "direct"

    if not gateway_missing:
        return "gateway"
    if direct_available:
        return "direct"
    raise TriageError(
        "config",
        "no usable auth credentials: gateway requires "
        f"{', '.join(gateway_missing)}; direct requires "
        "ZOHO_DESK_ACCESS_TOKEN or ZOHO_DESK_TOKEN_FILE",
        EXIT_CONFIG,
    )


def load_gateway_credentials(
    env: Mapping[str, str] | None = None,
) -> tuple[tuple[str, ...], str, str]:
    """Load the explicit legacy gateway credential group without agent defaults."""

    values = os.environ if env is None else env
    missing = [name for name in GATEWAY_ENV_GROUP if not _present_env(values, name)]
    if missing:
        raise TriageError(
            "config",
            f"gateway auth requires {', '.join(missing)}",
            EXIT_CONFIG,
        )
    raw_hosts = values["ZOHO_API_HOST"].strip()
    hosts = tuple(part.strip() for part in raw_hosts.split(",") if part.strip())
    if not hosts or any(not _SAFE_GATEWAY_HOST_RE.fullmatch(host) for host in hosts):
        raise TriageError(
            "config",
            "ZOHO_API_HOST must be a comma-separated list of hostnames or IP addresses",
            EXIT_CONFIG,
        )
    return hosts, values["ZOHO_API_SECRET"].strip(), values["ZOHO_AGENT_NAME"].strip()


def _collection(payload: object, *, context: str) -> list[dict[str, Any]]:
    """Normalize Desk collection payloads; an empty object is a valid miss."""

    if payload == {}:
        return []
    records: object = payload
    if isinstance(payload, dict):
        for key in ("data", "contacts", "results", "threads", "attachments"):
            if key in payload:
                records = payload[key]
                break
        else:
            raise DeskAPIError(f"{context} response has no collection field")
    if records == {} or records is None:
        return []
    if not isinstance(records, list):
        raise DeskAPIError(f"{context} response collection is not a list")
    if any(not isinstance(record, dict) for record in records):
        raise DeskAPIError(f"{context} response contains a non-object record")
    return records


class DeskClient:
    """Small read-only Zoho Desk REST client."""

    def __init__(
        self,
        access_token: str | None,
        *,
        mode: str = "direct",
        org_id: str | None = None,
        base_url: str = DEFAULT_DESK_BASE_URL,
        gateway_hosts: Sequence[str] = (),
        gateway_secret: str | None = None,
        gateway_agent: str | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        opener: Callable[..., Any] = urlopen,
    ):
        if mode not in AUTH_MODES:
            raise TriageError("config", f"unsupported Desk auth mode: {mode}", EXIT_CONFIG)
        self.mode = mode
        self.access_token = access_token.strip() if access_token else None
        self.org_id = org_id.strip() if org_id else None
        self.timeout_s = timeout_s
        self._opener = opener
        self._gateway_secret = gateway_secret.strip() if gateway_secret else None
        self._gateway_agent = gateway_agent.strip() if gateway_agent else None

        if mode == "direct":
            if not self.access_token:
                raise TriageError("config", "direct auth requires a Desk token", EXIT_CONFIG)
            self.base_urls = (base_url.rstrip("/") + "/",)
            parsed = urlparse(self.base_urls[0])
            if parsed.scheme != "https" or not parsed.netloc:
                raise TriageError("config", "ZOHO_DESK_BASE_URL must be an https URL", EXIT_CONFIG)
            self._base_host = parsed.netloc.casefold()
        else:
            if not gateway_hosts:
                raise TriageError("config", "gateway auth requires ZOHO_API_HOST", EXIT_CONFIG)
            if not self._gateway_secret:
                raise TriageError("config", "gateway auth requires ZOHO_API_SECRET", EXIT_CONFIG)
            if not self._gateway_agent:
                raise TriageError("config", "gateway auth requires ZOHO_AGENT_NAME", EXIT_CONFIG)
            self.base_urls = tuple(f"http://{host}:9100/desk/" for host in gateway_hosts)
            self._base_host = None
        self.base_url = self.base_urls[0]

    def _auth_headers(self, method: str, path_without_query: str) -> dict[str, str]:
        if self.mode == "direct":
            return {"Authorization": f"Zoho-oauthtoken {self.access_token}"}
        assert self._gateway_secret is not None
        assert self._gateway_agent is not None
        ts = int(time.time())
        headers = _sign(
            self._gateway_secret,
            self._gateway_agent,
            method,
            path_without_query,
            ts,
        )
        return headers

    def _headers(self, method: str, path_without_query: str, *, accept: str) -> dict[str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "pinky-rc-voicemail-triage/1",
        }
        headers.update(self._auth_headers(method, path_without_query))
        if self.mode == "direct" and self.org_id:
            headers["orgId"] = self.org_id
        return headers

    @staticmethod
    def _url(
        base_url: str,
        path: str,
        params: Mapping[str, object] | None = None,
    ) -> str:
        url = urljoin(base_url, path.lstrip("/"))
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    def _request_json(self, path: str, params: Mapping[str, object] | None = None) -> object:
        transport_errors: list[str] = []
        body: bytes | None = None
        for base_url in self.base_urls:
            url = self._url(base_url, path, params)
            path_without_query = urlparse(url).path
            request = Request(
                url,
                headers=self._headers("GET", path_without_query, accept="application/json"),
                method="GET",
            )
            try:
                with self._opener(request, timeout=self.timeout_s) as response:
                    body = response.read()
                break
            except HTTPError as exc:
                raise DeskAPIError(f"Desk HTTP {exc.code} for {path}") from exc
            except (URLError, TimeoutError, OSError, ValueError) as exc:
                transport_errors.append(f"{urlparse(base_url).hostname}: {exc}")
        if body is None:
            if self.mode == "gateway":
                raise DeskAPIError(
                    f"Desk gateway hosts unreachable for {path}: {'; '.join(transport_errors)}"
                )
            detail = transport_errors[0] if transport_errors else "unknown error"
            raise DeskAPIError(f"Desk request failed for {path}: {detail}")
        try:
            return json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DeskAPIError(f"Desk returned invalid JSON for {path}") from exc

    def list_threads(self, ticket_id: str) -> list[dict[str, Any]]:
        payload = self._request_json(
            f"tickets/{quote(ticket_id, safe='')}/threads",
            {"from": 0, "limit": 100},
        )
        return _collection(payload, context="ticket threads")

    def get_thread(self, ticket_id: str, thread_id: str) -> dict[str, Any]:
        if self.mode == "gateway":
            payload = self._request_json(
                f"tickets/{quote(ticket_id, safe='')}/threads/"
                f"{quote(thread_id, safe='')}/attachments"
            )
            return {
                "id": thread_id,
                "attachments": _collection(payload, context="thread attachments"),
            }
        payload = self._request_json(
            f"tickets/{quote(ticket_id, safe='')}/threads/{quote(thread_id, safe='')}",
            {"include": "plainText"},
        )
        if not isinstance(payload, dict) or not payload:
            raise DeskAPIError("thread detail response is not an object")
        return payload

    def search_contacts(self, query: str) -> list[dict[str, Any]]:
        if self.mode == "gateway":
            params = {"module": "contacts", "q": query}
        else:
            params = {"module": "contacts", "searchStr": query, "from": 0, "limit": 50}
        payload = self._request_json(
            "search",
            # The Desk operation calls this input ``q``; the REST API names
            # the corresponding wire parameter ``searchStr``.
            params,
        )
        return _collection(payload, context="contact search")

    def download_attachment(
        self,
        href: str,
        destination: Path,
        *,
        ticket_id: str,
        thread_id: str,
        attachment_id: str,
    ) -> None:
        if self.mode == "gateway":
            path = (
                f"tickets/{quote(ticket_id, safe='')}/threads/{quote(thread_id, safe='')}"
                f"/attachments/{quote(attachment_id, safe='')}/content"
            )
            urls = [self._url(base_url, path) for base_url in self.base_urls]
        else:
            url = urljoin(self.base_url, href)
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.netloc.casefold() != self._base_host:
                raise DeskAPIError("attachment href is outside the configured Desk host")
            urls = [url]

        part = destination.with_suffix(destination.suffix + ".part")
        transport_errors: list[str] = []
        try:
            for url in urls:
                path_without_query = urlparse(url).path
                request = Request(
                    url,
                    headers=self._headers(
                        "GET",
                        path_without_query,
                        accept="audio/mpeg,application/octet-stream",
                    ),
                    method="GET",
                )
                try:
                    with (
                        self._opener(request, timeout=self.timeout_s) as response,
                        part.open("wb") as handle,
                    ):
                        while chunk := response.read(1024 * 1024):
                            handle.write(chunk)
                    if part.stat().st_size == 0:
                        raise DeskAPIError("Desk returned an empty attachment")
                    part.replace(destination)
                    return
                except HTTPError as exc:
                    raise DeskAPIError(
                        f"Desk HTTP {exc.code} while downloading attachment"
                    ) from exc
                except (URLError, TimeoutError, OSError, ValueError) as exc:
                    transport_errors.append(f"{urlparse(url).hostname}: {exc}")
            if self.mode == "gateway":
                raise DeskAPIError(
                    "Desk gateway hosts unreachable while downloading attachment: "
                    f"{'; '.join(transport_errors)}"
                )
            detail = transport_errors[0] if transport_errors else "unknown error"
            raise DeskAPIError(f"attachment download failed: {detail}")
        finally:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass


def _thread_sender(thread: Mapping[str, Any]) -> str | None:
    for key in ("fromEmailAddress", "from", "replyTo"):
        value = thread.get(key)
        if isinstance(value, str) and "@" in value:
            return value.strip().casefold()
    author = thread.get("author")
    if isinstance(author, dict):
        value = author.get("email")
        if isinstance(value, str):
            return value.strip().casefold()
    return None


def _attachments(thread: Mapping[str, Any]) -> list[dict[str, Any]]:
    value = thread.get("attachments", [])
    if value in (None, {}):
        return []
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise DeskAPIError("thread attachments response is malformed")
    return value


def fetch_voicemail_audio(
    desk: DeskClient,
    ticket_id: str,
    destination_dir: Path,
) -> tuple[Path, dict[str, str | int | None]]:
    """Follow ticket -> RingCentral thread -> mp3 attachment and download it."""

    try:
        threads = desk.list_threads(ticket_id)
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for listed in threads:
            if _thread_sender(listed) != NOTIFY_ADDRESS:
                continue
            thread_id = listed.get("id")
            if not isinstance(thread_id, (str, int)):
                raise DeskAPIError("RingCentral thread is missing its id")
            detail = {**listed, **desk.get_thread(ticket_id, str(thread_id))}
            for attachment in _attachments(detail):
                name = attachment.get("name")
                if isinstance(name, str) and name.casefold().endswith(".mp3"):
                    matches.append((detail, attachment))
        if not matches:
            raise DeskAPIError("no RingCentral thread mp3 attachment found")
        if len(matches) > 1:
            raise DeskAPIError("multiple RingCentral thread mp3 attachments found")

        thread, attachment = matches[0]
        summary = thread.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise DeskAPIError("RingCentral thread summary is missing or empty")
        thread_id_value = thread.get("id")
        attachment_id_value = attachment.get("id")
        if not isinstance(thread_id_value, (str, int)):
            raise DeskAPIError("RingCentral thread is missing its id")
        if not isinstance(attachment_id_value, (str, int)):
            raise DeskAPIError("RingCentral attachment is missing its id")
        thread_id = str(thread_id_value)
        attachment_id = str(attachment_id_value)
        href = attachment.get("href")
        if not isinstance(href, str) or not href.strip():
            href = (
                f"tickets/{quote(ticket_id, safe='')}/threads/{quote(thread_id, safe='')}"
                f"/attachments/{quote(attachment_id, safe='')}/content"
            )
        destination_dir.mkdir(parents=True, exist_ok=True)
        destination = destination_dir / f"{ticket_id}_{thread_id}_{attachment_id}.mp3"
        suffix = 2
        while destination.exists():
            destination = destination_dir / (
                f"{ticket_id}_{thread_id}_{attachment_id}_{suffix}.mp3"
            )
            suffix += 1
        desk.download_attachment(
            href,
            destination,
            ticket_id=ticket_id,
            thread_id=thread_id,
            attachment_id=attachment_id,
        )
        return destination, parse_summary_line(summary)
    except TriageError:
        raise
    except DeskAPIError as exc:
        raise TriageError("fetch", str(exc), EXIT_FETCH) from exc


def parse_transcriber_output(stdout: str) -> tuple[str, bool]:
    """Accept machine-readable helper output or an explicit plain-text sentinel."""

    stripped = stdout.strip()
    if not stripped:
        # This parser is reached only after the helper exits zero. Geordi's
        # plain-text helper reports silence as an empty transcript; crashes
        # take the nonzero path in transcribe_audio before this call.
        return "", True
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        payload = None

    if isinstance(payload, dict):
        raw_no_speech = payload.get("no_speech")
        if raw_no_speech is not None and not isinstance(raw_no_speech, bool):
            raise TriageError("transcribe", "transcriber no_speech is not boolean", EXIT_TRANSCRIBE)
        text_fields: dict[str, str] = {}
        for field in ("transcript", "text"):
            if field not in payload:
                continue
            raw_text = payload[field]
            if raw_text is None:
                raw_text = ""
            if not isinstance(raw_text, str):
                raise TriageError(
                    "transcribe",
                    f"transcriber {field} is not text",
                    EXIT_TRANSCRIBE,
                )
            text_fields[field] = raw_text.strip()

        if raw_no_speech is True and any(text_fields.values()):
            raise TriageError(
                "transcribe",
                "transcriber returned text with no_speech:true",
                EXIT_TRANSCRIBE,
            )
        if len(set(text_fields.values())) > 1:
            raise TriageError(
                "transcribe",
                "transcriber transcript and text disagree",
                EXIT_TRANSCRIBE,
            )

        transcript = next(iter(text_fields.values()), "")
        if raw_no_speech is True:
            return "", True
        if transcript:
            return transcript, False
        if raw_no_speech is None and text_fields:
            # A successful machine-readable response with an explicit empty
            # transcript is the JSON equivalent of the helper's empty stdout.
            return "", True
        raise TriageError(
            "transcribe",
            "transcriber returned neither transcript nor explicit no_speech:true",
            EXIT_TRANSCRIBE,
        )

    if isinstance(payload, str):
        stripped = payload.strip()
    normalized_marker = stripped.casefold().strip(" \t\r\n.!:;")
    if normalized_marker in NO_SPEECH_MARKERS:
        return "", True
    if stripped:
        return stripped, False
    raise TriageError("transcribe", "transcriber output is unusable", EXIT_TRANSCRIBE)


def transcribe_audio(
    audio_path: Path,
    transcriber_path: Path,
    *,
    timeout_s: float = DEFAULT_TRANSCRIBE_TIMEOUT_S,
) -> tuple[str, bool]:
    if not transcriber_path.is_file():
        raise TriageError(
            "transcribe",
            f"transcriber not found: {transcriber_path}",
            EXIT_TRANSCRIBE,
        )

    # PROMPTLESS by construction: only the audio path is passed. Remove known
    # prompt-bearing environment hooks so a parent shell cannot add a prompt.
    child_env = os.environ.copy()
    for key in tuple(child_env):
        folded = key.casefold()
        if "prompt" in folded:
            child_env.pop(key, None)
    try:
        completed = subprocess.run(
            [sys.executable, str(transcriber_path), str(audio_path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_s,
            env=child_env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise TriageError(
            "transcribe", f"transcriber could not run: {exc}", EXIT_TRANSCRIBE
        ) from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip().splitlines()
        suffix = f": {detail[-1][:400]}" if detail else ""
        raise TriageError(
            "transcribe",
            f"transcriber exited {completed.returncode}{suffix}",
            EXIT_TRANSCRIBE,
        )
    return parse_transcriber_output(completed.stdout)


def _contact_city_values(contact: Mapping[str, Any]) -> set[str]:
    values: set[str] = set()
    for key, value in contact.items():
        if "city" in key.casefold() and isinstance(value, str) and value.strip():
            values.add(value.strip().casefold())
        if isinstance(value, dict):
            values.update(_contact_city_values(value))
    return values


def _account_rows(contact: Mapping[str, Any]) -> list[tuple[str | None, str | None]]:
    rows: list[tuple[str | None, str | None]] = []
    accounts = contact.get("accounts")
    if isinstance(accounts, list):
        for account in accounts:
            if isinstance(account, dict):
                account_id = account.get("id", account.get("accountId"))
                account_name = account.get("name", account.get("accountName"))
                rows.append(
                    (
                        str(account_id) if account_id is not None else None,
                        str(account_name) if account_name is not None else None,
                    )
                )
    account = contact.get("account")
    if isinstance(account, dict):
        account_id = account.get("id", account.get("accountId"))
        account_name = account.get("name", account.get("accountName"))
        rows.append(
            (
                str(account_id) if account_id is not None else None,
                str(account_name) if account_name is not None else None,
            )
        )
    if not rows:
        account_id = contact.get("accountId")
        account_name = contact.get("accountName")
        rows.append(
            (
                str(account_id) if account_id is not None else None,
                str(account_name) if account_name is not None else None,
            )
        )
    return rows


def _site_candidates(
    contacts: Sequence[Mapping[str, Any]],
    *,
    match_basis: str,
) -> list[dict[str, str | bool | None]]:
    candidates: list[dict[str, str | bool | None]] = []
    seen: set[tuple[str, str | None, str | None, str]] = set()
    for contact in contacts:
        contact_id = contact.get("id", contact.get("contactId"))
        if contact_id is None:
            raise DeskAPIError("contact search result is missing its id")
        contact_id = str(contact_id)
        for account_id, account_name in _account_rows(contact):
            key = (contact_id, account_id, account_name, match_basis)
            if key in seen:
                continue
            seen.add(key)
            candidates.append(
                {
                    "contact_id": contact_id,
                    "account_id": account_id,
                    "account_name": account_name,
                    "match_basis": match_basis,
                    "verified": False,
                }
            )
    return candidates


def resolve_site_candidates(
    desk: DeskClient,
    caller: Mapping[str, str | int | None],
) -> list[dict[str, str | bool | None]]:
    """Search digits first, then formatted phone, then an explicit city hint."""

    try:
        number = caller.get("number")
        digits = digits_only(number)
        if digits:
            contacts = desk.search_contacts(digits)
            if contacts:
                return _site_candidates(contacts, match_basis="phone_exact")
            formatted = format_phone(digits)
            if formatted and formatted != digits:
                contacts = desk.search_contacts(formatted)
                if contacts:
                    return _site_candidates(contacts, match_basis="phone_exact")

        city = extract_city_hint(caller.get("callerid_name"))
        if city:
            contacts = desk.search_contacts(city)
            city_key = city.casefold()
            city_contacts = [
                contact for contact in contacts if city_key in _contact_city_values(contact)
            ]
            return _site_candidates(city_contacts, match_basis="city_only")
        return []
    except DeskAPIError as exc:
        raise TriageError("resolve", str(exc), EXIT_RESOLVE) from exc


def _configured_ledger_paths(
    ledger_root: Path,
    env: Mapping[str, str] | None = None,
) -> dict[str, list[Path]]:
    values = os.environ if env is None else env
    if not ledger_root.is_dir():
        raise TriageError(
            "in_flight",
            f"ledger root is not a directory: {ledger_root}",
            EXIT_IN_FLIGHT,
        )
    configured: dict[str, list[Path]] = {}
    for canonical, aliases in LEDGER_ALIASES.items():
        env_key = f"RC_VOICEMAIL_LEDGER_{canonical.upper()}"
        explicit = values.get(env_key, "").strip()
        if explicit:
            path = Path(explicit).expanduser()
            if not path.exists():
                raise TriageError(
                    "in_flight",
                    f"configured ledger does not exist: {path}",
                    EXIT_IN_FLIGHT,
                )
            if not _readable_ledger_source(path):
                raise TriageError(
                    "in_flight",
                    f"configured ledger is not readable: {path}",
                    EXIT_IN_FLIGHT,
                )
            configured[canonical] = [path]
            continue

        paths: list[Path] = []
        for subdir in LEDGER_SUBDIRS:
            parent = ledger_root / subdir if subdir else ledger_root
            for alias in aliases:
                for suffix in LEDGER_SUFFIXES:
                    path = parent / f"{alias}{suffix}"
                    if _readable_ledger_source(path) and path not in paths:
                        paths.append(path)
        if not paths:
            aliases_text = " or ".join(aliases)
            raise TriageError(
                "in_flight",
                f"required ledger is missing or unreadable: {canonical} ({aliases_text})",
                EXIT_IN_FLIGHT,
            )
        configured[canonical] = paths
    return configured


def _readable_ledger_source(path: Path) -> bool:
    if path.is_symlink():
        return False
    try:
        if path.is_file():
            with path.open("rb"):
                return True
        if path.is_dir():
            with os.scandir(path) as entries:
                next(entries, None)
            return True
    except OSError:
        return False
    return False


def _ledger_files(paths: Iterable[Path]) -> Iterable[Path]:
    seen: set[Path] = set()
    for path in paths:
        if path.is_symlink():
            continue
        if path.is_file():
            resolved = path.resolve()
            if resolved not in seen:
                seen.add(resolved)
                yield path
        elif path.is_dir():
            for child in sorted(path.rglob("*")):
                if child.is_file() and not child.is_symlink():
                    resolved = child.resolve()
                    if resolved not in seen:
                        seen.add(resolved)
                        yield child


def _search_terms(
    caller: Mapping[str, str | int | None],
    site_candidates: Sequence[Mapping[str, object]],
) -> tuple[set[str], set[str]]:
    text_terms: set[str] = set()
    digit_terms: set[str] = set()
    caller_name = caller.get("callerid_name")
    if isinstance(caller_name, str) and len(caller_name.strip()) >= 3:
        text_terms.add(caller_name.strip().casefold())
    number = digits_only(caller.get("number"))
    if number:
        digit_terms.add(number)
    for candidate in site_candidates:
        for key in ("account_id", "account_name", "contact_id"):
            value = candidate.get(key)
            if value is not None and len(str(value).strip()) >= 3:
                text_terms.add(str(value).strip().casefold())
    return text_terms, digit_terms


def scan_in_flight(
    ledger_root: Path,
    caller: Mapping[str, str | int | None],
    site_candidates: Sequence[Mapping[str, object]],
    *,
    env: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    paths_by_ledger = _configured_ledger_paths(ledger_root, env)
    text_terms, digit_terms = _search_terms(caller, site_candidates)
    if not text_terms and not digit_terms:
        return []
    matches: list[dict[str, str]] = []
    try:
        for ledger, paths in paths_by_ledger.items():
            for path in _ledger_files(paths):
                with path.open(encoding="utf-8", errors="replace") as handle:
                    for line_number, raw_line in enumerate(handle, start=1):
                        one_line = raw_line.rstrip("\r\n")
                        folded = one_line.casefold()
                        normalized_digits = digits_only(one_line)
                        if not (
                            any(term in folded for term in text_terms)
                            or any(term in normalized_digits for term in digit_terms)
                        ):
                            continue
                        try:
                            key_path = path.resolve().relative_to(ledger_root.resolve())
                        except ValueError:
                            key_path = path
                        matches.append(
                            {
                                "ledger": ledger,
                                "key": f"{key_path}:{line_number}",
                                "one_line": one_line,
                            }
                        )
    except OSError as exc:
        raise TriageError("in_flight", f"cannot read ledger: {exc}", EXIT_IN_FLIGHT) from exc
    return matches


def build_output(
    ticket_id: str,
    caller: Mapping[str, str | int | None],
    transcript: str,
    no_speech: bool,
    site_candidates: Sequence[Mapping[str, object]],
    in_flight: Sequence[Mapping[str, str]],
) -> dict[str, object]:
    return {
        "ticket_id": ticket_id,
        "caller": {
            "callerid_name": caller.get("callerid_name"),
            "number": caller.get("number"),
            "duration_s": caller.get("duration_s"),
        },
        "transcript": transcript,
        "no_speech": no_speech,
        "site_candidates": list(site_candidates),
        "in_flight": list(in_flight),
    }


def triage_ticket(
    ticket_id: str,
    desk: DeskClient,
    *,
    transcriber_path: Path,
    ledger_root: Path,
    destination_dir: Path | None = None,
    transcribe_timeout_s: float = DEFAULT_TRANSCRIBE_TIMEOUT_S,
) -> dict[str, object]:
    if destination_dir is None:
        with tempfile.TemporaryDirectory(prefix="rc-voicemail-") as temporary:
            return _triage_in_directory(
                ticket_id,
                desk,
                transcriber_path=transcriber_path,
                ledger_root=ledger_root,
                destination_dir=Path(temporary),
                transcribe_timeout_s=transcribe_timeout_s,
            )
    return _triage_in_directory(
        ticket_id,
        desk,
        transcriber_path=transcriber_path,
        ledger_root=ledger_root,
        destination_dir=destination_dir,
        transcribe_timeout_s=transcribe_timeout_s,
    )


def _triage_in_directory(
    ticket_id: str,
    desk: DeskClient,
    *,
    transcriber_path: Path,
    ledger_root: Path,
    destination_dir: Path,
    transcribe_timeout_s: float,
) -> dict[str, object]:
    audio_path, caller = fetch_voicemail_audio(desk, ticket_id, destination_dir)
    transcript, no_speech = transcribe_audio(
        audio_path,
        transcriber_path,
        timeout_s=transcribe_timeout_s,
    )
    site_candidates = resolve_site_candidates(desk, caller)
    in_flight = scan_in_flight(ledger_root, caller, site_candidates)
    return build_output(ticket_id, caller, transcript, no_speech, site_candidates, in_flight)


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


class _TriageArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TriageError("config", message, EXIT_CONFIG)


def _parser() -> argparse.ArgumentParser:
    parser = _TriageArgumentParser(
        description="Fetch and triage a RingCentral voicemail from a Zoho Desk ticket"
    )
    parser.add_argument("ticket_id", help="Zoho Desk ticket id")
    parser.add_argument(
        "--desk-base-url",
        default=os.environ.get("ZOHO_DESK_BASE_URL", DEFAULT_DESK_BASE_URL),
    )
    parser.add_argument("--desk-org-id", default=os.environ.get("ZOHO_DESK_ORG_ID"))
    parser.add_argument(
        "--dest-dir",
        type=Path,
        default=Path(os.environ["RC_VOICEMAIL_DEST_DIR"]).expanduser()
        if os.environ.get("RC_VOICEMAIL_DEST_DIR")
        else None,
        help="Keep the downloaded mp3 in this directory (temporary by default)",
    )
    parser.add_argument(
        "--transcriber",
        type=Path,
        default=Path(
            os.environ.get(
                "WHISPER_TRANSCRIBE_PATH",
                str(Path(__file__).with_name("whisper_transcribe.py")),
            )
        ).expanduser(),
    )
    parser.add_argument(
        "--ledger-root",
        type=Path,
        default=Path(os.environ.get("RC_VOICEMAIL_LEDGER_ROOT", os.getcwd())).expanduser(),
    )
    parser.add_argument(
        "--timeout",
        type=_positive_float,
        default=DEFAULT_TIMEOUT_S,
        help="Desk request timeout in seconds",
    )
    parser.add_argument(
        "--transcribe-timeout",
        type=_positive_float,
        default=DEFAULT_TRANSCRIBE_TIMEOUT_S,
        help="Whisper helper timeout in seconds",
    )
    return parser


_CLI_OPTIONS_WITH_VALUES = {
    "--desk-base-url",
    "--desk-org-id",
    "--dest-dir",
    "--transcriber",
    "--ledger-root",
    "--timeout",
    "--transcribe-timeout",
}


def _best_effort_ticket_id(argv: Sequence[str]) -> str:
    index = 0
    while index < len(argv):
        token = argv[index]
        if token == "--":
            return argv[index + 1] if index + 1 < len(argv) else ""
        option = token.partition("=")[0]
        if option in _CLI_OPTIONS_WITH_VALUES:
            index += 1 if "=" in token else 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def _error_ticket_id(ticket_id: str) -> str:
    if _SAFE_TICKET_RE.fullmatch(ticket_id):
        return ticket_id
    return repr(ticket_id)


def _desk_client_from_env(
    args: argparse.Namespace,
    env: Mapping[str, str] | None = None,
) -> DeskClient:
    values = os.environ if env is None else env
    mode = select_auth_mode(values)
    if mode == "gateway":
        hosts, secret, agent = load_gateway_credentials(values)
        return DeskClient(
            None,
            mode="gateway",
            org_id=args.desk_org_id,
            gateway_hosts=hosts,
            gateway_secret=secret,
            gateway_agent=agent,
            timeout_s=args.timeout,
        )
    return DeskClient(
        load_access_token(values),
        org_id=args.desk_org_id,
        base_url=args.desk_base_url,
        timeout_s=args.timeout,
    )


def main(argv: Sequence[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    ticket_id = _best_effort_ticket_id(raw_argv)
    try:
        args = _parser().parse_args(raw_argv)
        ticket_id = args.ticket_id
        if not _SAFE_TICKET_RE.fullmatch(ticket_id):
            raise TriageError(
                "config",
                "ticket id contains invalid characters",
                EXIT_CONFIG,
            )
        desk = _desk_client_from_env(args)
        result = triage_ticket(
            ticket_id,
            desk,
            transcriber_path=args.transcriber,
            ledger_root=args.ledger_root,
            destination_dir=args.dest_dir,
            transcribe_timeout_s=args.transcribe_timeout,
        )
    except TriageError as exc:
        print(
            "rc_voicemail_triage: "
            f"stage={exc.stage} ticket_id={_error_ticket_id(ticket_id)} error={exc.message}",
            file=sys.stderr,
        )
        return exc.exit_code
    except Exception as exc:  # Defensive CLI boundary; never emit partial success JSON.
        print(
            "rc_voicemail_triage: "
            f"stage=internal ticket_id={_error_ticket_id(ticket_id)} error={exc}",
            file=sys.stderr,
        )
        return EXIT_INTERNAL
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
