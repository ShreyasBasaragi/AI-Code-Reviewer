"""Deterministic action routing after a validated webhook PR review."""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_policy import PolicyInput, evaluate_finding, load_repository_action_config
from mcp_server.github_policy import AUTONOMOUS_COMMIT_EXECUTION_ENABLED
from mcp_server.github_pr import GitHubPRException, PullRequestRef


class GitHubActionCoordinator:
    """Apply administrator policy to findings and persist every decision."""

    def __init__(self, store: Any, *, read_client: Any, adapter: Optional[GitHubMCPActionAdapter] = None,
                 configs: Optional[Mapping[str, Any]] = None, action_token: Optional[str] = None) -> None:
        self.store = store
        self.read_client = read_client
        self.configs = dict(configs) if configs is not None else load_repository_action_config()
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
            "autonomous_fixing": "disabled",
            "commit_execution_enabled": "true" if AUTONOMOUS_COMMIT_EXECUTION_ENABLED else "false",
        }

    def process_review(self, event: Mapping[str, Any], review: Mapping[str, Any]) -> dict[str, Any]:
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
                finding_for_policy = dict(finding)
                if finding.get("origin") == "llm":
                    evidence_ids = {
                        evidence.get("evidence_id") for evidence in file_result.get("evidence", [])
                        if isinstance(evidence, dict) and isinstance(evidence.get("evidence_id"), str)
                    }
                    cited = finding.get("evidence_ids")
                    if not isinstance(cited, list) or not cited or any(item not in evidence_ids for item in cited):
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
                created, action_id = self.store.record_action(
                    delivery_id=str(event["delivery_id"]), repository=repository,
                    pr_number=ref.number, base_sha=str(event.get("base_sha", "")),
                    head_sha=expected_head, finding_id=finding_id, action_type=action_type,
                    policy_decision=policy.to_dict(), publication_status=state,
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
                        result = self.adapter.publish_inline_comment(
                            ref, repository=repository, expected_head_sha=expected_head,
                            finding=finding, changed_paths=paths, operation_id=action_id,
                        )
                        self.store.finish_action(
                            action_id, result.status, github_comment_id=result.github_id,
                            failure_code=result.error_code,
                        )
                        outcome.update(result.to_dict())
                elif policy.action == "potential_autonomous_fix":
                    outcome["status"] = "PENDING_REQUIRED_VERIFICATION"
                    outcome["commit_execution"] = "AUTONOMOUS_COMMIT_DISABLED"
                elif not comments_enabled:
                    outcome["status"] = "review_only"
                decision_rows.append(outcome)
        return {
            "mode": "comment_publishing_enabled" if comments_enabled else "review_only",
            "autonomous_fixing": "disabled",
            "actions": decision_rows,
        }
