"""Inbound Slack mention/link resolution (BrokerSlackPoller text enrichment)."""

import asyncio

from pinky_daemon.pollers import resolve_slack_text_refs

_USERS = {"U774M8XDE": "Brett Jones", "U123": "Alice"}
_CHANNELS = {"C1": "orders-and-equipment"}


async def _user(uid: str) -> str:
    return _USERS.get(uid, "")


async def _chan(cid: str) -> str:
    return _CHANNELS.get(cid, "")


def _run(text: str) -> str:
    return asyncio.run(
        resolve_slack_text_refs(text, user_namer=_user, channel_namer=_chan)
    )


def test_user_mention_resolved_to_name():
    assert _run("Hi <@U774M8XDE> there") == "Hi @Brett Jones there"


def test_user_mention_with_embedded_label_no_lookup():
    # label after | wins; no resolver call needed
    assert _run("ping <@U999|brett>") == "ping @brett"


def test_unknown_user_keeps_raw_id():
    # resolver returns "" (e.g. users:read missing) → raw id retained, no crash
    assert _run("yo <@UZZZ>") == "yo @UZZZ"


def test_channel_mention_with_inline_name():
    assert _run("see <#C1|orders-and-equipment>") == "see #orders-and-equipment"


def test_channel_mention_without_name_resolved():
    assert _run("see <#C1>") == "see #orders-and-equipment"


def test_special_mentions():
    assert _run("<!here> <!channel> <!everyone>") == "@here @channel @everyone"


def test_subteam_mention():
    assert _run("ask <!subteam^S1|@project-mgmt>") == "ask @project-mgmt"


def test_date_token_uses_fallback():
    assert _run("due <!date^1700000000^{date}|Nov 14>") == "due Nov 14"


def test_link_with_label():
    assert _run("click <https://example.com|here>") == "click here"


def test_bare_link_keeps_url():
    assert _run("go <https://example.com>") == "go https://example.com"


def test_mailto_link():
    assert _run("mail <mailto:user@example.com|user@example.com>") == (
        "mail user@example.com"
    )


def test_html_entities_unescaped():
    assert _run("a &amp; b &lt;3 x&gt;y") == "a & b <3 x>y"


def test_plain_text_untouched():
    assert _run("just a normal message") == "just a normal message"


def test_empty_text():
    assert _run("") == ""


def test_multiple_mentions_in_one_message():
    out = _run("Hi <@U774M8XDE> and <@U123> in <#C1|orders>")
    assert out == "Hi @Brett Jones and @Alice in #orders"


def test_screenshot_case():
    # The exact failure Brad reported: a raw <@U…> in a nudge body.
    raw = "Hi <@U774M8XDE> Re: Savor Ice Cream — Install 1905 Notre Dame Blvd"
    assert _run(raw) == "Hi @Brett Jones Re: Savor Ice Cream — Install 1905 Notre Dame Blvd"
