"""Bounded process-local handles shared by MCP and HTTP interfaces."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
import hashlib
import json
from threading import RLock
from typing import Any, Optional

from fix_generation import FixCandidate
from mcp_server.lifecycle_store import SQLiteLifecycleStore
from review_schema import RetrievedEvidence, ReviewFinding, ReviewResponse


@dataclass(frozen=True)
class ReviewHandle:
    code: str
    response: ReviewResponse
    source_sha256: str = ""
    repository: Optional[str] = None
    pr_number: Optional[int] = None
    base_sha: Optional[str] = None
    head_sha: Optional[str] = None


class LifecycleState:
    """Small bounded in-memory state; deliberately not shared across processes."""

    def __init__(self, limit: int = 32, *,
                 persistence_store: Optional[SQLiteLifecycleStore] = None) -> None:
        self.limit = limit
        # Legacy MCP callers/tests may store the prior tuple shape directly.
        self.reviews: OrderedDict[str, Any] = OrderedDict()
        self.candidates: OrderedDict[str, FixCandidate] = OrderedDict()
        self.persistence_store = persistence_store
        self._lock = RLock()

    def put_review(self, review_id: str, code: str, response: ReviewResponse, *,
                   repository: Optional[str] = None, pr_number: Optional[int] = None,
                   base_sha: Optional[str] = None, head_sha: Optional[str] = None) -> None:
        source_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
        handle = ReviewHandle(code, response, source_sha256, repository, pr_number, base_sha, head_sha)
        with self._lock:
            if self.persistence_store is not None:
                payload = response.to_dict()
                payload["raw_response"] = ""
                self.persistence_store.save_review(
                    review_id, code, payload, repository=repository, pr_number=pr_number,
                    base_sha=base_sha, head_sha=head_sha,
                )
            self.reviews[review_id] = handle
            self.reviews.move_to_end(review_id)
            self._trim(self.reviews)

    def get_review(self, review_id: str) -> Optional[ReviewHandle]:
        if not isinstance(review_id, str):
            return None
        with self._lock:
            value = self.reviews.get(review_id)
            if value is not None:
                self.reviews.move_to_end(review_id)
            if isinstance(value, ReviewHandle):
                return value
            if isinstance(value, tuple) and len(value) == 2:
                return ReviewHandle(value[0], value[1])
            if self.persistence_store is None:
                return None
            record = self.persistence_store.load_review(review_id)
            if record is None:
                return None
            try:
                response = _restore_review(record["response"])
                handle = ReviewHandle(
                    record["source_text"], response, record["source_sha256"],
                    record.get("repository"), record.get("pr_number"),
                    record.get("base_sha"), record.get("head_sha"),
                )
            except (KeyError, TypeError, ValueError):
                return None
            self.reviews[review_id] = handle
            self.reviews.move_to_end(review_id)
            self._trim(self.reviews)
            return handle

    def put_candidate(self, candidate: FixCandidate) -> None:
        with self._lock:
            if self.persistence_store is not None:
                self.persistence_store.save_candidate(candidate.to_dict())
            self.candidates[candidate.candidate_id] = candidate
            self.candidates.move_to_end(candidate.candidate_id)
            self._trim(self.candidates)

    def get_candidate(self, candidate_id: str) -> Optional[FixCandidate]:
        if not isinstance(candidate_id, str):
            return None
        with self._lock:
            value = self.candidates.get(candidate_id)
            if value is not None:
                self.candidates.move_to_end(candidate_id)
                return value
            if self.persistence_store is None:
                return None
            payload = self.persistence_store.load_candidate(candidate_id)
            if payload is None:
                return None
            try:
                candidate = _restore_candidate(payload)
            except (KeyError, TypeError, ValueError):
                return None
            self.candidates[candidate_id] = candidate
            self.candidates.move_to_end(candidate_id)
            self._trim(self.candidates)
            return candidate

    def _trim(self, cache: OrderedDict) -> None:
        while len(cache) > self.limit:
            cache.popitem(last=False)


PROCESS_STATE = LifecycleState(limit=32, persistence_store=SQLiteLifecycleStore.from_environment())


def _restore_review(payload: dict[str, Any]) -> ReviewResponse:
    findings = []
    for item in payload.get("findings", ()):
        if not isinstance(item, dict):
            raise ValueError("lifecycle_review_finding_invalid")
        row = dict(item)
        row["evidence_ids"] = tuple(row.get("evidence_ids", ()))
        row["also_detected_by"] = tuple(row.get("also_detected_by", ()))
        findings.append(ReviewFinding(**row))
    evidence = []
    for item in payload.get("evidence", ()):
        if not isinstance(item, dict):
            raise ValueError("lifecycle_review_evidence_invalid")
        evidence.append(RetrievedEvidence(**item))
    llm_findings = [item for item in findings if item.origin == "llm"]
    raw_response = json.dumps({
        "summary": str(payload["summary"]),
        "findings": [{
            "title": item.title, "issue_type": item.issue_type,
            "explanation": item.explanation, "recommended_fix": item.recommended_fix,
            "affected_file": item.affected_file, "line_start": item.line_start,
            "line_end": item.line_end, "corrected_code": item.corrected_code,
            "evidence_ids": list(item.evidence_ids), "uncertain": item.uncertain,
            "abstained": item.abstained, "risk_priority": item.risk_priority,
            "verification_status": item.verification_status,
        } for item in llm_findings],
    }, ensure_ascii=False, separators=(",", ":"))
    return ReviewResponse(
        summary=str(payload["summary"]), findings=tuple(findings),
        processing_errors=tuple(payload.get("processing_errors", ())),
        raw_response=raw_response, valid=bool(payload.get("valid", False)),
        evidence=tuple(evidence),
        static_analysis_status=str(payload.get("static_analysis_status", "not_run")),
        static_analysis_error=payload.get("static_analysis_error"),
    )


def _restore_candidate(payload: dict[str, Any]) -> FixCandidate:
    row = dict(payload)
    for name in ("assumptions", "evidence_ids", "generation_errors"):
        row[name] = tuple(row.get(name, ()))
    row["approval_history"] = tuple(row.get("approval_history", ()))
    row["raw_response"] = ""
    return FixCandidate(**row)
