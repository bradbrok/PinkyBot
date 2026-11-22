"""Validated, resumable fold history for bounded transcript scans."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

MAX_PENDING_ROW_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True)
class _TranscriptProbeRow:
    offset: int
    end: int
    raw: bytes
    raw_start: int = 0


@dataclass(frozen=True)
class _TranscriptProbeFoldChain:
    prompt: str
    enqueue: _TranscriptProbeRow
    remove: _TranscriptProbeRow
    attachment: _TranscriptProbeRow


@dataclass
class _TranscriptProbeFoldOccurrence:
    prompt: str
    enqueue: _TranscriptProbeRow
    attachment: _TranscriptProbeRow | None = None


@dataclass
class _Checkpoint:
    path: Path
    identity: tuple[int, int]
    offset: int = 0
    cursor: int = 0
    anchor_digest: bytes = field(default_factory=lambda: hashlib.sha256(b"").digest())
    cursor_digest: bytes = field(default_factory=lambda: hashlib.sha256(b"").digest())
    open_occurrences: list[_TranscriptProbeFoldOccurrence] = field(default_factory=list)
    pending_removes: list[tuple[_TranscriptProbeFoldOccurrence, _TranscriptProbeRow]] = field(
        default_factory=list
    )
    complete_chains: list[_TranscriptProbeFoldChain] = field(default_factory=list)
    primary_chains: list[_TranscriptProbeFoldChain] = field(default_factory=list)
    pending_chunks: list[bytes] = field(default_factory=list)
    pending_size: int = 0
    complete: bool = False
    oversized: bool = False


@dataclass(frozen=True)
class FoldHistoryRead:
    chains: list[_TranscriptProbeFoldChain]
    complete: bool
    remaining: int


def _descriptor(handle):
    current = handle
    for _ in range(4):
        fileno = getattr(current, "fileno", None)
        if fileno is not None:
            return int(fileno())
        current = getattr(current, "_wrapped")
    raise AttributeError("opened transcript has no file descriptor")


@dataclass
class _Budget:
    remaining: int

    def readline(self, handle):
        raw = handle.readline(self.remaining)
        self.remaining -= len(raw)
        return raw

    def anchor(self, descriptor, offset, size):
        count = min(offset, size)
        if count > self.remaining:
            raise ValueError("insufficient validation budget")
        raw = os.pread(descriptor, count, offset - count)
        self.remaining -= len(raw)
        if len(raw) != count:
            raise ValueError("short validation anchor")
        return raw


def _record_chain(state, occurrence, remove, attachment):
    primary = _TranscriptProbeFoldChain(occurrence.prompt, occurrence.enqueue, remove, attachment)
    state.primary_chains.append(primary)
    state.complete_chains.append(primary)
    state.complete_chains.extend(
        _TranscriptProbeFoldChain(
            occurrence.prompt, alternative.enqueue, remove, alternative.attachment
        )
        for alternative in state.open_occurrences
        if alternative.prompt == occurrence.prompt and alternative.attachment is not None
    )


def _consume(state, parsed, row):
    entry_type = parsed.get("type")
    if entry_type == "queue-operation":
        operation, content = parsed.get("operation"), parsed.get("content")
        if operation == "enqueue" and isinstance(content, str):
            state.open_occurrences.append(_TranscriptProbeFoldOccurrence(content, row))
        elif operation == "remove" and isinstance(content, str):
            index = next(
                (
                    i
                    for i, occurrence in enumerate(state.open_occurrences)
                    if occurrence.prompt == content
                ),
                None,
            )
            if index is not None:
                occurrence = state.open_occurrences.pop(index)
                if occurrence.attachment is None:
                    state.pending_removes.append((occurrence, row))
                else:
                    _record_chain(state, occurrence, row, occurrence.attachment)
        return
    if entry_type != "attachment":
        return
    attachment = parsed.get("attachment")
    if (
        not isinstance(attachment, dict)
        or attachment.get("type") != "queued_command"
        or not isinstance(attachment.get("prompt"), str)
    ):
        return
    prompt = attachment["prompt"]
    index = next(
        (
            i
            for i, (occurrence, _remove) in enumerate(state.pending_removes)
            if occurrence.prompt == prompt
        ),
        None,
    )
    if index is not None:
        occurrence, remove = state.pending_removes.pop(index)
        _record_chain(state, occurrence, remove, row)
        return
    occurrence = next(
        (
            item
            for item in state.open_occurrences
            if item.prompt == prompt and item.attachment is None
        ),
        None,
    )
    if occurrence is not None:
        occurrence.attachment = row
        state.complete_chains.extend(
            _TranscriptProbeFoldChain(prompt, occurrence.enqueue, primary.remove, row)
            for primary in state.primary_chains
            if primary.prompt == prompt
        )


class FoldHistoryIndex:
    """Keep at most four validated source checkpoints; never cache candidate claims."""

    def __init__(self, *, max_sources=4, anchor_bytes=4096):
        self.max_sources = max_sources
        self.anchor_bytes = anchor_bytes
        self.max_pending_row_bytes = MAX_PENDING_ROW_BYTES
        self._states: OrderedDict[Path, _Checkpoint] = OrderedDict()

    def clear(self):
        self._states.clear()

    def discard(self, path):
        self._states.pop(path, None)

    def _row(self, offset, raw):
        start = max(0, len(raw) - self.anchor_bytes)
        return _TranscriptProbeRow(offset, offset + len(raw), raw[start:], start)

    def _cold(self, handle, path, identity, budget):
        """The original byte-zero loop, including its exact capped EOF behavior."""
        state = _Checkpoint(path, identity)
        complete = False
        try:
            handle.seek(0)
            while budget.remaining > 0:
                offset = handle.tell()
                raw = budget.readline(handle)
                if not raw:
                    complete = True
                    break
                if not raw.endswith(b"\n"):
                    break
                try:
                    parsed = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    break
                if isinstance(parsed, dict):
                    _consume(state, parsed, self._row(offset, raw))
        except Exception:
            complete = False
        return FoldHistoryRead(
            state.complete_chains if complete else [], complete, budget.remaining
        )

    def _validate(self, state, handle, identity, budget):
        opened = os.fstat(_descriptor(handle))
        if state.identity != identity:
            raise ValueError("source identity changed")
        if (opened.st_dev, opened.st_ino) != identity:
            raise ValueError("opened source identity changed")
        if opened.st_size < max(state.offset, state.cursor):
            raise ValueError("source truncated")
        tail = budget.anchor(_descriptor(handle), state.offset, self.anchor_bytes)
        if hashlib.sha256(tail).digest() != state.anchor_digest:
            raise ValueError("checkpoint anchor changed")
        if state.cursor != state.offset:
            cursor_tail = budget.anchor(_descriptor(handle), state.cursor, self.anchor_bytes)
            if hashlib.sha256(cursor_tail).digest() != state.cursor_digest:
                raise ValueError("partial-row anchor changed")
        else:
            cursor_tail = tail
        return tail, cursor_tail, opened.st_size

    def read(self, handle, path, identity, remaining, *, use_cache=True):
        budget = _Budget(remaining)
        path = Path(path)
        if not use_cache:
            return self._cold(handle, path, identity, budget)
        try:
            cached = self._states.get(path)
            if cached is None:
                state = _Checkpoint(path, identity)
                tail = cursor_tail = b""
                size = os.fstat(_descriptor(handle)).st_size
            else:
                tail, cursor_tail, size = self._validate(cached, handle, identity, budget)
                if cached.cursor == size and cached.complete:
                    self._states.move_to_end(path)
                    return FoldHistoryRead(cached.complete_chains, True, budget.remaining)
                state = copy.deepcopy(cached)
            state.complete = False
            handle.seek(state.cursor)
            while budget.remaining > 0 and not state.oversized:
                raw = budget.readline(handle)
                if not raw:
                    break
                state.cursor += len(raw)
                cursor_tail = (
                    raw[-self.anchor_bytes :]
                    if len(raw) >= self.anchor_bytes
                    else (cursor_tail + raw)[-self.anchor_bytes :]
                )
                state.cursor_digest = hashlib.sha256(cursor_tail).digest()
                if state.pending_size + len(raw) > self.max_pending_row_bytes:
                    state.oversized = True
                    state.pending_chunks = []
                    state.pending_size = 0
                    break
                if not raw.endswith(b"\n"):
                    state.pending_chunks.append(raw)
                    state.pending_size += len(raw)
                    break
                if state.pending_chunks:
                    raw = b"".join([*state.pending_chunks, raw])
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    _consume(state, parsed, self._row(state.offset, raw))
                tail = (
                    raw[-self.anchor_bytes :]
                    if len(raw) >= self.anchor_bytes
                    else (tail + raw)[-self.anchor_bytes :]
                )
                state.offset = state.cursor
                state.anchor_digest = hashlib.sha256(tail).digest()
                state.pending_chunks = []
                state.pending_size = 0
            final = os.fstat(_descriptor(handle))
            if (final.st_dev, final.st_ino) != identity or final.st_size < state.cursor:
                raise ValueError("source changed during scan")
            state.complete = (
                not state.oversized and not state.pending_chunks and state.cursor == final.st_size
            )
            self._states[path] = state
            self._states.move_to_end(path)
            while len(self._states) > self.max_sources:
                self._states.popitem(last=False)
            return FoldHistoryRead(
                state.complete_chains if state.complete else [], state.complete, budget.remaining
            )
        except Exception:
            self._states.pop(path, None)
            return self._cold(handle, path, identity, budget)
