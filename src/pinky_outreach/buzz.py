"""Native Buzz outbound adapter using signed Nostr events and NIP-98 HTTP auth."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit

import coincurve
import httpx

from pinky_outreach.types import Message, Platform

_HEX_64_RE = re.compile(r"^[0-9a-f]{64}$")
_COMMUNITY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_ASCII_AT_WORD_RE = re.compile(r"@(?=\w)", flags=re.UNICODE)
_MAX_REWRITE_POSITIONS = 32
_REFERENCE_EVENT_PREFIX = 16
_MAX_REFERENCE_LINE = 192


class BuzzError(RuntimeError):
    """Base Buzz adapter failure."""


class BuzzProtocolError(BuzzError):
    """Buzz returned or was given an invalid protocol object."""


class BuzzTransportError(BuzzError):
    """Buzz relay transport failed."""


@dataclass(frozen=True)
class MentionNormalization:
    content: str
    rewrite_count: int
    positions: tuple[int, ...]


@dataclass(frozen=True)
class VerifiedReplyMetadata:
    event_id: str
    kind: int
    author_pubkey: str
    channel_id: str


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def normalize_buzz_mentions(content: str) -> MentionNormalization:
    """Replace every parser-matching ASCII ``@word`` introducer visibly.

    This intentionally also covers emails and inline/fenced code: Buzz parses
    the final wire string, not Markdown semantics, so those contexts cannot be
    exempted safely.
    """
    if not isinstance(content, str):
        raise TypeError("Buzz content must be a string")
    positions = tuple(match.start() for match in _ASCII_AT_WORD_RE.finditer(content))
    return MentionNormalization(
        content=_ASCII_AT_WORD_RE.sub("＠", content),
        rewrite_count=len(positions),
        positions=positions[:_MAX_REWRITE_POSITIONS],
    )


def _canonical_event_bytes(
    *, pubkey: str, created_at: int, kind: int, tags: list[list[str]], content: str
) -> bytes:
    return json.dumps(
        [0, pubkey, created_at, kind, tags, content],
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def verify_nostr_event(event: dict) -> bool:
    """Strictly recompute and BIP340-verify one Nostr event."""
    try:
        required = {"id", "pubkey", "created_at", "kind", "tags", "content", "sig"}
        if set(event) != required:
            return False
        if not _HEX_64_RE.fullmatch(event["id"]):
            return False
        if not _HEX_64_RE.fullmatch(event["pubkey"]):
            return False
        if not isinstance(event["created_at"], int) or isinstance(event["created_at"], bool):
            return False
        if not isinstance(event["kind"], int) or isinstance(event["kind"], bool):
            return False
        if not isinstance(event["content"], str) or not isinstance(event["tags"], list):
            return False
        if not isinstance(event["sig"], str) or len(event["sig"]) != 128:
            return False
        if any(
            not isinstance(tag, list) or any(not isinstance(part, str) for part in tag)
            for tag in event["tags"]
        ):
            return False
        computed = hashlib.sha256(
            _canonical_event_bytes(
                pubkey=event["pubkey"],
                created_at=event["created_at"],
                kind=event["kind"],
                tags=event["tags"],
                content=event["content"],
            )
        ).hexdigest()
        if not secrets.compare_digest(computed, event["id"]):
            return False
        return coincurve.PublicKeyXOnly(bytes.fromhex(event["pubkey"])).verify(
            bytes.fromhex(event["sig"]), bytes.fromhex(computed)
        )
    except Exception:
        return False


class _NostrSigner:
    """Small signing helper whose repr never contains the private scalar."""

    def __init__(self, private_key: bytes) -> None:
        if not isinstance(private_key, bytes) or len(private_key) != 32:
            raise BuzzProtocolError("Buzz private key must be 32 bytes")
        try:
            self._key = coincurve.PrivateKey(private_key)
        except Exception as exc:
            raise BuzzProtocolError("Buzz private key is not a valid secp256k1 scalar") from exc
        self.pubkey = self._key.public_key_xonly.format().hex()

    def __repr__(self) -> str:
        return f"_NostrSigner(pubkey={self.pubkey!r}, private_key=<redacted>)"

    def sign_event(
        self,
        *,
        kind: int,
        tags: list[list[str]],
        content: str,
        created_at: int | None = None,
    ) -> dict:
        timestamp = int(time.time()) if created_at is None else int(created_at)
        event_id = hashlib.sha256(
            _canonical_event_bytes(
                pubkey=self.pubkey,
                created_at=timestamp,
                kind=kind,
                tags=tags,
                content=content,
            )
        ).hexdigest()
        return {
            "id": event_id,
            "pubkey": self.pubkey,
            "created_at": timestamp,
            "kind": kind,
            "tags": tags,
            "content": content,
            "sig": self._key.sign_schnorr(bytes.fromhex(event_id)).hex(),
        }


def _validate_channel_id(channel_id: str) -> str:
    try:
        parsed = uuid.UUID(str(channel_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise BuzzProtocolError("Buzz chat_id must be a channel UUID") from exc
    canonical = str(parsed)
    if canonical != str(channel_id).lower():
        raise BuzzProtocolError("Buzz chat_id must be a canonical channel UUID")
    return canonical


def _validate_relay_url(relay_url: str) -> tuple[str, str]:
    parsed = urlsplit(relay_url)
    if (
        parsed.scheme not in {"wss", "ws"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise BuzzProtocolError("Buzz relay_url must be a plain ws:// or wss:// URL")
    http_scheme = "https" if parsed.scheme == "wss" else "http"
    base = urlunsplit((http_scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    return relay_url, base


class BuzzAdapter:
    """Synchronous outbound adapter used from the broker executor."""

    def __init__(
        self,
        private_key: bytes,
        *,
        relay_url: str,
        community_id: str,
        timeout: float = 20.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._signer = _NostrSigner(private_key)
        self.relay_url, self._http_base = _validate_relay_url(relay_url)
        community = str(community_id or "").lower()
        if not _COMMUNITY_RE.fullmatch(community):
            raise BuzzProtocolError("Buzz community_id is invalid")
        self.community_id = community
        self._client = httpx.Client(timeout=timeout, transport=transport)

    @property
    def pubkey(self) -> str:
        return self._signer.pubkey

    def __repr__(self) -> str:
        return (
            f"BuzzAdapter(pubkey={self.pubkey!r}, relay_url={self.relay_url!r}, "
            f"community_id={self.community_id!r}, private_key=<redacted>)"
        )

    def close(self) -> None:
        self._client.close()

    def _authorization(self, url: str, body: bytes) -> str:
        auth = self._signer.sign_event(
            kind=27235,
            tags=[
                ["u", url],
                ["method", "POST"],
                ["nonce", secrets.token_hex(16)],
                ["payload", hashlib.sha256(body).hexdigest()],
            ],
            content="",
        )
        encoded = base64.b64encode(
            json.dumps(auth, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        ).decode("ascii")
        return f"Nostr {encoded}"

    def _post(self, path: str, payload: object) -> object:
        url = f"{self._http_base}{path}"
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        try:
            response = self._client.post(
                url,
                content=body,
                headers={
                    "Authorization": self._authorization(url, body),
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    "User-Agent": "PinkyBot-Buzz/1.0",
                },
            )
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise BuzzTransportError(
                f"Buzz relay request failed for {path} ({type(exc).__name__})"
            ) from exc

    def _query_reply(self, event_id: str, channel_id: str) -> VerifiedReplyMetadata:
        if not _HEX_64_RE.fullmatch(event_id):
            raise BuzzProtocolError("Buzz reply target must be a 64-hex event id")
        result = self._post("/query", [{"ids": [event_id], "limit": 1}])
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], dict):
            raise BuzzProtocolError("Buzz reply target was not found uniquely")
        event = result[0]
        if not verify_nostr_event(event) or event["id"] != event_id:
            raise BuzzProtocolError("Buzz reply target failed event verification")
        channels = [tag[1] for tag in event["tags"] if len(tag) >= 2 and tag[0] == "h"]
        if channels != [channel_id]:
            raise BuzzProtocolError("Buzz reply target belongs to a different channel")
        return VerifiedReplyMetadata(
            event_id=event["id"],
            kind=event["kind"],
            author_pubkey=event["pubkey"],
            channel_id=channel_id,
        )

    @staticmethod
    def _stored_reply_metadata(
        metadata: dict | None,
        *,
        event_id: str,
        channel_id: str,
    ) -> VerifiedReplyMetadata | None:
        if not metadata:
            return None
        candidate = metadata.get("buzz_verified_event")
        if not isinstance(candidate, dict) or candidate.get("verified") is not True:
            return None
        author = str(candidate.get("author_pubkey") or "").lower()
        stored_event = str(candidate.get("event_id") or "").lower()
        stored_channel = str(candidate.get("channel_id") or "").lower()
        kind = candidate.get("kind")
        if (
            stored_event != event_id
            or stored_channel != channel_id
            or not _HEX_64_RE.fullmatch(author)
            or not isinstance(kind, int)
            or isinstance(kind, bool)
        ):
            raise BuzzProtocolError("stored Buzz reply metadata is invalid")
        return VerifiedReplyMetadata(
            event_id=stored_event,
            kind=kind,
            author_pubkey=author,
            channel_id=stored_channel,
        )

    def _publish(self, *, kind: int, tags: list[list[str]], content: str) -> tuple[dict, int]:
        normalized = normalize_buzz_mentions(content)
        event = self._signer.sign_event(kind=kind, tags=tags, content=normalized.content)
        if normalized.rewrite_count:
            _log(
                "buzz-normalize: "
                f"event_id={event['id']} rewrites={normalized.rewrite_count} "
                "rule=ascii-at-word-to-fullwidth reason=buzz-parser-safety "
                f"positions={list(normalized.positions)}"
            )
        self._post("/events", event)
        return event, normalized.rewrite_count

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        reply_to: str | None = None,
        reply_metadata: dict | None = None,
    ) -> Message:
        channel = _validate_channel_id(channel_id)
        tags: list[list[str]] = [["h", channel]]
        final_content = content
        if reply_to:
            target_id = str(reply_to).lower()
            if not _HEX_64_RE.fullmatch(target_id):
                raise BuzzProtocolError("Buzz reply target must be a 64-hex event id")
            stored_target = self._stored_reply_metadata(
                reply_metadata,
                event_id=target_id,
                channel_id=channel,
            )
            target = stored_target
            if target is None:
                target = self._query_reply(target_id, channel)
            if target.kind == 20002:
                if stored_target is None:
                    raise BuzzProtocolError(
                        "ephemeral Buzz replies require verified stored metadata"
                    )
                # Reference-only: no raw ephemeral content is accepted or read.
                reference = (
                    f"[reply to ephemeral {target.event_id[:_REFERENCE_EVENT_PREFIX]} "
                    f"by buzz:{self.community_id}:{target.author_pubkey}]"
                )
                if "\n" in reference or len(reference) > _MAX_REFERENCE_LINE:
                    raise BuzzProtocolError("Buzz ephemeral reference metadata is too long")
                final_content = f"{reference}\n{content}"
            elif target.kind == 9:
                tags.append(["e", target.event_id, "", "reply"])
            else:
                raise BuzzProtocolError(f"Buzz event kind {target.kind} cannot be replied to")
        event, rewrites = self._publish(kind=9, tags=tags, content=final_content)
        return Message(
            platform=Platform.buzz,
            chat_id=channel,
            sender=self.pubkey,
            content="",
            timestamp=datetime.now(timezone.utc),
            message_id=event["id"],
            reply_to=str(reply_to or ""),
            is_outbound=True,
            metadata={"mention_normalized": bool(rewrites), "rewrite_count": rewrites},
        )

    def add_reaction(self, channel_id: str, event_id: str, emoji: str) -> Message:
        channel = _validate_channel_id(channel_id)
        target = str(event_id or "").lower()
        if not _HEX_64_RE.fullmatch(target):
            raise BuzzProtocolError("Buzz reaction target must be a 64-hex event id")
        if not isinstance(emoji, str) or not emoji.strip() or "\n" in emoji:
            raise BuzzProtocolError("Buzz reaction emoji must be one non-empty line")
        event, rewrites = self._publish(kind=7, tags=[["e", target]], content=emoji.strip())
        return Message(
            platform=Platform.buzz,
            chat_id=channel,
            sender=self.pubkey,
            content="",
            timestamp=datetime.now(timezone.utc),
            message_id=event["id"],
            reply_to=target,
            is_outbound=True,
            metadata={"mention_normalized": bool(rewrites), "rewrite_count": rewrites},
        )


__all__ = [
    "BuzzAdapter",
    "BuzzError",
    "BuzzProtocolError",
    "BuzzTransportError",
    "MentionNormalization",
    "VerifiedReplyMetadata",
    "normalize_buzz_mentions",
    "verify_nostr_event",
]
