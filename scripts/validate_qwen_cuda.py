"""Explicit target-machine smoke validation for the local CUDA Qwen adapter.

This command loads model weights and runs two local generations. Run it only on
the provisioned CUDA deployment machine, never as part of Mac test workflows.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import torch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.qwen_client import QwenLLM
from mcp_server.orchestrator import run_review_pipeline
from review_schema import parse_review_response


SAMPLE = '''def calculate_total(subtotal: float, tax_rate: float) -> float:
    """Return a subtotal with the supplied tax rate applied."""
    return subtotal * (1.0 + tax_rate)
'''


def main() -> int:
    if not torch.cuda.is_available():
        print(json.dumps({"status": "failed", "error": "CUDA is not available."}, indent=2))
        return 2
    device_index = 0
    device = torch.device("cuda", device_index)
    properties = torch.cuda.get_device_properties(device_index)
    torch.cuda.reset_peak_memory_stats(device)

    load_started = time.perf_counter()
    provider = QwenLLM()
    load_seconds = time.perf_counter() - load_started
    adapter_configs = getattr(provider.model, "peft_config", None)
    adapter_attached = bool(adapter_configs)
    if not adapter_attached:
        print(json.dumps({"status": "failed", "error": "PEFT adapter is not attached."}, indent=2))
        return 3

    inference_started = time.perf_counter()
    raw = provider.analyze_code(SAMPLE, context=[])
    inference_seconds = time.perf_counter() - inference_started
    standalone = parse_review_response(raw, evidence=[])

    pipeline_started = time.perf_counter()
    result = run_review_pipeline(
        code=SAMPLE,
        file_path="cuda_validation.py",
        notify_desktop=False,
        llm=provider,
    )
    pipeline_seconds = time.perf_counter() - pipeline_started
    peak_allocated = torch.cuda.max_memory_allocated(device) / (1024 ** 3)
    peak_reserved = torch.cuda.max_memory_reserved(device) / (1024 ** 3)
    report = {
        "status": "passed" if standalone.valid and result.review_response.valid else "failed",
        "provider": "QwenPEFTProvider",
        "model_id": provider.config.base_model_id,
        "device": str(device),
        "gpu_name": properties.name,
        "gpu_total_memory_gib": round(properties.total_memory / (1024 ** 3), 3),
        "precision": provider.config.precision,
        "adapter_attached": adapter_attached,
        "standalone_schema_valid": standalone.valid,
        "orchestrator_schema_valid": result.review_response.valid,
        "static_analysis_status": result.review_response.static_analysis_status,
        "finding_count": len(result.review_response.findings),
        "evidence_count": len(result.review_response.evidence),
        "model_load_seconds": round(load_seconds, 3),
        "standalone_inference_seconds": round(inference_seconds, 3),
        "orchestrator_review_seconds": round(pipeline_seconds, 3),
        "peak_allocated_memory_gib": round(peak_allocated, 3),
        "peak_reserved_memory_gib": round(peak_reserved, 3),
    }
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report["status"] == "passed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
