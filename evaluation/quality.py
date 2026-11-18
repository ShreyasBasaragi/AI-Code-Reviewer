"""Auditable finding-quality evaluation for stored fixtures or explicit live runs.

Offline fixture mode consumes stored predictions and never initializes a model.
Live mode is only reachable with an explicit CLI opt-in and uses the configured
provider through the shared orchestrator. Synthetic labels are not model truth.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib.metadata
import json
import math
import os
import statistics
import subprocess
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

QUALITY_DATASET_PATH = Path(__file__).with_name("quality_cases_v1.jsonl")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    cases = []
    seen = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on line {line_number} of {path.name}") from exc
        if not isinstance(value, dict):
            raise ValueError(f"expected JSON object on line {line_number} of {path.name}")
        case_id = value.get("case_id")
        if not isinstance(case_id, str) or not case_id or case_id in seen:
            raise ValueError(f"missing or duplicate case_id on line {line_number} of {path.name}")
        seen.add(case_id)
        cases.append(value)
    return cases


def _location_matches(prediction: Mapping[str, Any], expected: Mapping[str, Any], tolerance: int) -> bool:
    predicted_line = prediction.get("line_start")
    expected_line = expected.get("line_start")
    if predicted_line is None or expected_line is None:
        return True
    try:
        return abs(int(predicted_line) - int(expected_line)) <= tolerance
    except (TypeError, ValueError):
        return False


def _compatible(prediction: Mapping[str, Any], expected: Mapping[str, Any], tolerance: int) -> bool:
    expected_rule = expected.get("rule_id")
    if expected_rule:
        if str(prediction.get("rule_id") or "").casefold() != str(expected_rule).casefold():
            return False
    else:
        predicted_category = str(prediction.get("category") or prediction.get("issue_type") or "").strip().casefold()
        expected_category = str(expected.get("category") or expected.get("issue_type") or "").strip().casefold()
        if not expected_category or predicted_category != expected_category:
            return False
    return _location_matches(prediction, expected, tolerance)


def match_case_findings(
    ground_truth: Iterable[Mapping[str, Any]], predictions: Iterable[Mapping[str, Any]], *,
    line_tolerance: int = 2,
) -> dict[str, Any]:
    """Maximum-cardinality deterministic one-to-one matching by rule/category and location."""
    expected = list(ground_truth)
    predicted = list(predictions)
    adjacency = [
        [index for index, finding in enumerate(predicted)
         if _compatible(finding, label, line_tolerance)]
        for label in expected
    ]
    prediction_owner: dict[int, int] = {}

    def assign(label_index: int, visited: set[int]) -> bool:
        for prediction_index in adjacency[label_index]:
            if prediction_index in visited:
                continue
            visited.add(prediction_index)
            owner = prediction_owner.get(prediction_index)
            if owner is None or assign(owner, visited):
                prediction_owner[prediction_index] = label_index
                return True
        return False

    for label_index in range(len(expected)):
        assign(label_index, set())
    pairs = sorted((label_index, prediction_index)
                   for prediction_index, label_index in prediction_owner.items())
    matched_predictions = {prediction_index for _, prediction_index in pairs}
    return {
        "true_positives": len(pairs),
        "false_positives": len(predicted) - len(pairs),
        "false_negatives": len(expected) - len(pairs),
        "matches": [
            {"ground_truth_index": left, "prediction_index": right,
             "category": expected[left].get("rule_id") or expected[left].get("category"),
             "severity_correct": str(expected[left].get("severity", "")).casefold()
             == str(predicted[right].get("severity", "")).casefold()}
            for left, right in pairs
        ],
        "unmatched_prediction_indexes": sorted(set(range(len(predicted))) - matched_predictions),
    }


def _prf(tp: int, fp: int, fn: int) -> dict[str, Any]:
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    if tp + fp + fn == 0:
        f1 = None
    elif tp == 0:
        f1 = 0.0
    elif precision is not None and recall is not None:
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    else:
        f1 = None
    return {"true_positives": tp, "false_positives": fp, "false_negatives": fn,
            "precision": precision, "recall": recall, "f1": f1}


def summarize_quality(case_results: list[dict[str, Any]], dataset_size: int) -> dict[str, Any]:
    evaluated = [row for row in case_results if row.get("status") == "completed"]
    skipped = [row for row in case_results if row.get("status") == "skipped"]
    failed = [row for row in case_results if row.get("status") == "failed"]
    tp = sum(row["matching"]["true_positives"] for row in evaluated)
    fp = sum(row["matching"]["false_positives"] for row in evaluated)
    fn = sum(row["matching"]["false_negatives"] for row in evaluated)
    by_category: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for row in evaluated:
        for metric_name, index in (("true_positives", 0), ("false_positives", 1), ("false_negatives", 2)):
            by_category[str(row.get("category", "unknown"))][index] += row["matching"][metric_name]
    category_metrics = {name: _prf(*counts) for name, counts in sorted(by_category.items())}
    f1_values = [item["f1"] for item in category_metrics.values() if item["f1"] is not None]
    severity = [match for row in evaluated for match in row["matching"]["matches"]]
    latency = [float(row["latency_seconds"]) for row in case_results
               if isinstance(row.get("latency_seconds"), (int, float))]
    retrieval_rows = [row for row in case_results if row.get("retrieval_status") in {"passed", "failed"}]
    retrieval_latencies = [float(row["timings"]["rag_retrieval_seconds"])
                           for row in retrieval_rows
                           if isinstance(row.get("timings", {}).get("rag_retrieval_seconds"), (int, float))]
    evidence_rows = [row for row in case_results if row.get("evidence_validation_status") in {"passed", "failed"}]
    return {
        "micro": _prf(tp, fp, fn),
        "macro": {"f1": sum(f1_values) / len(f1_values) if f1_values else None,
                  "category_count": len(f1_values)},
        "per_category": category_metrics,
        "false_positives_per_reviewed_file": fp / len(evaluated) if evaluated else None,
        "severity_classification_accuracy": (
            sum(bool(item["severity_correct"]) for item in severity) / len(severity) if severity else None
        ),
        "severity_match_count": len(severity),
        "rag": {
            "retrieval_attempts": len(retrieval_rows),
            "retrieval_success_rate": (sum(row["retrieval_status"] == "passed" for row in retrieval_rows)
                                       / len(retrieval_rows) if retrieval_rows else None),
            "retrieved_chunks": sum(int(row.get("retrieved_evidence_count", 0)) for row in retrieval_rows),
            "retrieval_latency_seconds": {"count": len(retrieval_latencies),
                                          "median": statistics.median(retrieval_latencies) if retrieval_latencies else None,
                                          "p95": _percentile(retrieval_latencies, 0.95)},
            "evidence_validation_failure_rate": (sum(row["evidence_validation_status"] == "failed" for row in evidence_rows)
                                                  / len(evidence_rows) if evidence_rows else None),
        },
        "case_counts": {"dataset": dataset_size, "evaluated": len(evaluated),
                        "failed": len(failed), "skipped": len(skipped),
                        "coverage": len(evaluated) / dataset_size if dataset_size else None},
        "timing_seconds": {
            "count": len(latency), "minimum": min(latency) if latency else None,
            "maximum": max(latency) if latency else None,
            "median": statistics.median(latency) if latency else None,
            "p95": _percentile(latency, 0.95),
        },
        "matching_policy": "One-to-one maximum-cardinality matching; exact rule_id where ground truth has one, otherwise exact normalized category; line distance tolerance <= 2 only when both lines are present; missing lines do not disqualify. Severity is measured separately. Failed/skipped cases do not become false negatives.",
    }


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = max(0, math.ceil(quantile * len(ordered)) - 1)
    return ordered[rank]


def evaluate_fixture_predictions(
    cases: list[dict[str, Any]], predictions: Mapping[str, Any], *, line_tolerance: int = 2,
) -> list[dict[str, Any]]:
    """Evaluate stored outputs; missing/invalid records are skipped or failed, never guessed."""
    rows = []
    for case in cases:
        record = predictions.get(case["case_id"])
        if record is None:
            rows.append({"case_id": case["case_id"], "category": "mixed", "status": "skipped",
                         "processing_error": "missing_fixture_prediction"})
            continue
        if record.get("status") == "failed":
            rows.append({"case_id": case["case_id"], "category": "mixed", "status": "failed",
                         "processing_error": str(record.get("error", "fixture_failure"))[:120]})
            continue
        findings = record.get("findings")
        if not isinstance(findings, list) or any(not isinstance(item, dict) for item in findings):
            rows.append({"case_id": case["case_id"], "category": "mixed", "status": "failed",
                         "processing_error": "invalid_fixture_findings"})
            continue
        matching = match_case_findings(case.get("ground_truth", []), findings,
                                       line_tolerance=line_tolerance)
        category_values = [str(item.get("rule_id") or item.get("category") or "unknown")
                           for item in case.get("ground_truth", [])]
        category = category_values[0] if len(set(category_values)) == 1 and category_values else "mixed"
        rows.append({"case_id": case["case_id"], "category": category if category != "mixed" or category_values else "clean", "status": "completed",
                     "findings": findings, "matching": matching,
                     "latency_seconds": record.get("latency_seconds")})
    return rows


def _git_revision() -> dict[str, Any]:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True,
                               text=True, timeout=2).stdout.strip()
        status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                check=True, capture_output=True, text=True, timeout=2).stdout
        dirty = bool(status.strip())
        return {"commit": commit, "working_tree_dirty": dirty}
    except Exception:
        return {"commit": None, "working_tree_dirty": None}


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _environment() -> dict[str, Any]:
    packages = {}
    for name in ("torch", "transformers", "peft", "safetensors", "chromadb", "fastapi"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {"python": os.sys.version.split()[0], "platform": os.sys.platform,
            "machine": os.uname().machine if hasattr(os, "uname") else None,
            "packages": packages}


def _write_report_bundle(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    fields = ["case_id", "status", "category", "true_positives", "false_positives", "false_negatives", "latency_seconds", "processing_error"]
    with (output_dir / "case_results.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for row in report["case_results"]:
            matching = row.get("matching", {})
            writer.writerow({**{key: row.get(key) for key in fields},
                             "true_positives": matching.get("true_positives"),
                             "false_positives": matching.get("false_positives"),
                             "false_negatives": matching.get("false_negatives")})
    metrics = report["metrics"]
    markdown = [f"# Quality evaluation: {report['dataset_id']}", "",
                f"- Timestamp: {report['execution_timestamp']}",
                f"- Mode: {report['evaluation_mode']}", f"- Provider: {report['provider_identity']}",
                f"- Dataset size: {report['dataset_size']}",
                f"- Coverage: {metrics['case_counts']['evaluated']}/{metrics['case_counts']['dataset']}",
                f"- Micro precision / recall / F1: {metrics['micro']['precision']} / {metrics['micro']['recall']} / {metrics['micro']['f1']}",
                f"- Macro F1: {metrics['macro']['f1']}",
                f"- False positives per reviewed file: {metrics['false_positives_per_reviewed_file']}",
                f"- Severity accuracy: {metrics['severity_classification_accuracy']}",
                f"- Median / p95 latency (seconds): {metrics['timing_seconds']['median']} / {metrics['timing_seconds']['p95']}",
                "", f"Limitations: {report['limitations']}", ""]
    (output_dir / "summary.md").write_text("\n".join(markdown), encoding="utf-8")


def _make_report(cases: list[dict[str, Any]], rows: list[dict[str, Any]], *, mode: str,
                 provider_identity: str, dataset_path: Path, environment: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "execution_timestamp": datetime.now(timezone.utc).isoformat(), "revision": _git_revision(),
        "evaluation_mode": mode, "provider_identity": provider_identity,
        "dataset_id": dataset_path.stem, "dataset_sha256": _sha256(dataset_path),
        "dataset_size": len(cases), "metrics": summarize_quality(rows, len(cases)),
        "case_results": rows,
        "failed_case_ids": [row["case_id"] for row in rows if row["status"] == "failed"],
        "skipped_case_ids": [row["case_id"] for row in rows if row["status"] == "skipped"],
        "environment": environment or _environment(),
        "limitations": "Fixture labels are synthetic and manually authored. Offline stored-prediction results validate the metric harness; live model results on these fixtures do not establish real-world model quality. No behavior is tested or executed.",
    }


def load_prediction_records(path: Path) -> dict[str, dict[str, Any]]:
    return {item["case_id"]: item for item in load_jsonl(path)}


def run_live_evaluation(cases: list[dict[str, Any]], *, provider: Any, provider_identity: str,
                        rag_enabled: bool = False) -> list[dict[str, Any]]:
    """Run only when explicitly requested by a caller; tests inject offline providers."""
    from mcp_server.orchestrator import run_review_pipeline

    rows = []
    for case in cases:
        timing: dict[str, float] = {}
        started = time.perf_counter()
        try:
            result = run_review_pipeline(
                code=case["source_code"], file_path=f"{case['case_id']}.py", notify_desktop=False,
                llm=provider, retriever=None if rag_enabled else (lambda _code: []),
                phase_timings=timing,
            )
            findings = [item.to_dict() for item in result.review_response.findings if item.origin == "llm"]
            matching = match_case_findings(case.get("ground_truth", []), findings)
            cats = [str(item.get("rule_id") or item.get("category") or "unknown") for item in case.get("ground_truth", [])]
            errors = list(result.review_response.processing_errors)
            retrieval_failed = any(error.startswith("retrieval_failure") for error in errors)
            evidence_status = (
                "failed" if any("evidence" in error.casefold() for error in errors)
                else "passed" if result.review_response.valid else "not_assessed"
            )
            rows.append({"case_id": case["case_id"], "category": cats[0] if len(set(cats)) == 1 and cats else "clean" if not cats else "mixed",
                         "status": "completed" if result.review_response.valid else "failed",
                         "findings": findings, "matching": matching,
                         "latency_seconds": time.perf_counter() - started,
                         "processing_error": None if result.review_response.valid else errors,
                         "timings": timing, "retrieved_evidence_count": len(result.retrieved_evidence),
                         "retrieval_status": ("not_run" if not rag_enabled else "failed" if retrieval_failed else "passed"),
                         "evidence_validation_status": evidence_status})
        except Exception as exc:
            rows.append({"case_id": case["case_id"], "category": "mixed", "status": "failed",
                         "processing_error": type(exc).__name__, "latency_seconds": time.perf_counter() - started})
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("offline-fixture", "live-model"), required=True)
    parser.add_argument("--dataset", type=Path, default=QUALITY_DATASET_PATH)
    parser.add_argument("--predictions-jsonl", type=Path, help="Stored structured predictions required by offline-fixture mode")
    parser.add_argument("--allow-live-model", action="store_true", help="Explicit opt-in for provider calls in live-model mode")
    parser.add_argument("--rag-enabled", action="store_true", help="Use the configured retrieval integration in live-model mode")
    parser.add_argument("--output-root", type=Path, default=Path("evaluation/quality_reports"))
    args = parser.parse_args()
    cases = load_jsonl(args.dataset)
    if args.mode == "offline-fixture":
        if not args.predictions_jsonl:
            parser.error("offline-fixture mode requires --predictions-jsonl; it never initializes a provider")
        rows = evaluate_fixture_predictions(cases, load_prediction_records(args.predictions_jsonl))
        provider_identity = "stored_fixture_predictions"
    else:
        if not args.allow_live_model:
            parser.error("live-model mode requires --allow-live-model")
        from llm.provider_factory import get_configured_provider, configured_provider_name
        provider_name = configured_provider_name("review")
        provider = get_configured_provider("review")
        rows = run_live_evaluation(cases, provider=provider, provider_identity=provider_name,
                                   rag_enabled=args.rag_enabled)
        provider_identity = provider_name
    report = _make_report(cases, rows, mode=args.mode, provider_identity=provider_identity,
                          dataset_path=args.dataset)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir = args.output_root / f"{timestamp}-{report['dataset_id']}-{args.mode}"
    _write_report_bundle(output_dir, report)
    print(json.dumps({"output_dir": str(output_dir), "dataset_id": report["dataset_id"],
                      "metrics": report["metrics"], "failed_case_ids": report["failed_case_ids"],
                      "skipped_case_ids": report["skipped_case_ids"]}, indent=2))
    return 0 if not report["failed_case_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
