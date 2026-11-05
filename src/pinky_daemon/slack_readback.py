"""Bounded, metadata-only Slack reads using a caller-selected adapter."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from threading import Lock
from time import monotonic

import httpx
from fastapi import HTTPException

from pinky_outreach.slack import SlackError, SlackRateLimitError

REQUEST_SECONDS = 60.0
_WORKERS = 4
_EXECUTOR = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="slack-readback")
_LOCK = Lock()
_OUTSTANDING = 0


class _BusyError(Exception):
    pass


class _DeadlineError(Exception):
    pass


def _release_slot(future):
    global _OUTSTANDING
    with _LOCK:
        _OUTSTANDING -= 1


class _Reader:
    def __init__(self):
        self.deadline = monotonic() + REQUEST_SECONDS

    def remaining(self):
        remaining = self.deadline - monotonic()
        if remaining <= 0:
            raise _DeadlineError
        return remaining

    async def offload(self, call, *args, **kwargs):
        global _OUTSTANDING
        remaining = self.remaining()

        def invoke():
            # Queue time consumes the same budget as HTTP calls and retry waits.
            return call(*args, timeout=min(30.0, self.remaining()), **kwargs)

        # Submitted calls retain capacity until completion, even if their caller stops waiting.
        with _LOCK:
            if _OUTSTANDING >= _WORKERS:
                raise _BusyError
            _OUTSTANDING += 1
        try:
            future = _EXECUTOR.submit(invoke)
        except BaseException:
            with _LOCK:
                _OUTSTANDING -= 1
            raise
        future.add_done_callback(_release_slot)
        try:
            return await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(future)), remaining)
        except TimeoutError:
            raise _DeadlineError from None

    async def call(self, call, *args, **kwargs):
        for attempt in range(4):
            try:
                return await self.offload(call, *args, **kwargs)
            except SlackRateLimitError as exc:
                if attempt == 3:
                    raise
                remaining = self.remaining()
                if exc.retry_after >= remaining:
                    raise _DeadlineError from None
                try:
                    await asyncio.wait_for(asyncio.sleep(exc.retry_after), remaining)
                except TimeoutError:
                    raise _DeadlineError from None

    async def start(self, select_adapter):
        adapter = select_adapter()
        if adapter is None or not adapter.bot_token:
            raise HTTPException(409, {"code": "slack_not_configured"})
        info = await self.call(adapter.get_bot_info)
        bot = {key: info.get(key) for key in ("user_id", "bot_id", "team_id")}
        if (not bot["user_id"] or not bot["team_id"]
                or any(value is not None and not isinstance(value, str) for value in bot.values())):
            raise SlackError("invalid_response")
        _without_token(bot, adapter.bot_token)
        return adapter, bot


def _without_token(value, token):
    if token and token in json.dumps(value, ensure_ascii=False):
        raise SlackError("invalid_response")
    return value


def _messages(page, bot, token):
    messages = page.get("messages")
    if not isinstance(messages, list):
        raise SlackError("invalid_response")
    return [_message(message, bot, token) for message in messages]


def _message(message, bot, token):
    if not isinstance(message, dict):
        raise SlackError("invalid_response")
    text = message.get("text", "")
    fields = {key: message.get(key) for key in
              ("ts", "thread_ts", "user", "bot_id", "app_id", "subtype")}
    if (not isinstance(text, str) or not isinstance(fields["ts"], str)
            or not re.fullmatch(r"[0-9]{9,10}\.[0-9]{6}", fields["ts"])
            or any(value is not None and not isinstance(value, str) for value in fields.values())):
        raise SlackError("invalid_response")
    edited = message.get("edited")
    if edited is not None:
        if not isinstance(edited, dict):
            raise SlackError("invalid_response")
        edited = {key: edited.get(key) for key in ("ts", "user")}
        if any(value is not None and not isinstance(value, str) for value in edited.values()):
            raise SlackError("invalid_response")
    files, reply_count = message.get("files", []), message.get("reply_count", 0)
    if not isinstance(files, list) or type(reply_count) is not int or reply_count < 0:
        raise SlackError("invalid_response")
    try:
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise SlackError("invalid_response") from None
    return _without_token({
        **fields, "edited": edited, "reply_count": reply_count, "files_count": len(files),
        "text_sha256": digest, "text_length": len(text),
        "ours": bool(bot["user_id"] and fields["user"] == bot["user_id"]
                     or bot["bot_id"] and fields["bot_id"] == bot["bot_id"]),
    }, token)


def _slack_error(exc):
    error = exc.error
    if not isinstance(error, str) or not re.fullmatch(r"[a-z_]{1,64}", error):
        error = "invalid_response"
    detail = {"code": "slack_error", "error": error}
    if isinstance(exc, SlackRateLimitError):
        detail["retry_after"] = exc.retry_after
    return HTTPException(502, detail)


async def read_message(select_adapter, channel, ts, thread_ts):
    reader = _Reader()
    try:
        adapter, bot = await reader.start(select_adapter)
        method = "conversations.history"
        params = {"channel": channel, "oldest": ts, "latest": ts, "inclusive": "true", "limit": 1}
        if thread_ts and thread_ts != ts:
            method = "conversations.replies"
            params["ts"] = thread_ts
            params["limit"] = 2
        page = await reader.call(adapter._request_form, method, **params)
        messages = _messages(page, bot, adapter.bot_token)
        message = next((message for message in messages if message["ts"] == ts), None)
        return {"found": message is not None, "channel": channel, "ts": ts,
                "message": message, "bot": bot}
    except _BusyError:
        raise HTTPException(503, {"code": "slack_busy"}) from None
    except SlackError as exc:
        raise _slack_error(exc) from None
    except (httpx.HTTPError, _DeadlineError):
        raise HTTPException(504, {"code": "slack_unreachable"}) from None


async def read_history(select_adapter, channel, oldest, latest, max_pages):
    reader = _Reader()
    try:
        adapter, bot = await reader.start(select_adapter)
        result = {"complete": False, "stopped_reason": None, "pages": 0,
                  "channel": channel, "oldest": oldest, "latest": latest,
                  "threads_included": False, "messages": [], "bot": bot}
        cursor, seen = None, set()
        while result["pages"] < max_pages:
            try:
                page = await reader.call(adapter._request_form, "conversations.history",
                                         channel=channel, oldest=oldest, latest=latest,
                                         inclusive="true", limit=200, cursor=cursor)
            except _BusyError:
                if not result["pages"]:
                    raise
                result["stopped_reason"] = "busy"
                return result
            except SlackRateLimitError:
                result["stopped_reason"] = "rate_limited"
                return result
            except _DeadlineError:
                result["stopped_reason"] = "deadline"
                return result
            messages = _messages(page, bot, adapter.bot_token)
            if type(page.get("has_more")) is not bool:
                raise SlackError("invalid_response")
            result["messages"].extend(messages)
            result["pages"] += 1
            if not page["has_more"]:
                result["complete"] = True
                return result
            metadata = page.get("response_metadata", {})
            if not isinstance(metadata, dict):
                raise SlackError("invalid_response")
            cursor = metadata.get("next_cursor", "")
            if not isinstance(cursor, str):
                raise SlackError("invalid_response")
            if not cursor or cursor in seen:
                result["stopped_reason"] = "invalid_cursor"
                return result
            seen.add(cursor)
        result["stopped_reason"] = "page_cap"
        return result
    except _BusyError:
        raise HTTPException(503, {"code": "slack_busy"}) from None
    except SlackError as exc:
        raise _slack_error(exc) from None
    except (httpx.HTTPError, _DeadlineError):
        raise HTTPException(504, {"code": "slack_unreachable"}) from None
