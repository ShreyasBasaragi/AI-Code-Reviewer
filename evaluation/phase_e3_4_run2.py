"""Prepare, preflight, and verify isolated Phase E3.4 Run 2 snapshots."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from evaluation.chroma_snapshot import (
    SnapshotVerificationError, compare_working_copy_to_snapshot,
    directory_sha256, inspect_collection_readonly, load_snapshot_manifest,
    validate_saved_snapshot,
)
from evaluation.rate_limit import estimate_tokens
from evaluation.runner import load_cases

RUN_DIR = Path(__file__).with_name("results") / "phase_e3_4_run2"
SUPPORTED_ARMS = ("no_rag", "v2_baseline", "v3_baseline", "v3_optimized")
DEFAULT_ARMS = ("v3_baseline", "v3_optimized")
WORKING_NAMES = {"v2_baseline": ("v2", "critique_code_context_v2"),
                 "v3_baseline": ("v3", "critique_code_context_v3"),
                 "v3_optimized": ("v3", "critique_code_context_v3")}
SYSTEM_PROMPT = ("You are a precise software code reviewer. Use supplied retrieval context as evidence, "
                 "but make your own judgment from the code.")


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def select_arms(value: str | list[str] | tuple[str, ...] | None = None) -> tuple[str, ...]:
    """Validate and return selected arms in a stable canonical order."""
    if value is None:
        value = (os.environ["PHASE_E3_4_RUN2_ARMS"]
                 if "PHASE_E3_4_RUN2_ARMS" in os.environ
                 else ",".join(DEFAULT_ARMS))
    raw = value.split(",") if isinstance(value, str) else list(value)
    selected = [str(item).strip() for item in raw]
    if not selected or any(not arm for arm in selected):
        raise ValueError("arm selection must not be empty and cannot contain blank entries")
    unknown = sorted(set(selected) - set(SUPPORTED_ARMS))
    if unknown:
        raise ValueError("unknown Run 2 arm(s): " + ", ".join(unknown))
    if len(set(selected)) != len(selected):
        raise ValueError("duplicate Run 2 arm selection is not allowed")
    selected_set = set(selected)
    return tuple(arm for arm in SUPPORTED_ARMS if arm in selected_set)


def _required_versions(arms: tuple[str, ...]) -> tuple[str, ...]:
    versions = {WORKING_NAMES[arm][0] for arm in arms if arm in WORKING_NAMES}
    return tuple(version for version in ("v2", "v3") if version in versions)


def _atomic_json(path: Path, value: Any, *, overwrite: bool = True, readonly: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not overwrite:
        raise FileExistsError(f"refusing to overwrite existing Run 2 artifact: {path}")
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", suffix=".tmp", delete=False) as stream:
            temp_name = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        if readonly:
            os.chmod(temp_name, 0o444)
        if not overwrite and path.exists():
            raise FileExistsError(f"refusing to overwrite existing Run 2 artifact: {path}")
        os.replace(temp_name, path)
        temp_name = None
    finally:
        if temp_name and os.path.exists(temp_name):
            os.unlink(temp_name)


def _manifest_index(snapshot_root: Path, version: str) -> tuple[Path, dict[str, Any]]:
    return load_snapshot_manifest(snapshot_root, version)


def _snapshot_component_result(snapshot_root: Path, version: str) -> dict[str, Any]:
    path, manifest = _manifest_index(snapshot_root, version)
    snapshot_path = snapshot_root / f"chroma_db_{version}"
    validated = validate_saved_snapshot(
        copy_root=snapshot_root, version=version, copy_path=snapshot_path,
        collection_name=f"critique_code_context_{version}",
    )
    if validated["status"] != "manifest_verified":
        raise SnapshotVerificationError("Run 2 snapshot must have a verified initial-copy manifest")
    return {"version": version, "manifest_path": str(path),
            "manifest_sha256": manifest["manifest_sha256"],
            "verification_type": manifest.get("verification_type"),
            "source_path": manifest.get("source_path"),
            "source_directory_sha256": manifest.get("source_directory_sha256"),
            "snapshot_directory_sha256": manifest["snapshot_directory_sha256"],
            "quiescence_evidence": manifest.get("quiescence_evidence"),
            "verified_at_utc": manifest.get("created_at_utc"),
            "snapshot_path": str(snapshot_path.resolve()),
            "collection": manifest["collection"]}


def prepare_working_copies(*, snapshot_root: Path, working_root: Path,
                           run_dir: Path = RUN_DIR,
                           arms: tuple[str, ...] = DEFAULT_ARMS) -> dict[str, Any]:
    arms = select_arms(arms)
    if run_dir.exists() and any(run_dir.iterdir()):
        raise FileExistsError(f"Run 2 output directory already contains artifacts: {run_dir}")
    if working_root.exists() and any(working_root.iterdir()):
        raise FileExistsError(f"Run 2 working-copy directory is not empty: {working_root}")
    versions = _required_versions(arms)
    refs = {version: _snapshot_component_result(snapshot_root, version) for version in versions}
    working_root.mkdir(parents=True, exist_ok=True)
    working_entries = {}
    for arm in arms:
        if arm not in WORKING_NAMES:
            continue
        version, _collection_name = WORKING_NAMES[arm]
        source = snapshot_root / f"chroma_db_{version}"
        destination = working_root / arm
        shutil.copytree(source, destination, copy_function=shutil.copy2)
        destination.chmod(stat.S_IMODE(destination.stat().st_mode) | 0o700)
        for item in destination.rglob("*"):
            if item.is_dir():
                item.chmod(stat.S_IMODE(item.stat().st_mode) | 0o700)
            elif item.is_file():
                item.chmod(stat.S_IMODE(item.stat().st_mode) | 0o200)
        if directory_sha256(destination) != refs[version]["snapshot_directory_sha256"]:
            raise SnapshotVerificationError(f"initial working copy did not match immutable {version} snapshot")
        identity = inspect_collection_readonly(destination, f"critique_code_context_{version}")
        comparison = compare_working_copy_to_snapshot(refs[version]["collection"], identity)
        if not comparison["semantic_collection_unchanged"] or not comparison["vector_index_unchanged"]:
            raise SnapshotVerificationError(f"working-copy content differs from immutable {version} snapshot")
        working_entries[arm] = {
            "version": version, "path": str(destination.resolve()),
            "initial_directory_sha256": directory_sha256(destination),
            "collection": identity, "initial_component_comparison": comparison,
            "reference_manifest_sha256": refs[version]["manifest_sha256"],
        }
    manifest_body = {"run": "phase_e3_4_run2", "manifest_version": 1,
                     "selected_arms": list(arms),
                     "references": refs, "working_copies": working_entries}
    aggregate = {**manifest_body, "manifest_sha256": _digest(manifest_body)}
    run_dir.mkdir(parents=True, exist_ok=True)
    _atomic_json(run_dir / "snapshot_manifest.json", aggregate, overwrite=False, readonly=True)
    return aggregate


def _load_run_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "snapshot_manifest.json"
    if not path.is_file() or path.stat().st_mode & 0o222:
        raise SnapshotVerificationError("Run 2 immutable snapshot manifest is missing or writable")
    value = json.loads(path.read_text(encoding="utf-8"))
    body = {key: item for key, item in value.items() if key != "manifest_sha256"}
    if value.get("manifest_sha256") != _digest(body):
        raise SnapshotVerificationError("Run 2 snapshot manifest integrity check failed")
    return value


def verify_working_copy(*, run_dir: Path, working_root: Path, arm: str) -> dict[str, Any]:
    if arm not in WORKING_NAMES:
        raise ValueError(f"unknown RAG arm: {arm}")
    manifest = _load_run_manifest(run_dir)
    initial = manifest["working_copies"][arm]
    version, collection_name = WORKING_NAMES[arm]
    current_path = working_root / arm
    if current_path.resolve() != Path(initial["path"]).resolve():
        raise SnapshotVerificationError("working-copy path differs from the pinned Run 2 manifest")
    current = inspect_collection_readonly(current_path, collection_name)
    reference = manifest["references"][version]["collection"]
    comparison = compare_working_copy_to_snapshot(reference, current)
    return {"arm": arm, "working_directory_sha256": directory_sha256(current_path),
            "initial_working_directory_sha256": initial["initial_directory_sha256"],
            "collection": current, "component_comparison": comparison,
            "reference_snapshot_sha256": manifest["references"][version]["snapshot_directory_sha256"],
            "reference_manifest_sha256": manifest["references"][version]["manifest_sha256"],
            "safe_to_continue": comparison["semantic_collection_unchanged"]
            and comparison["vector_index_unchanged"]}


def _retrieval_context(evidence: list[Any]) -> list[str]:
    return [item.to_prompt_text() if hasattr(item, "to_prompt_text") else str(item)
            for item in evidence]


def _build_quota_projection(*, snapshot_root: Path, working_root: Path,
                             remaining_daily_tokens: int,
                             arms: tuple[str, ...] = DEFAULT_ARMS,
                             safety_fraction: float = 0.8) -> dict[str, Any]:
    from evaluation.runner import _copy_retriever
    from llm.groq_client import GroqLLM
    if not 0 < safety_fraction <= 1:
        raise ValueError("safety fraction must be greater than 0 and at most 1")

    cases = load_cases()
    configurations: dict[str, dict[str, Any]] = {}
    for arm in arms:
        if arm == "no_rag":
            configurations[arm] = {"retrieval": None,
                                   "contexts": {case["case_id"]: [] for case in cases}}
            continue
        version, _ = WORKING_NAMES[arm]
        if version == "v2":
            retriever = _copy_retriever(working_root / arm, version="v2", strategy="v2_baseline")
        else:
            from rag.retrieve_v3 import RetrievalConfig
            if arm == "v3_baseline":
                config = RetrievalConfig(strategy="v3_baseline", parent_aware=False,
                                         language_aware=False, source_diversity=False,
                                         applicability_diagnostics=False, context_budgeting=False)
            else:
                config = RetrievalConfig(strategy="v3_optimized")
            retriever = _copy_retriever(working_root / arm, version="v3",
                                        strategy=config.strategy, retrieval_config=config)
        contexts = {}
        for case in cases:
            contexts[case["case_id"]] = _retrieval_context(retriever(case["source_code"]))
        configurations[arm] = {"retrieval": arm, "contexts": contexts}

    estimates = {}
    total = 0
    maximum_request_tokens = 0
    for arm, config in configurations.items():
        arm_rows = []
        for case in cases:
            from llm.groq_client import GroqLLM
            text = GroqLLM._build_prompt(case["source_code"], config["contexts"][case["case_id"]])
            estimated = estimate_tokens(SYSTEM_PROMPT + "\n" + text, output_allowance=1024)
            item = {"case_id": case["case_id"], **estimated}
            arm_rows.append(item)
            total += estimated["total_tokens_reserved"]
            maximum_request_tokens = max(maximum_request_tokens, estimated["total_tokens_reserved"])
        estimates[arm] = {"cases": len(arm_rows),
                          "projected_tokens": sum(item["total_tokens_reserved"] for item in arm_rows),
                          "per_case": arm_rows}
    safety_budget = int(min(200000, remaining_daily_tokens) * safety_fraction)
    minute_token_budget = int(8000 * safety_fraction)
    planned_requests = len(arms) * len(cases)
    return {"method": "UTF-8 byte length / 2 for full request plus 1,024 token completion reserve; conservative estimate, not tokenizer-exact",
            "selected_arms": list(arms), "cases_per_arm": len(cases),
            "planned_requests": planned_requests, "projected_total_tokens": total,
            "maximum_projected_single_request_tokens": maximum_request_tokens,
            "conservative_minute_token_budget": minute_token_budget,
            "projected_requests_fit_minute_token_budget": maximum_request_tokens <= minute_token_budget,
            "safety_fraction": safety_fraction,
            "remaining_daily_tokens_reported_by_user_from_provider_dashboard": remaining_daily_tokens,
            "reserved_daily_budget_after_safety_margin": safety_budget,
            "projected_within_daily_budget": total <= safety_budget,
            "per_arm": estimates}


def _select_pending_batch(*, case_ids: list[str], arms: tuple[str, ...],
                          completed_by_arm: dict[str, set[str]],
                          estimates_by_arm: dict[str, dict[str, int]],
                          max_new_pairs: int, available_daily_tokens: int,
                          minute_token_budget: int) -> dict[str, Any]:
    """Choose a deterministic prefix of pending pairs that fits quota."""
    selected: list[dict[str, Any]] = []
    used_tokens = 0
    minute_fit = True
    pending_pair_count = 0
    for case_id in case_ids:
        missing = [arm for arm in arms if case_id not in completed_by_arm.get(arm, set())]
        if not missing:
            continue
        pending_pair_count += 1
        if len(selected) >= max_new_pairs:
            continue
        request_estimates = {arm: int(estimates_by_arm.get(arm, {}).get(case_id, 0)) for arm in missing}
        pair_tokens = sum(request_estimates.values())
        if any(tokens > minute_token_budget for tokens in request_estimates.values()):
            if not selected:
                minute_fit = False
            break
        if used_tokens + pair_tokens > available_daily_tokens:
            break
        selected.append({"case_id": case_id, "pending_arms": missing,
                         "estimated_tokens": pair_tokens, "request_estimates": request_estimates})
        used_tokens += pair_tokens
    selected_ids = [item["case_id"] for item in selected]
    per_arm = {arm: [item["case_id"] for item in selected if arm in item["pending_arms"]]
               for arm in arms}
    return {"selected_pairs": selected, "selected_case_ids": selected_ids,
            "pending_pair_count": pending_pair_count,
            "requests_by_arm": per_arm,
            "request_count": sum(map(len, per_arm.values())),
            "estimated_tokens": used_tokens,
            "per_request_minute_budget_fit": minute_fit}


def _plan_identity(*, arms: tuple[str, ...], run_manifest: dict[str, Any],
                   safety_fraction: float) -> dict[str, Any]:
    from evaluation.runner import _cases_digest, load_cases, prompt_configuration_digest, scoring_configuration_digest
    from review_schema import REVIEW_SCHEMA_VERSION

    cases = load_cases()
    versions = _required_versions(arms)
    return {
        "plan_version": 1,
        "selected_arms": list(arms),
        "benchmark_sha256": _cases_digest(cases),
        "case_ids": [case["case_id"] for case in cases],
        "model": "openai/gpt-oss-20b",
        "provider": "groq",
        "temperature": 0.1,
        "strict_schema_version": REVIEW_SCHEMA_VERSION,
        "quota_policy": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000,
                         "request_interval_seconds": 20,
                         "safety_fraction": safety_fraction},
        "prompt_configuration_sha256": prompt_configuration_digest(),
        "scoring_configuration_sha256": scoring_configuration_digest(),
        "snapshot_identities": {
            version: {"manifest_sha256": run_manifest["references"][version]["manifest_sha256"],
                      "snapshot_directory_sha256": run_manifest["references"][version]["snapshot_directory_sha256"]}
            for version in versions
        },
    }


def _pin_plan(run_dir: Path, plan: dict[str, Any]) -> dict[str, Any]:
    path = run_dir / "plan.json"
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved != plan:
            raise SnapshotVerificationError(
                "selected arms or experiment identity differ from the pinned Run 2 plan"
            )
        return saved
    _atomic_json(path, plan, overwrite=False, readonly=True)
    return plan


def run_preflight(*, snapshot_root: Path, working_root: Path, run_dir: Path,
                  remaining_daily_tokens: int, resume: bool = False,
                  arms: tuple[str, ...] = DEFAULT_ARMS,
                  safety_fraction: float = 0.8,
                  max_new_pairs: int = 15,
                  confirm_daily_quota_reset: bool = False) -> dict[str, Any]:
    arms = select_arms(arms)
    if not 0 < safety_fraction <= 1:
        raise ValueError("safety fraction must be greater than 0 and at most 1")
    if max_new_pairs < 1:
        raise ValueError("maximum new case pairs must be at least one")
    run_manifest = _load_run_manifest(run_dir)
    if tuple(run_manifest.get("selected_arms", ())) != arms:
        raise SnapshotVerificationError("selected arms differ from the prepared snapshot/working-copy set")
    versions = _required_versions(arms)
    refs = {version: _snapshot_component_result(snapshot_root, version) for version in versions}
    if any(refs[version]["manifest_sha256"] != run_manifest["references"][version]["manifest_sha256"]
           for version in versions):
        raise SnapshotVerificationError("snapshot references differ from the Run 2 manifest")
    selected_rag_arms = [arm for arm in arms if arm in WORKING_NAMES]
    working = {arm: verify_working_copy(run_dir=run_dir, working_root=working_root, arm=arm)
               for arm in selected_rag_arms}
    if not all(item["safe_to_continue"] for item in working.values()):
        raise SnapshotVerificationError("a working copy has record or vector-index changes")

    plan = _pin_plan(run_dir, _plan_identity(arms=arms, run_manifest=run_manifest,
                                             safety_fraction=safety_fraction))
    checkpoint_paths = {arm: run_dir / f"groq_{arm}.checkpoint.json" for arm in arms}
    previous_checkpoints = {arm: path.is_file() for arm, path in checkpoint_paths.items()}
    checkpoint_status: dict[str, dict[str, Any]] = {}
    state_path = run_dir / "rate_limit_usage.json"
    missing_ledger = bool(any(previous_checkpoints.values()) and not state_path.is_file())
    ledger_blocks = []
    if state_path.is_file():
        rate_state = json.loads(state_path.read_text(encoding="utf-8"))
        saved_policy = rate_state.get("policy", {})
        if saved_policy.get("safety_fraction") != safety_fraction:
            ledger_blocks.append("safety fraction differs from the saved quota ledger")
        daily_window = rate_state.get("daily_window", {})
        last_observed = int(daily_window.get(
            "last_remaining_tpd_observed", daily_window.get("starting_remaining_tpd", remaining_daily_tokens)
        ))
        if remaining_daily_tokens > last_observed and not confirm_daily_quota_reset:
            ledger_blocks.append("dashboard quota increased; explicit confirmed-reset mode is required")
    else:
        rate_state = None

    preflight_path = run_dir / "preflight.json"
    if resume and preflight_path.is_file():
        existing = json.loads(preflight_path.read_text(encoding="utf-8"))
        if existing.get("selected_arms") != list(arms):
            raise SnapshotVerificationError("saved preflight arm selection differs from the pinned plan")
        projection = existing.get("quota_projection")
        if not projection:
            raise SnapshotVerificationError("saved Run 2 preflight has no quota projection")
        projection["remaining_daily_tokens_reported_by_user_from_provider_dashboard"] = remaining_daily_tokens
        projection["safety_fraction"] = safety_fraction
        projection["reserved_daily_budget_after_safety_margin"] = int(
            min(200000, remaining_daily_tokens) * safety_fraction
        )
        projection["conservative_minute_token_budget"] = int(8000 * safety_fraction)
        projection["projected_requests_fit_minute_token_budget"] = (
            projection["maximum_projected_single_request_tokens"]
            <= projection["conservative_minute_token_budget"]
        )
    else:
        projection = _build_quota_projection(
            snapshot_root=snapshot_root, working_root=working_root,
            remaining_daily_tokens=remaining_daily_tokens, arms=arms,
            safety_fraction=safety_fraction,
        )
    total_case_ids = [case["case_id"] for case in load_cases()]
    pending_request_count = 0
    pending_token_count = 0
    for arm in arms:
        arm_projection = projection.get("per_arm", {}).get(arm, {})
        case_estimates = {item["case_id"]: item.get("total_tokens_reserved", 0)
                          for item in arm_projection.get("per_case", [])}
        checkpoint_path = checkpoint_paths[arm]
        checkpoint_results = {}
        if checkpoint_path.is_file():
            checkpoint_results = json.loads(checkpoint_path.read_text(encoding="utf-8")).get(
                "case_results", {}
            )
        pending_ids = [case_id for case_id in total_case_ids
                       if checkpoint_results.get(case_id, {}).get("status") != "completed"]
        arm_projection["pending_case_ids"] = pending_ids
        arm_projection["pending_case_count"] = len(pending_ids)
        arm_projection["pending_projected_tokens"] = sum(
            case_estimates.get(case_id, 0) for case_id in pending_ids
        )
        pending_request_count += len(pending_ids)
        pending_token_count += arm_projection["pending_projected_tokens"]
        checkpoint_status[arm] = {
            "exists": checkpoint_path.is_file(),
            "completed_case_count": len(total_case_ids) - len(pending_ids),
            "pending_case_count": len(pending_ids),
            "failed_case_ids": [case_id for case_id in total_case_ids
                                if checkpoint_results.get(case_id, {}).get("status") in {"failed", "interrupted"}],
            "quota_deferred_case_ids": [case_id for case_id in total_case_ids
                                         if checkpoint_results.get(case_id, {}).get("status") == "quota_deferred"],
        }
    full_request_count = len(arms) * len(total_case_ids)
    full_token_count = projection.get("initial_projected_total_tokens", projection.get("projected_total_tokens", 0))
    full_minute_fit = projection["projected_requests_fit_minute_token_budget"]
    available_token_budget = projection["reserved_daily_budget_after_safety_margin"]
    if state_path.is_file() and not confirm_daily_quota_reset:
        saved_rate_state = json.loads(state_path.read_text(encoding="utf-8"))
        saved_window = saved_rate_state.get("daily_window", {})
        if saved_window:
            local_remaining = max(
                0, int(saved_window.get("daily_token_budget", 0))
                - int(saved_window.get("tokens_accounted", 0))
            )
            available_token_budget = min(available_token_budget, local_remaining)
    projection["available_token_budget_after_safety_margin"] = available_token_budget
    estimates_by_arm = {
        arm: {item["case_id"]: int(item.get("total_tokens_reserved", 0))
              for item in projection.get("per_arm", {}).get(arm, {}).get("per_case", [])}
        for arm in arms
    }
    completed_by_arm = {arm: set(total_case_ids) - set(projection["per_arm"][arm]["pending_case_ids"])
                        for arm in arms}
    batch_selection = _select_pending_batch(
        case_ids=total_case_ids, arms=arms, completed_by_arm=completed_by_arm,
        estimates_by_arm=estimates_by_arm, max_new_pairs=max_new_pairs,
        available_daily_tokens=available_token_budget,
        minute_token_budget=projection["conservative_minute_token_budget"],
    )
    selected_pair_ids = batch_selection["selected_case_ids"]
    batch_requests_by_arm = batch_selection["requests_by_arm"]
    batch_request_count = batch_selection["request_count"]
    batch_tokens = batch_selection["estimated_tokens"]
    batch_minute_fit = batch_selection["per_request_minute_budget_fit"]
    full_experiment = {
        "total_cases": len(total_case_ids), "total_planned_requests": full_request_count,
        "selected_arms": list(arms), "projected_total_tokens": full_token_count,
        "projected_within_available_daily_budget": full_token_count <= available_token_budget,
        "projected_requests_fit_minute_token_budget": full_minute_fit,
        "completion_status": "completed" if pending_request_count == 0 else "pending_or_partial",
    }
    next_batch = {
        "maximum_new_case_pairs_requested": max_new_pairs,
        "selected_pending_case_ids": selected_pair_ids,
        "pending_pair_count": batch_selection["pending_pair_count"],
        "requests_already_completed": full_request_count - pending_request_count,
        "requests_remaining_before_batch": pending_request_count,
        "requests_planned_for_next_invocation": batch_request_count,
        "planned_case_ids_by_arm": batch_requests_by_arm,
        "estimated_batch_tokens": batch_tokens,
        "available_daily_tokens_after_safety_margin": available_token_budget,
        "permitted": bool(selected_pair_ids),
        "reason": ("ready" if selected_pair_ids else
                   "experiment already complete" if pending_request_count == 0 else
                   "no pending pairs fit the available token budget or per-request minute limit"),
        "per_request_minute_budget_fit": batch_minute_fit,
    }
    # Keep legacy top-level names as whole-experiment pending-work counts.
    projection["initial_planned_requests"] = full_request_count
    projection["initial_projected_total_tokens"] = full_token_count
    projection["planned_requests"] = pending_request_count
    projection["projected_total_tokens"] = pending_token_count
    projection["projected_within_daily_budget"] = pending_token_count <= available_token_budget
    projection["projected_requests_fit_minute_token_budget"] = full_minute_fit
    credentials_present = bool(os.getenv("GROQ_API_KEY"))
    blocked_reasons = []
    if not next_batch["permitted"] and pending_request_count:
        blocked_reasons.append(next_batch["reason"])
    if not credentials_present:
        blocked_reasons.append("GROQ_API_KEY is not configured")
    if missing_ledger:
        blocked_reasons.append("existing checkpoints have no quota ledger; refusing unsafe resume")
    blocked_reasons.extend(ledger_blocks)
    if any(previous_checkpoints.values()) and not resume:
        blocked_reasons.append("selected arm checkpoints exist; set explicit resume mode")
    result = {
        "experiment": "phase_e3_4_run2",
        "status": "completed" if pending_request_count == 0 and not blocked_reasons else
                  "ready" if not blocked_reasons else "blocked",
        "execution_permitted": not blocked_reasons and next_batch["permitted"],
        "blocked_reasons": blocked_reasons,
        "selected_arms": list(arms),
        "cases_per_arm": len(load_cases()),
        "total_planned_requests": projection["planned_requests"],
        "initial_total_planned_requests": projection.get(
            "initial_planned_requests", projection["planned_requests"]
        ),
        "total_pending_cases": projection["planned_requests"],
        "previous_checkpoints": previous_checkpoints,
        "checkpoint_status": checkpoint_status,
        "quota_ledger_exists": state_path.is_file(),
        "quota_ledger_status": ({
            "path": str(state_path), "integrity": "present_and_readable",
            "daily_window": rate_state.get("daily_window", {}),
            "policy": rate_state.get("policy", {}),
            "total_tokens_accounted": rate_state.get("total_tokens_accounted"),
            "total_requests_attempted": rate_state.get("total_requests_attempted"),
        } if rate_state is not None else {"path": str(state_path), "integrity": "not_initialized"}),
        "daily_quota_reset_confirmed": confirm_daily_quota_reset,
        "provider": {"provider": "groq", "model": "openai/gpt-oss-20b", "temperature": 0.1,
                     "strict_schema": "review-v1", "sdk_retries": 2,
                     "credentials_present": credentials_present,
                     "credential_value_recorded": False},
        "rate_limits": {"rpm": 30, "rpd": 1000, "tpm": 8000, "tpd": 200000,
                        "request_interval_seconds": 20, "max_wait_seconds": 3600,
                        "safety_fraction": safety_fraction,
                        "headers_observed_during_preflight": False},
        "benchmark": {"case_count": len(load_cases()),
                      "case_ids": [case["case_id"] for case in load_cases()]},
        "plan": plan,
        "snapshot_manifest_sha256": run_manifest["manifest_sha256"],
        "references": run_manifest["references"],
        "working_copies": working,
        "quota_projection": projection,
        "full_experiment": full_experiment,
        "next_batch": next_batch,
        "available_token_budget_after_safety_margin": available_token_budget,
        "network": {"status": "not_probed", "live_requests_made": 0},
        "notes": ["This preflight does not call Groq.",
                  "Preflight retrieval may change SQLite operational bytes only in selected disposable working copies.",
                  "The immutable reference snapshots are never opened by PersistentClient.",
                  "Daily remaining tokens come from the user's provider dashboard; outside account use after that check may change available quota."],
    }
    prior_sessions_path = run_dir / "batch_sessions.json"
    prior_sessions = json.loads(prior_sessions_path.read_text(encoding="utf-8")) if prior_sessions_path.is_file() else []
    session = {"planned_at_utc": datetime.now(timezone.utc).isoformat(),
               "maximum_new_case_pairs_requested": max_new_pairs,
               "selected_case_ids": selected_pair_ids,
               "planned_requests": batch_request_count,
               "estimated_tokens": batch_tokens,
               "status": "preflight_ready" if next_batch["permitted"] and not blocked_reasons else
                         "completed" if pending_request_count == 0 and not blocked_reasons else "blocked",
               "execution_started": False}
    result["batch_session"] = session
    prior_sessions.append(session)
    _atomic_json(prior_sessions_path, prior_sessions, overwrite=True)
    _atomic_json(preflight_path, result, overwrite=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare-working")
    prep.add_argument("--snapshot-root", type=Path, required=True)
    prep.add_argument("--working-root", type=Path, required=True)
    prep.add_argument("--run-dir", type=Path, default=RUN_DIR)
    prep.add_argument("--arms", help="Comma-separated Run 2 arms; defaults to the two v3 strategies")
    prep.set_defaults(handler=lambda a: prepare_working_copies(snapshot_root=a.snapshot_root,
                                                               working_root=a.working_root,
                                                               run_dir=a.run_dir,
                                                               arms=select_arms(a.arms)))
    check = sub.add_parser("preflight")
    check.add_argument("--snapshot-root", type=Path, required=True)
    check.add_argument("--working-root", type=Path, required=True)
    check.add_argument("--run-dir", type=Path, default=RUN_DIR)
    check.add_argument("--remaining-daily-tokens", type=int, required=True)
    check.add_argument("--arms", help="Comma-separated Run 2 arms; defaults to the two v3 strategies")
    check.add_argument("--safety-fraction", type=float, default=0.8)
    check.add_argument("--max-new-pairs", type=int, default=15)
    check.add_argument("--confirm-daily-quota-reset", action="store_true")
    check.add_argument("--resume", action="store_true")
    check.set_defaults(handler=lambda a: run_preflight(snapshot_root=a.snapshot_root,
                                                       working_root=a.working_root,
                                                       run_dir=a.run_dir,
                                                       remaining_daily_tokens=a.remaining_daily_tokens,
                                                       resume=a.resume,
                                                       arms=select_arms(a.arms),
                                                       safety_fraction=a.safety_fraction,
                                                       max_new_pairs=a.max_new_pairs,
                                                       confirm_daily_quota_reset=a.confirm_daily_quota_reset))
    verify = sub.add_parser("verify-working")
    verify.add_argument("--working-root", type=Path, required=True)
    verify.add_argument("--run-dir", type=Path, default=RUN_DIR)
    verify.add_argument("--arm", choices=tuple(WORKING_NAMES), required=True)
    verify.set_defaults(handler=lambda a: verify_working_copy(run_dir=a.run_dir,
                                                              working_root=a.working_root,
                                                              arm=a.arm))
    identities = sub.add_parser("identity")
    identities.add_argument("--run-dir", type=Path, default=RUN_DIR)
    identities.add_argument("--arm", choices=("v2_baseline", "v3_baseline", "v3_optimized"), required=True)
    identities.set_defaults(handler=lambda a: _identity_for_arm(a.run_dir, a.arm))
    select = sub.add_parser("select-arms")
    select.add_argument("--value")
    select.set_defaults(handler=lambda a: {"selected_arms": list(select_arms(a.value))})
    args = parser.parse_args()
    result = args.handler(args)
    if args.command == "verify-working" and not result.get("safe_to_continue"):
        parser.exit(2, "Working copy content/index integrity check failed.\n")
    if args.command == "select-arms":
        print(",".join(result["selected_arms"]))
    else:
        print(json.dumps(result, indent=2, sort_keys=True))


def _identity_for_arm(run_dir: Path, arm: str) -> dict[str, Any]:
    manifest = _load_run_manifest(run_dir)
    version, _ = WORKING_NAMES[arm]
    ref = manifest["references"][version]
    work = manifest["working_copies"][arm]
    return {"reference_snapshot_sha256": ref["snapshot_directory_sha256"],
            "reference_manifest_sha256": ref["manifest_sha256"],
            "working_copy_manifest_sha256": _digest(work),
            "working_copy_path": work["path"]}


if __name__ == "__main__":
    main()
