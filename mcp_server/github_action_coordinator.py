"""Deterministic action routing after a validated webhook PR review."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_policy import PolicyInput, evaluate_finding, load_repository_action_config
from mcp_server.github_pr import GitHubPRException, PullRequestRef


class GitHubActionCoordinator:
    """Apply administrator policy to findings and persist every decision."""

    def __init__(self, store: Any, *, read_client: Any, adapter: Optional[GitHubMCPActionAdapter] = None,
                 configs: Optional[Mapping[str, Any]] = None, action_token: Optional[str] = None,
                 autonomous_executor: Optional[Any] = None) -> None:
        self.store = store
        self.read_client = read_client
        self.configs = dict(configs) if configs is not None else load_repository_action_config()
        self.autonomous_executor = autonomous_executor
        self.adapter = adapter or GitHubMCPActionAdapter(
            read_client=read_client,
            token=action_token if action_token is not None else os.getenv("GITHUB_ACTIONS_TOKEN"),
        )

    def describe_repository(self, repository: str) -> dict[str, str]:
        config = self.configs.get(repository.casefold()) if isinstance(repository, str) else None
        comments = bool(config and config.publish_review_comments and self.adapter.token)
        return {
            "manual_pr_review": "review_only",
            "webhook_comment_publication": "enabled" if comments else "disabled",
            "autonomous_fix_preparation": "enabled by administrator policy" if config and config.enable_autonomous_fixes else "disabled",
            "repository_source_sharing": "allowed" if config and config.allow_source_sharing else "blocked",
            "autonomous_fixing": "enabled" if config and config.enable_autonomous_fixes else "disabled",
            "candidate_staging_enabled": "true" if self.adapter.staging_enabled and self.adapter.staging_token else "false",
            "commit_execution_enabled": "true" if self.adapter.commit_enabled and self.adapter.commit_token else "false",
        }

    def process_review(self, event: Mapping[str, Any], review: Mapping[str, Any], *,
                       runtime_files: Optional[Mapping[str, Mapping[str, Any]]] = None) -> dict[str, Any]:
        repository = f"{event.get('owner', '')}/{event.get('repository', '')}"
        ref = PullRequestRef(str(event.get("owner", "")), str(event.get("repository", "")),
                             int(event.get("number", 0)))
        expected_head = str(event.get("head_sha", ""))
        owner_repo = review.get("repository")
        if not isinstance(owner_repo, str) or owner_repo.casefold() != repository.casefold():
            return {"mode": "review_only", "actions": [], "error": "review_repository_identity_mismatch"}
        if review.get("stale") or review.get("completion_status") == "stale" or review.get("head_sha") != expected_head:
            return {"mode": "review_only", "actions": [], "error": "stale_review_cannot_authorize_actions"}

        config = self.configs.get(repository.casefold())
        action_authorized = bool(getattr(self.adapter, "token", None))
        comments_enabled = bool(config and config.publish_review_comments and action_authorized)
        decision_rows: list[dict[str, Any]] = []
        for file_result in review.get("files", []):
            if not isinstance(file_result, dict) or not isinstance(file_result.get("findings"), list):
                continue
            path = file_result.get("path")
            if not isinstance(path, str):
                continue
            for finding in file_result["findings"]:
                if not isinstance(finding, dict):
                    continue
                # An explicitly opted-in repository may enter the staged
                # candidate workflow only for allowlisted low-risk findings.
                # All missing proof and infrastructure return to manual review.
                if (config and config.enable_autonomous_fixes and self.autonomous_executor is not None
                        and finding.get("issue_type") in {"style", "naming", "formatting"}
                        and finding.get("issue_type") in config.allowed_low_risk_categories):
                    finding_id = str(finding.get("finding_id", "unknown"))
                    policy_record = {"action": "potential_autonomous_fix",
                                     "eligible": False, "status": "pending_required_verification",
                                     "evidence": {"file_path": path, "category": finding.get("issue_type")}}
                    created, action_id = self.store.record_action(
                        delivery_id=str(event["delivery_id"]), repository=repository,
                        pr_number=ref.number, base_sha=str(event.get("base_sha", "")),
                        head_sha=expected_head, finding_id=finding_id, action_type="autonomous_fix",
                        policy_decision=policy_record, publication_status="pending",
                    )
                    if not created:
                        outcome = {"action_id": action_id, "finding_id": finding_id,
                                   "action": "potential_autonomous_fix", "status": "duplicate_suppressed",
                                   "manual_review_fallback": True}
                    else:
                        runtime = (runtime_files or {}).get(path, {})
                        outcome = self.autonomous_executor.execute(
                            event=event, review=review, file_result=file_result, finding=finding,
                            review_response=runtime.get("review_response"),
                            original_code=runtime.get("source_code", ""), config=config,
                            action_id=action_id,
                        )
                        outcome.update({"action_id": action_id, "finding_id": finding_id,
                                        "action": "potential_autonomous_fix"})
                    decision_rows.append(outcome)
                    continue
                finding_for_policy = dict(finding)
                if finding.get("origin") == "llm":
                    evidence_ids = {
                        evidence.get("evidence_id") for evidence in file_result.get("evidence", [])
                        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
                    }
                    cited = finding.get("evidence_ids")
                    if (not isinstance(cited, (list, tuple)) or not cited
                            or any(not isinstance(item, str) or item not in evidence_ids for item in cited)):
                        finding_for_policy["uncertain"] = True
                policy = evaluate_finding(self.configs, PolicyInput(
                    repository=repository,
                    finding=finding_for_policy,
                    expected_head_sha=expected_head,
                    current_head_sha=review.get("head_sha"),
                    changed_lines=None,
                    file_path=path,
                    # Review schema has no independent calibration output. Do
                    # not read a confidence claim from model-authored fields.
                    confidence=None,
                    confidence_source=None,
                    confidence_calibrated=False,
                    action_authorized=action_authorized,
                    branch=review.get("head_branch"),
                    is_fork=(review.get("head_repository") or repository).casefold() != repository.casefold(),
                ))
                action_type = (
                    "review_comment" if policy.action == "review_comment" else
                    "autonomous_fix" if policy.action == "potential_autonomous_fix" else
                    "policy_decision"
                )
                state = "pending" if policy.action == "review_comment" else (
                    "pending_required_verification" if policy.action == "potential_autonomous_fix" else "denied"
                )
                finding_id = str(finding.get("finding_id", "unknown"))
                comment_kind = "inline" if finding.get("inline_supported") is True else "general"
                policy_record = policy.to_dict()
                policy_record["comment_kind"] = comment_kind
                created, action_id = self.store.record_action(
                    delivery_id=str(event["delivery_id"]), repository=repository,
                    pr_number=ref.number, base_sha=str(event.get("base_sha", "")),
                    head_sha=expected_head, finding_id=finding_id, action_type=action_type,
                    policy_decision=policy_record, publication_status=state,
                    verification_status=str(finding.get("verification_status", "not_run")),
                )
                outcome: dict[str, Any] = {
                    "action_id": action_id, "finding_id": finding_id,
                    "action": policy.action, "eligible": policy.eligible,
                    "status": "review_only" if not comments_enabled else state,
                    "denial_reasons": list(policy.denial_reasons),
                    "required_checks": list(policy.required_checks),
                }
                if policy.action == "review_comment" and comments_enabled:
                    if not created or not self.store.claim_action(action_id):
                        outcome["status"] = "duplicate_suppressed"
                    else:
                        paths = {
                            file.get("path") for file in review.get("files", [])
                            if isinstance(file, dict) and isinstance(file.get("path"), str)
                            and file.get("status") in {"added", "modified", "renamed"}
                        }
                        if comment_kind == "inline":
                            result = self.adapter.publish_inline_comment(
                                ref, repository=repository, expected_head_sha=expected_head,
                                finding=finding, changed_paths=paths, operation_id=action_id,
                            )
                        else:
                            result = self.adapter.publish_general_comment(
                                ref, repository=repository, expected_head_sha=expected_head,
                                finding=finding, operation_id=action_id,
                            )
                        self.store.finish_action(
                            action_id, result.status, github_comment_id=result.github_id,
                            failure_code=result.error_code,
                        )
                        outcome.update(result.to_dict())
                elif policy.action == "potential_autonomous_fix":
                    outcome["status"] = "PENDING_REQUIRED_VERIFICATION"
                    outcome["commit_execution"] = "required_runtime_gates_unavailable"
                elif not comments_enabled:
                    outcome["status"] = "review_only"
                decision_rows.append(outcome)
        return {
            "mode": "comment_publishing_enabled" if comments_enabled else "review_only",
            "autonomous_fixing": "enabled" if config and config.enable_autonomous_fixes else "disabled",
            "actions": decision_rows,
        }
