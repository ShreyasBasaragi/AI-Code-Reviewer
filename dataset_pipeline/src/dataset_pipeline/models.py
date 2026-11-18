"""Canonical record helpers; source values remain namespaced and unchanged."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_hash(value: Any) -> str:
    return hashlib.sha256(stable_json(value).encode("utf-8")).hexdigest()


def record_id(source: str, revision: str, source_file: str, source_id: Any, ordinal: int,
              source_locator: Mapping[str, Any] | None = None) -> str:
    locator = {"source_id": source_id, "locator": dict(source_locator or {"ordinal": ordinal})}
    digest = stable_hash([source, revision, source_file, locator])
    return f"{source}:{digest[:24]}"


def canonical_record(
    *, source: str, revision: str, source_file: str, source_id: Any, ordinal: int,
    task: str, split: str | None, repository: str | None = None,
    commit_id: str | None = None, pull_request_id: str | None = None,
    source_checksum: str | None = None,
    language: str | None = None, before_code: str | None = None,
    before_file: str | None = None,
    after_code: str | None = None, diff: str | None = None,
    review_comment: str | None = None, source_labels: Mapping[str, Any] | None = None,
    label_meanings: Mapping[str, str] | None = None,
    source_metadata: Mapping[str, Any] | None = None,
    source_locator: Mapping[str, Any] | None = None,
    eligibility: Mapping[str, bool] | None = None,
    provenance_status: str = "unverified",
    token_count: int | None = None,
) -> dict[str, Any]:
    payload = {
        "schema_version": "1.0",
        "record_id": record_id(source, revision, source_file, source_id, ordinal, source_locator),
        "source": source,
        "source_revision": revision,
        "source_checksum": source_checksum,
        "source_file": source_file,
        "source_record_id": source_id,
        "source_metadata": dict(source_metadata or {}),
        "source_ordinal": None if source_locator and "byte_offset" in source_locator else ordinal,
        "source_locator": dict(source_locator or {"ordinal": ordinal}),
        "source_task": task,
        "source_split": split,
        "repository": repository,
        "commit_id": commit_id,
        "pull_request_id": pull_request_id,
        "language": language,
        "before_code": before_code,
        "before_file": before_file,
        "after_code": after_code,
        "diff": diff,
        "review_comment": review_comment,
        "source_labels": dict(source_labels or {}),
        "label_meanings": dict(label_meanings or {}),
        "defect_status": None,
        "severity": None,
        "explanation": None,
        "fix_recommendation": None,
        "annotation_provenance": {"status": provenance_status},
        "quality_status": [],
        "eligibility": dict(eligibility or {}),
        "exclusion_reasons": [],
        "pair_id": None,
        "leakage_group_id": None,
        "content_fingerprint": stable_hash({
            "source": source, "task": task, "before_code": before_code,
            "before_file": before_file,
            "after_code": after_code, "diff": diff,
            "review_comment": review_comment,
        }),
        "token_count": token_count,
    }
    if repository and pull_request_id:
        payload["leakage_group_id"] = stable_hash([repository, "pr", pull_request_id])
    elif repository and commit_id:
        payload["leakage_group_id"] = stable_hash([repository, "commit", commit_id])
    elif repository:
        payload["leakage_group_id"] = stable_hash([repository])
    elif commit_id:
        payload["leakage_group_id"] = stable_hash(["commit", commit_id])
    return payload
