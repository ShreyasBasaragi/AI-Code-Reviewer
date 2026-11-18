"""Leakage grouping and official-split integrity checks."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping

from .models import stable_hash


def grouping_key(record: Mapping[str, Any]) -> str:
    if record.get("pair_id"):
        return "pair:" + str(record["pair_id"])
    if record.get("repository") and record.get("pull_request_id"):
        parts = [record["repository"], record["pull_request_id"]]
    elif record.get("repository") and record.get("commit_id"):
        parts = [record["repository"], record["commit_id"]]
    elif record.get("repository"):
        parts = [record["repository"]]
    else:
        parts = [record.get("record_id")]
    return "group:" + stable_hash(parts)


def validate_splits(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    groups: dict[str, set[str]] = defaultdict(set)
    unknown = 0
    for row in records:
        split = row.get("source_split")
        if split is None:
            unknown += 1
            continue
        groups[grouping_key(row)].add(str(split))
    leaking = sorted(key for key, splits in groups.items() if len(splits) > 1)
    return {"groups_checked": len(groups), "unknown_split_records": unknown,
            "cross_split_groups": leaking, "split_integrity_verified": not leaking and unknown == 0}
