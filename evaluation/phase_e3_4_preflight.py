"""Offline Phase E3.4 environment/configuration preflight; makes no provider call."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from evaluation.runner import (
    BENCHMARK_PATH, BENCHMARK_VERSION, SCORING_VERSION, load_cases,
    prompt_configuration_digest, scoring_configuration_digest,
)
from evaluation.chroma_snapshot import (
    SnapshotVerificationError, directory_sha256, inspect_collection_readonly,
    validate_saved_snapshot,
)
from llm.groq_client import DEFAULT_MODEL, STRICT_STRUCTURED_OUTPUT_MODELS
from rag.config import PROJECT_ROOT
from rag.retrieve_v3 import MODEL_ID, RetrievalConfig, retrieval_backend_selection
from review_schema import REVIEW_SCHEMA_VERSION

RESULTS = PROJECT_ROOT / "evaluation" / "results"


def _directory_info(path: Path, expected_collection: str) -> dict[str, Any]:
    info: dict[str, Any] = {
        "path": str(path), "exists": path.is_dir(), "size_bytes": None,
        "expected_collection": expected_collection, "collection_available": None,
        "record_count": None, "distance_metric": None,
    }
    if not path.is_dir():
        return info
    info["size_bytes"] = sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    try:
        collection = inspect_collection_readonly(path, expected_collection)
        info.update({"collection_available": True, "record_count": collection["record_count"],
                     "distance_metric": collection["distance_metric"],
                     "collection_id": collection["collection_id"],
                     "dimension": collection["dimension"]})
    except Exception as exc:
        info.update({"collection_available": False, "open_error_type": type(exc).__name__})
    return info


def build_preflight(*, network_status: str = "not_probed_known_prior_dns_restriction",
                    writer_status: str = "unverified_process_listing_denied",
                    copy_root: Path | None = None,
                    resume_checkpoint: Path | None = None,
                    rag_version: str | None = None) -> dict[str, Any]:
    cases = load_cases()
    case_ids = [case["case_id"] for case in cases]
    benchmark_bytes = BENCHMARK_PATH.read_bytes()
    from evaluation.runner import _cases_digest
    selected_model = os.getenv("GROQ_MODEL", DEFAULT_MODEL)
    try:
        from huggingface_hub import snapshot_download
        from rag.v3.build import EXPECTED_DIMENSION, DISTANCE_METRIC, NORMALIZE_EMBEDDINGS
        snapshot = Path(snapshot_download(MODEL_ID, local_files_only=True)).resolve(strict=True)
        embedding = {"available_locally": True, "model_id": MODEL_ID,
                     "revision": snapshot.name, "dimension": EXPECTED_DIMENSION,
                     "distance_metric": DISTANCE_METRIC,
                     "normalize_embeddings": NORMALIZE_EMBEDDINGS}
    except Exception as exc:
        from rag.v3.build import EXPECTED_DIMENSION, DISTANCE_METRIC, NORMALIZE_EMBEDDINGS
        embedding = {"available_locally": False, "model_id": MODEL_ID,
                     "dimension": EXPECTED_DIMENSION, "distance_metric": DISTANCE_METRIC,
                     "normalize_embeddings": NORMALIZE_EMBEDDINGS,
                     "error_type": type(exc).__name__}
    dirs = {
        "canonical_v2": _directory_info(PROJECT_ROOT / "rag/data/chroma_db_v2", "critique_code_context_v2"),
        "canonical_v3": _directory_info(PROJECT_ROOT / "rag/data/chroma_db_v3", "critique_code_context_v3"),
    }
    copy_identities: dict[str, Any] = {"v2": None, "v3": None}
    copies_verified = False
    checked_versions: list[str] = []
    if copy_root is not None:
        checkpoint = None
        if resume_checkpoint is not None:
            checkpoint = json.loads(resume_checkpoint.read_text(encoding="utf-8"))
            if rag_version not in {"v2", "v3"}:
                raise ValueError("resume verification requires --rag-version v2 or v3")
        versions_to_check = ((rag_version, "critique_code_context_v2" if rag_version == "v2"
                              else "critique_code_context_v3"),) if rag_version else (
                                  ("v2", "critique_code_context_v2"),
                                  ("v3", "critique_code_context_v3"))
        for version, collection_name in versions_to_check:
            copied_path = copy_root / f"chroma_db_{version}"
            try:
                validation = validate_saved_snapshot(
                    copy_root=copy_root, version=version, copy_path=copied_path,
                    collection_name=collection_name,
                    expected_record_count=dirs[f"canonical_{version}"].get("record_count"),
                    expected_collection_id=dirs[f"canonical_{version}"].get("collection_id"),
                    checkpoint=checkpoint if version == rag_version else None,
                    allow_legacy_checkpoint=True,
                )
                copy_identities[version] = {"path": str(copied_path), **validation,
                                            "verified": validation["safe_to_resume"]}
            except (SnapshotVerificationError, OSError, json.JSONDecodeError) as exc:
                failed: dict[str, Any] = {"path": str(copied_path), "verified": False,
                                          "verification_error": str(exc)}
                try:
                    failed["observed_snapshot_directory_sha256"] = directory_sha256(copied_path)
                    failed["observed_collection"] = inspect_collection_readonly(copied_path, collection_name)
                except SnapshotVerificationError as inspect_exc:
                    failed["observed_collection_error"] = str(inspect_exc)
                copy_identities[version] = failed
        checked_versions = [version for version in ("v2", "v3") if copy_identities[version] is not None]
        copies_verified = bool(checked_versions) and all(
            copy_identities[version].get("verified") for version in checked_versions
        )
    previous = {}
    for key, filename in (("historical_no_rag", "phase_e2_30_no_rag.checkpoint.json"),
                          ("historical_v2", "phase_e2_30_rag.checkpoint.json")):
        checkpoint_path = RESULTS / filename
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {}
        previous[key] = {
            "checkpoint_exists": checkpoint_path.is_file(),
            "benchmark_sha256": checkpoint.get("config", {}).get("benchmark_sha256"),
            "case_count": len(checkpoint.get("config", {}).get("case_ids", [])),
            "model_id": checkpoint.get("config", {}).get("model_id"),
            "schema_version": checkpoint.get("config", {}).get("schema_version"),
        }
    result = {
        "experiment": "phase_e3_4",
        "preflight_status": ("ready_for_local_endpoint_check" if copies_verified
                              else "blocked_before_live_execution"),
        "live_requests_made": 0,
        "credential_check": {"GROQ_API_KEY_present": bool(os.getenv("GROQ_API_KEY")),
                              "credential_value_recorded": False},
        "network": {"status": network_status,
                    "basis": "Previous Groq sandbox attempts failed at DNS/connection; this preflight did not retry."},
        "copy_safety": {"writer_quiescence": (
                            "initial_copy_guard_recorded_in_manifest"
                            if copies_verified and all(
                                (copy_identities[v] or {}).get("status") == "manifest_verified"
                                for v in checked_versions)
                            else "legacy_copy_quiescence_provenance_unavailable"
                            if copies_verified else writer_status),
                        "reason": ("Temporary copies were validated against saved initial-copy manifests or, for a legacy checkpoint, its pinned snapshot hash."
                                   if copies_verified else
                                   "One or more temporary copies lack valid provenance or do not match their saved integrity identity."),
                        "copies_verified": copies_verified,
                        "temporary_copies_created": bool(copy_root is not None),
                        "temporary_copy_identities": copy_identities},
        "python": {"virtual_environment_present": (PROJECT_ROOT / ".venv/bin/python").is_file()},
        "provider": {
            "provider": os.getenv("AI_CODE_REVIEWER_PROVIDER", "groq"),
            "model": selected_model,
            "default_model": DEFAULT_MODEL,
            "strict_json_schema_supported_by_provider_code": selected_model in STRICT_STRUCTURED_OUTPUT_MODELS,
            "structured_output_mode": "json_schema", "strict": True,
            "temperature": 0.1, "sdk_max_retries": 2,
            "application_retry_layer": False,
            "request_interval_seconds": 12,
            "planned_configurations": 4,
            "planned_review_attempts": 120,
            "minimum_pacing_wait_seconds": 4 * 29 * 12,
            "account_quota_or_billing_status": "not_observable_without_live_account access",
        },
        "benchmark": {
            "path": str(BENCHMARK_PATH), "case_count": len(cases),
            "unique_case_count": len(set(case_ids)),
            "case_ids": case_ids,
            "category_counts": {name: sum(case["category"] == name for case in cases)
                                for name in ("security", "logic_runtime", "clean")},
            "file_sha256": hashlib.sha256(benchmark_bytes).hexdigest(),
            "evaluation_identity_sha256": _cases_digest(cases),
            "matches_historical_checkpoint_identity": all(
                previous[key]["benchmark_sha256"] == _cases_digest(cases)
                for key in ("historical_no_rag", "historical_v2")
            ),
            "version": BENCHMARK_VERSION,
            "scoring_version": SCORING_VERSION,
            "scoring_configuration_sha256": scoring_configuration_digest(),
            "review_schema_version": REVIEW_SCHEMA_VERSION,
            "prompt_configuration_sha256": prompt_configuration_digest(),
        },
        "retrieval": {
            "production_default": list(retrieval_backend_selection()),
            "configurations": {"v2_baseline": list(retrieval_backend_selection("v2", "v2_baseline")),
                               "v3_baseline": list(retrieval_backend_selection("v3", "v3_baseline")),
                               "v3_optimized": list(retrieval_backend_selection("v3", "v3_optimized"))},
            "v3_baseline_config": {
                "strategy": "v3_baseline", "parent_aware": False,
                "language_aware": False, "source_diversity": False,
                "applicability_diagnostics": False, "context_budgeting": False,
            },
            "v3_optimized_config": RetrievalConfig(strategy="v3_optimized").__dict__,
        },
        "embedding": embedding,
        "databases": dirs,
        "historical_checkpoints": previous,
        "execution_decision": ("local_endpoint_check_required_before_live_run" if copies_verified
                               else "not_started_no_dns_retry_and_no_consistent_temporary_database_copies"),
    }
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=RESULTS / "phase_e3_4_preflight.json")
    parser.add_argument("--copy-root", type=Path,
                        help="Temporary copy root; validates the saved manifest rather than current canonical bytes")
    parser.add_argument("--resume-checkpoint", type=Path,
                        help="Checkpoint to bind to the current temporary snapshot during resume")
    parser.add_argument("--rag-version", choices=("v2", "v3"),
                        help="RAG version to verify; associated with --resume-checkpoint when supplied")
    args = parser.parse_args()
    report = build_preflight(copy_root=args.copy_root, resume_checkpoint=args.resume_checkpoint,
                             rag_version=args.rag_version)
    if args.copy_root is not None and not report["copy_safety"]["copies_verified"]:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=args.output.parent,
                                         prefix=f".{args.output.name}.", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(json.dumps(report, indent=2, sort_keys=True) + "\n")
        temporary.replace(args.output)
        parser.exit(2, "Temporary Chroma copies failed identity validation; no live request was made.\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(args.output)
    print(json.dumps({"output": str(args.output), "preflight_status": report["preflight_status"],
                      "live_requests_made": 0,
                      "copy_verification": {
                          version: ({"status": value.get("status"),
                                     "safe_to_resume": value.get("safe_to_resume"),
                                     "provenance": value.get("provenance"),
                                     "snapshot_directory_sha256": value.get("snapshot_directory_sha256"),
                                     "verification_error": value.get("verification_error")}
                                    if value else None)
                          for version, value in report["copy_safety"]["temporary_copy_identities"].items()
                      }}, indent=2))


if __name__ == "__main__":
    main()
