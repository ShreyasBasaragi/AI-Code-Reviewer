"""Summarize four E3.4 result files without substituting missing outcomes."""
from __future__ import annotations

import argparse
import json
import random
import statistics
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from evaluation.runner import load_cases

RESULTS = Path(__file__).with_name("results")
CONFIGS = {
    "no_rag": "phase_e3_4_groq_no_rag.json",
    "v2_baseline": "phase_e3_4_groq_v2_baseline.json",
    "v3_baseline": "phase_e3_4_groq_v3_baseline.json",
    "v3_optimized": "phase_e3_4_groq_v3_optimized.json",
}
RUN2_CONFIGS = {
    "no_rag": "groq_no_rag.json",
    "v2_baseline": "groq_v2_baseline.json",
    "v3_baseline": "groq_v3_baseline.json",
    "v3_optimized": "groq_v3_optimized.json",
}


def _atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, indent=2, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
        temporary.replace(path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def _load_result(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if value.get("execution_status") == "pending":
        return None
    return value


def _completed(result: dict[str, Any] | None) -> bool:
    if not result or result.get("interrupted"):
        return False
    rows = result.get("case_results", [])
    return (len(rows) == 30 and len({row.get("case_id") for row in rows}) == 30
            and all(row.get("case_status") == "completed" for row in rows))


def _case_quality(rows: list[dict[str, Any]]) -> dict[str, float | int | None]:
    evaluable = [row for row in rows if row.get("schema_valid") and not row.get("processing_errors")]
    tp = sum(int(row.get("matching", {}).get("true_positives", 0)) for row in evaluable)
    fp = sum(int(row.get("matching", {}).get("false_positives", 0)) for row in evaluable)
    fn = sum(int(row.get("matching", {}).get("false_negatives", 0)) for row in evaluable)
    clean = [row for row in evaluable if row.get("category") == "clean"]
    clean_fp = sum(bool(row.get("findings")) for row in clean)
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else None
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": precision, "recall": recall, "f1": f1,
            "clean_cases_evaluable": len(clean), "clean_cases_with_false_positives": clean_fp,
            "clean_false_positive_rate": clean_fp / len(clean) if clean else None}


def _bootstrap_difference(base_rows: list[dict[str, Any]], compare_rows: list[dict[str, Any]],
                          metric: str, *, samples: int = 5000, seed: int = 3404) -> dict[str, Any] | None:
    base = {row["case_id"]: row for row in base_rows}
    compare = {row["case_id"]: row for row in compare_rows}
    paired_ids = sorted(set(base) & set(compare))
    if len(paired_ids) != 30:
        return None
    rng = random.Random(seed)
    differences = []
    for _ in range(samples):
        sampled = [rng.choice(paired_ids) for _ in paired_ids]
        left = _case_quality([base[item] for item in sampled]).get(metric)
        right = _case_quality([compare[item] for item in sampled]).get(metric)
        if isinstance(left, (int, float)) and isinstance(right, (int, float)):
            differences.append(float(right) - float(left))
    if not differences:
        return None
    differences.sort()
    low = differences[int(0.025 * (len(differences) - 1))]
    high = differences[int(0.975 * (len(differences) - 1))]
    full_left = _case_quality([base[item] for item in paired_ids]).get(metric)
    full_right = _case_quality([compare[item] for item in paired_ids]).get(metric)
    return {"comparison": "paired bootstrap, exploratory; not a significance claim",
            "metric": metric, "difference": float(full_right) - float(full_left),
            "percentile_95_interval": [low, high], "resamples": len(differences), "seed": seed}


def _configuration_summary(name: str, result: dict[str, Any] | None) -> dict[str, Any]:
    if result is None:
        return {"configuration": name, "status": "pending", "total_cases": 30,
                "completed_cases": None, "provider_errors": None, "schema_failures": None,
                "evidence_validation_failures": None, "quota_deferred_cases": None,
                "failed_cases": None, "metrics": None, "retrieval": None}
    rows = result.get("case_results", [])
    completed = sum(row.get("case_status") == "completed" for row in rows)
    schema_failures = sum(not bool(row.get("schema_valid")) for row in rows)
    provider_errors = sum(bool(row.get("provider_errors")) or any(
        error.startswith(("model_failure:", "evaluation_error:"))
        for error in row.get("processing_errors", [])
    ) for row in rows)
    evidence_failures = sum(row.get("evidence_references_valid") is False for row in rows)
    schema_validity = sum(bool(row.get("schema_valid")) for row in rows) / len(rows) if rows else None
    families: Counter[str] = Counter()
    counts, retrieval_latencies, provider_latencies, pacing = [], [], [], []
    for row in rows:
        items = row.get("retrieval_diagnostics", []) or []
        counts.append(len(items))
        for item in items:
            if item.get("source_family"):
                families[str(item["source_family"])] += 1
        timings = row.get("timings", {}) or {}
        if isinstance(timings.get("retrieval_seconds"), (int, float)):
            retrieval_latencies.append(timings["retrieval_seconds"])
        if isinstance(timings.get("provider_inference_seconds"), (int, float)):
            provider_latencies.append(timings["provider_inference_seconds"])
        if isinstance(timings.get("request_pacing_seconds"), (int, float)):
            pacing.append(timings["request_pacing_seconds"])
    rag_enabled = bool(result.get("rag_enabled"))
    retrieval = None
    if rag_enabled:
        retrieval = {
            "mean_evidence_count": sum(counts) / len(counts) if counts else None,
            "empty_evidence_rate": sum(count == 0 for count in counts) / len(counts) if counts else None,
            "source_family_distribution": dict(sorted(families.items())),
            "evidence_token_utilization": None,
            "evidence_token_utilization_reason": "runner does not persist a comparable serialized-context budget denominator",
            "mean_retrieval_latency_seconds": statistics.mean(retrieval_latencies) if retrieval_latencies else None,
            "median_retrieval_latency_seconds": statistics.median(retrieval_latencies) if retrieval_latencies else None,
        }
    review_latencies = [row.get("review_latency_seconds") for row in rows
                        if isinstance(row.get("review_latency_seconds"), (int, float))]
    return {
        "configuration": name,
        "status": "completed" if _completed(result) else "incomplete",
        "total_cases": len(rows), "completed_cases": completed,
        "provider_error_cases": provider_errors, "schema_validation_failures": schema_failures,
        "quota_deferred_cases": sum(row.get("case_status") == "quota_deferred" for row in rows),
        "failed_cases": sum(row.get("case_status") in {"failed", "interrupted"} for row in rows),
        "schema_validity_rate": schema_validity,
        "evidence_id_validation_failures": evidence_failures if rag_enabled else None,
        "metrics": {**result.get("metrics", {}), "case_level_quality": _case_quality(rows)},
        "per_category_quality": {
            category: _case_quality([row for row in rows if row.get("category") == category])
            for category in ("security", "logic_runtime", "clean")
        },
        "retrieval": retrieval,
        "mean_provider_inference_seconds": statistics.mean(provider_latencies) if provider_latencies else None,
        "mean_request_pacing_seconds_per_case": statistics.mean(pacing) if pacing else None,
        "mean_review_latency_seconds": statistics.mean(review_latencies) if review_latencies else None,
        "median_review_latency_seconds": statistics.median(review_latencies) if review_latencies else None,
        "rate_limit_usage": result.get("rate_limit_usage"),
        "run_duration_seconds": result.get("run_duration_seconds"),
        "provider_request_counts": result.get("provider_request_counts"),
        "model_id": result.get("model_id"),
        "prompt_configuration_sha256": result.get("prompt_configuration_sha256"),
        "scoring_version": result.get("scoring_version"),
        "scoring_configuration_sha256": result.get("scoring_configuration_sha256"),
    }


def _historical_references(results_dir: Path) -> dict[str, Any]:
    references = {
        "phase_e3_4_no_rag": "phase_e3_4_groq_no_rag.json",
        "phase_e3_4_v2_baseline": "phase_e3_4_groq_v2_baseline.json",
        "phase_e2_no_rag": "phase_e2_30_no_rag.json",
        "phase_e2_rag": "phase_e2_30_rag.json",
    }
    output = {}
    for label, filename in references.items():
        result = _load_result(results_dir.parent / filename)
        if result is None:
            continue
        output[label] = {
            "source_file": filename,
            "case_count": result.get("cases_run"),
            "model_id": result.get("model_id"),
            "metrics": result.get("metrics"),
            "status": "historical_reference_only; not part of controlled Run 2",
        }
    return output


def _paired_v3_comparison(base: dict[str, Any] | None, optimized: dict[str, Any] | None,
                          case_ids: list[str]) -> dict[str, Any] | None:
    base_rows = {row["case_id"]: row for row in (base or {}).get("case_results", [])}
    optimized_rows = {row["case_id"]: row for row in (optimized or {}).get("case_results", [])}
    cases = []
    counts = Counter()
    for case_id in case_ids:
        left, right = base_rows.get(case_id), optimized_rows.get(case_id)
        left_ok = bool(left and left.get("case_status") == "completed")
        right_ok = bool(right and right.get("case_status") == "completed")
        if left is None and right is None:
            outcome = "neither_result_available"
        elif left is not None and right is not None and left_ok and right_ok:
            outcome = "both_succeed"
        elif left is not None and right is not None and not left_ok and not right_ok:
            outcome = "both_fail"
        elif left is not None and right is not None and left_ok:
            outcome = "baseline_only_succeeds"
        elif left is not None and right is not None and right_ok:
            outcome = "optimized_only_succeeds"
        elif left is not None and right is None:
            outcome = "baseline_only_result"
        elif left is None and right is not None:
            outcome = "optimized_only_result"
        else:
            outcome = "both_fail"
        counts[outcome] += 1
        left_fp = int((left or {}).get("matching", {}).get("false_positives", 0))
        right_fp = int((right or {}).get("matching", {}).get("false_positives", 0))
        left_invalid = bool(left and not left.get("schema_valid")
                            and left.get("schema_errors") and not left.get("provider_errors"))
        right_invalid = bool(right and not right.get("schema_valid")
                             and right.get("schema_errors") and not right.get("provider_errors"))
        paired_success = left_ok and right_ok
        left_only_fp = paired_success and left_fp > 0 and right_fp == 0
        right_only_fp = paired_success and right_fp > 0 and left_fp == 0
        invalid_schema_baseline_only = bool(left and right and left_invalid and right.get("schema_valid"))
        invalid_schema_optimized_only = bool(left and right and right_invalid and left.get("schema_valid"))
        if left_only_fp:
            counts["false_positive_baseline_only"] += 1
        if right_only_fp:
            counts["false_positive_optimized_only"] += 1
        if invalid_schema_baseline_only:
            counts["invalid_schema_baseline_only"] += 1
        if invalid_schema_optimized_only:
            counts["invalid_schema_optimized_only"] += 1
        cases.append({
            "case_id": case_id,
            "baseline_status": (left or {}).get("case_status"),
            "optimized_status": (right or {}).get("case_status"),
            "outcome": outcome,
            "baseline_schema_valid": (left or {}).get("schema_valid"),
            "optimized_schema_valid": (right or {}).get("schema_valid"),
            "baseline_evidence_references_valid": (left or {}).get("evidence_references_valid"),
            "optimized_evidence_references_valid": (right or {}).get("evidence_references_valid"),
            "baseline_tp_fp_fn": {key: (left or {}).get("matching", {}).get(key)
                                   for key in ("true_positives", "false_positives", "false_negatives")},
            "optimized_tp_fp_fn": {key: (right or {}).get("matching", {}).get(key)
                                   for key in ("true_positives", "false_positives", "false_negatives")},
            "false_positive_baseline_only": left_only_fp,
            "false_positive_optimized_only": right_only_fp,
            "invalid_schema_baseline_only": invalid_schema_baseline_only,
            "invalid_schema_optimized_only": invalid_schema_optimized_only,
            "baseline_review_latency_seconds": (left or {}).get("review_latency_seconds"),
            "optimized_review_latency_seconds": (right or {}).get("review_latency_seconds"),
            "baseline_retrieval_latency_seconds": ((left or {}).get("timings") or {}).get("retrieval_seconds"),
            "optimized_retrieval_latency_seconds": ((right or {}).get("timings") or {}).get("retrieval_seconds"),
            "baseline_provider_errors": (left or {}).get("provider_errors", []),
            "optimized_provider_errors": (right or {}).get("provider_errors", []),
            "baseline_processing_errors": (left or {}).get("processing_errors", []),
            "optimized_processing_errors": (right or {}).get("processing_errors", []),
        })
    base_summary = _configuration_summary("v3_baseline", base)
    optimized_summary = _configuration_summary("v3_optimized", optimized)
    common_ids = [case_id for case_id in case_ids
                  if base_rows.get(case_id, {}).get("case_status") == "completed"
                  and optimized_rows.get(case_id, {}).get("case_status") == "completed"]
    paired_base_rows = [base_rows[case_id] for case_id in common_ids]
    paired_optimized_rows = [optimized_rows[case_id] for case_id in common_ids]
    base_quality = _case_quality(paired_base_rows)
    optimized_quality = _case_quality(paired_optimized_rows)
    base_retrieval = base_summary.get("retrieval") or {}
    optimized_retrieval = optimized_summary.get("retrieval") or {}
    base_rate = base_summary.get("rate_limit_usage") or {}
    optimized_rate = optimized_summary.get("rate_limit_usage") or {}
    metric_values = {
        "precision": (base_quality.get("precision"), optimized_quality.get("precision")),
        "recall": (base_quality.get("recall"), optimized_quality.get("recall")),
        "f1": (base_quality.get("f1"), optimized_quality.get("f1")),
        "clean_case_false_positive_rate": (base_quality.get("clean_false_positive_rate"),
                                            optimized_quality.get("clean_false_positive_rate")),
        "schema_validity_rate": (
            (sum(bool(row.get("schema_valid")) for row in paired_base_rows) / len(paired_base_rows)
             if paired_base_rows else None),
            (sum(bool(row.get("schema_valid")) for row in paired_optimized_rows) / len(paired_optimized_rows)
             if paired_optimized_rows else None)),
        "evidence_reference_validity_rate": (
            (sum(bool(row.get("evidence_references_valid")) for row in paired_base_rows) / len(paired_base_rows)
             if paired_base_rows else None),
            (sum(bool(row.get("evidence_references_valid")) for row in paired_optimized_rows) / len(paired_optimized_rows)
             if paired_optimized_rows else None)),
        "mean_review_latency_seconds": (
            (statistics.mean(row["review_latency_seconds"] for row in paired_base_rows
                             if isinstance(row.get("review_latency_seconds"), (int, float)))
             if any(isinstance(row.get("review_latency_seconds"), (int, float)) for row in paired_base_rows) else None),
            (statistics.mean(row["review_latency_seconds"] for row in paired_optimized_rows
                             if isinstance(row.get("review_latency_seconds"), (int, float)))
             if any(isinstance(row.get("review_latency_seconds"), (int, float)) for row in paired_optimized_rows) else None)),
        "mean_retrieval_latency_seconds": (
            (statistics.mean((row.get("timings") or {}).get("retrieval_seconds", 0) for row in paired_base_rows)
             if paired_base_rows else None),
            (statistics.mean((row.get("timings") or {}).get("retrieval_seconds", 0) for row in paired_optimized_rows)
             if paired_optimized_rows else None)),
        "tokens_accounted": (base_rate.get("tokens_accounted"), optimized_rate.get("tokens_accounted")),
        "rate_limit_wait_seconds": (base_rate.get("rate_limit_wait_seconds"),
                                     optimized_rate.get("rate_limit_wait_seconds")),
    }
    metric_differences = {
        metric: (right - left if isinstance(left, (int, float)) and isinstance(right, (int, float)) else None)
        for metric, (left, right) in metric_values.items()
    }
    completed_base = sum(row.get("case_status") == "completed" for row in base_rows.values())
    completed_optimized = sum(row.get("case_status") == "completed" for row in optimized_rows.values())
    partially_paired = sum(
        (base_rows.get(case_id, {}).get("case_status") == "completed") !=
        (optimized_rows.get(case_id, {}).get("case_status") == "completed") for case_id in case_ids
    )
    fully_paired = len(common_ids)
    return {
        "baseline": base_summary,
        "optimized": optimized_summary,
        "optimized_minus_baseline": metric_differences,
        "case_outcome_counts": dict(counts),
        "paired_case_count": len(cases),
        "complete_paired_case_count": fully_paired,
        "completed_baseline_cases": completed_base,
        "completed_optimized_cases": completed_optimized,
        "partially_paired_case_count": partially_paired,
        "pending_case_count": len(case_ids) - fully_paired - partially_paired,
        "provider_failure_count": sum(bool(row.get("provider_errors"))
                                       for row in list(base_rows.values()) + list(optimized_rows.values())),
        "quota_deferred_case_count": sum(row.get("case_status") == "quota_deferred"
                                          for row in list(base_rows.values()) + list(optimized_rows.values())),
        "paired_quality": {"baseline": base_quality, "optimized": optimized_quality},
        "comparison_basis_case_ids": common_ids,
        "provisional": not (_completed(base) and _completed(optimized)),
        "cases": cases,
    }


def build_artifacts(*, results_dir: Path = RESULTS, run2: bool = False) -> dict[str, Any]:
    cases = load_cases()
    if run2:
        from evaluation.phase_e3_4_run2 import DEFAULT_ARMS, select_arms
        plan_path = results_dir / "plan.json"
        plan = json.loads(plan_path.read_text(encoding="utf-8")) if plan_path.is_file() else None
        selected_arms = select_arms(plan.get("selected_arms") if plan else DEFAULT_ARMS)
        configs = {arm: RUN2_CONFIGS[arm] for arm in selected_arms}
    else:
        selected_arms = tuple(CONFIGS)
        configs = CONFIGS
    results = {name: _load_result(results_dir / filename) for name, filename in configs.items()}
    summary = {
        "experiment": "phase_e3_4_run2" if run2 else "phase_e3_4",
        "status": "completed" if all(_completed(value) for value in results.values()) else "pending_or_incomplete",
        "expected_cases_per_configuration": 30,
        "historical_results_are_not_current_measurements": True,
        "selected_arms": list(selected_arms),
        "configurations": {name: _configuration_summary(name, result) for name, result in results.items()},
        "absolute_differences_vs_no_rag": {},
        "exploratory_paired_bootstrap_vs_no_rag": {},
    }
    base = results.get("no_rag")
    if base and all(_completed(value) for value in results.values()):
        base_quality = _case_quality(base["case_results"])
        for name, result in results.items():
            if name == "no_rag":
                continue
            quality = _case_quality(result["case_results"])
            summary["absolute_differences_vs_no_rag"][name] = {
                key: (quality[key] - base_quality[key]
                      if isinstance(quality.get(key), (int, float)) and isinstance(base_quality.get(key), (int, float))
                      else None)
                for key in ("precision", "recall", "f1", "clean_false_positive_rate")
            }
            summary["exploratory_paired_bootstrap_vs_no_rag"][name] = {
                metric: _bootstrap_difference(base["case_results"], result["case_results"], metric)
                for metric in ("precision", "recall", "f1", "clean_false_positive_rate")
            }

    by_config = {}
    for name, result in results.items():
        if result:
            by_config[name] = {row.get("case_id"): row for row in result.get("case_results", [])}
    comparisons = []
    for case in cases:
        row = {"case_id": case["case_id"], "category": case["category"],
               "expected_issue_type": case.get("expected_issue_type"), "configurations": {}}
        for name in selected_arms:
            actual = by_config.get(name, {}).get(case["case_id"])
            row["configurations"][name] = None if actual is None else {
                "case_status": actual.get("case_status"), "schema_valid": actual.get("schema_valid"),
                "processing_errors": actual.get("processing_errors", []),
                "provider_errors": actual.get("provider_errors", []),
                "evidence_references_valid": actual.get("evidence_references_valid"),
                "matching": actual.get("matching"), "findings": actual.get("findings"),
                "evidence_ids": [evidence_id for finding in actual.get("findings", [])
                                 for evidence_id in finding.get("evidence_ids", [])],
            }
        states = {name: row["configurations"].get(name) for name in selected_arms}
        base_row, v2_row = states.get("no_rag"), states.get("v2_baseline")
        v3b_row, v3o_row = states.get("v3_baseline"), states.get("v3_optimized")
        def tp(entry): return bool(entry and entry.get("matching", {}).get("true_positives", 0))
        def fp(entry): return int(entry.get("matching", {}).get("false_positives", 0)) if entry else None
        def observed(left, right, predicate):
            return None if left is None or right is None else bool(predicate(left, right))
        if run2:
            row["paired_v3_outcome"] = None
            if v3b_row is not None and v3o_row is not None:
                base_success = v3b_row.get("case_status") == "completed"
                optimized_success = v3o_row.get("case_status") == "completed"
                if base_success and optimized_success:
                    row["paired_v3_outcome"] = "both_succeed"
                elif not base_success and not optimized_success:
                    row["paired_v3_outcome"] = "both_fail"
                elif base_success:
                    row["paired_v3_outcome"] = "baseline_only_succeeds"
                else:
                    row["paired_v3_outcome"] = "optimized_only_succeeds"
        else:
            row["observed_changes_vs_v2"] = {
            "v3_baseline_fixes_v2_false_negative": observed(v2_row, v3b_row, lambda a, b: not tp(a) and tp(b)),
            "v3_optimized_fixes_v2_false_negative": observed(v2_row, v3o_row, lambda a, b: not tp(a) and tp(b)),
            "v3_baseline_new_false_positive": observed(v2_row, v3b_row, lambda a, b: (fp(a) or 0) == 0 and (fp(b) or 0) > 0),
            "v3_optimized_new_false_positive": observed(v2_row, v3o_row, lambda a, b: (fp(a) or 0) == 0 and (fp(b) or 0) > 0),
            "v3_baseline_suppresses_v2_false_positive": observed(v2_row, v3b_row, lambda a, b: (fp(a) or 0) > 0 and (fp(b) or 0) == 0),
            "v3_optimized_suppresses_v2_false_positive": observed(v2_row, v3o_row, lambda a, b: (fp(a) or 0) > 0 and (fp(b) or 0) == 0),
            "v3_baseline_loses_v2_true_positive": observed(v2_row, v3b_row, lambda a, b: tp(a) and not tp(b)),
            "v3_optimized_loses_v2_true_positive": observed(v2_row, v3o_row, lambda a, b: tp(a) and not tp(b)),
                "optimized_differs_from_v3_baseline": observed(v3b_row, v3o_row, lambda a, b:
                    (a.get("findings"), a.get("schema_valid")) !=
                    (b.get("findings"), b.get("schema_valid"))),
            }
        comparisons.append(row)

    annotation_rows = []
    for name in ("v2_baseline", "v3_baseline", "v3_optimized"):
        if name not in selected_arms:
            continue
        for case_id, actual in by_config.get(name, {}).items():
            evidence_by_id = {item.get("evidence_id"): item
                              for item in actual.get("retrieval_diagnostics", []) if item.get("evidence_id")}
            for finding_index, finding in enumerate(actual.get("findings", [])):
                cited = finding.get("evidence_ids", [])
                if not cited:
                    cited = [None]
                reference_rows = actual.get("evidence_reference_diagnostics", []) or []
                reference = next((item for item in reference_rows
                                  if item.get("finding_index") == finding_index), None)
                for evidence_id in cited:
                    item = evidence_by_id.get(evidence_id) if evidence_id else None
                    annotation_rows.append({
                        "configuration": name, "case_id": case_id,
                        "finding_id": finding.get("finding_id"), "finding_title": finding.get("title"),
                        "issue_type": finding.get("issue_type"), "line_start": finding.get("line_start"),
                        "line_end": finding.get("line_end"), "uncertain": finding.get("uncertain"),
                        "cited_evidence_id": evidence_id,
                        "reference_status": (reference or {}).get("reference_status"),
                        "citation_id_status": ("uncited" if not evidence_id else
                            "valid" if item else "unknown_or_fabricated"),
                        "source_family": item.get("source_family") if item else None,
                        "source_url": item.get("source_url") if item else None,
                        "source_identifiers": item.get("source_identifiers") if item else None,
                        "distance": item.get("distance") if item else None,
                        "evidence_preview": item.get("text_preview") if item else None,
                        "semantic_support_judgment": None,
                        "contradiction_judgment": None,
                        "annotation_status": "pending_human_review",
                        "reviewer_notes": None,
                    })
    if not annotation_rows:
        annotation_rows = [{
            "configuration": config, "case_id": None, "finding_id": None,
            "finding_title": None, "issue_type": None, "line_start": None, "line_end": None,
            "uncertain": None, "cited_evidence_id": None, "citation_id_status": None,
            "reference_status": None, "source_family": None, "source_url": None,
            "source_identifiers": None, "distance": None,
            "evidence_preview": None, "semantic_support_judgment": None,
            "contradiction_judgment": None, "annotation_status": "awaiting_live_result",
            "reviewer_notes": None,
        } for config in selected_arms if config != "no_rag"]
    if run2:
        baseline_result = results.get("v3_baseline")
        optimized_result = results.get("v3_optimized")
        if "v3_baseline" in selected_arms and "v3_optimized" in selected_arms:
            summary["paired_v3_comparison"] = _paired_v3_comparison(
                baseline_result, optimized_result, [case["case_id"] for case in cases]
            )
        else:
            summary["paired_v3_comparison"] = None
        summary["historical_reference_results"] = _historical_references(results_dir)
        preflight_path = results_dir / "preflight.json"
        summary["preflight"] = (json.loads(preflight_path.read_text(encoding="utf-8"))
                                if preflight_path.is_file() else None)
        pair_summary = summary.get("paired_v3_comparison")
        if pair_summary is not None:
            preflight = summary["preflight"] or {}
            projection = preflight.get("quota_projection") or {}
            pair_summary["requests_remaining"] = projection.get("planned_requests")
            pair_summary["estimated_tokens_remaining"] = projection.get("projected_total_tokens")
            pair_summary["next_batch"] = preflight.get("next_batch")
        rate_state_path = results_dir / "rate_limit_usage.json"
        quota_report_path = results_dir / "quota_usage_report.json"
        if rate_state_path.is_file():
            rate_state = json.loads(rate_state_path.read_text(encoding="utf-8"))
            quota_report = {
                "selected_arms": list(selected_arms),
                "policy": rate_state.get("policy"),
                "daily_window": rate_state.get("daily_window"),
                "totals": {key: rate_state.get(key) for key in (
                    "total_requests_attempted", "total_successful_requests", "total_failed_requests",
                    "total_retries", "total_input_tokens", "total_output_tokens",
                    "total_tokens_reported", "total_tokens_accounted", "http_429_count",
                    "total_rate_limit_wait_seconds",
                )},
                "per_arm": {arm: rate_state.get("per_arm", {}).get(arm) for arm in selected_arms},
                "available_token_budget_after_safety_margin": (summary["preflight"] or {}).get(
                    "available_token_budget_after_safety_margin"),
                "accounting_note": rate_state.get("accounting_note"),
            }
            _atomic_json(quota_report_path, {"status": "usage_recorded", **quota_report})
        else:
            projection = (summary["preflight"] or {}).get("quota_projection") or {}
            _atomic_json(quota_report_path, {
                "status": ("preflight_only_no_provider_requests_recorded"
                           if summary["preflight"] and
                           (summary["preflight"].get("network") or {}).get("live_requests_made") == 0
                           else "usage_unavailable"),
                "selected_arms": list(selected_arms),
                "projected_requests": projection.get("planned_requests"),
                "projected_tokens": projection.get("projected_total_tokens"),
                "available_token_budget_after_safety_margin": (summary["preflight"] or {}).get(
                    "available_token_budget_after_safety_margin"),
                "actual_usage": None,
                "accounting_note": "No rate-limit ledger exists because no provider request has started.",
            })
    summary_name = "summary.json" if run2 else "phase_e3_4_summary.json"
    comparison_name = "case_comparison.json" if run2 else "phase_e3_4_case_comparison.json"
    annotation_name = ("grounding_annotation_template.jsonl" if run2
                       else "phase_e3_4_grounding_annotation_template.jsonl")
    _atomic_json(results_dir / summary_name, summary)
    _atomic_json(results_dir / comparison_name, {
        "status": summary["status"], "case_count": len(comparisons), "cases": comparisons,
        "causal_attribution": "not_inferred; paired changes are observations only",
    })
    annotation_path = results_dir / annotation_name
    results_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=results_dir,
                                     prefix=f".{annotation_path.name}.", suffix=".tmp", delete=False) as stream:
        temporary = Path(stream.name)
        for row in annotation_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    temporary.replace(annotation_path)
    _write_markdown_report(summary, comparisons, results_dir, run2=run2)
    return summary


def _write_markdown_report(summary: dict[str, Any], comparisons: list[dict[str, Any]],
                           results_dir: Path, *, run2: bool = False) -> None:
    if run2:
        _write_run2_markdown_report(summary, comparisons, results_dir)
        return
    def fmt(value: Any) -> str:
        return "Unavailable" if value is None else f"{value:.3f}" if isinstance(value, (int, float)) else str(value)

    summary_name = "summary.json" if run2 else "phase_e3_4_summary.json"
    comparison_name = "case_comparison.json" if run2 else "phase_e3_4_case_comparison.json"
    annotation_name = ("grounding_annotation_template.jsonl" if run2
                       else "phase_e3_4_grounding_annotation_template.jsonl")
    pacing_seconds = 20 if run2 else 12

    historical = {
        "No RAG": {"precision": .760, "recall": .950, "f1": .844, "clean_fpr": .300,
                    "schema": "30/30", "provider_errors": 0},
        "RAG v2": {"precision": .696, "recall": .842, "f1": .762, "clean_fpr": .400,
                   "schema": "29/30", "provider_errors": 0},
    }
    lines = [
        ("# Phase E3.4 Run 2 — Controlled Groq benchmark and production-readiness report"
         if run2 else "# Phase E3.4 — Controlled Groq benchmark and production-readiness report"), "",
        f"**Current experiment status:** `{summary['status']}`. Historical values below are context only and are not current measurements.", "",
        "## Executive summary", "",
    ]
    if summary["status"] != "completed":
        if run2:
            lines += [
                "The isolated Run 2 four-way live benchmark is pending or incomplete. No complete current quality result is available. Run 2 uses separate immutable reference snapshots, disposable working copies, quota accounting, and checkpoints; Run 1 files are not inputs or outputs for this run.",
                "",
                "**Production readiness: Insufficient evidence. Keep the production default on v2.**",
                "",
            ]
        else:
            lines += [
                "The four-way live benchmark is pending or incomplete. No current code-review quality result is available. The Groq key is present in the runtime, but this Codex environment has a known prior DNS/connection failure; no repeated network probe or live request was made. The sandbox also denied process listing, so writer quiescence could not be confirmed and no temporary Chroma copy was made.",
                "",
                "**Production readiness: Insufficient evidence. Keep the production default on v2.**",
                "",
            ]
    else:
        lines += ["All four configurations have 30 completed cases. Metrics remain preliminary because the benchmark labels are synthetic and evidence support has not been human-verified.", ""]
    lines += [
        "## Preflight", "",
        "- Model: `openai/gpt-oss-20b`, temperature 0.1, strict JSON Schema `review-v1`; provider implementation supports this model.",
        f"- Groq SDK retry count: 2; application-level retry loop: none; request pacing: {pacing_seconds} seconds.",
        "- Benchmark: 30 unique cases, balanced 10 security / 10 logic-runtime / 10 clean. Its evaluation identity hash matches both historical E2 checkpoints.",
        "- Embedding model: cached MiniLM revision `1110a243fdf4706b3f48f1d95db1a4f5529b4d41`; v2/v3 canonical collections were found with 6,100 / 19,418 records, respectively.",
        "- Temporary copies: not created because the sandbox could not verify that Chroma writers were stopped. Canonical v2/v3 were not used for benchmark execution.",
        "- Live Groq requests: 0. No credentials are recorded in these artifacts.",
        "",
        "## Current four-way results", "",
        "| Configuration | Status | TP | FP | FN | Precision | Recall | F1 | Clean FP rate | Schema validity | Evidence-ID validity |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for name, label in (("no_rag", "No RAG"), ("v2_baseline", "RAG v2 baseline"),
                        ("v3_baseline", "RAG v3 baseline"), ("v3_optimized", "RAG v3 optimized")):
        config = summary["configurations"][name]
        quality = (config.get("metrics") or {}).get("case_level_quality") or {}
        metric = config.get("metrics") or {}
        schema = config.get("schema_validity_rate")
        schema_text = (f"{schema:.1%}" if schema is not None else "Unavailable")
        evidence_rate = metric.get("evidence_reference_validity_rate")
        evidence_text = (f"{evidence_rate:.1%}" if evidence_rate is not None else "N/A")
        lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            label, config.get("status", "pending"), quality.get("true_positives", "Unavailable"),
            quality.get("false_positives", "Unavailable"), quality.get("false_negatives", "Unavailable"),
            fmt(quality.get("precision")), fmt(quality.get("recall")), fmt(quality.get("f1")),
            fmt(quality.get("clean_false_positive_rate")), schema_text, evidence_text))
    lines += ["", "Unavailable values are null in JSON. The existing scorer excludes schema-invalid or processing-error cases from finding-quality denominators and reports them in reliability counts.",
              "", "## Historical reference (not current results)", "",
              "| Historical run | Precision | Recall | F1 | Clean FP rate | Schema validity | Provider errors |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for label, values in historical.items():
        lines.append(f"| {label} | {values['precision']:.3f} | {values['recall']:.3f} | {values['f1']:.3f} | {values['clean_fpr']:.3f} | {values['schema']} | {values['provider_errors']} |")
    lines += [
        "",
        "## Per-category performance", "",
        "| Configuration | Category | TP | FP | FN | Precision | Recall | Clean FP rate |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, label in (("no_rag", "No RAG"), ("v2_baseline", "RAG v2 baseline"),
                        ("v3_baseline", "RAG v3 baseline"), ("v3_optimized", "RAG v3 optimized")):
        config = summary["configurations"][name]
        for category in ("security", "logic_runtime", "clean"):
            value = config.get("per_category_quality", {}).get(category, {})
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                label, category, value.get("true_positives", "Unavailable"),
                value.get("false_positives", "Unavailable"), value.get("false_negatives", "Unavailable"),
                fmt(value.get("precision")), fmt(value.get("recall")),
                fmt(value.get("clean_false_positive_rate"))))
    lines += [
        "",
        "## Clean-code false-positive analysis", "",
        "Clean-case counts and false-positive rates are shown above and in the machine-readable summary. These remain unavailable until all 30 live cases complete for the configuration.",
        "",
        "## Schema, provider, and citation reliability", "",
        "| Configuration | Completed / total | Provider-error cases | Schema failures | Schema-validity rate | Evidence-ID validation failures |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for name, label in (("no_rag", "No RAG"), ("v2_baseline", "RAG v2 baseline"),
                        ("v3_baseline", "RAG v3 baseline"), ("v3_optimized", "RAG v3 optimized")):
        config = summary["configurations"][name]
        completed = config.get("completed_cases")
        total = config.get("total_cases")
        schema_rate = config.get("schema_validity_rate")
        lines.append("| {} | {} | {} | {} | {} | {} |".format(
            label, f"{completed}/{total}" if completed is not None else "Unavailable",
            config.get("provider_error_cases", "Unavailable"),
            config.get("schema_validation_failures", "Unavailable"),
            f"{schema_rate:.1%}" if schema_rate is not None else "Unavailable",
            config.get("evidence_id_validation_failures") if config.get("evidence_id_validation_failures") is not None else "N/A"))
    lines += [
        "",
        "## Retrieval diagnostics", "",
        f"Retrieved evidence counts, empty-evidence rate, source-family distribution, retrieval latency, and provider inference latency are recorded in `{summary_name}` when results become available. Evidence-token utilization is unavailable because the runner does not persist a comparable serialized-context budget denominator.",
        "",
        "## Paired case analysis and grounding", "",
        f"`{comparison_name}` contains all benchmark IDs and null outcomes until live results arrive. When results are present, flags describe paired observations only and do not attribute causes to retrieved context. `{annotation_name}` records citation identity, source provenance, and a bounded retrieved preview where available. Semantic support/contradiction judgments remain pending human annotation; valid IDs are not semantic grounding accuracy.",
        "",
        "The sample is only 30 synthetic cases. The analyzer computes exploratory paired bootstrap intervals only when all four configurations contain complete paired case sets. Such intervals are not significance claims and do not establish production generalization.",
        "",
        "## Execution and reproduction", "",
        ("A completed four-way Run 2 plans 120 review requests and at least 2,380 seconds of request pacing, before model time or SDK retries. No live request is made during preparation or preflight."
         if run2 else "Live execution was not started. The prior sandbox DNS failure is known, so this phase did not repeat network diagnostics or attempt to bypass network restrictions. The API account's remaining quota/billing state cannot be inspected offline. A completed four-way run plans 120 review requests and at least 1,392 seconds of pacing waits across four separately paced runs, before model time or SDK retries."),
        "",
        *( [
            "From a local macOS terminal, stop application, API/MCP, and ingestion writers, then prepare isolated Run 2 snapshots and working copies:",
            "", "```sh", "bash evaluation/phase_e3_4_prepare_run2.sh", "```", "",
            "Read the current remaining daily token quota from the Groq dashboard. Enter only that number when prompted (never enter the API key here), then start the bounded four-arm run:",
            "", "```sh", "read -r -p 'Remaining TPD from dashboard: ' GROQ_REMAINING_TPD; export GROQ_REMAINING_TPD",
            "bash evaluation/phase_e3_4_run2.sh", "```", "",
            "On interruption or a provider failure, inspect the saved per-arm result and checkpoint, enter the current dashboard quota value, then resume explicitly:",
            "", "```sh", "read -r -p 'Remaining TPD from dashboard: ' GROQ_REMAINING_TPD; export GROQ_REMAINING_TPD",
            "PHASE_E3_4_RUN2_RESUME=1 bash evaluation/phase_e3_4_run2.sh", "```", "",
            "After confirming a provider daily quota reset, set `PHASE_E3_4_RUN2_CONFIRM_DAILY_RESET=1` for the next resume. Resume pins the model, strict-output/schema mode, benchmark content, RAG mode/strategy, immutable snapshot and working-copy identities, and quota policy; completed cases are skipped.",
        ] if run2 else [
            "From a local macOS terminal, stop the app/API/MCP servers and any ingestion writers, confirm no other Chroma writers are active, then create verified temporary copies:",
            "", "```sh", "bash evaluation/phase_e3_4_prepare_copies.sh", "```", "",
            "Before spending quota, perform one unauthenticated endpoint reachability check from that terminal (do not include or print the API key). A reachable server may return 401/405; DNS or connection failures mean stop and do not start the benchmark:",
            "", "```sh", "curl --max-time 5 -sS -o /dev/null -w 'HTTP %{http_code}\\n' https://api.groq.com/openai/v1/models", "```", "",
            "Confirm the Groq account has sufficient quota for up to 120 review requests (plus SDK retries). Run the exact sequential, checkpointed command:",
            "", "```sh", "bash evaluation/phase_e3_4_run.sh", "```", "",
            "If a request fails, the runner records that case, stops before making the remaining requests in that configuration, writes output/checkpoint, and exits nonzero. Resolve the provider/network cause and resume only that configuration using:",
            "", "```sh", "PHASE_E3_4_RESUME=1 bash evaluation/phase_e3_4_run.sh", "```", "",
            "Resume is guarded by pinned benchmark contents, model, strict-output/schema mode, prompt and scoring fingerprints, RAG strategy/snapshot, SDK retry policy, pacing, and case list. Completed cases are skipped; failed/incomplete cases are retried. The script will not advance to the next configuration until the current one completes.",
        ] ),
        "",
        "After all four configurations complete:",
        "",
        "```sh",
        ".venv/bin/python -m evaluation.phase_e3_4_analysis",
        "```", "",
        "## Offline verification", "",
        ("Run 2 verification is reported by the executing test command; no live Groq requests are made by tests."
         if run2 else "Executed: `.venv/bin/python -m pytest tests -q -k 'not retrieval_has_no_side_effects'` — **163 passed, 1 deselected**. Focused checkpoint/analysis tests: **10 passed**. Targeted source compilation and `bash -n` for the two Phase E3.4 scripts passed. `git diff --check` passed. No bytecode/cache artifacts were found under `rag/data/raw`."),
        "",
        "## Production readiness and next phase", "",
        ("**Insufficient evidence.** Keep v2 as the production default until the controlled benchmark completes and human reviewers assess semantic evidence support."
         if summary["status"] != "completed" else
         "**Insufficient evidence pending human grounding review.** Keep v2 as the production default; do not infer causality or general performance from this synthetic benchmark."),
        "",
        "Next: establish quiescent copies in the user's local terminal; complete/resume the four Groq runs; inspect paired regressions and citation previews; human-annotate evidence relevance/support; then decide whether targeted retrieval work is justified.",
        "",
    ]
    output = results_dir / ("evaluation_report.md" if run2 else "phase_e3_4_evaluation_report.md")
    output.write_text("\n".join(lines), encoding="utf-8")


def _write_run2_markdown_report(summary: dict[str, Any], comparisons: list[dict[str, Any]],
                                results_dir: Path) -> None:
    def fmt(value: Any) -> str:
        return "Unavailable" if value is None else f"{value:.3f}" if isinstance(value, (int, float)) else str(value)

    selected = summary["selected_arms"]
    preflight = summary.get("preflight") or {}
    projection = preflight.get("quota_projection") or {}
    refs = preflight.get("references") or {}
    planned = preflight.get("initial_total_planned_requests", 30 * len(selected))
    pending_requests = preflight.get("total_planned_requests", planned)
    lines = [
        "# Phase E3.4 Run 2 — V3 retrieval strategy comparison", "",
        f"**Run status:** `{summary['status']}`. Results are preliminary until both selected arms complete the same benchmark cases.", "",
        "## Controlled experiment", "",
        f"- Selected arms: `{', '.join(selected)}`; 30 benchmark cases per arm and 60 unique review evaluations in the full two-arm experiment; initial planned review requests: {planned}; pending at the latest preflight: {pending_requests}.",
        "- The v3 baseline and optimized arms share the same verified immutable v3 snapshot and differ by retrieval strategy configuration.",
        "- Model: `openai/gpt-oss-20b`, Groq, temperature 0.1, strict `review-v1` schema; prompts, benchmark, labels, and scoring remain pinned.",
        f"- V3 snapshot directory SHA-256: `{(refs.get('v3') or {}).get('snapshot_directory_sha256', 'Unavailable')}`.",
        f"- V3 snapshot manifest SHA-256: `{(refs.get('v3') or {}).get('manifest_sha256', 'Unavailable')}`.",
        "",
        "## Preflight and quota estimate", "",
        f"- Status: `{preflight.get('status', 'not_run')}`; execution permitted: `{preflight.get('execution_permitted', False)}`.",
        f"- Full experiment projection: `{json.dumps(preflight.get('full_experiment') or {}, sort_keys=True)}`.",
        f"- Next batch: `{json.dumps(preflight.get('next_batch') or {}, sort_keys=True)}`.",
        f"- Estimated tokens: {projection.get('projected_total_tokens', 'Unavailable')} using {projection.get('method', 'the recorded preflight estimator')}.",
        f"- Available token budget after safety margin: {preflight.get('available_token_budget_after_safety_margin', 'Unavailable')}.",
        f"- Estimated maximum tokens in one request: {projection.get('maximum_projected_single_request_tokens', 'Unavailable')}; safety-adjusted TPM budget: {projection.get('conservative_minute_token_budget', 'Unavailable')}.",
        "- These are projections, not guaranteed upper bounds. Runtime token usage and provider rate-limit headers are recorded separately.",
        "",
        "## Results by arm", "",
        "| Arm | Status | Cases completed | TP | FP | FN | Precision | Recall | F1 | Clean FP rate | Schema validity | Evidence-reference validity | Provider-error cases | Mean review sec | Mean retrieval sec | Tokens accounted | Rate-limit waits sec |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for arm in selected:
        config = summary["configurations"][arm]
        quality = (config.get("metrics") or {}).get("case_level_quality") or {}
        raw_metrics = (config.get("metrics") or {})
        rate_usage = config.get("rate_limit_usage") or {}
        retrieval = config.get("retrieval") or {}
        lines.append("| {} | {} | {}/30 | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
            arm, config.get("status"), config.get("completed_cases") or 0,
            quality.get("true_positives", "Unavailable"), quality.get("false_positives", "Unavailable"),
            quality.get("false_negatives", "Unavailable"), fmt(quality.get("precision")),
            fmt(quality.get("recall")), fmt(quality.get("f1")),
            fmt(quality.get("clean_false_positive_rate")), fmt(config.get("schema_validity_rate")),
            fmt(raw_metrics.get("evidence_reference_validity_rate")),
            config.get("provider_error_cases", "Unavailable"),
            fmt(config.get("mean_review_latency_seconds")),
            fmt(retrieval.get("mean_retrieval_latency_seconds")),
            rate_usage.get("tokens_accounted", "Unavailable"),
            fmt(rate_usage.get("rate_limit_wait_seconds"))))
    pair = summary.get("paired_v3_comparison")
    lines += ["", "## Paired v3 comparison", ""]
    if pair is None:
        lines.append("The selected arms do not include both v3 strategies; a paired v3 comparison is unavailable.")
    else:
        lines += [
            f"- Cases represented: {pair['paired_case_count']}; completed baseline: {pair['completed_baseline_cases']}; completed optimized: {pair['completed_optimized_cases']}; fully paired: {pair['complete_paired_case_count']}; partially paired: {pair['partially_paired_case_count']}; pending/failed on both sides: {pair['pending_case_count']}; provisional: `{pair['provisional']}`.",
            f"- Paired quality metrics use only common completed cases `{pair['comparison_basis_case_ids']}`; partial and unmatched cases are not treated as paired observations.",
            f"- Remaining estimated work at the latest preflight: {pair.get('requests_remaining', 'Unavailable')} requests and {pair.get('estimated_tokens_remaining', 'Unavailable')} reserved tokens.",
            f"- Outcome counts: `{json.dumps(pair['case_outcome_counts'], sort_keys=True)}`.",
            f"- Optimized minus baseline: `{json.dumps(pair['optimized_minus_baseline'], sort_keys=True)}`.",
            "- Case-level findings, schema/evidence validity, provider errors, review/retrieval latency, and false-positive/schema-only differences are in `case_comparison.json`.",
        ]
    lines += ["", "## Historical Reference Results — Not Part of the Controlled Run 2 Comparison", ""]
    historical = summary.get("historical_reference_results") or {}
    if not historical:
        lines.append("No historical result files were available at report generation time.")
    else:
        lines += [
            "These results are retained as references only. They may have different execution conditions and do not establish a controlled head-to-head difference.",
            "", "| Historical result | Source file | Cases | Precision | Recall | Clean FP rate | Schema validity |", "|---|---|---:|---:|---:|---:|---:|",
        ]
        for label, item in historical.items():
            metrics = item.get("metrics") or {}
            lines.append("| {} | {} | {} | {} | {} | {} | {} |".format(
                label, item.get("source_file"), item.get("case_count", "Unavailable"),
                fmt(metrics.get("finding_precision")), fmt(metrics.get("finding_recall")),
                fmt(metrics.get("clean_case_false_positive_rate")),
                fmt(metrics.get("json_schema_validity_rate"))))
    lines += [
        "", "## Outputs and resume", "",
        "Arm results and checkpoints are independent. A deterministic same-case batch is passed to each arm; completed successful cases are skipped, while failed or quota-deferred cases remain retryable. Batch sizing is session metadata and does not change the pinned experiment identity. Quota usage is stored in `rate_limit_usage.json` and summarized in `quota_usage_report.json`.",
        "", "## Local commands", "",
        "From `/Users/shreyasbasaragi/AI Code Reviewer`, default preparation copies v3 only. Set `PHASE_E3_4_RUN2_ARMS` to another supported arm list before both preparation and execution to run a different subset:",
        "", "```sh", "bash evaluation/phase_e3_4_prepare_run2.sh", "```", "",
        "Set the remaining daily token count from the Groq dashboard. The following preflight is local-only and does not make Groq requests:",
        "", "```sh", "read -r -p 'Remaining TPD from dashboard: ' GROQ_REMAINING_TPD",
        "SNAPSHOT_ROOT=\"${PHASE_E3_4_RUN2_SNAPSHOT_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_chroma_snapshots}\"",
        "WORKING_ROOT=\"${PHASE_E3_4_RUN2_WORKING_ROOT:-${TMPDIR:-/tmp}/phase_e3_4_run2_working}\"",
        f".venv/bin/python -m evaluation.phase_e3_4_run2 preflight --snapshot-root \"$SNAPSHOT_ROOT\" --working-root \"$WORKING_ROOT\" --run-dir evaluation/results/phase_e3_4_run2 --remaining-daily-tokens \"$GROQ_REMAINING_TPD\" --arms {','.join(selected)}",
        "```", "",
        f"Start the selected arms (this plans up to {planned} Groq review requests):", "", "```sh",
        "export GROQ_REMAINING_TPD", "bash evaluation/phase_e3_4_run2.sh", "```", "",
        "Resume an interrupted selected arm after checking the saved result/checkpoint and current dashboard quota (this can make Groq requests for unfinished cases):", "", "```sh",
        "export GROQ_REMAINING_TPD", "PHASE_E3_4_RUN2_RESUME=1 bash evaluation/phase_e3_4_run2.sh", "```", "",
        "Review `paired_v3_comparison` in `summary.json` and the per-case rows in `case_comparison.json`. No live requests are made by analysis.",
        "", "## Limitations", "",
        "The token projection is byte-based and not tokenizer-exact. Provider-account use outside this run can change remaining quota after the dashboard value is read. A passing result is not evidence of behavioral correctness or general performance beyond this synthetic benchmark.",
        "",
    ]
    output = results_dir / "evaluation_report.md"
    output.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", type=Path, default=RESULTS)
    parser.add_argument("--run2", action="store_true",
                        help="Use the isolated phase_e3_4_run2 artifact filenames")
    args = parser.parse_args()
    print(json.dumps(build_artifacts(results_dir=args.results_dir, run2=args.run2), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
