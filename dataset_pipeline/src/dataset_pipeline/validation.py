"""Source-aware record validation and quarantine classification."""

from __future__ import annotations

from typing import Any, Mapping

from .normalization import check_required


def validate_record(record: Mapping[str, Any]) -> list[str]:
    issues: list[str] = []
    for field in ("record_id", "source", "source_file", "source_task", "content_fingerprint"):
        if not record.get(field):
            issues.append(f"missing_provenance:{field}")
    issues.extend(check_required(record))
    labels = record.get("source_labels") or {}
    if record.get("source_task") == "review_quality" and "y" in labels and labels["y"] not in (0, 1):
        issues.append("invalid_microsoft_review_quality_label")
    if record.get("source_task") == "review_quality" and "y" not in labels:
        issues.append("missing_microsoft_review_quality_label")
    if record.get("source_task") == "defect_detection_candidate" and labels.get("target") not in (0, 1):
        issues.append("invalid_devign_candidate_target")
    return issues
