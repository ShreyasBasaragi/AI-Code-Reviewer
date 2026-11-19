"""Fail-closed orchestration for explicitly authorized low-risk PR fixes.

No candidate code is executed in this process. External staging and final
writes are delegated to the fixed GitHub adapter and are disabled by default.
"""
from __future__ import annotations

import hashlib
import os
from typing import Any, Callable, Mapping, Optional

from fix_generation import FixCandidate
from mcp_server.behavioral_verification import BehavioralVerifier
from mcp_server.github_policy import (PolicyDecision, PolicyInput, evaluate_finding,
                                      is_autonomous_low_risk_finding)
from mcp_server.github_pr import PullRequestRef


class AutonomousFixExecutor:
    def __init__(self, *, store: Any, adapter: Any,
                 fix_generator: Callable[[Any, str, str], FixCandidate],
                 confidence_resolver: Callable[[Mapping[str, Any]], Optional[Mapping[str, Any]]],
                 behavioral_verifier: Optional[BehavioralVerifier] = None,
                 behavioral_tests: Optional[Callable[[str], bytes]] = None,
                 workflow_identity: Optional[str] = None) -> None:
        self.store = store
        self.adapter = adapter
        self.fix_generator = fix_generator
        self.confidence_resolver = confidence_resolver
        self.behavioral_verifier = behavioral_verifier
        self.behavioral_tests = behavioral_tests
        self.workflow_identity = workflow_identity or os.getenv("GITHUB_CANDIDATE_WORKFLOW_ID")

    def execute(self, *, event: Mapping[str, Any], review: Mapping[str, Any],
                file_result: Mapping[str, Any], finding: Mapping[str, Any],
                review_response: Any, original_code: str, config: Any,
                action_id: str) -> dict[str, Any]:
        repo = f"{event.get('owner', '')}/{event.get('repository', '')}"
        ref = PullRequestRef(str(event.get("owner", "")), str(event.get("repository", "")),
                             int(event.get("number", 0)))
        head_sha = event.get("head_sha")
        path = file_result.get("path")
        finding_id = finding.get("finding_id")
        def blocked(reason: str, **extra: Any) -> dict[str, Any]:
            self.store.finish_action(action_id, "blocked", failure_code=reason,
                                     verification_status=str(extra.get("verification_status", "not_run")))
            return {"status": "blocked", "reason": reason,
                    "manual_review_fallback": True, **extra}

        if not self.store.claim_action(action_id):
            return {"status": "duplicate_suppressed", "action_id": action_id}
        if (not config or not config.enable_autonomous_fixes
                or not self.adapter.staging_enabled or not self.adapter.staging_token):
            return blocked("owner_opt_in_or_candidate_staging_authorization_missing")
        if (review.get("stale") is True or review.get("head_sha") != head_sha
                or (review.get("head_repository") or repo).casefold() != repo.casefold()
                or not isinstance(path, str) or not path.endswith(".py")
                or finding.get("issue_type") not in config.allowed_low_risk_categories
                or finding.get("issue_type") not in {"style", "naming", "formatting"}
                or finding.get("origin") not in {"llm", "static"}
                or finding.get("severity") in {"critical", "high"}
                or finding.get("uncertain") is True or finding.get("abstained") is True
                or (finding.get("origin") == "static" and str(finding.get("rule_id", "")).startswith("PYSEC"))):
            return blocked("finding_not_allowlisted_low_risk")
        if not is_autonomous_low_risk_finding(finding, file_path=path, config=config):
            return blocked("finding_risk_policy_denied")
        if finding.get("origin") == "llm":
            evidence_ids = {item.get("evidence_id") for item in file_result.get("evidence", [])
                            if isinstance(item, Mapping) and isinstance(item.get("evidence_id"), str)}
            cited = finding.get("evidence_ids")
            # The review schema preserves evidence references as tuples while
            # webhook/API serialization may produce lists. Accept both without
            # relaxing membership or type validation.
            if (not isinstance(cited, (list, tuple)) or not cited
                    or any(not isinstance(item, str) or not item or item not in evidence_ids
                           for item in cited)):
                return blocked("finding_evidence_validation_failed")
        if (not isinstance(review_response, object) or not getattr(review_response, "valid", False)
                or not isinstance(original_code, str)):
            return blocked("validated_review_source_unavailable")
        confidence = self.confidence_resolver(finding)
        if not isinstance(confidence, Mapping):
            return blocked("independent_calibration_unavailable")
        if (confidence.get("finding_category") != finding.get("issue_type")
                or confidence.get("model_id") != review.get("provider_identity")):
            return blocked("calibration_model_or_category_mismatch")
        threshold = max(0.80, float(config.confidence_threshold))
        if confidence.get("calibrated_confidence", 0) < threshold:
            return blocked("calibrated_confidence_below_threshold")
        candidate = self.fix_generator(review_response, str(finding_id), original_code)
        if not isinstance(candidate, FixCandidate) or candidate.generation_status != "generated" or not candidate.corrected_code:
            return blocked("fix_generation_failed")
        proposal = self.adapter.prepare_fix_candidate(
            ref, repository=repo, expected_head_sha=str(head_sha), file_path=path,
            candidate=candidate, allowed_paths={path},
            policy_decision=PolicyDecision("potential_autonomous_fix", True,
                                           "PENDING_REQUIRED_VERIFICATION",
                                           required_checks=("syntax", "static", "behavioral", "ci")),
            max_changed_lines=min(3, config.max_changed_lines),
        )
        if proposal.get("status") != "PENDING_REQUIRED_VERIFICATION":
            return blocked(str(proposal.get("reason", "candidate_preparation_failed")), proposal=proposal)
        changed_lines = proposal.get("changed_lines")
        if (review.get("head_branch") not in config.permitted_branches
                or changed_lines is None or changed_lines > min(3, config.max_changed_lines)):
            return blocked("repository_scope_or_line_limit_denied")
        if self.behavioral_verifier is None or not callable(self.behavioral_tests):
            return blocked("isolated_behavioral_verification_unavailable")
        try:
            test_bundle = self.behavioral_tests(path)
        except Exception:
            return blocked("behavioral_test_bundle_unavailable")
        if not isinstance(test_bundle, bytes) or not test_bundle:
            return blocked("behavioral_test_bundle_empty")
        verification = self.behavioral_verifier.verify(
            original_source=original_code.encode("utf-8"),
            candidate_source=candidate.corrected_code.encode("utf-8"),
            expected_source_sha256=hashlib.sha256(original_code.encode("utf-8")).hexdigest(),
            tests=test_bundle, changed_files=(path,),
        )
        behavioral_evidence = verification.to_dict()
        if verification.status != "passed" or verification.attestation_verified is not True:
            return blocked("behavioral_verification_not_attested", verification_status=verification.status,
                           behavioral_evidence=behavioral_evidence)
        created = self.adapter.create_candidate_revision(
            ref, repository=repo, branch=str(review.get("head_branch")),
            expected_head_sha=str(head_sha), file_path=path, original_code=original_code,
            corrected_code=candidate.corrected_code, operation_id=action_id,
        )
        if created.get("status") != "created":
            if created.get("status") == "unknown":
                self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                                 verification_status="behavioral_passed", details={
                                                     "candidate": created,
                                                     "branch": review.get("head_branch"),
                                                     "staging_outcome_unknown": True,
                                                 })
                self.store.finish_action(action_id, "requires_reconciliation",
                                         failure_code=str(created.get("error_code", "candidate_stage_unknown")),
                                         verification_status="behavioral_passed")
                return {"status": "unknown", "reason": created.get("error_code"),
                        "candidate": created, "manual_review_fallback": True}
            return blocked(str(created.get("error_code", "candidate_revision_creation_failed")),
                           candidate=created, verification_status="behavioral_passed")
        self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                         verification_status="behavioral_passed", details={
                                             "candidate": created,
                                             "branch": review.get("head_branch"),
                                         })
        required_github_checks = tuple(config.required_github_checks)
        if not required_github_checks or not self.workflow_identity:
            return blocked("candidate_ci_workflow_or_required_checks_unconfigured",
                           candidate=created, verification_status="behavioral_passed")
        self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                         verification_status="behavioral_passed", details={
                                             "candidate": created,
                                             "branch": review.get("head_branch"),
                                             "workflow_dispatch_attempted": True,
                                             "workflow_identity": self.workflow_identity,
                                         })
        dispatch = self.adapter.dispatch_candidate_ci(
            ref, candidate_sha=created["candidate_sha"],
            candidate_branch=created["candidate_branch"],
            workflow_identity=self.workflow_identity,
        )
        if dispatch.get("status") != "dispatched":
            if dispatch.get("status") == "unknown":
                self.store.finish_action(action_id, "requires_reconciliation",
                                         failure_code=str(dispatch.get("error_code", "candidate_ci_dispatch_unknown")),
                                         verification_status="behavioral_passed")
                return {"status": "unknown", "reason": dispatch.get("error_code"),
                        "candidate": created, "ci_dispatch": dispatch,
                        "manual_review_fallback": True}
            return blocked(str(dispatch.get("error_code", "candidate_ci_dispatch_failed")),
                           candidate=created, ci_dispatch=dispatch,
                           verification_status="behavioral_passed")
        ci = self.adapter.verify_candidate_ci(
            ref, candidate_sha=created["candidate_sha"], expected_pr_head_sha=str(head_sha),
            required_checks=required_github_checks,
            expected_workflow_identity=self.workflow_identity,
        )
        if ci.get("status") != "passed":
            self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                             verification_status="behavioral_passed", details={
                                                 "candidate": created, "ci": ci,
                                             "ci_dispatch": dispatch,
                                                 "branch": review.get("head_branch"),
                                             })
            return blocked(str(ci.get("error_code", "candidate_ci_not_passed")),
                           candidate=created, ci=ci, verification_status="behavioral_passed")
        policy = evaluate_finding({repo.casefold(): config}, PolicyInput(
            repository=repo, finding=finding, expected_head_sha=str(head_sha),
            current_head_sha=review.get("head_sha"), changed_lines=changed_lines,
            file_path=path, confidence=float(confidence["calibrated_confidence"]),
            confidence_source="independent_calibration", confidence_calibrated=True,
            syntax_status="passed", static_status="passed", behavioral_status="passed",
            ci_status="passed", action_authorized=True,
            branch=review.get("head_branch"), is_fork=False,
        ))
        if not policy.eligible or policy.action != "potential_autonomous_fix":
            return blocked("repository_policy_denied", denial_reasons=list(policy.denial_reasons),
                           candidate=created, ci=ci, verification_status="passed")
        gates = {
            "repository_opt_in": True,
            "eligible_low_risk_category": True,
            "calibration_supported": True,
            "source_hashes_valid": proposal.get("original_sha256") == hashlib.sha256(original_code.encode()).hexdigest()
                                   and proposal.get("corrected_sha256") == hashlib.sha256(candidate.corrected_code.encode()).hexdigest(),
            "syntax_passed": proposal.get("syntax_status") == "passed",
            "static_passed": proposal.get("static_status") == "passed",
            "behavioral_passed": verification.status == "passed",
            "candidate_ci_passed": ci.get("status") == "passed",
            "scope_valid": proposal.get("changed_lines", 99) <= min(3, config.max_changed_lines),
            "write_permission_valid": True,
            "durable_record_reserved": True,
            "idempotency_reserved": True,
        }
        # Candidate staging and exact-SHA CI can be exercised independently.
        # A separate final-write switch and token remain mandatory for commits.
        if not self.adapter.commit_enabled or not self.adapter.commit_token:
            self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                             verification_status="passed", details={
                                                 "candidate": created,
                                                 "behavioral": behavioral_evidence,
                                                 "ci": ci, "ci_dispatch": dispatch,
                                                 "confidence": dict(confidence),
                                                 "changed_lines": changed_lines,
                                                 "branch": review.get("head_branch"),
                                                 "commit_attempted": False,
                                                 "final_commit_disabled": True,
                                             })
            self.store.finish_action(action_id, "AUTONOMOUS_COMMIT_DISABLED",
                                     failure_code="final_commit_write_not_enabled",
                                     verification_status="passed")
            return {"status": "AUTONOMOUS_COMMIT_DISABLED",
                    "reason": "final_commit_write_not_enabled",
                    "candidate_id": candidate.candidate_id,
                    "candidate_sha": created["candidate_sha"],
                    "candidate_branch": created["candidate_branch"],
                    "ci_dispatch": dispatch, "ci": ci,
                    "behavioral": behavioral_evidence,
                    "confidence": dict(confidence),
                    "commit_sha": None, "manual_review_fallback": True}
        self.store.update_action_details(action_id, candidate_id=candidate.candidate_id,
                                         verification_status="passed", details={
                                             "candidate": created, "behavioral": behavioral_evidence,
                                             "ci": ci, "ci_dispatch": dispatch,
                                             "confidence": dict(confidence),
                                             "changed_lines": changed_lines,
                                             "branch": review.get("head_branch"),
                                             "commit_attempted": True,
                                         })
        result = self.adapter.execute_commit(
            ref=ref, repository=repo, branch=str(review.get("head_branch")),
            expected_head_sha=str(head_sha), candidate_sha=created["candidate_sha"],
            operation_id=action_id, file_path=path, corrected_code=candidate.corrected_code,
            behavioral_evidence=behavioral_evidence, ci_evidence=ci,
            confidence_evidence=confidence, confidence_threshold=threshold,
            required_checks=required_github_checks, changed_lines=changed_lines,
            permitted_branches=config.permitted_branches,
            protected_branches=tuple(config.protected_paths), is_fork=False,
            gates=gates,
        )
        if result.status == "published":
            self.store.finish_action(action_id, "published", commit_sha=result.commit_sha,
                                     verification_status="passed")
        elif result.status == "unknown":
            self.store.finish_action(action_id, "requires_reconciliation",
                                     failure_code=result.error_code, verification_status="passed")
        else:
            self.store.finish_action(action_id, "failed" if result.status == "failed" else "blocked",
                                     failure_code=result.error_code or "commit_not_confirmed",
                                     verification_status="passed")
        return {"status": result.status, "candidate_id": candidate.candidate_id,
                "candidate_sha": created["candidate_sha"], "candidate_branch": created["candidate_branch"],
                "ci_dispatch": dispatch, "ci": ci, "behavioral": behavioral_evidence,
                "confidence": dict(confidence),
                "commit_sha": result.commit_sha, "error_code": result.error_code,
                "manual_review_fallback": result.status != "published"}
