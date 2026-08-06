"""Wire-shape and content-safety tests for the native Buzz sender."""

from __future__ import annotations

import base64
import hashlib
import json
import re

import httpx
import pytest

from pinky_outreach.buzz import BuzzAdapter, BuzzProtocolError, verify_nostr_event

PRIVATE_KEY = bytes.fromhex("11" * 32)
OTHER_PRIVATE_KEY = bytes.fromhex("22" * 32)
CHANNEL = "00000000-0000-4000-8000-000000000001"


def _capturing_adapter(*, query_events=None):
    captured: list[dict] = []
    query_events = list(query_events or [])

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append({"path": request.url.path, "body": body, "headers": request.headers})
        if request.url.path == "/query":
            return httpx.Response(200, json=query_events)
        return httpx.Response(200, json={"accepted": True})

    adapter = BuzzAdapter(
        PRIVATE_KEY,
        relay_url="wss://example.communities.buzz.xyz",
        community_id="example",
        transport=httpx.MockTransport(handler),
    )
    return adapter, captured


def _signed_parent(*, kind: int, content: str = "parent") -> dict:
    adapter = BuzzAdapter(
        OTHER_PRIVATE_KEY,
        relay_url="wss://example.communities.buzz.xyz",
        community_id="example",
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json={})),
    )
    return adapter._signer.sign_event(
        kind=kind,
        tags=[["h", CHANNEL]],
        content=content,
        created_at=1_700_000_000,
    )


def _posted_event(captured: list[dict]) -> dict:
    return next(item["body"] for item in captured if item["path"] == "/events")


def test_send_uses_cli_compatible_kind9_h_tag_and_nip98():
    adapter, captured = _capturing_adapter()
    result = adapter.send_message(CHANNEL, "hello")

    request = captured[0]
    event = request["body"]
    assert request["path"] == "/events"
    assert event["kind"] == 9
    assert event["tags"] == [["h", CHANNEL]]
    assert event["content"] == "hello"
    assert verify_nostr_event(event)
    assert result.message_id == event["id"]

    scheme, token = request["headers"]["authorization"].split(" ", 1)
    auth = json.loads(base64.b64decode(token))
    assert scheme == "Nostr"
    assert auth["kind"] == 27235
    assert verify_nostr_event(auth)
    assert [tag[0] for tag in auth["tags"]] == ["u", "method", "nonce", "payload"]
    payload_tag = next(tag[1] for tag in auth["tags"] if tag[0] == "payload")
    canonical_body = json.dumps(event, separators=(",", ":"), ensure_ascii=False).encode()
    assert payload_tag == hashlib.sha256(canonical_body).hexdigest()


def test_thread_uses_four_field_reply_marker_after_parent_verification():
    parent = _signed_parent(kind=9)
    adapter, captured = _capturing_adapter(query_events=[parent])
    adapter.send_message(CHANNEL, "thread reply", reply_to=parent["id"])

    assert captured[0]["path"] == "/query"
    event = _posted_event(captured)
    assert event["tags"] == [
        ["h", CHANNEL],
        ["e", parent["id"], "", "reply"],
    ]
    assert verify_nostr_event(event)


def test_ephemeral_reply_uses_verified_stored_reference_only_and_no_e_tag():
    parent = _signed_parent(kind=20002, content="RAW EPHEMERAL @secret MUST NOT PERSIST")
    adapter, captured = _capturing_adapter()
    metadata = {
        "buzz_verified_event": {
            "verified": True,
            "event_id": parent["id"],
            "kind": 20002,
            "author_pubkey": parent["pubkey"],
            "channel_id": CHANNEL,
            # Deliberately ignored even if a future context carries it.
            "content": parent["content"],
        }
    }
    adapter.send_message(
        CHANNEL,
        "answer @owner",
        reply_to=parent["id"],
        reply_metadata=metadata,
    )

    assert [item["path"] for item in captured] == ["/events"]
    event = _posted_event(captured)
    assert event["tags"] == [["h", CHANNEL]]
    assert "RAW EPHEMERAL" not in event["content"]
    assert parent["id"][:16] in event["content"]
    assert f"buzz:example:{parent['pubkey']}" in event["content"]
    assert "＠owner" in event["content"]
    assert "\n" in event["content"]
    assert verify_nostr_event(event)


def test_ephemeral_reply_without_stored_metadata_refuses_before_publish():
    parent = _signed_parent(kind=20002, content="do not retain")
    adapter, captured = _capturing_adapter(query_events=[parent])
    with pytest.raises(BuzzProtocolError, match="verified stored metadata"):
        adapter.send_message(CHANNEL, "reply", reply_to=parent["id"])
    assert [item["path"] for item in captured] == ["/query"]


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("@alice start", "＠alice start"),
        ("middle @alice token", "middle ＠alice token"),
        ("@alice and @bob", "＠alice and ＠bob"),
        ("mail alice@example.com", "mail alice＠example.com"),
        ("inline `@code`", "inline `＠code`"),
        ("fenced ```\n@code\n```", "fenced ```\n＠code\n```"),
        ("Unicode @Мария and ＠kept", "Unicode ＠Мария and ＠kept"),
    ],
)
def test_normalization_is_applied_to_final_signed_wire_content(source, expected, capsys):
    adapter, captured = _capturing_adapter()
    result = adapter.send_message(CHANNEL, source)

    event = _posted_event(captured)
    assert event["content"] == expected
    assert re.search(r"@(?=\w)", event["content"], flags=re.UNICODE) is None
    assert verify_nostr_event(event)
    assert result.metadata["mention_normalized"] is True
    logs = capsys.readouterr().err
    assert "rule=ascii-at-word-to-fullwidth" in logs
    assert source not in logs
    assert expected not in logs


def test_reaction_uses_kind7_e_tag():
    adapter, captured = _capturing_adapter()
    target = "33" * 32
    result = adapter.add_reaction(CHANNEL, target, "👍")

    event = _posted_event(captured)
    assert event["kind"] == 7
    assert event["tags"] == [["e", target]]
    assert event["content"] == "👍"
    assert verify_nostr_event(event)
    assert result.reply_to == target
