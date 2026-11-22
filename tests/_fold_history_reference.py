"""Frozen byte-zero reference from 6e13255, with only naming/format changes."""
from __future__ import annotations

import json
import os
import sys
from contextlib import ExitStack

from pinky_daemon.tmux_session import (
    _TRANSCRIPT_BOUNDARY_ANCHOR_BYTES,
    _InflightMeta,
    _TranscriptCandidateSource,
    _TranscriptProbeFoldChain,
    _TranscriptProbeFoldOccurrence,
    _TranscriptProbeRow,
    _TranscriptSourceKey,
)

_PHANTOM_TRANSCRIPT_SCAN_BYTES = 4 * 1024 * 1024


def _probe_with_classes(session, candidates, call=None):
    """Observe existing local scan flags without adding a production test hook."""
    from pinky_daemon.tmux_session import TmuxSession

    call = call or TmuxSession._phantom_consumption_verdicts
    snapshot = {}
    previous = sys.getprofile()

    def profile(frame, event, _arg):
        if event == "return" and frame.f_code is call.__code__:
            snapshot.update(frame.f_locals)

    sys.setprofile(profile)
    try:
        verdicts = call(session, candidates)
    finally:
        sys.setprofile(previous)
    incomplete = snapshot.get("incomplete", set())
    exhausted = snapshot.get("budget_exhausted", set())
    fold_incomplete = snapshot.get("fold_history_incomplete", set())
    classes = []
    for source in snapshot["sources"]:
        if source is None or source[0] in incomplete or source[0] in exhausted:
            classes.append("b")
        elif source[0] in fold_incomplete:
            classes.append("c")
        else:
            classes.append("a")
    return verdicts, classes


def _assert_reference(session, candidates, verdicts, classes):
    from pinky_daemon import tmux_session

    global _PHANTOM_TRANSCRIPT_SCAN_BYTES
    previous = _PHANTOM_TRANSCRIPT_SCAN_BYTES
    try:
        _PHANTOM_TRANSCRIPT_SCAN_BYTES = tmux_session._PHANTOM_TRANSCRIPT_SCAN_BYTES
        capped = _reference_consumption_verdicts(session, candidates)
        _PHANTOM_TRANSCRIPT_SCAN_BYTES = sys.maxsize
        unlimited = _reference_consumption_verdicts(session, candidates)
    finally:
        _PHANTOM_TRANSCRIPT_SCAN_BYTES = previous
    expected = [unlimited[i] if kind == "a" else capped[i] for i, kind in enumerate(classes)]
    assert verdicts == expected, (classes, verdicts, capped, unlimited)


def pytest_addoption(parser):
    parser.addoption("--fold-history-differential", action="store_true")


def pytest_configure(config):
    if not config.getoption("--fold-history-differential"):
        return
    from pinky_daemon.tmux_session import TmuxSession

    original = TmuxSession._phantom_consumption_verdicts

    def checked(session, candidates):
        verdicts, classes = _probe_with_classes(session, candidates, call=original)
        _assert_reference(session, candidates, verdicts, classes)
        return verdicts

    TmuxSession._phantom_consumption_verdicts = checked

def _reference_consumption_verdicts(self, candidates: list[_InflightMeta]) -> list[bool | None]:
    sources: list[_TranscriptCandidateSource | None] = []
    handles: dict[_TranscriptSourceKey, object] = {}
    scan_starts: dict[_TranscriptSourceKey, int] = {}
    budget_remaining = _PHANTOM_TRANSCRIPT_SCAN_BYTES
    anchor_checks: dict[tuple[_TranscriptSourceKey, int, int, bytes], bool] = {}

    def _descriptor(handle: object) -> int:
        current = handle
        for _ in range(4):
            fileno = getattr(current, 'fileno', None)
            if fileno is not None:
                return int(fileno())
            current = getattr(current, '_wrapped')
        raise AttributeError('opened transcript has no file descriptor')
    with ExitStack() as stack:
        for entry in candidates:
            ticket_had_path = entry.transcript_path_at_paste is not None
            path = entry.transcript_path_at_paste
            if path is None:
                tailer = self._tailer
                path = getattr(tailer, 'transcript_path', None) if tailer else None
            try:
                transcript = Path(path) if path is not None else None
            except (TypeError, ValueError):
                transcript = None
            if transcript is None:
                sources.append(None)
                continue
            try:
                opened_handle = self._open_transcript(transcript, entry.transcript_file_identity_at_paste)
                if opened_handle is None:
                    sources.append(None)
                    continue
                handle = stack.enter_context(opened_handle)
                descriptor = _descriptor(handle)
                opened = os.fstat(descriptor)
            except (AttributeError, OSError, TypeError, ValueError):
                sources.append(None)
                continue
            key = (opened.st_dev, opened.st_ino)
            handles.setdefault(key, handle)
            ticket_identity = entry.transcript_file_identity_at_paste
            offset = entry.transcript_offset_at_paste
            if ticket_identity is None or offset is None:
                allocation_start = 0
                proof_available = False
                fallback: bool | None = None if ticket_had_path else False
            elif ticket_identity == key:
                start = max(0, offset)
                anchor_start = entry.transcript_anchor_start_at_paste
                anchor = entry.transcript_anchor_at_paste
                guard_valid = entry.transcript_ticket_captured_at_ns is not None and anchor_start is not None and (anchor is not None) and (anchor_start >= 0) and (len(anchor) <= _TRANSCRIPT_BOUNDARY_ANCHOR_BYTES) and (anchor_start + len(anchor) == start)
                epoch_stable = False
                if guard_valid:
                    check_key = (key, start, anchor_start, anchor)
                    cached = anchor_checks.get(check_key)
                    if cached is not None:
                        epoch_stable = cached
                    elif len(anchor) <= budget_remaining:
                        try:
                            current_anchor = os.pread(descriptor, len(anchor), anchor_start)
                        except OSError:
                            guard_valid = False
                        else:
                            budget_remaining -= len(current_anchor)
                            epoch_stable = len(current_anchor) == len(anchor) and current_anchor == anchor and (opened.st_size >= start)
                            anchor_checks[check_key] = epoch_stable
                    else:
                        guard_valid = False
                proof_available = guard_valid
                if epoch_stable:
                    allocation_start = start
                else:
                    allocation_start = 0
                fallback = False
            else:
                allocation_start = 0
                proof_available = False
                fallback = False
            source = (key, allocation_start, proof_available, fallback)
            sources.append(source)
            scan_starts[key] = min(scan_starts.get(key, allocation_start), allocation_start)

        def _allocation_complete(key: _TranscriptSourceKey, found: list[tuple[int, int, str, bytes]]) -> bool:
            reserved: set[int] = set()
            for (entry, source) in zip(candidates, sources, strict=True):
                if source is None or source[0] != key:
                    continue
                allocation_start = source[1]
                for (row_offset, _row_end, prompt, _raw) in found:
                    if row_offset >= allocation_start and row_offset not in reserved and (prompt == entry.turn.prompt or self._transcript_prompt_identity(prompt) == entry.turn.prompt):
                        reserved.add(row_offset)
                        break
                else:
                    return False
            return True
        rows: dict[_TranscriptSourceKey, list[tuple[int, int, str, bytes]]] = {}
        incomplete: set[_TranscriptSourceKey] = set()
        parsed_rows: set[_TranscriptSourceKey] = set()
        budget_exhausted: set[_TranscriptSourceKey] = set()
        for (key, start) in scan_starts.items():
            found: list[tuple[int, int, str, bytes]] = []
            handle = handles[key]
            try:
                handle.seek(start)
                while budget_remaining > 0:
                    row_offset = handle.tell()
                    read_budget = budget_remaining
                    raw = handle.readline(read_budget)
                    if not raw:
                        break
                    budget_remaining -= len(raw)
                    if not raw.endswith(b'\n'):
                        incomplete.add(key)
                        if len(raw) == read_budget and os.fstat(_descriptor(handle)).st_size > handle.tell():
                            budget_exhausted.add(key)
                        break
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        continue
                    if not isinstance(parsed, dict):
                        continue
                    parsed_rows.add(key)
                    row_end = row_offset + len(raw)
                    if parsed.get('type') != 'user':
                        continue
                    prompt = self._transcript_user_text(parsed)
                    if prompt is None:
                        continue
                    found.append((row_offset, row_end, prompt, raw))
                    if _allocation_complete(key, found):
                        break
                else:
                    incomplete.add(key)
                    budget_exhausted.add(key)
            except (AttributeError, OSError, TypeError, ValueError):
                incomplete.add(key)
            rows[key] = found
        exact_rows: set[tuple[_TranscriptSourceKey, int]] = set()
        claims: list[_TranscriptProbeRow | None] = [None] * len(candidates)
        for (index, (entry, source)) in enumerate(zip(candidates, sources, strict=True)):
            if source is None:
                continue
            (key, allocation_start, _proof_available, _fallback) = source
            for (row_offset, row_end, prompt, raw) in rows.get(key, []):
                occurrence = (key, row_offset)
                if row_offset >= allocation_start and occurrence not in exact_rows and (prompt == entry.turn.prompt or self._transcript_prompt_identity(prompt) == entry.turn.prompt):
                    exact_rows.add(occurrence)
                    claims[index] = _TranscriptProbeRow(row_offset, row_end, raw)
                    break
        occupied_spans: dict[tuple[_TranscriptSourceKey, int], list[tuple[int, int]]] = {}
        for (index, (entry, source)) in enumerate(zip(candidates, sources, strict=True)):
            candidate_prompt = entry.turn.prompt
            if claims[index] is not None or source is None or (not candidate_prompt):
                continue
            (key, allocation_start, _proof_available, _fallback) = source
            for (row_offset, row_end, prompt, raw) in rows.get(key, []):
                row_key = (key, row_offset)
                if row_offset < allocation_start or row_key in exact_rows:
                    continue
                spans = occupied_spans.setdefault(row_key, [])
                claimed_span = self._first_unoccupied_prompt_span(prompt, candidate_prompt, spans)
                if claimed_span is None:
                    continue
                spans.append(claimed_span)
                claims[index] = _TranscriptProbeRow(row_offset, row_end, raw)
                break
        phase3_keys = {source[0] for (index, source) in enumerate(sources) if claims[index] is None and source is not None and bool(candidates[index].turn.prompt)}
        chains: dict[_TranscriptSourceKey, list[_TranscriptProbeFoldChain]] = {}
        fold_history_incomplete: set[_TranscriptSourceKey] = set()
        for key in phase3_keys:
            open_occurrences: list[_TranscriptProbeFoldOccurrence] = []
            pending_removes: list[tuple[_TranscriptProbeFoldOccurrence, _TranscriptProbeRow]] = []
            complete_chains: list[_TranscriptProbeFoldChain] = []
            primary_chains: list[_TranscriptProbeFoldChain] = []
            history_complete = False
            handle = handles[key]

            def _record_primary_chain(occurrence: _TranscriptProbeFoldOccurrence, remove: _TranscriptProbeRow, attachment: _TranscriptProbeRow) -> None:
                primary = _TranscriptProbeFoldChain(occurrence.prompt, occurrence.enqueue, remove, attachment)
                primary_chains.append(primary)
                complete_chains.append(primary)
                complete_chains.extend((_TranscriptProbeFoldChain(occurrence.prompt, alternative.enqueue, remove, alternative.attachment) for alternative in open_occurrences if alternative.prompt == occurrence.prompt and alternative.attachment is not None))
            try:
                handle.seek(0)
                while budget_remaining > 0:
                    row_offset = handle.tell()
                    raw = handle.readline(budget_remaining)
                    if not raw:
                        history_complete = True
                        break
                    budget_remaining -= len(raw)
                    if not raw.endswith(b'\n'):
                        break
                    try:
                        parsed = json.loads(raw)
                    except (json.JSONDecodeError, UnicodeDecodeError):
                        break
                    if not isinstance(parsed, dict):
                        continue
                    row = _TranscriptProbeRow(row_offset, row_offset + len(raw), raw)
                    entry_type = parsed.get('type')
                    if entry_type == 'queue-operation':
                        operation = parsed.get('operation')
                        content = parsed.get('content')
                        if operation == 'enqueue' and isinstance(content, str):
                            open_occurrences.append(_TranscriptProbeFoldOccurrence(content, row))
                        elif operation == 'remove' and isinstance(content, str):
                            matching_enqueue = next((index for (index, occurrence) in enumerate(open_occurrences) if occurrence.prompt == content), None)
                            if matching_enqueue is None:
                                continue
                            occurrence = open_occurrences.pop(matching_enqueue)
                            if occurrence.attachment is None:
                                pending_removes.append((occurrence, row))
                            else:
                                _record_primary_chain(occurrence, row, occurrence.attachment)
                        continue
                    if entry_type != 'attachment':
                        continue
                    attachment = parsed.get('attachment')
                    if not isinstance(attachment, dict) or attachment.get('type') != 'queued_command' or (not isinstance(attachment.get('prompt'), str)):
                        continue
                    prompt = attachment['prompt']
                    matching_remove = next((index for (index, (occurrence, _remove)) in enumerate(pending_removes) if occurrence.prompt == prompt), None)
                    if matching_remove is not None:
                        (occurrence, remove) = pending_removes.pop(matching_remove)
                        _record_primary_chain(occurrence, remove, row)
                        continue
                    open_occurrence = next((occurrence for occurrence in open_occurrences if occurrence.prompt == prompt and occurrence.attachment is None), None)
                    if open_occurrence is not None:
                        open_occurrence.attachment = row
                        complete_chains.extend((_TranscriptProbeFoldChain(prompt, open_occurrence.enqueue, primary.remove, row) for primary in primary_chains if primary.prompt == prompt))
            except (AttributeError, OSError, TypeError, ValueError):
                history_complete = False
            if history_complete:
                chains[key] = complete_chains
            else:
                chains[key] = []
                fold_history_incomplete.add(key)
        chain_claims: list[list[_TranscriptProbeFoldChain]] = [[] for _candidate in candidates]
        reserved_chains: set[tuple[_TranscriptSourceKey, int]] = set()
        for (index, (entry, source)) in enumerate(zip(candidates, sources, strict=True)):
            candidate_prompt = entry.turn.prompt
            if claims[index] is not None or source is None or (not candidate_prompt):
                continue
            key = source[0]
            for (chain_index, chain) in enumerate(chains.get(key, [])):
                occurrence = (key, chain_index)
                if occurrence in reserved_chains or (chain.prompt != candidate_prompt and self._transcript_prompt_identity(chain.prompt) != candidate_prompt):
                    continue
                reserved_chains.add(occurrence)
                chain_claims[index].append(chain)

        def _probe_row_is_paste_bound(entry: _InflightMeta, row: _TranscriptProbeRow, *, proof_available: bool) -> bool:
            if not proof_available:
                return False
            offset = entry.transcript_offset_at_paste
            anchor_start = entry.transcript_anchor_start_at_paste
            anchor = entry.transcript_anchor_at_paste
            if offset is not None and row.end > offset:
                return True
            if offset is None or anchor_start is None or anchor is None or (row.end > offset):
                return False
            overlap_start = max(row.offset, anchor_start)
            overlap_end = min(row.end, offset)
            if overlap_start >= overlap_end:
                return False
            prior_start = overlap_start - anchor_start
            prior_end = overlap_end - anchor_start
            current_start = overlap_start - row.offset
            current_end = overlap_end - row.offset
            prior = anchor[prior_start:prior_end]
            current = row.raw[current_start:current_end]
            return len(prior) == len(current) and prior != current
        verdicts: list[bool | None] = []
        for (index, (entry, source)) in enumerate(zip(candidates, sources, strict=True)):
            claimed = claims[index]
            claimed_chains = chain_claims[index]
            if source is not None:
                (key, _allocation_start, proof_available, fallback) = source
            else:
                key = None
                proof_available = False
                fallback = None
            paste_bound = claimed is not None and _probe_row_is_paste_bound(entry, claimed, proof_available=proof_available)
            if claimed_chains:
                paste_bound = key is not None and any((self._fold_pair_rows_share_occurrence(enqueue_offset=claimed_chain.enqueue.offset, enqueue_identity=key, remove_offset=claimed_chain.remove.offset, remove_identity=key, attachment_offset=claimed_chain.attachment.offset, attachment_identity=key) and all((_probe_row_is_paste_bound(entry, row, proof_available=proof_available) for row in (claimed_chain.enqueue, claimed_chain.remove, claimed_chain.attachment))) for claimed_chain in claimed_chains))
            if entry.turn.transport_accepted:
                verdicts.append(True)
            elif paste_bound:
                verdicts.append(True)
            elif claimed is None and key is not None and bool(entry.turn.prompt) and (key in fold_history_incomplete) and (key in budget_exhausted or key not in incomplete or key in parsed_rows):
                verdicts.append(False)
            elif key is not None and key in incomplete and (proof_available or fallback is None):
                verdicts.append(None)
            else:
                verdicts.append(fallback)
        return verdicts
