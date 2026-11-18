"""In-memory approval transitions and safe fix export payloads."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, replace
from typing import Optional

from fix_generation import FixCandidate
from fix_verification import generate_unified_diff


@dataclass(frozen=True)
class FixApprovalResult:
    candidate: FixCandidate
    action: str
    approval_status: str
    outcome: str
    message: str
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "candidate_id": self.candidate.candidate_id,
            "action": self.action,
            "approval_status": self.approval_status,
            "outcome": self.outcome,
            "message": self.message,
            "error": self.error,
            "verification_status": self.candidate.verification_status,
            "candidate": self.candidate.to_dict(),
        }


@dataclass(frozen=True)
class FixExportResult:
    candidate_id: str
    filename: str
    corrected_code: str
    unified_diff: str
    approval_status: str
    syntax_verification_status: str
    static_verification_status: str
    warning: str = "Behavioral tests were not run. Static analysis and syntax checks do not establish behavioral correctness."

    def to_dict(self) -> dict:
        return asdict(self)


def record_verification(candidate: FixCandidate, report) -> FixCandidate:
    """Return a candidate snapshot with the latest explicit verification report."""
    payload = report.to_dict()
    if payload.get("candidate_id") != candidate.candidate_id:
        return candidate
    return replace(
        candidate,
        verification_status=payload.get("verification_status", "verification_error"),
        verification_report=payload,
    )


def accept_fix_candidate(candidate: FixCandidate) -> FixApprovalResult:
    return _decide(candidate, "accepted")


def reject_fix_candidate(candidate: FixCandidate) -> FixApprovalResult:
    return _decide(candidate, "rejected")


def _decide(candidate: FixCandidate, decision: str) -> FixApprovalResult:
    if not isinstance(candidate, FixCandidate):
        raise TypeError("candidate must be a FixCandidate")
    current = candidate.approval_status
    if current == decision:
        return FixApprovalResult(
            candidate=candidate,
            action=decision,
            approval_status=current,
            outcome="already_applied",
            message=f"Candidate is already {decision}; decision unchanged.",
        )
    if current in {"accepted", "rejected"}:
        message = f"Candidate is already {current}; conflicting {decision} action was not applied."
        updated = _append_event(candidate, decision, "conflict", message)
        return FixApprovalResult(updated, decision, current, "conflict", message, "conflicting_decision")

    if decision == "accepted":
        report = candidate.verification_report
        if not isinstance(report, dict):
            report = {}
        eligible = _verification_passed(candidate, report)
        if not eligible:
            message = "Acceptance requires matching successful syntax and static-analysis verification; behavioral tests were not run."
            updated = _append_event(candidate, decision, "blocked", message)
            return FixApprovalResult(
                updated, decision, current, "blocked", message, "required_verification_failed"
            )

    message = (
        "Candidate explicitly accepted after syntax and static-analysis verification; behavioral tests were not run."
        if decision == "accepted"
        else "Candidate explicitly rejected; original review and source remain unchanged."
    )
    updated = replace(candidate, approval_status=decision)
    updated = _append_event(updated, decision, decision, message)
    return FixApprovalResult(updated, decision, decision, decision, message)


def _append_event(
    candidate: FixCandidate, action: str, outcome: str, message: str
) -> FixCandidate:
    event = {"action": action, "outcome": outcome, "message": message}
    return replace(candidate, approval_history=candidate.approval_history + (event,))


def export_fix_candidate(candidate: FixCandidate) -> FixExportResult:
    if not isinstance(candidate, FixCandidate):
        raise TypeError("candidate must be a FixCandidate")
    if candidate.approval_status != "accepted":
        raise ValueError("export requires an accepted candidate")
    report = candidate.verification_report
    if not _verification_passed(candidate, report):
        raise ValueError("export requires successful matching syntax and static verification")
    if candidate.corrected_code is None:
        raise ValueError("export requires corrected code")
    # The filename is a fixed safe basename. No model or caller path is trusted.
    return FixExportResult(
        candidate_id=candidate.candidate_id,
        filename="candidate.py",
        corrected_code=candidate.corrected_code,
        unified_diff=generate_unified_diff(candidate.original_code, candidate.corrected_code),
        approval_status=candidate.approval_status,
        syntax_verification_status=candidate.verification_status,
        static_verification_status=str(report.get("static_analysis_status", "not_run")),
    )


def _verification_passed(candidate: FixCandidate, report) -> bool:
    if not isinstance(report, dict) or candidate.corrected_code is None:
        return False
    syntax = report.get("syntax_check", {})
    return bool(
        candidate.generation_status == "generated"
        and report.get("candidate_id") == candidate.candidate_id
        and report.get("verification_status") == "syntax_passed"
        and candidate.verification_status == "syntax_passed"
        and isinstance(syntax, dict)
        and syntax.get("status") == "passed"
        and report.get("static_analysis_status") == "passed"
        and not report.get("introduced_static_issues")
        and not report.get("static_analysis_errors")
        and report.get("original_sha256") == hashlib.sha256(candidate.original_code.encode("utf-8")).hexdigest()
        and report.get("corrected_sha256") == hashlib.sha256(candidate.corrected_code.encode("utf-8")).hexdigest()
    )
