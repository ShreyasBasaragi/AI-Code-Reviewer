"""Offline calibration evaluation; it never manufactures runtime confidence."""

from __future__ import annotations

import math
import hashlib
import hmac
import json
import os
import stat
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


CALIBRATION_VERSION = "review-confidence-calibration-v1"
MIN_CALIBRATION_EXAMPLES = 200
MAX_EXPECTED_CALIBRATION_ERROR = 0.05
MODEL_ESTIMATE_CONFIDENCE_POLICY = "academic_prototype"
_MODEL_ESTIMATE_CATEGORIES = frozenset({"style", "naming", "formatting"})


def valid_model_confidence_estimate(value: Any) -> Optional[float]:
    """Validate a model's subjective score without treating it as calibration."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        score = float(value)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(score) or not 0 <= score <= 1:
        return None
    return score


def model_estimate_prototype_enabled(*, repository: str, branch: str,
                                     permitted_branches: Iterable[str]) -> bool:
    """Require owner opt-in and an exact, allowlisted disposable test branch."""
    configured_repository = os.getenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY", "").strip()
    configured_branch = os.getenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH", "").strip()
    return (
        os.getenv("GITHUB_MODEL_CONFIDENCE_POLICY", "").strip().casefold()
        == MODEL_ESTIMATE_CONFIDENCE_POLICY
        and bool(configured_repository)
        and configured_repository.casefold() == repository.casefold()
        and bool(configured_branch)
        and configured_branch.startswith("test/")
        and branch == configured_branch
        and branch in permitted_branches
    )


def model_estimate_confidence_evidence(
    finding: Mapping[str, Any], *, model_id: Optional[str], repository: str,
    branch: str, permitted_branches: Iterable[str],
) -> Optional[dict[str, Any]]:
    """Build explicitly uncalibrated evidence only for the opt-in test scope."""
    category = finding.get("issue_type")
    if (not isinstance(category, str) or category not in _MODEL_ESTIMATE_CATEGORIES
            or not isinstance(model_id, str)
            or not model_id.strip()
            or not model_estimate_prototype_enabled(
                repository=repository, branch=branch, permitted_branches=permitted_branches
            )):
        return None
    score = valid_model_confidence_estimate(finding.get("model_confidence_estimate"))
    if score is None or score < 0.80:
        return None
    return {
        "confidence_source": "uncalibrated_model_estimate",
        "calibrated": False,
        "manifest_verified": False,
        "model_id": model_id,
        "finding_category": category,
        "model_confidence_estimate": score,
        "prototype_policy": MODEL_ESTIMATE_CONFIDENCE_POLICY,
        "prototype_repository": repository,
        "prototype_branch": branch,
    }


def verify_confidence_evidence(evidence: Any, *, repository: str, branch: str,
                               permitted_branches: Iterable[str], threshold: Any) -> bool:
    """Revalidate calibrated or explicitly scoped prototype evidence at write time."""
    if not isinstance(evidence, Mapping):
        return False
    if evidence.get("confidence_source") in (None, "independent_calibration"):
        score = evidence.get("calibrated_confidence")
        return (
            evidence.get("calibrated") is True
            and evidence.get("manifest_verified") is True
            and isinstance(evidence.get("model_id"), str)
            and isinstance(evidence.get("dataset_id"), str)
            and isinstance(evidence.get("finding_category"), str)
            and evidence.get("finding_category") in _MODEL_ESTIMATE_CATEGORIES
            and isinstance(evidence.get("artifact_sha256"), str)
            and valid_model_confidence_estimate(score) is not None
            and valid_model_confidence_estimate(threshold) is not None
            and float(score) >= max(0.80, float(threshold))
        )
    if evidence.get("confidence_source") != "uncalibrated_model_estimate":
        return False
    score = valid_model_confidence_estimate(evidence.get("model_confidence_estimate"))
    evidence_repository = evidence.get("prototype_repository")
    evidence_branch = evidence.get("prototype_branch")
    return (
        evidence.get("calibrated") is False
        and evidence.get("manifest_verified") is False
        and evidence.get("prototype_policy") == MODEL_ESTIMATE_CONFIDENCE_POLICY
        and isinstance(evidence_repository, str)
        and evidence_repository.casefold() == repository.casefold()
        and isinstance(evidence_branch, str) and evidence_branch == branch
        and isinstance(evidence.get("finding_category"), str)
        and evidence.get("finding_category") in _MODEL_ESTIMATE_CATEGORIES
        and isinstance(evidence.get("model_id"), str) and bool(evidence.get("model_id").strip())
        and score is not None
        and valid_model_confidence_estimate(threshold) is not None
        and score >= max(0.80, float(threshold))
        and model_estimate_prototype_enabled(
            repository=repository, branch=branch, permitted_branches=permitted_branches
        )
    )


@dataclass(frozen=True)
class LabeledConfidence:
    """A reviewed, independently labeled prediction from a pinned model build."""

    model_id: str
    dataset_id: str
    confidence: float
    correct: bool
    label_source: str
    split: str = "holdout"
    prediction_type: str = "finding"
    prediction_id: str = ""


@dataclass(frozen=True)
class CalibrationReport:
    status: str
    model_id: Optional[str]
    dataset_id: Optional[str]
    sample_count: int
    brier_score: Optional[float]
    expected_calibration_error: Optional[float]
    confidence_threshold: float
    calibrated: bool
    manifest_verified: bool = False
    limitations: tuple[str, ...] = ()
    calibration_count: int = 0
    holdout_count: int = 0
    metrics_by_prediction_type: Optional[dict[str, dict[str, float | int]]] = None
    examples_sha256: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def evaluate_calibration(
    examples: Iterable[LabeledConfidence], *, model_id: Optional[str] = None,
    dataset_id: Optional[str] = None, bins: int = 10,
    minimum_examples: int = MIN_CALIBRATION_EXAMPLES,
    maximum_ece: float = MAX_EXPECTED_CALIBRATION_ERROR,
    signed_manifest: Optional[Mapping[str, Any]] = None,
) -> CalibrationReport:
    """Evaluate supplied offline labels; test fixtures alone do not mint a token."""
    rows = list(examples)
    if not 2 <= bins <= 100 or minimum_examples < 2 or not 0 < maximum_ece < 1:
        raise ValueError("invalid_calibration_evaluation_parameters")
    reasons: list[str] = []
    normalized = []
    identities = set()
    identifiers: set[str] = set()
    for row in rows:
        if not isinstance(row, LabeledConfidence):
            reasons.append("untrusted_example_type")
            continue
        if (not isinstance(row.model_id, str) or not row.model_id.strip()
                or not isinstance(row.dataset_id, str) or not row.dataset_id.strip()
                or not isinstance(row.label_source, str) or not row.label_source.strip()
                or row.label_source != "independent_human_adjudication"
                or isinstance(row.confidence, bool) or not isinstance(row.confidence, (float, int))
                or not math.isfinite(float(row.confidence)) or not 0 <= row.confidence <= 1
                or not isinstance(row.correct, bool)):
            reasons.append("invalid_or_untrusted_labeled_example")
            continue
        if row.split not in {"calibration", "holdout"}:
            reasons.append("explicit_calibration_or_holdout_split_required")
            continue
        if row.prediction_type not in {"finding", "fix_safety"}:
            reasons.append("unsupported_prediction_type")
            continue
        if not isinstance(row.prediction_id, str) or not row.prediction_id.strip():
            reasons.append("independent_prediction_id_required")
        elif row.prediction_id in identifiers:
            reasons.append("duplicate_independent_prediction_id")
            continue
        else:
            identifiers.add(row.prediction_id)
        identities.add((row.model_id, row.dataset_id))
        normalized.append((float(row.confidence), 1.0 if row.correct else 0.0,
                           row.split, row.prediction_type))
    if len(identities) > 1:
        reasons.append("mixed_model_or_dataset_identity")
    actual_model = next(iter(identities))[0] if len(identities) == 1 else model_id
    actual_dataset = next(iter(identities))[1] if len(identities) == 1 else dataset_id
    if model_id and actual_model and model_id != actual_model:
        reasons.append("model_identity_mismatch")
    if dataset_id and actual_dataset and dataset_id != actual_dataset:
        reasons.append("dataset_identity_mismatch")
    n = len(normalized)
    calibration_rows = [row for row in normalized if row[2] == "calibration"]
    holdout_rows = [row for row in normalized if row[2] == "holdout"]
    if len(calibration_rows) < minimum_examples:
        reasons.append("insufficient_calibration_examples")
    if len(holdout_rows) < minimum_examples:
        reasons.append("insufficient_holdout_examples")
    if {row[3] for row in calibration_rows} != {"finding", "fix_safety"}:
        reasons.append("calibration_labels_must_cover_findings_and_fix_safety")
    if {row[3] for row in holdout_rows} != {"finding", "fix_safety"}:
        reasons.append("holdout_labels_must_cover_findings_and_fix_safety")
    example_payload = [
        {"model_id": item.model_id, "dataset_id": item.dataset_id,
         "confidence": float(item.confidence), "correct": item.correct,
         "label_source": item.label_source, "split": item.split,
         "prediction_type": item.prediction_type, "prediction_id": item.prediction_id}
        for item in rows if isinstance(item, LabeledConfidence)
    ]
    examples_sha256 = hashlib.sha256(json.dumps(example_payload, sort_keys=True,
                                                 separators=(",", ":")).encode()).hexdigest()
    manifest_verified = _verify_manifest(
        signed_manifest, rows, actual_model, actual_dataset, n, examples_sha256
    )
    if not manifest_verified:
        reasons.append("independent_dataset_manifest_not_verified")
    if n == 0:
        return CalibrationReport("insufficient_evidence", actual_model, actual_dataset, 0,
                                 None, None, 0.80, False, False, tuple(dict.fromkeys(reasons)),
                                 len(calibration_rows), len(holdout_rows), {}, examples_sha256)
    # Report calibration quality only on the held-out portion. Calibration-set
    # results never count as evidence of generalization.
    brier = sum((confidence - outcome) ** 2 for confidence, outcome, _, _ in holdout_rows) / len(holdout_rows) if holdout_rows else None
    ece = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [(p, y) for p, y, _split, _kind in holdout_rows
                   if low <= p < high or (index == bins - 1 and p == 1.0)]
        if members:
            ece += len(members) / len(holdout_rows) * abs(
                sum(p for p, _ in members) / len(members)
                - sum(y for _, y in members) / len(members)
            )
    if ece > maximum_ece:
        reasons.append("calibration_error_above_limit")
    metrics: dict[str, dict[str, float | int]] = {}
    for kind in ("finding", "fix_safety"):
        kind_rows = [row for row in holdout_rows if row[3] == kind]
        high = [row for row in kind_rows if row[0] >= 0.80]
        metrics[kind] = {
            "holdout_count": len(kind_rows),
            "brier_score": (sum((p - y) ** 2 for p, y, _, _ in kind_rows) / len(kind_rows)
                            if kind_rows else 0.0),
            "at_or_above_threshold_count": len(high),
            "at_or_above_threshold_accuracy": (sum(y for _, y, _, _ in high) / len(high)
                                                if high else 0.0),
        }
    calibrated = (manifest_verified and len(calibration_rows) >= minimum_examples
                  and len(holdout_rows) >= minimum_examples and ece <= maximum_ece and not reasons)
    return CalibrationReport(
        "calibrated" if calibrated else "not_calibrated", actual_model, actual_dataset,
        n, brier, ece, 0.80, calibrated, manifest_verified,
        tuple(dict.fromkeys(reasons)), len(calibration_rows), len(holdout_rows),
        metrics, examples_sha256,
    )


def _verify_manifest(manifest: Optional[Mapping[str, Any]], rows: list[LabeledConfidence], model_id: Optional[str],
                     dataset_id: Optional[str], sample_count: int, examples_sha256: str) -> bool:
    """Verify an operator-signed manifest for reviewed labels and exact examples."""
    key = os.getenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", "").encode("utf-8")
    if not isinstance(manifest, Mapping) or len(key) < 32:
        return False
    required = {"version", "model_id", "dataset_id", "sample_count", "examples_sha256",
                "label_source", "reviewed_by", "signature"}
    if set(manifest) != required:
        return False
    unsigned = {name: manifest[name] for name in required if name != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode()
    expected_signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if not isinstance(manifest.get("signature"), str) or not hmac.compare_digest(
            manifest["signature"], expected_signature):
        return False
    return (
        manifest.get("version") == CALIBRATION_VERSION
        and manifest.get("model_id") == model_id
        and manifest.get("dataset_id") == dataset_id
        and manifest.get("sample_count") == sample_count
        and manifest.get("examples_sha256") == examples_sha256
        and manifest.get("label_source") == "independent_human_adjudication"
        and isinstance(manifest.get("reviewed_by"), str)
        and len(manifest["reviewed_by"].strip()) >= 3
    )


def persist_calibration_report(report: CalibrationReport, path: Path | str) -> Path:
    """Persist signed metrics without granting runtime eligibility to test data."""
    if not isinstance(report, CalibrationReport):
        raise TypeError("calibration_report_invalid")
    key = os.getenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", "").encode("utf-8")
    if len(key) < 32:
        raise ValueError("calibration_signing_key_unavailable")
    target = Path(path).expanduser()
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = target.parent.lstat()
    if parent.st_uid != os.getuid() or not stat.S_ISDIR(parent.st_mode):
        raise ValueError("calibration_directory_owner_invalid")
    if parent.st_mode & 0o077:
        raise ValueError("calibration_directory_permissions_invalid")
    body = {"artifact_version": CALIBRATION_VERSION, "report": asdict(report)}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    artifact = {**body, "signature": hmac.new(key, canonical, hashlib.sha256).hexdigest()}
    descriptor, temporary = tempfile.mkstemp(prefix=".confidence-calibration-", dir=target.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(artifact, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return target


def load_calibration_report(path: Path | str) -> Optional[CalibrationReport]:
    """Load only an owner-only, correctly signed calibration metrics artifact."""
    target = Path(path).expanduser()
    key = os.getenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", "").encode("utf-8")
    if len(key) < 32:
        return None
    try:
        info = target.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 256 * 1024):
            return None
        artifact = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(artifact, dict) or set(artifact) != {"artifact_version", "report", "signature"}:
        return None
    body = {"artifact_version": artifact["artifact_version"], "report": artifact["report"]}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    if (artifact.get("artifact_version") != CALIBRATION_VERSION
            or not isinstance(artifact.get("signature"), str)
            or not hmac.compare_digest(artifact["signature"], expected)
            or not isinstance(artifact.get("report"), dict)):
        return None
    data = artifact["report"]
    try:
        return CalibrationReport(
            status=data["status"], model_id=data.get("model_id"), dataset_id=data.get("dataset_id"),
            sample_count=data["sample_count"], brier_score=data.get("brier_score"),
            expected_calibration_error=data.get("expected_calibration_error"),
            confidence_threshold=data["confidence_threshold"], calibrated=data["calibrated"],
            manifest_verified=data.get("manifest_verified", False),
            limitations=tuple(data.get("limitations", ())),
            calibration_count=data.get("calibration_count", 0),
            holdout_count=data.get("holdout_count", 0),
            metrics_by_prediction_type=data.get("metrics_by_prediction_type"),
            examples_sha256=data.get("examples_sha256"),
        )
    except (KeyError, TypeError, ValueError):
        return None


def independently_supported_confidence(
    raw_model_confidence: object, report: Optional[CalibrationReport], *,
    model_id: Optional[str], dataset_id: Optional[str],
) -> Optional[float]:
    """Return a value only when a validated calibration report matches the source.

    The provider schema may emit a subjective model estimate. Callers must not
    pass that value here and call it independently calibrated.
    """
    if (report is None or not report.calibrated or report.status != "calibrated"
            or not model_id or not dataset_id or report.model_id != model_id
            or report.dataset_id != dataset_id):
        return None
    if (isinstance(raw_model_confidence, bool)
            or not isinstance(raw_model_confidence, (int, float))
            or not math.isfinite(float(raw_model_confidence))
            or not 0 <= raw_model_confidence <= 1):
        return None
    # Calibration metrics are not yet tied to a reliable per-prediction mapping.
    # Do not present the raw score as an independently calibrated score.
    return None


def load_runtime_confidence(path: Path | str, *, model_id: str, dataset_id: str,
                            finding_category: str, now: Optional[float] = None) -> Optional[dict[str, Any]]:
    """Load an owner-signed, category-specific confidence calibration record.

    The artifact contains independently adjudicated calibration outcomes; it
    cannot be created from a model response. Missing, stale, uncalibrated,
    mismatched, or weak artifacts fail closed.
    """
    key = os.getenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", "").encode("utf-8")
    target = Path(path).expanduser()
    try:
        info = target.lstat()
        if (len(key) < 32 or not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_mode & 0o077 or info.st_size > 64 * 1024):
            return None
        raw_bytes = target.read_bytes()
        artifact = json.loads(raw_bytes.decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    fields = {"version", "model_id", "dataset_id", "finding_category", "calibrated_confidence",
              "sample_count", "expected_calibration_error", "manifest_verified", "issued_at",
              "expires_at", "signature"}
    if not isinstance(artifact, dict) or set(artifact) != fields:
        return None
    unsigned = {name: artifact[name] for name in fields if name != "signature"}
    canonical = json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected_signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    current = time.time() if now is None else float(now)
    confidence = artifact.get("calibrated_confidence")
    ece = artifact.get("expected_calibration_error")
    valid = (
        hmac.compare_digest(str(artifact.get("signature", "")), expected_signature)
        and artifact.get("version") == CALIBRATION_VERSION
        and artifact.get("model_id") == model_id
        and artifact.get("dataset_id") == dataset_id
        and artifact.get("finding_category") == finding_category
        and finding_category in {"style", "naming", "formatting"}
        and artifact.get("manifest_verified") is True
        and isinstance(artifact.get("sample_count"), int) and artifact["sample_count"] >= MIN_CALIBRATION_EXAMPLES
        and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
        and math.isfinite(float(confidence)) and 0.80 <= float(confidence) <= 1.0
        and isinstance(ece, (int, float)) and not isinstance(ece, bool)
        and math.isfinite(float(ece)) and 0 <= float(ece) <= MAX_EXPECTED_CALIBRATION_ERROR
        and isinstance(artifact.get("issued_at"), (int, float))
        and isinstance(artifact.get("expires_at"), (int, float))
        and artifact["issued_at"] <= current < artifact["expires_at"]
        and artifact["expires_at"] - artifact["issued_at"] <= 90 * 24 * 60 * 60
    )
    if not valid:
        return None
    return {
        "confidence_source": "independent_calibration",
        "calibrated": True, "manifest_verified": True,
        "model_id": model_id, "dataset_id": dataset_id,
        "finding_category": finding_category,
        "calibrated_confidence": float(confidence),
        "sample_count": artifact["sample_count"],
        "expected_calibration_error": float(ece),
        "artifact_sha256": hashlib.sha256(raw_bytes).hexdigest(),
    }
