"""Offline metadata-only validation for a local Qwen base model and PEFT adapter."""

from __future__ import annotations

import argparse
import json
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from llm.qwen_peft import DEFAULT_ADAPTER_DIR, DEFAULT_BASE_MODEL_ID, inspect_qwen_artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-model-dir", required=True, type=Path,
                        help="Local Hugging Face snapshot directory; this command never downloads files.")
    parser.add_argument("--adapter-dir", type=Path, default=DEFAULT_ADAPTER_DIR)
    parser.add_argument("--base-model-id", default=DEFAULT_BASE_MODEL_ID)
    args = parser.parse_args()
    try:
        report = inspect_qwen_artifacts(
            args.base_model_dir,
            args.adapter_dir,
            expected_base_model_id=args.base_model_id,
        )
    except Exception as exc:
        print(json.dumps({"compatible": False, "error": str(exc)}, indent=2))
        return 1

    report["versions"] = {}
    for package in ("torch", "transformers", "peft", "accelerate", "safetensors"):
        try:
            report["versions"][package] = version(package)
        except PackageNotFoundError:
            report["versions"][package] = None
    report["weights_loaded"] = False
    report["network_access"] = False
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
