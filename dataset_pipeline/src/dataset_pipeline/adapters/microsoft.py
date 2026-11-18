"""Streaming Microsoft CodeReviewer JSONL adapters."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterator

from ..models import canonical_record

TASKS = {
    "Comment_Generation": ("msg", "comment_generation"),
    "Code_Refinement": ("ref", "code_refinement"),
    "Diff_Quality_Estimation": ("cls", "review_quality"),
}


def iter_jsonl(path: str | Path, *, max_records: int | None = None,
               max_bytes: int | None = None, max_record_bytes: int | None = None
               ) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Yield parsed rows with one-based source line and parse error if malformed."""
    if max_record_bytes is not None:
        if max_record_bytes < 1:
            raise ValueError("max_record_bytes must be positive")
        total_bytes = os.path.getsize(path)
        consumed = line_number = 0
        # Buffered file I/O stays bounded (one small buffer plus the record cap)
        # while avoiding per-line system calls on large JSONL sources.
        with open(path, "rb", buffering=1024 * 1024) as f:
            while max_records is None or line_number < max_records:
                if max_bytes is not None and consumed >= max_bytes:
                    break
                request = max_record_bytes + 1
                if max_bytes is not None:
                    request = min(request, max_bytes - consumed)
                raw = f.readline(request)
                if not raw:
                    break
                consumed += len(raw)
                line_number += 1
                oversized = len(raw) > max_record_bytes
                if oversized:
                    ended = raw.endswith(b"\n")
                    while not ended and (max_bytes is None or consumed < max_bytes):
                        request = 65536 if max_bytes is None else min(65536, max_bytes-consumed)
                        more = f.readline(request)
                        if not more:
                            break
                        consumed += len(more)
                        ended = more.endswith(b"\n")
                    if max_bytes is not None and consumed >= max_bytes and not ended and consumed < total_bytes:
                        yield line_number, None, "sample_boundary_partial_record"
                    else:
                        yield line_number, None, "record_exceeds_max_record_bytes"
                    continue
                if not raw.endswith(b"\n") and max_bytes is not None and consumed >= max_bytes and consumed < total_bytes:
                    yield line_number, None, "sample_boundary_partial_record"
                    continue
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        yield line_number, value, None
                    else:
                        yield line_number, None, "record_is_not_object"
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    yield line_number, None, f"malformed_json:{type(exc).__name__}"
        return
    consumed = 0
    line_number = 0
    buffer = b""
    total_bytes = os.path.getsize(path)
    fd = os.open(path, os.O_RDONLY)

    def parse(raw: bytes, number: int):
        try:
            val = json.loads(raw)
            if not isinstance(val, dict):
                return number, None, "record_is_not_object"
            return number, val, None
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return number, None, f"malformed_json:{type(exc).__name__}"

    try:
        while max_records is None or line_number < max_records:
            if max_bytes is not None and consumed >= max_bytes:
                break
            request = 65536 if max_bytes is None else min(65536, max_bytes - consumed)
            block = os.read(fd, request)
            if not block:
                break
            consumed += len(block)
            buffer += block
            while (b"\n" in buffer and
                   (max_records is None or line_number < max_records)):
                raw, buffer = buffer.split(b"\n", 1)
                line_number += 1
                yield parse(raw, line_number)
        if buffer and (max_records is None or line_number < max_records):
            line_number += 1
            result = parse(buffer, line_number)
            if (max_bytes is not None and consumed >= max_bytes and consumed < total_bytes
                    and result[1] is None):
                yield line_number, None, "sample_boundary_partial_record"
            else:
                yield result
    finally:
        os.close(fd)


def iter_jsonl_windows(path: str | Path, *, byte_budget: int, seed: str,
                       windows: int = 8) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Read deterministic windows spread over a JSONL file under a byte cap."""
    import hashlib
    size = os.path.getsize(path)
    if size == 0 or byte_budget <= 0:
        return
    count = min(windows, max(1, byte_budget))
    per_window = byte_budget // count
    fd = os.open(path, os.O_RDONLY)
    try:
        for i in range(count):
            left = i / count
            digest = hashlib.sha256(f"{seed}:{i}".encode()).digest()
            jitter = int.from_bytes(digest[:4], "big") / (2**32)
            position = min(size - 1, int((left + (0.2 + 0.6 * jitter) / count) * size))
            os.lseek(fd, position, os.SEEK_SET)
            window = os.read(fd, min(per_window, size - position))
            base = position
            if position > 0:
                newline = window.find(b"\n")
                if newline < 0:
                    continue
                base += newline + 1
                window = window[newline + 1:]
            chunks = window.split(b"\n")
            cursor = 0
            complete = chunks[:-1]
            if base + len(window) >= size and chunks[-1]:
                complete.append(chunks[-1])
            for raw in complete:
                start = base + cursor
                cursor += len(raw) + 1
                if not raw.strip():
                    continue
                try:
                    value = json.loads(raw)
                    if isinstance(value, dict):
                        yield start, value, None
                    else:
                        yield start, None, "record_is_not_object"
                except (json.JSONDecodeError, UnicodeDecodeError) as exc:
                    yield start, None, f"malformed_json:{type(exc).__name__}"
    finally:
        os.close(fd)


def map_microsoft(row: dict[str, Any], *, task: str, split: str, source_file: str,
                  revision: str, ordinal: int, source_locator: dict[str, Any] | None = None) -> dict[str, Any]:
    if task not in TASKS:
        raise ValueError(f"unsupported Microsoft task: {task}")
    _, canonical_task = TASKS[task]
    labels: dict[str, Any] = {}
    meanings: dict[str, str] = {}
    if "y" in row:
        labels["y"] = row["y"]
        meanings["y"] = ("comment-present according to official preprocessing rule; not defect/severity"
                          if task == "Diff_Quality_Estimation" else "source task-specific binary value; semantics not generalized")
    if task == "Comment_Generation":
        before, after, diff, comment = row.get("oldf"), None, row.get("patch"), row.get("msg")
        elig = {"defect_detection": False, "severity_classification": False,
                "explanation_generation": False, "fix_recommendation": False,
                "review_comment_generation": bool(comment), "review_quality_classification": False}
    elif task == "Code_Refinement":
        before, after, diff, comment = row.get("old"), row.get("new"), row.get("hunk"), row.get("comment")
        elig = {"defect_detection": False, "severity_classification": False,
                "explanation_generation": False, "fix_recommendation": bool(after and comment),
                "review_comment_generation": False, "review_quality_classification": False}
    else:
        before, after, diff, comment = row.get("oldf"), None, row.get("patch"), row.get("msg")
        elig = {"review_quality_classification": "y" in row, "explanation_generation": False,
                "fix_recommendation": False, "defect_detection": False, "severity_classification": False}
    return canonical_record(
        source="microsoft_codereviewer", revision=revision, source_file=source_file,
        source_id=row.get("id", row.get("ids", row.get("idx", row.get("ghid")))),
        ordinal=ordinal, task=canonical_task, split=split,
        repository=row.get("proj") or row.get("repo"), commit_id=None,
        language=row.get("lang"), before_code=before, after_code=after, diff=diff,
        before_file=row.get("oldf") if task == "Code_Refinement" else None,
        review_comment=comment, source_labels=labels, label_meanings=meanings,
        source_metadata={k: row[k] for k in ("idx", "ids", "ghid", "old_hunk") if k in row},
        source_locator=source_locator,
        eligibility=elig, provenance_status="source_file_local_revision_unpinned",
    )
