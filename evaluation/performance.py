"""Reproducible mocked-provider end-to-end performance benchmark."""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fix_generation import parse_fix_response
from fix_verification import verify_fix_candidate
from mcp_server.http_api import ReviewFixService
from mcp_server.orchestrator import run_review_pipeline

_REVIEW = json.dumps({"summary": "Deterministic offline fixture.", "findings": []})
_SAMPLES = [
    "def add(left, right):\n    return left + right\n",
    "import pickle\nvalue = pickle.loads(payload)\n",
    "import subprocess\nsubprocess.run(command, shell=True)\n",
]


class _MockProvider:
    model = "mocked-provider-not-a-model"
    temperature = 0

    def analyze_code(self, code: str, context: list[str]) -> str:
        return _REVIEW


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    return sorted(values)[max(0, math.ceil(q * len(values)) - 1)]


def _stats(values: list[float]) -> dict[str, Any]:
    return {"count": len(values), "minimum": min(values) if values else None,
            "median": statistics.median(values) if values else None,
            "p95": _percentile(values, 0.95), "maximum": max(values) if values else None}


def _revision() -> dict[str, Any]:
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                                text=True, check=True, timeout=2)
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                    capture_output=True, text=True, check=True, timeout=2).stdout.strip())
        return {"commit": result.stdout.strip(), "working_tree_dirty": dirty}
    except Exception:
        return {"commit": None, "working_tree_dirty": None}


def run_offline_benchmark(*, runs: int = 10, warmups: int = 2, provider=None) -> dict[str, Any]:
    if runs < 1 or warmups < 0:
        raise ValueError("runs must be positive and warmups cannot be negative")
    provider = provider or _MockProvider()
    rows = []
    for index in range(warmups + runs):
        code = _SAMPLES[index % len(_SAMPLES)]
        request_started = time.perf_counter()
        try:
            ReviewFixService._validate_code(code)
            request_validation = time.perf_counter() - request_started
            timings = {"request_validation_seconds": request_validation}
            review_started = time.perf_counter()
            result = run_review_pipeline(
                code=code, file_path=f"benchmark_{index}.py", notify_desktop=False,
                llm=provider, retriever=lambda _code: [], phase_timings=timings,
            )
            timings["total_review_seconds"] = time.perf_counter() - review_started
            findings = result.review_response.findings
            successful = result.review_response.valid
            error = None if successful else "invalid_review_response"
        except Exception as exc:
            timings = {"request_validation_seconds": time.perf_counter() - request_started}
            findings = ()
            successful = False
            error = type(exc).__name__
        if index >= warmups:
            rows.append({"case_index": index - warmups, "input_bytes": len(code.encode("utf-8")),
                         "finding_count": len(findings), "success": successful, "error": error,
                         "timings": timings})

    candidate = parse_fix_response(
        json.dumps({"finding_ref": "performance-fixture", "corrected_code": _SAMPLES[0],
                    "explanation": "Deterministic verification fixture.", "assumptions": [], "evidence_ids": []}),
        finding_ref="performance-fixture", original_code=_SAMPLES[0], allowed_evidence_ids=[],
    )
    verification_times = []
    for _ in range(runs):
        started = time.perf_counter()
        verify_fix_candidate(candidate)
        verification_times.append(time.perf_counter() - started)

    timing_names = sorted({key for row in rows for key in row["timings"]})
    timing_summary = {name: _stats([row["timings"][name] for row in rows if name in row["timings"]])
                      for name in timing_names}
    timing_summary["fix_verification_seconds"] = _stats(verification_times)
    return {
        "execution_timestamp": datetime.now(timezone.utc).isoformat(), "revision": _revision(),
        "evaluation_mode": "offline_mocked_provider", "provider_identity": getattr(provider, "model", type(provider).__name__),
        "dataset_id": "offline-performance-samples-v1", "dataset_size": len(_SAMPLES),
        "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                        "machine": os.uname().machine if hasattr(os, "uname") else None},
        "warmup_runs": warmups, "measured_runs": runs,
        "successful_requests": sum(row["success"] for row in rows),
        "failed_requests": sum(not row["success"] for row in rows),
        "input_bytes": {"minimum": min(row["input_bytes"] for row in rows),
                        "maximum": max(row["input_bytes"] for row in rows)},
        "finding_count": {"minimum": min(row["finding_count"] for row in rows),
                           "maximum": max(row["finding_count"] for row in rows),
                           "total": sum(row["finding_count"] for row in rows)},
        "timings": timing_summary,
        "case_results": rows,
        "limitations": "Provider latency is mocked and does not represent model inference, network, or production traffic. No candidate source is executed and no live provider or RAG database is used.",
    }


def write_report_bundle(output_dir: Path, report: dict[str, Any]) -> None:
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "case_results.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=("case_index", "input_bytes", "finding_count", "success", "error", "total_review_seconds"))
        writer.writeheader()
        for row in report["case_results"]:
            writer.writerow({**{key: row.get(key) for key in ("case_index", "input_bytes", "finding_count", "success", "error")},
                             "total_review_seconds": row["timings"].get("total_review_seconds")})
    markdown = ["# Offline performance benchmark", "",
                f"- Timestamp: {report['execution_timestamp']}", f"- Mode: {report['evaluation_mode']}",
                f"- Provider: {report['provider_identity']}",
                f"- Successful / failed: {report['successful_requests']} / {report['failed_requests']}", "",
                "| Stage | Count | Median (s) | p95 (s) | Min (s) | Max (s) |", "|---|---:|---:|---:|---:|---:|"]
    for name, data in report["timings"].items():
        markdown.append(f"| {name} | {data['count']} | {data['median']} | {data['p95']} | {data['minimum']} | {data['maximum']} |")
    markdown.extend(["", report["limitations"], ""])
    (output_dir / "summary.md").write_text("\n".join(markdown), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output-root", type=Path, default=Path("evaluation/performance_runs"))
    args = parser.parse_args()
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    output_dir = args.output_root / f"{timestamp}-offline-mocked"
    report = run_offline_benchmark(runs=args.runs, warmups=args.warmups)
    write_report_bundle(output_dir, report)
    print(json.dumps({"output_dir": str(output_dir), "successful_requests": report["successful_requests"],
                      "failed_requests": report["failed_requests"], "timings": report["timings"]}, indent=2))
    return 0 if report["failed_requests"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
