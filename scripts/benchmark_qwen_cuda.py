"""Explicit repeated CUDA benchmark for the local PEFT review provider.

Run only on the provisioned NVIDIA host. This loads weights and performs local
inference. It is never imported by the application or ordinary tests.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.qwen_client import QwenLLM
from mcp_server.orchestrator import run_review_pipeline

SAMPLE = '''def calculate_total(subtotal: float, tax_rate: float) -> float:
    """Return a subtotal with the supplied tax rate applied."""
    return subtotal * (1.0 + tax_rate)
'''


def _summary(values: list[float]) -> dict:
    ordered = sorted(values)
    if not ordered:
        return {"count": 0, "minimum": None, "median": None, "p95": None, "maximum": None}
    index = max(0, int(0.95 * len(ordered) + 0.999999) - 1)
    return {"count": len(ordered), "minimum": min(ordered),
            "median": statistics.median(ordered), "p95": ordered[index], "maximum": max(ordered)}


def _revision() -> dict:
    try:
        commit = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                               text=True, check=True, timeout=2).stdout.strip()
        dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=normal"],
                                    capture_output=True, text=True, check=True, timeout=2).stdout.strip())
        return {"commit": commit, "working_tree_dirty": dirty}
    except Exception:
        return {"commit": None, "working_tree_dirty": None}


def run_benchmark(*, warmups: int, runs: int) -> dict:
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA unavailable; this benchmark requires the deployment GPU.")
    device_index = 0
    device = torch.device("cuda", device_index)
    props = torch.cuda.get_device_properties(device_index)
    torch.cuda.synchronize(device)
    load_started = time.perf_counter()
    provider = QwenLLM()
    torch.cuda.synchronize(device)
    load_seconds = time.perf_counter() - load_started
    torch.cuda.reset_peak_memory_stats(device)
    direct, end_to_end = [], []
    for index in range(warmups + runs):
        torch.cuda.synchronize(device)
        started = time.perf_counter()
        provider.analyze_code(SAMPLE, context=[])
        torch.cuda.synchronize(device)
        direct_seconds = time.perf_counter() - started
        metadata = dict(provider.last_generation_metadata)
        torch.cuda.synchronize(device)
        started = time.perf_counter()
        result = run_review_pipeline(
            code=SAMPLE, file_path="cuda_benchmark.py", notify_desktop=False,
            llm=provider, retriever=lambda _code: [],
        )
        torch.cuda.synchronize(device)
        pipeline_seconds = time.perf_counter() - started
        if index >= warmups:
            direct.append({"seconds": direct_seconds, **metadata})
            end_to_end.append({"seconds": pipeline_seconds,
                               "schema_valid": result.review_response.valid,
                               "finding_count": len(result.review_response.findings)})
    if not all(row["schema_valid"] for row in end_to_end):
        status = "failed_schema_validation"
    else:
        status = "passed"
    direct_times = [row["seconds"] for row in direct]
    pipeline_times = [row["seconds"] for row in end_to_end]
    token_throughput = [row["generated_token_count"] / row["seconds"]
                        for row in direct if row.get("generated_token_count") and row["seconds"] > 0]
    return {
        "execution_timestamp": datetime.now(timezone.utc).isoformat(),
        "evaluation_mode": "cuda_local_model_benchmark", "status": status,
        "revision": _revision(),
        "environment": {"python": sys.version.split()[0], "platform": sys.platform,
                        "machine": os.uname().machine if hasattr(os, "uname") else None,
                        "torch": torch.__version__, "cuda_runtime": torch.version.cuda,
                        "transformers": importlib.metadata.version("transformers"),
                        "peft": importlib.metadata.version("peft")},
        "provider": "QwenPEFTProvider", "model_id": provider.config.base_model_id,
        "device": str(device), "gpu_model": props.name,
        "gpu_total_vram_gib": round(props.total_memory / 1024**3, 3),
        "precision": provider.config.precision, "adapter_attached": bool(getattr(provider.model, "peft_config", None)),
        "warmup_runs": warmups, "measured_runs": runs,
        "model_load_seconds": load_seconds,
        "direct_generation": {"latency_seconds": _summary(direct_times),
                              "prompt_token_count": _summary([row["prompt_token_count"] for row in direct]),
                              "generated_token_count": _summary([row["generated_token_count"] for row in direct]),
                              "tokens_per_second": _summary(token_throughput)},
        "end_to_end_review_latency_seconds": _summary(pipeline_times),
        "peak_allocated_vram_gib": round(torch.cuda.max_memory_allocated(device) / 1024**3, 3),
        "peak_reserved_vram_gib": round(torch.cuda.max_memory_reserved(device) / 1024**3, 3),
        "case_results": {"direct_generation": direct, "end_to_end": end_to_end},
        "limitations": "Measures one fixed synthetic review prompt with RAG disabled; results are target-machine inference performance, not model-quality or behavioral-correctness evidence.",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--output", type=Path, required=True, help="New report path; existing files are never overwritten")
    args = parser.parse_args()
    if args.warmups < 0 or args.runs < 1:
        parser.error("--warmups must be non-negative and --runs must be positive")
    if args.output.exists():
        parser.error("output path already exists")
    try:
        report = run_benchmark(warmups=args.warmups, runs=args.runs)
    except torch.cuda.OutOfMemoryError:
        report = {"execution_timestamp": datetime.now(timezone.utc).isoformat(),
                  "evaluation_mode": "cuda_local_model_benchmark", "status": "out_of_memory",
                  "error": "CUDA ran out of memory; no retry or fallback was attempted."}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: report.get(key) for key in ("status", "gpu_model", "model_load_seconds",
                                                        "direct_generation", "end_to_end_review_latency_seconds",
                                                        "peak_allocated_vram_gib", "peak_reserved_vram_gib")}, indent=2))
    return 0 if report.get("status") == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
