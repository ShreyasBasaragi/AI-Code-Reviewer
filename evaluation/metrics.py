"""Transparent finding-level metrics for the synthetic baseline benchmark."""

from __future__ import annotations

import statistics
from typing import Any, Iterable, Mapping


def _matches(prediction: Mapping[str, Any], case: Mapping[str, Any]) -> bool:
    expected_type = case.get("expected_issue_type")
    if expected_type is None:
        return False
    if str(prediction.get("issue_type", "")).casefold() != str(expected_type).casefold():
        return False
    expected_line = case.get("expected_line")
    predicted_line = prediction.get("line_start")
    # A missing expected or predicted location does not reject an otherwise
    # correctly typed finding; when both exist, allow a two-line tolerance.
    return (
        expected_line is None
        or predicted_line is None
        or abs(int(predicted_line) - int(expected_line)) <= 2
    )


def match_findings(case: Mapping[str, Any], predictions: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Greedy one-to-one matching by exact issue type and ±2 line tolerance."""
    prediction_list = list(predictions)
    if case.get("expected_issue_type") is None:
        return {"true_positives": 0, "false_positives": len(prediction_list), "false_negatives": 0,
                "matched_prediction_indexes": []}
    unmatched = set(range(len(prediction_list)))
    matched: list[int] = []
    # Benchmark cases contain one independent expected issue each.
    for index in sorted(unmatched):
        if _matches(prediction_list[index], case):
            matched.append(index)
            unmatched.remove(index)
            break
    return {
        "true_positives": len(matched),
        "false_positives": len(prediction_list) - len(matched),
        "false_negatives": 1 - len(matched),
        "matched_prediction_indexes": matched,
    }


def aggregate_metrics(case_results: list[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate only observable denominators; undefined ratios are null."""
    # Provider/schema/retrieval failures are not evidence of a missed or found
    # defect. Keep them in the error-rate denominator, but exclude them from
    # finding-quality metrics so a wholly failed run reports quality as null.
    evaluable = [item for item in case_results
                 if item.get("schema_valid") and not item.get("processing_errors")]
    tp = sum(int(item["matching"]["true_positives"]) for item in evaluable)
    fp = sum(int(item["matching"]["false_positives"]) for item in evaluable)
    fn = sum(int(item["matching"]["false_negatives"]) for item in evaluable)
    clean = [item for item in evaluable if item.get("category") == "clean"]
    clean_fp = sum(bool(item.get("findings")) for item in clean)
    schema_count = len(case_results)
    valid_schema = sum(bool(item.get("schema_valid")) for item in case_results)
    error_cases = sum(bool(item.get("processing_errors")) for item in case_results)
    provider_error_cases = sum(
        any("model_failure" in str(error) or "evaluation_error" in str(error)
            for error in item.get("processing_errors", []))
        for item in case_results
    )
    abstained_cases = sum(bool(item.get("abstained")) for item in case_results)
    evidence_cases = evaluable
    evidence_valid = sum(bool(item.get("evidence_references_valid")) for item in evidence_cases)
    fix_attempts = sum(bool(item.get("fix_attempted")) for item in case_results)
    fix_successes = sum(bool(item.get("fix_generated")) for item in case_results)
    syntax_attempts = sum(item.get("fix_syntax_status") in {"passed", "failed"} for item in case_results)
    syntax_passes = sum(item.get("fix_syntax_status") == "passed" for item in case_results)
    latencies = [float(item["review_latency_seconds"]) for item in case_results
                 if isinstance(item.get("review_latency_seconds"), (int, float))]
    return {
        "finding_precision": tp / (tp + fp) if tp + fp else None,
        "finding_recall": tp / (tp + fn) if tp + fn else None,
        "clean_case_false_positive_rate": clean_fp / len(clean) if clean else None,
        "json_schema_validity_rate": valid_schema / schema_count if schema_count else None,
        "evidence_reference_validity_rate": evidence_valid / len(evidence_cases) if evidence_cases else None,
        "fix_generation_success_rate": fix_successes / fix_attempts if fix_attempts else None,
        "python_syntax_validity_rate": syntax_passes / syntax_attempts if syntax_attempts else None,
        "review_latency_seconds": {
            "count": len(latencies),
            "mean": sum(latencies) / len(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "case_error_rate": error_cases / schema_count if schema_count else None,
        "provider_error_case_rate": provider_error_cases / schema_count if schema_count else None,
        "explicit_abstention_rate": abstained_cases / schema_count if schema_count else None,
        "counts": {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
                   "clean_cases": len(clean), "clean_false_positive_cases": clean_fp,
                   "evaluable_cases": len(evaluable),
                   "schema_valid_cases": valid_schema, "cases": schema_count,
                   "error_cases": error_cases, "provider_error_cases": provider_error_cases,
                   "explicit_abstention_cases": abstained_cases,
                   "fix_attempts": fix_attempts, "fix_successes": fix_successes,
                   "syntax_attempts": syntax_attempts, "syntax_passes": syntax_passes},
        "matching_policy": "One-to-one exact issue_type match; if expected_line and line_start are both present, absolute difference must be at most 2. Missing locations do not disqualify a type match. Each positive benchmark case has one expected issue. Cases with invalid schema or processing errors are excluded from finding-quality denominators and counted in error rates.",
    }
