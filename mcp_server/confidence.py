"""Offline calibration evaluation; it never manufactures runtime confidence."""

from __future__ import annotations

import math
import hashlib
import hmac
import json
import os
from dataclasses import asdict, dataclass
from typing import Iterable, Mapping, Optional


CALIBRATION_VERSION = "review-confidence-calibration-v1"
MIN_CALIBRATION_EXAMPLES = 200
MAX_EXPECTED_CALIBRATION_ERROR = 0.05


@dataclass(frozen=True)
class LabeledConfidence:
    """A reviewed, independently labeled prediction from a pinned model build."""

    model_id: str
    dataset_id: str
    confidence: float
    correct: bool
    label_source: str


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
        identities.add((row.model_id, row.dataset_id))
        normalized.append((float(row.confidence), 1.0 if row.correct else 0.0))
    if len(identities) > 1:
        reasons.append("mixed_model_or_dataset_identity")
    actual_model = next(iter(identities))[0] if len(identities) == 1 else model_id
    actual_dataset = next(iter(identities))[1] if len(identities) == 1 else dataset_id
    if model_id and actual_model and model_id != actual_model:
        reasons.append("model_identity_mismatch")
    if dataset_id and actual_dataset and dataset_id != actual_dataset:
        reasons.append("dataset_identity_mismatch")
    n = len(normalized)
    if n < minimum_examples:
        reasons.append("insufficient_independent_labeled_examples")
    manifest_verified = _verify_manifest(
        signed_manifest, rows, actual_model, actual_dataset, n
    )
    if not manifest_verified:
        reasons.append("independent_dataset_manifest_not_verified")
    if n == 0:
        return CalibrationReport("insufficient_evidence", actual_model, actual_dataset, 0,
                                 None, None, 0.80, False, False, tuple(dict.fromkeys(reasons)))
    brier = sum((confidence - outcome) ** 2 for confidence, outcome in normalized) / n
    ece = 0.0
    for index in range(bins):
        low, high = index / bins, (index + 1) / bins
        members = [(p, y) for p, y in normalized
                   if low <= p < high or (index == bins - 1 and p == 1.0)]
        if members:
            ece += len(members) / n * abs(
                sum(p for p, _ in members) / len(members)
                - sum(y for _, y in members) / len(members)
            )
    if ece > maximum_ece:
        reasons.append("calibration_error_above_limit")
    calibrated = manifest_verified and n >= minimum_examples and ece <= maximum_ece and not reasons
    return CalibrationReport("calibrated" if calibrated else "not_calibrated",
                             actual_model, actual_dataset, n, brier, ece, 0.80,
                             calibrated, manifest_verified, tuple(dict.fromkeys(reasons)))


def _verify_manifest(manifest: Optional[Mapping[str, Any]], rows: list[LabeledConfidence], model_id: Optional[str],
                     dataset_id: Optional[str], sample_count: int) -> bool:
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
    example_payload = [
        {"model_id": item.model_id, "dataset_id": item.dataset_id,
         "confidence": float(item.confidence), "correct": item.correct,
         "label_source": item.label_source}
        for item in rows if isinstance(item, LabeledConfidence)
    ]
    digest = hashlib.sha256(json.dumps(example_payload, sort_keys=True,
                                       separators=(",", ":")).encode()).hexdigest()
    return (
        manifest.get("version") == CALIBRATION_VERSION
        and manifest.get("model_id") == model_id
        and manifest.get("dataset_id") == dataset_id
        and manifest.get("sample_count") == sample_count
        and manifest.get("examples_sha256") == digest
        and manifest.get("label_source") == "independent_human_adjudication"
        and isinstance(manifest.get("reviewed_by"), str)
        and len(manifest["reviewed_by"].strip()) >= 3
    )


def independently_supported_confidence(
    raw_model_confidence: object, report: Optional[CalibrationReport], *,
    model_id: Optional[str], dataset_id: Optional[str],
) -> Optional[float]:
    """Return a value only when a validated calibration report matches the source.

    The current provider schema does not emit confidence. Callers must not pass a
    value copied from a model response and call it calibrated.
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
