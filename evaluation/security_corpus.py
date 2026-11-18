"""Run the manually labeled, synthetic regression corpus for PYSEC rules."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from static_analysis import analyze_python

CORPUS_PATH = Path(__file__).with_name("security_regressions_v1.jsonl")


def load_corpus(path: Path = CORPUS_PATH) -> list[dict]:
    cases = []
    seen = set()
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            item = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON on corpus line {line_number}") from exc
        if not isinstance(item, dict) or not item.get("case_id") or item["case_id"] in seen:
            raise ValueError(f"missing or duplicate case_id on corpus line {line_number}")
        if item.get("provenance") != "synthetic_manual" or not isinstance(item.get("source_code"), str):
            raise ValueError(f"invalid synthetic corpus metadata on line {line_number}")
        labels = item.get("expected_findings")
        if not isinstance(labels, list) or any(
            not isinstance(label, dict)
            or not all(key in label for key in ("finding_key", "rule_id", "line_start", "severity"))
            for label in labels
        ):
            raise ValueError(f"invalid expected finding labels on corpus line {line_number}")
        seen.add(item["case_id"])
        cases.append(item)
    return cases


def run_corpus(cases: list[dict] | None = None) -> dict:
    cases = load_corpus() if cases is None else cases
    reports = []
    tp = fp = fn = 0
    for case in cases:
        result = analyze_python(case["source_code"], file_path=f"{case['case_id']}.py")
        expected = case["expected_findings"]
        unmatched = set(range(len(result.findings)))
        matched = []
        for label in expected:
            index = next((idx for idx in sorted(unmatched)
                          if result.findings[idx].rule_id == label["rule_id"]
                          and result.findings[idx].line_start == label["line_start"]
                          and result.findings[idx].severity == label["severity"]), None)
            if index is not None:
                matched.append({"finding_key": label["finding_key"], "prediction_index": index})
                unmatched.remove(index)
        case_tp = len(matched)
        case_fp = len(result.findings) - case_tp
        case_fn = len(expected) - case_tp
        tp += case_tp
        fp += case_fp
        fn += case_fn
        reports.append({
            "case_id": case["case_id"], "status": result.status,
            "expected_count": len(expected), "predicted_count": len(result.findings),
            "true_positives": case_tp, "false_positives": case_fp, "false_negatives": case_fn,
            "matched": matched, "predictions": [item.to_dict() for item in result.findings],
            "parse_error": result.error_type,
        })
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    f1 = 2 * precision * recall / (precision + recall) if precision is not None and recall is not None and precision + recall else 0.0 if precision is not None and recall is not None else None
    return {
        "dataset_id": "synthetic-static-security-regressions-v1",
        "evaluation_mode": "offline_static_analyzer",
        "provenance": "synthetic_manual; not real-world analyzer accuracy",
        "run_timestamp": datetime.now(timezone.utc).isoformat(),
        "case_count": len(cases), "true_positives": tp, "false_positives": fp,
        "false_negatives": fn, "precision": precision, "recall": recall, "f1": f1,
        "case_results": reports,
        "unsupported_patterns": [
            {"case_id": case["case_id"], "patterns": case["unsupported_patterns"]}
            for case in cases if case.get("unsupported_patterns")
        ],
        "limitations": "Results measure only the declared PYSEC rules on a small synthetic corpus; they are not real-world detection or model-quality claims.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New JSON output path; existing files are never overwritten")
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output path already exists")
    report = run_corpus()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("dataset_id", "case_count", "true_positives", "false_positives", "false_negatives", "precision", "recall", "f1")}, indent=2))
    return 0 if report["false_positives"] == report["false_negatives"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
