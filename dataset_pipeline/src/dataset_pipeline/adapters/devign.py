"""Bounded streaming parser for the local Devign-like JSON array."""

from __future__ import annotations

import json
import codecs
import hashlib
import os
from pathlib import Path
from typing import Any, Iterator

from ..models import canonical_record


def iter_json_array(path: str | Path, *, max_records: int | None = None,
                    max_bytes: int | None = None) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Incrementally decode array items; memory is bounded by one object + buffer."""
    decoder = json.JSONDecoder()
    consumed = 0
    buffer = ""
    utf8 = codecs.getincrementaldecoder("utf-8")()
    with open(path, "rb", buffering=0) as f:
        while not buffer:
            raw = f.read(1)
            if not raw:
                return
            consumed += len(raw)
            ch = raw.decode("utf-8")
            if not ch.isspace():
                if ch != "[":
                    yield 0, None, "root_is_not_array"
                    return
                break
        ordinal = 0
        while max_records is None or ordinal < max_records:
            while True:
                i = 0
                while i < len(buffer) and (buffer[i].isspace() or buffer[i] == ","):
                    i += 1
                if i < len(buffer):
                    break
                if max_bytes is not None and consumed >= max_bytes:
                    return
                size = 65536 if max_bytes is None else min(65536, max_bytes - consumed)
                if size <= 0:
                    return
                raw = f.read(size)
                if not raw:
                    return
                consumed += len(raw)
                buffer += utf8.decode(raw)
            if buffer[i] == "]":
                return
            try:
                value, end = decoder.raw_decode(buffer, i)
            except json.JSONDecodeError:
                if max_bytes is not None and consumed >= max_bytes:
                    return
                size = 65536 if max_bytes is None else min(65536, max_bytes - consumed)
                if size <= 0:
                    return
                raw = f.read(size)
                if not raw:
                    yield ordinal + 1, None, "truncated_or_malformed_json_array"
                    return
                consumed += len(raw)
                buffer += utf8.decode(raw)
                continue
            ordinal += 1
            buffer = buffer[end:]
            if isinstance(value, dict):
                yield ordinal, value, None
            else:
                yield ordinal, None, "array_item_is_not_object"


def iter_json_array_windows(path: str | Path, *, byte_budget: int, seed: str,
                            windows: int = 8) -> Iterator[tuple[int, dict[str, Any] | None, str | None]]:
    """Read bounded windows over the known function.json top-level object layout."""
    size = Path(path).stat().st_size
    if size == 0 or byte_budget <= 0:
        return
    count = min(windows, max(1, byte_budget))
    per_window = byte_budget // count
    fd = os.open(path, os.O_RDONLY)
    decoder = json.JSONDecoder()
    seen_offsets: set[int] = set()
    try:
        for i in range(count):
            left = i / count
            digest = hashlib.sha256(f"{seed}:{i}".encode()).digest()
            jitter = int.from_bytes(digest[:4], "big") / (2**32)
            position = min(size - 1, int((left + (0.2 + 0.6 * jitter) / count) * size))
            os.lseek(fd, position, os.SEEK_SET)
            raw = os.read(fd, min(per_window, size - position))
            # The source's observed top-level record layout begins with this key;
            # JSON strings escape embedded quotation marks, so code braces do not match.
            marker = b'{"project"'
            idx = raw.find(marker)
            while idx >= 0:
                absolute = position + idx
                if absolute not in seen_offsets:
                    try:
                        text = raw[idx:].decode("utf-8")
                        value, end = decoder.raw_decode(text)
                        tail = text[end:].lstrip()
                        if isinstance(value, dict) and value.get("project") is not None and tail[:1] in (",", "]"):
                            seen_offsets.add(absolute)
                            yield absolute, value, None
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        pass
                idx = raw.find(marker, idx + len(marker))
    finally:
        os.close(fd)


def map_devign(row: dict[str, Any], *, source_file: str, revision: str,
               ordinal: int, source_locator: dict[str, Any] | None = None) -> dict[str, Any]:
    target = row.get("target")
    return canonical_record(
        source="devign_candidate", revision=revision, source_file=source_file,
        source_id=row.get("idx"), ordinal=ordinal, task="defect_detection_candidate",
        split=None, repository=row.get("project"), commit_id=row.get("commit_id"),
        language=None, before_code=row.get("func"),
        source_labels={"target": target},
        source_locator=source_locator,
        label_meanings={"target": "CodeXGLUE Devign target: 1 insecure, 0 secure; exact local source/split not verified"},
        eligibility={"defect_detection": False, "severity_classification": False,
                     "explanation_generation": False, "fix_recommendation": False,
                     "review_comment_generation": False, "review_quality_classification": False},
        provenance_status="candidate_source_identity_and_split_unverified",
    )


def map_devign_official(row: dict[str, Any], *, source_file: str, revision: str,
                        source_checksum: str, ordinal: int, split: str,
                        source_locator: dict[str, Any] | None = None) -> dict[str, Any]:
    """Map a row only after its official source identity and split are verified."""
    target = row.get("target")
    verified_binary = isinstance(target, int) and not isinstance(target, bool) and target in (0, 1)
    record = canonical_record(
        source="devign_codexglue", revision=revision, source_file=source_file,
        source_id=ordinal, ordinal=ordinal, task="defect_detection", split=split,
        repository=row.get("project"), commit_id=row.get("commit_id"),
        source_checksum=source_checksum, before_code=row.get("func"),
        source_labels={"target": target},
        label_meanings={"target": "CodeXGLUE Devign: 1=insecure/vulnerable, 0=secure"},
        source_metadata={
            "dataset": "Microsoft CodeXGLUE Devign defect detection",
            "upstream_record_id_field": "idx (assigned by official preprocessing to raw array ordinal)",
            "split_source": "official train.txt/valid.txt/test.txt index files",
            "data_terms": "Computational Use of Data Agreement (C-UDA) 1.0",
        },
        source_locator=source_locator or {"official_array_index": ordinal},
        eligibility={"defect_detection": verified_binary},
        provenance_status="source_file_sha256_matched_official_copy_and_official_split_index",
    )
    record["defect_status"] = (
        "defective" if target == 1 else "non_defective" if target == 0 else None
    )
    if not verified_binary:
        record["eligibility"]["defect_detection"] = False
        record["exclusion_reasons"].append("invalid_or_missing_official_binary_target")
    return record
