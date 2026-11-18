"""Exact duplicate and conflicting-label audits; no near-duplicate search."""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Iterable, Mapping


def audit_duplicates(records: Iterable[Mapping[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in records:
        groups[str(row.get("content_fingerprint"))].append(row)
    duplicates: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for fp, rows in sorted(groups.items()):
        if len(rows) < 2:
            continue
        labels = {repr(sorted((r.get("source_labels") or {}).items())) for r in rows}
        item = {"fingerprint": fp, "record_ids": sorted(str(r.get("record_id")) for r in rows), "count": len(rows)}
        duplicates.append(item)
        if len(labels) > 1:
            conflicts.append(item)
    return {"exact_duplicate_groups": duplicates, "conflicting_label_groups": conflicts}
