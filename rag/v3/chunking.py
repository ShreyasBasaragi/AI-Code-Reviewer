"""Deterministic, source-preserving chunk boundaries for v3 preparation."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class SourceSpan:
    index: int
    start: int
    end: int
    text: str
    token_count: int
    oversized_indivisible_line: bool = False
    semantic_unit_split: bool = False
    split_line_fallback: bool = False


def _encoded_length(tokenizer, text: str) -> int:
    encoded = tokenizer.encode(text, add_special_tokens=False)
    if encoded and isinstance(encoded[0], list):
        encoded = encoded[0]
    return len(encoded)


def _lines_with_offsets(text: str) -> list[tuple[int, int, str]]:
    result = []
    offset = 0
    for line in text.splitlines(keepends=True):
        end = offset + len(line)
        result.append((offset, end, line))
        offset = end
    if offset < len(text):
        result.append((offset, len(text), text[offset:]))
    return result


def _semantic_units(text: str, kind: str) -> list[tuple[int, int]]:
    lines = _lines_with_offsets(text)
    if not lines:
        return []
    boundaries = [0]
    for index, (start, _end, line) in enumerate(lines):
        stripped = line.lstrip()
        if index == 0:
            continue
        if kind == "diff" and (stripped.startswith("@@") or stripped.startswith("diff --git ")):
            boundaries.append(start)
        elif kind == "documentation" and re.match(r"#{1,6}\s+", stripped):
            boundaries.append(start)
        elif kind == "paragraph" and not stripped.strip():
            # Keep paragraph separators attached to the preceding semantic unit.
            continue
    boundaries = sorted(set(boundaries + [len(text)]))
    return [(start, end) for start, end in zip(boundaries, boundaries[1:]) if start < end]


def _split_unit_by_lines(
    text: str,
    start: int,
    end: int,
    tokenizer,
    max_tokens: int,
    prefix_for_chunk: Callable[[int], str],
    next_index: int,
) -> list[tuple[int, int, int, bool, bool, bool]]:
    """Split one oversized semantic unit at line boundaries; never split a line."""
    lines = [(a, b, line) for a, b, line in _lines_with_offsets(text[start:end])]
    if not lines:
        return []
    output = []
    group_start = start + lines[0][0]
    group_end = group_start
    group_text = ""
    index = next_index
    for rel_start, rel_end, line in lines:
        absolute_start, absolute_end = start + rel_start, start + rel_end
        candidate = group_text + line
        candidate_prefix = prefix_for_chunk(index)
        if group_text and _encoded_length(tokenizer, candidate_prefix + candidate) > max_tokens:
            count = _encoded_length(tokenizer, prefix_for_chunk(index) + group_text)
            output.append((group_start, group_end, count, False, True, False))
            index += 1
            group_start, group_end, group_text = absolute_start, absolute_start, ""
            candidate = line
            candidate_prefix = prefix_for_chunk(index)
        group_end = absolute_end
        group_text += line
        if _encoded_length(tokenizer, candidate_prefix + candidate) > max_tokens:
            line_parts = _split_oversized_line(
                text, absolute_start, absolute_end, tokenizer, max_tokens,
                prefix_for_chunk, index,
            )
            output.extend(line_parts)
            index += len(line_parts)
            group_start, group_end, group_text = absolute_end, absolute_end, ""
    if group_text:
        output.append((group_start, group_end,
                       _encoded_length(tokenizer, prefix_for_chunk(index) + group_text), False, True, False))
    return output


def _split_oversized_line(
    text: str,
    start: int,
    end: int,
    tokenizer,
    max_tokens: int,
    prefix_for_chunk: Callable[[int], str],
    next_index: int,
) -> list[tuple[int, int, int, bool, bool, bool]]:
    """Explicit tested fallback: split one over-budget line at Unicode codepoint offsets."""
    parts = []
    cursor = start
    while cursor < end:
        prefix = prefix_for_chunk(next_index + len(parts))
        if _encoded_length(tokenizer, prefix) > max_tokens:
            raise ValueError("chunk prefix alone exceeds the configured token budget")
        low, high = cursor + 1, end
        best_end = cursor
        best_count = 0
        while low <= high:
            middle = (low + high) // 2
            count = _encoded_length(tokenizer, prefix + text[cursor:middle])
            if count <= max_tokens:
                best_end, best_count = middle, count
                low = middle + 1
            else:
                high = middle - 1
        if best_end == cursor:
            raise ValueError("token budget cannot hold a single source character and its prefix")
        # Keep CRLF together where the following newline still fits.
        if best_end < end and text[best_end - 1:best_end] == "\r" and text[best_end:best_end + 1] == "\n":
            extended_count = _encoded_length(tokenizer, prefix + text[cursor:best_end + 1])
            if extended_count <= max_tokens:
                best_end += 1
                best_count = extended_count
        parts.append((cursor, best_end, best_count, False, True, True))
        cursor = best_end
    return parts


def chunk_source_text(
    text: str,
    tokenizer,
    *,
    max_tokens: int,
    kind: str = "code",
    prefix_for_chunk: Callable[[int], str] | None = None,
) -> list[SourceSpan]:
    """Split text losslessly at semantic/line boundaries using the actual tokenizer.

    Returned source spans are contiguous, character-indexed slices into ``text``.
    Token IDs are counted, never decoded back into source. An individually oversized
    line is emitted intact where possible. A single over-budget line is split at
    deterministic Unicode character offsets and every such span is flagged.
    """
    if not isinstance(text, str):
        raise TypeError("source text must be a string")
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if not text:
        return []
    prefix_for_chunk = prefix_for_chunk or (lambda _index: "")
    units = _semantic_units(text, "diff" if kind == "diff" else
                            "documentation" if kind == "documentation" else
                            "paragraph" if kind == "paragraph" else "code")
    packed: list[tuple[int, int, int, bool, bool, bool]] = []
    pending_start: int | None = None
    pending_end: int | None = None
    pending_text = ""

    for unit_start, unit_end in units:
        unit = text[unit_start:unit_end]
        chunk_index = len(packed)
        candidate = pending_text + unit
        if _encoded_length(tokenizer, prefix_for_chunk(chunk_index) + candidate) <= max_tokens:
            if pending_start is None:
                pending_start = unit_start
            pending_end = unit_end
            pending_text = candidate
            continue
        if pending_text:
            packed.append((pending_start, pending_end,
                           _encoded_length(tokenizer, prefix_for_chunk(len(packed)) + pending_text),
                           False, False, False))
            pending_start = pending_end = None
            pending_text = ""

        if _encoded_length(tokenizer, prefix_for_chunk(len(packed)) + unit) <= max_tokens:
            pending_start, pending_end, pending_text = unit_start, unit_end, unit
            continue
        pieces = _split_unit_by_lines(
            text, unit_start, unit_end, tokenizer, max_tokens,
            prefix_for_chunk, len(packed),
        )
        packed.extend(pieces)

    if pending_text:
        packed.append((pending_start, pending_end,
                       _encoded_length(tokenizer, prefix_for_chunk(len(packed)) + pending_text),
                       False, False, False))

    spans = [SourceSpan(i, start, end, text[start:end], count, oversized, semantic_split, split_line)
             for i, (start, end, count, oversized, semantic_split, split_line) in enumerate(packed)]
    if "".join(span.text for span in spans) != text:
        raise AssertionError("chunk spans do not reconstruct original source text")
    return spans


def assert_lossless_spans(original: str, spans: list[SourceSpan]) -> None:
    """Assert contiguous exact source extraction for downstream preparation tests."""
    cursor = 0
    for span in spans:
        if span.start != cursor or original[span.start:span.end] != span.text:
            raise AssertionError("source spans are not contiguous exact slices")
        cursor = span.end
    if cursor != len(original):
        raise AssertionError("source spans do not cover the complete input")
