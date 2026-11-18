"""Non-destructive canonicalization utilities."""

from __future__ import annotations

from typing import Any, Mapping


def check_required(record: Mapping[str, Any]) -> list[str]:
    task = record.get("source_task")
    requirements = {
        "comment_generation": ("before_code", "diff", "review_comment"),
        "code_refinement": ("before_code", "review_comment", "after_code"),
        "review_quality": ("before_code", "diff"),
        "defect_detection_candidate": ("before_code",),
    }.get(task, ())
    return [f"missing_required:{field}" for field in requirements if not record.get(field)]
