"""Narrow, policy-gated GitHub action adapter for the existing webhook flow.

All write routes are fixed and inputs are constructed from validated PR data.
The adapter exposes no arbitrary URL, path, branch, or request operation.
"""

from __future__ import annotations

import json
import hashlib
import re
import socket
import os
import urllib.request
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional
import time
from datetime import datetime, timezone

from mcp_server.github_pr import API_BASE, API_VERSION, GitHubPRException, PullRequestRef
from mcp_server.github_policy import commit_execution_decision
from fix_verification import check_python_syntax, generate_unified_diff
from fix_verification import verify_fix_candidate
from fix_generation import FixCandidate
from mcp_server.github_policy import PolicyDecision
from mcp_server.github_ci import GitHubCIClient
from static_analysis import analyze_python, static_fingerprint_counts


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _workflow_run_created_after(value: Any, timestamp: float) -> bool:
    if not isinstance(value, str):
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).timestamp() >= timestamp
    except (ValueError, OverflowError):
        return False


@dataclass(frozen=True)
class GitHubActionResult:
    status: str
    action_type: str
    repository: str
    pull_number: int
    finding_id: Optional[str] = None
    github_id: Optional[int] = None
    commit_sha: Optional[str] = None
    error_code: Optional[str] = None
    detail: Optional[Mapping[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status, "action_type": self.action_type,
            "repository": self.repository, "pull_number": self.pull_number,
            "finding_id": self.finding_id, "github_id": self.github_id,
            "commit_sha": self.commit_sha, "error_code": self.error_code,
            "detail": dict(self.detail or {}),
        }


class GitHubMCPActionAdapter:
    """Narrow GitHub action boundary for comments and gated candidate commits."""

    def __init__(self, *, read_client: Any, token: Optional[str] = None,
                 transport: Optional[Callable[[str, str, Mapping[str, str], bytes], Any]] = None,
                 staging_token: Optional[str] = None, commit_token: Optional[str] = None,
                 staging_enabled: Optional[bool] = None, commit_enabled: Optional[bool] = None) -> None:
        self.read_client = read_client
        self.token = token
        self.staging_token = staging_token if staging_token is not None else os.getenv("GITHUB_CANDIDATE_STAGING_TOKEN")
        self.commit_token = commit_token if commit_token is not None else os.getenv("GITHUB_AUTONOMOUS_COMMIT_TOKEN")
        self.staging_enabled = (_explicit_env_enabled("GITHUB_CANDIDATE_STAGING_ENABLED")
                                if staging_enabled is None else staging_enabled is True)
        self.commit_enabled = (_explicit_env_enabled("GITHUB_AUTONOMOUS_COMMIT_ENABLED")
                               if commit_enabled is None else commit_enabled is True)
        self.transport = transport or self._request

    def publish_inline_comment(self, ref: PullRequestRef, *, repository: str,
                               expected_head_sha: str, finding: Mapping[str, Any],
                               changed_paths: set[str], operation_id: Optional[str] = None) -> GitHubActionResult:
        finding_id = finding.get("finding_id") if isinstance(finding.get("finding_id"), str) else None
        path = finding.get("path")
        line = finding.get("source_line")
        if (not isinstance(path, str) or path not in changed_paths
                or finding.get("inline_supported") is not True
                or isinstance(line, bool) or not isinstance(line, int) or line < 1
                or (finding.get("line_end") is not None and finding.get("line_end") != finding.get("line_start"))
                or not _safe_changed_path(path)):
            return self._result("skipped_unmappable", ref, repository, finding_id,
                                "finding_location_not_safely_mappable")
        if not _valid_finding(finding):
            return self._result("skipped_invalid_finding", ref, repository, finding_id,
                                "finding_validation_failed")
        try:
            latest = self.read_client.get_pull(ref)
        except GitHubPRException as exc:
            return self._result("failed", ref, repository, finding_id, exc.code)
        if not _pull_matches(latest, ref, expected_head_sha):
            return self._result("stale", ref, repository, finding_id, "stale_or_mismatched_pull_request")
        body = _comment_body(finding, operation_id=operation_id)
        payload = json.dumps({
            "body": body,
            "commit_id": expected_head_sha,
            "path": path,
            "line": line,
            "side": "RIGHT",
        }, separators=(",", ":")).encode("utf-8")
        endpoint = f"{self._repo_path(ref)}/pulls/{ref.number}/comments"
        try:
            response = self.transport("POST", API_BASE + endpoint, self._headers(), payload)
        except GitHubPRException as exc:
            # A network timeout after a write is ambiguous. Never retry here.
            status = "unknown" if exc.code in {"github_timeout", "github_network_error", "github_unavailable"} else "failed"
            return self._result(status, ref, repository, finding_id, exc.code)
        github_id = response.get("id") if isinstance(response, dict) and isinstance(response.get("id"), int) else None
        if github_id is None:
            return self._result("unknown", ref, repository, finding_id, "github_write_response_unverifiable")
        return GitHubActionResult("published", "review_comment", repository, ref.number,
                                  finding_id, github_id=github_id)

    def publish_general_comment(self, ref: PullRequestRef, *, repository: str,
                                expected_head_sha: str, finding: Mapping[str, Any],
                                operation_id: Optional[str] = None) -> GitHubActionResult:
        """Publish a validated finding as an issue comment when no diff line is safe."""
        finding_id = finding.get("finding_id") if isinstance(finding.get("finding_id"), str) else None
        if f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold():
            return self._result("failed", ref, repository, finding_id, "repository_identity_mismatch")
        if not _valid_finding(finding):
            return self._result("skipped_invalid_finding", ref, repository, finding_id,
                                "finding_validation_failed")
        try:
            latest = self.read_client.get_pull(ref)
        except GitHubPRException as exc:
            return self._result("failed", ref, repository, finding_id, exc.code)
        if not _pull_matches(latest, ref, expected_head_sha):
            return self._result("stale", ref, repository, finding_id, "stale_or_mismatched_pull_request")
        payload = json.dumps({"body": _comment_body(finding, operation_id=operation_id)},
                             separators=(",", ":")).encode("utf-8")
        endpoint = f"{self._repo_path(ref)}/issues/{ref.number}/comments"
        try:
            response = self.transport("POST", API_BASE + endpoint, self._headers(), payload)
        except GitHubPRException as exc:
            status = "unknown" if exc.code in {"github_timeout", "github_network_error", "github_unavailable"} else "failed"
            return self._result(status, ref, repository, finding_id, exc.code)
        github_id = response.get("id") if isinstance(response, dict) and isinstance(response.get("id"), int) else None
        if github_id is None:
            return self._result("unknown", ref, repository, finding_id, "github_write_response_unverifiable")
        return GitHubActionResult("published", "review_comment", repository, ref.number,
                                  finding_id, github_id=github_id,
                                  detail={"comment_kind": "general"})

    def reconcile_inline_comment(self, ref: PullRequestRef, *, repository: str,
                                 expected_head_sha: str, operation_id: str,
                                 comment_kind: str = "inline") -> dict[str, Any]:
        """Check remote state by durable operation marker before any retry."""
        if not re.fullmatch(r"[a-f0-9]{32,64}", operation_id or ""):
            return {"status": "unknown", "error_code": "operation_id_invalid"}
        if f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold():
            return {"status": "unknown", "error_code": "repository_identity_mismatch"}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected_head_sha):
                return {"status": "stale", "error_code": "expected_revision_changed"}
            if comment_kind not in {"inline", "general"}:
                return {"status": "unknown", "error_code": "comment_kind_invalid"}
            endpoint_name = "pulls" if comment_kind == "inline" else "issues"
            endpoint = f"{self._repo_path(ref)}/{endpoint_name}/{ref.number}/comments"
            marker = f"<!-- ai-code-review-action:{operation_id} -->"
            observed = []
            complete = False
            for page in range(1, 11):
                query = urllib.parse.urlencode({"per_page": 100, "page": page})
                headers = self._headers()
                read_token = getattr(self.read_client, "token", None)
                if read_token:
                    headers["Authorization"] = f"Bearer {read_token}"
                else:
                    headers.pop("Authorization", None)
                response = self.transport("GET", API_BASE + endpoint + "?" + query,
                                          headers, b"")
                if not isinstance(response, list):
                    return {"status": "unknown", "error_code": "comment_reconciliation_response_invalid"}
                observed.extend(item for item in response if isinstance(item, dict))
                if len(response) < 100:
                    complete = True
                    break
            matches = [item for item in observed if marker in str(item.get("body", ""))
                       and (comment_kind == "general" or item.get("commit_id") == expected_head_sha)]
            if matches:
                github_id = matches[0].get("id")
                return {"status": "published", "github_comment_id": github_id if isinstance(github_id, int) else None,
                        "operation_id": operation_id}
            if complete:
                return {"status": "not_published", "operation_id": operation_id,
                        "retry_safe": True, "expected_head_sha": expected_head_sha}
            return {"status": "unknown", "error_code": "comment_history_truncated"}
        except Exception as exc:
            return {"status": "unknown", "error_code": f"reconciliation_{type(exc).__name__}"}

    def prepare_change(self, ref: PullRequestRef, *, repository: str, expected_head_sha: str,
                       file_path: str, original_code: str, corrected_code: str,
                       allowed_paths: set[str], max_changed_lines: int = 3) -> dict[str, Any]:
        """Return a bounded diff proposal; this method has no write capability."""
        if not _safe_changed_path(file_path) or file_path not in allowed_paths:
            return {"status": "denied", "reason": "target_path_not_allowlisted", "diff": ""}
        lowered_path = file_path.casefold()
        parts = lowered_path.split("/")
        protected_prefixes = (".github", "migrations", "infra", "security", "auth")
        if (parts[0] in protected_prefixes or lowered_path.endswith((".pem", ".key", ".p12", ".pfx", ".env", ".lock"))
                or parts[-1] in {"dockerfile", "makefile", "pyproject.toml", "requirements.txt"}):
            return {"status": "denied", "reason": "protected_path", "diff": ""}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected_head_sha):
                return {"status": "denied", "reason": "stale_or_unverified_head_sha", "diff": ""}
            head = pull.get("head", {})
            head_repository = head.get("repo") if isinstance(head.get("repo"), dict) else None
            head_owner = head_repository.get("owner", {}).get("login") if isinstance(head_repository, dict) and isinstance(head_repository.get("owner"), dict) else None
            head_name = head_repository.get("name") if isinstance(head_repository, dict) else None
            if not isinstance(head_owner, str) or not isinstance(head_name, str):
                return {"status": "denied", "reason": "head_repository_identity_unavailable", "diff": ""}
            if f"{head_owner}/{head_name}".casefold() != repository.casefold():
                return {"status": "denied", "reason": "fork_write_not_explicitly_authorized", "diff": ""}
            head_ref = PullRequestRef(head_owner, head_name, ref.number)
            files, _truncated, _pages, _error = self.read_client.get_files(ref, max_files=200, max_pages=2)
            if not any(isinstance(item, dict) and item.get("filename") == file_path
                       and item.get("status") in {"added", "modified", "renamed"} for item in files):
                return {"status": "denied", "reason": "target_not_changed_in_pull_request", "diff": ""}
            current_source = self.read_client.get_file(
                head_ref, file_path, expected_head_sha, max_source_bytes=256 * 1024,
            )
            if current_source != original_code:
                return {"status": "denied", "reason": "reviewed_source_mismatch", "diff": ""}
        except Exception:
            return {"status": "denied", "reason": "current_source_unavailable", "diff": ""}
        syntax = check_python_syntax(corrected_code)
        if syntax.status != "passed":
            return {"status": "denied", "reason": "syntax_verification_not_passed", "diff": ""}
        original_analysis = analyze_python(original_code, file_path=file_path)
        candidate_analysis = analyze_python(corrected_code, file_path=file_path)
        if original_analysis.status == "errored" or candidate_analysis.status == "errored":
            return {"status": "denied", "reason": "static_analysis_errored", "diff": ""}
        introduced = static_fingerprint_counts(candidate_analysis) - static_fingerprint_counts(original_analysis)
        if introduced:
            return {"status": "denied", "reason": "new_static_violation", "diff": ""}
        diff = generate_unified_diff(
            original_code, corrected_code,
            fromfile=f"a/{file_path}", tofile=f"b/{file_path}",
        )
        changed_lines = sum(1 for line in diff.splitlines() if line.startswith(("+", "-"))
                            and not line.startswith(("+++", "---")))
        if changed_lines == 0 or changed_lines > min(max_changed_lines, 3):
            return {"status": "denied", "reason": "changed_line_limit_exceeded_or_empty",
                    "changed_lines": changed_lines, "diff": ""}
        if not self._revision_current(ref, expected_head_sha):
            return {"status": "denied", "reason": "stale_or_unverified_head_sha", "diff": ""}
        return {
            "status": "PENDING_REQUIRED_VERIFICATION",
            "repository": repository, "pull_number": ref.number,
            "expected_head_sha": expected_head_sha, "file_path": file_path,
            "changed_lines": changed_lines, "diff": diff,
            "syntax_status": "passed", "static_status": "passed",
            "behavioral_status": "not_run", "ci_status": "not_run",
            "commit_execution": "requires_independent_runtime_gates",
        }

    def prepare_fix_candidate(self, ref: PullRequestRef, *, repository: str,
                              expected_head_sha: str, file_path: str,
                              candidate: FixCandidate, allowed_paths: set[str],
                              policy_decision: PolicyDecision,
                              max_changed_lines: int = 3) -> dict[str, Any]:
        """Reuse the existing candidate verifier before creating a patch proposal."""
        if not isinstance(policy_decision, PolicyDecision) or not policy_decision.eligible or policy_decision.action != "potential_autonomous_fix":
            return {"status": "denied", "reason": "policy_did_not_authorize_fix_preparation", "diff": ""}
        if not isinstance(candidate, FixCandidate) or candidate.generation_status != "generated" or not candidate.corrected_code:
            return {"status": "denied", "reason": "invalid_fix_candidate", "diff": ""}
        verification = verify_fix_candidate(candidate, expected_candidate_id=candidate.candidate_id)
        if verification.verification_status != "syntax_passed" or verification.static_analysis_status != "passed":
            return {
                "status": "denied", "reason": "required_syntax_or_static_verification_failed",
                "candidate_id": candidate.candidate_id,
                "verification": verification.to_dict(), "diff": "",
            }
        proposal = self.prepare_change(
            ref, repository=repository, expected_head_sha=expected_head_sha,
            file_path=file_path, original_code=candidate.original_code,
            corrected_code=candidate.corrected_code, allowed_paths=allowed_paths,
            max_changed_lines=max_changed_lines,
        )
        proposal["candidate_id"] = candidate.candidate_id
        proposal["original_sha256"] = verification.original_sha256
        proposal["corrected_sha256"] = verification.corrected_sha256
        proposal["verification"] = verification.to_dict()
        proposal["behavioral_status"] = "not_run"
        proposal["ci_status"] = "not_run"
        proposal["commit_execution"] = "requires_independent_runtime_gates"
        return proposal

    def verify_candidate_ci(self, ref: PullRequestRef, *, candidate_sha: str,
                            expected_pr_head_sha: str,
                            required_checks: tuple[str, ...],
                            expected_workflow_identity: Optional[str] = None,
                            workflow_run_branch: Optional[str] = None,
                            dispatched_after: Optional[float] = None) -> dict[str, Any]:
        """Verify named checks on a candidate SHA, never infer from the old PR head."""
        if candidate_sha == expected_pr_head_sha:
            return {"status": "unknown", "candidate_sha": candidate_sha,
                    "checks": [], "error_code": "pre_patch_pr_head_is_not_candidate_revision"}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected_pr_head_sha):
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "pr_head_changed_before_ci_verification"}
            fetch = getattr(self.read_client, "get_ci_evidence", None)
            if not callable(fetch):
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "github_ci_reader_unavailable"}
            if expected_workflow_identity is None:
                ci_fetch = lambda sha: fetch(ref, sha)
            else:
                ci_fetch = lambda sha: fetch(
                    ref, sha, expected_workflow_identity,
                    workflow_run_branch=workflow_run_branch, dispatched_after=dispatched_after,
                )
            ci_client = GitHubCIClient(ci_fetch)
            deadline = time.monotonic() + _candidate_ci_wait_seconds()
            while True:
                report = ci_client.verify(
                    repository=f"{ref.owner}/{ref.repository}", candidate_sha=candidate_sha,
                    required=required_checks,
                    expected_workflow_identity=expected_workflow_identity,
                )
                report_value = report.to_dict()
                checks = report_value.get("checks", [])
                awaiting = (report.status == "pending" or
                            (report.status == "unknown" and any(
                                isinstance(item, Mapping) and item.get("status") in {"missing", "pending"}
                                for item in checks)))
                remaining = deadline - time.monotonic()
                if not awaiting or remaining <= 0:
                    break
                time.sleep(min(1.0, remaining))
            try:
                latest = self.read_client.get_pull(ref)
            except Exception:
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "pr_freshness_recheck_failed"}
            if not _pull_matches(latest, ref, expected_pr_head_sha):
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "pr_head_changed_during_ci_verification"}
            value = report.to_dict()
            run_id = None
            if workflow_run_branch is not None or dispatched_after is not None:
                response = ci_fetch(candidate_sha)
                runs = response.get("workflow_runs") if isinstance(response, Mapping) else None
                exact_runs = [run for run in runs if isinstance(run, Mapping)
                              and run.get("workflow_id") == int(expected_workflow_identity or "0")
                              and run.get("head_sha") == candidate_sha
                              and run.get("event") == "workflow_dispatch"
                              and run.get("head_branch") == workflow_run_branch
                              and isinstance(dispatched_after, (int, float))
                              and run.get("display_title") == f"CodeMentor candidate verification {candidate_sha}"
                              and run.get("candidate_sha") == candidate_sha
                              and _workflow_run_created_after(run.get("created_at"), dispatched_after)] if isinstance(runs, list) else []
                if len(exact_runs) != 1:
                    return {**value, "status": "unknown", "workflow_identity": expected_workflow_identity,
                            "error_code": "candidate_workflow_run_missing_stale_or_ambiguous"}
                run_status = exact_runs[0].get("status")
                run_conclusion = exact_runs[0].get("conclusion")
                if run_status != "completed":
                    if run_status in {"queued", "in_progress", "requested", "pending"}:
                        return {**value, "status": "pending", "workflow_identity": expected_workflow_identity,
                                "workflow_run_id": exact_runs[0].get("id")}
                    return {**value, "status": "unknown", "workflow_identity": expected_workflow_identity,
                            "error_code": "candidate_workflow_run_status_invalid"}
                if run_conclusion != "success":
                    return {**value, "status": "failed" if run_conclusion in {
                        "failure", "timed_out", "cancelled", "action_required", "startup_failure"
                    } else "unknown", "workflow_identity": expected_workflow_identity,
                        "error_code": "candidate_workflow_run_not_successful"}
                run_id = exact_runs[0].get("id")
                if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
                    return {**value, "status": "unknown", "workflow_identity": expected_workflow_identity,
                            "error_code": "candidate_workflow_run_identity_invalid"}
            value["workflow_identity"] = expected_workflow_identity
            if run_id is not None:
                value["workflow_run_id"] = run_id
            return value
        except Exception as exc:
            return {"status": "unknown", "candidate_sha": candidate_sha,
                    "checks": [], "error_code": f"github_ci_{type(exc).__name__}"}

    def dispatch_candidate_ci(self, ref: PullRequestRef, *, candidate_sha: str,
                              candidate_branch: str,
                              workflow_identity: str) -> dict[str, Any]:
        """Dispatch the owner-configured workflow on the immutable candidate ref."""
        if not self.staging_enabled or not self.staging_token:
            return {"status": "blocked", "error_code": "candidate_staging_write_not_enabled"}
        if (not _full_sha(candidate_sha)
                or not isinstance(candidate_branch, str)
                or not re.fullmatch(r"codementor-candidates/[a-f0-9]{32,64}", candidate_branch)
                or not isinstance(workflow_identity, str)
                or not re.fullmatch(r"[0-9]{1,20}", workflow_identity)):
            return {"status": "blocked", "error_code": "candidate_workflow_identity_invalid"}
        try:
            ref_result = self._api_call(
                "GET", self._repo_path(ref) + "/git/ref/heads/" + _quote_branch(candidate_branch),
                token=self.staging_token,
            )
            staged_sha = ref_result.get("object", {}).get("sha") if isinstance(ref_result, dict) else None
            if staged_sha != candidate_sha:
                return {"status": "stale", "error_code": "candidate_ref_sha_mismatch"}
            repository_info = self.read_client.get_repository(ref.owner, ref.repository)
            workflow_run_branch = repository_info.get("default_branch") if isinstance(repository_info, dict) else None
            if not isinstance(workflow_run_branch, str) or not re.fullmatch(r"[A-Za-z0-9_.\-/]{1,200}", workflow_run_branch):
                return {"status": "blocked", "error_code": "trusted_workflow_default_branch_unavailable"}
            # Take the lower-bound timestamp immediately before dispatch so an
            # older same-SHA run cannot be mistaken for this dispatch.
            dispatch_started_at = time.time()
            self._api_call(
                "POST", self._repo_path(ref) + "/actions/workflows/"
                + urllib.parse.quote(workflow_identity, safe="") + "/dispatches",
                {"ref": workflow_run_branch, "inputs": {"candidate_sha": candidate_sha}},
                token=self.staging_token,
            )
            return {"status": "dispatched", "candidate_sha": candidate_sha,
                    "candidate_branch": candidate_branch, "workflow_identity": workflow_identity,
                    "workflow_run_branch": workflow_run_branch,
                    "dispatched_at": dispatch_started_at}
        except GitHubPRException as exc:
            status = "unknown" if exc.code in {"github_timeout", "github_network_error", "github_unavailable"} else "failed"
            return {"status": status, "candidate_sha": candidate_sha,
                    "candidate_branch": candidate_branch, "workflow_identity": workflow_identity,
                    "workflow_run_branch": locals().get("workflow_run_branch"),
                    "dispatched_at": locals().get("dispatch_started_at"),
                    "error_code": exc.code}
        except Exception as exc:
            return {"status": "unknown", "candidate_sha": candidate_sha,
                    "candidate_branch": candidate_branch, "workflow_identity": workflow_identity,
                    "workflow_run_branch": locals().get("workflow_run_branch"),
                    "dispatched_at": locals().get("dispatch_started_at"),
                    "error_code": f"workflow_dispatch_{type(exc).__name__}"}

    def prepare_commit(self, ref: PullRequestRef, *, repository: str, branch: Optional[str],
                       expected_head_sha: str, is_fork: bool, diff: str,
                       protected_branches: tuple[str, ...] = ("main", "master")) -> dict[str, Any]:
        try:
            pull = self.read_client.get_pull(ref)
        except Exception:
            pull = None
        current = pull.get("head", {}).get("sha") if isinstance(pull, dict) else None
        actual_branch = pull.get("head", {}).get("ref") if isinstance(pull, dict) else None
        head_repo = pull.get("head", {}).get("repo") if isinstance(pull, dict) else None
        head_owner = head_repo.get("owner", {}).get("login") if isinstance(head_repo, dict) and isinstance(head_repo.get("owner"), dict) else None
        head_name = head_repo.get("name") if isinstance(head_repo, dict) else None
        is_actual_fork = bool(
            isinstance(head_owner, str) and isinstance(head_name, str)
            and f"{head_owner}/{head_name}".casefold() != repository.casefold()
        )
        gate = commit_execution_decision(
            repository=repository, branch=branch, expected_head_sha=expected_head_sha,
            current_head_sha=current, is_fork=is_fork or is_actual_fork, protected_branches=protected_branches,
        )
        if not isinstance(actual_branch, str) or branch != actual_branch:
            gate["denial_reasons"].append("branch_identity_mismatch")
        if not isinstance(head_owner, str) or not isinstance(head_name, str):
            gate["denial_reasons"].append("head_repository_identity_unavailable")
        if not isinstance(diff, str) or not diff or len(diff.encode("utf-8")) > 64 * 1024:
            gate["denial_reasons"].append("invalid_or_oversized_prepared_diff")
        gate.update({
            "action_type": "prepare_ai_commit",
            "repository": repository,
            "pull_number": ref.number,
            "branch": branch,
            "commit_message": "AI Code Reviewer: proposed low-risk fix (not applied)",
            "commit_sha": None,
            "force_push": False,
        })
        return gate

    def create_candidate_revision(self, ref: PullRequestRef, *, repository: str, branch: str,
                                  expected_head_sha: str, file_path: str, original_code: str,
                                  corrected_code: str, operation_id: str) -> dict[str, Any]:
        """Build and publish a temporary candidate commit without moving the PR ref."""
        candidate_sha: Optional[str] = None
        candidate_branch = f"codementor-candidates/{operation_id}" if isinstance(operation_id, str) else None
        if not self.staging_enabled or not self.staging_token:
            return {"status": "blocked", "error_code": "candidate_staging_write_not_enabled"}
        if (f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold()
                or not _full_sha(expected_head_sha) or not _safe_changed_path(file_path)
                or not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32,64}", operation_id)
                or not isinstance(original_code, str) or not isinstance(corrected_code, str)):
            return {"status": "failed", "error_code": "candidate_identity_invalid"}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected_head_sha) or pull.get("head", {}).get("ref") != branch:
                return {"status": "stale", "error_code": "pr_head_changed_before_staging"}
            head_repo = pull.get("head", {}).get("repo", {})
            head_owner = head_repo.get("owner", {}).get("login") if isinstance(head_repo, dict) else None
            head_name = head_repo.get("name") if isinstance(head_repo, dict) else None
            write_context = self._write_repository_context(ref, self.staging_token)
            if (f"{head_owner}/{head_name}".casefold() != f"{ref.owner}/{ref.repository}".casefold()
                    or pull.get("base", {}).get("ref") == branch
                    or not write_context.get("push")
                    or branch == write_context.get("default_branch")):
                return {"status": "blocked", "error_code": "candidate_repository_or_branch_not_writable"}
            source = self.read_client.get_file(ref, file_path, expected_head_sha, max_source_bytes=256 * 1024)
            if source != original_code:
                return {"status": "stale", "error_code": "original_source_mismatch"}
            path = self._repo_path(ref)
            candidate_branch = f"codementor-candidates/{operation_id}"
            try:
                existing_ref = self._api_call(
                    "GET", path + "/git/ref/heads/" + _quote_branch(candidate_branch),
                    token=self.staging_token,
                )
            except GitHubPRException as exc:
                if exc.code != "github_not_found":
                    raise
                existing_ref = None
            if isinstance(existing_ref, dict):
                existing_sha = existing_ref.get("object", {}).get("sha")
                if not _full_sha(existing_sha):
                    return {"status": "unknown", "candidate_branch": candidate_branch,
                            "error_code": "existing_candidate_ref_invalid"}
                existing_commit = self._api_call(
                    "GET", path + "/git/commits/" + urllib.parse.quote(existing_sha, safe=""),
                    token=self.staging_token,
                )
                parents = existing_commit.get("parents") if isinstance(existing_commit, dict) else None
                parent_shas = [item.get("sha") for item in parents if isinstance(item, dict)] if isinstance(parents, list) else []
                existing_source = self.read_client.get_file(ref, file_path, existing_sha, max_source_bytes=256 * 1024)
                if parent_shas == [expected_head_sha] and existing_source == corrected_code:
                    return {"status": "created", "repository": repository, "candidate_sha": existing_sha,
                            "candidate_branch": candidate_branch, "parent_sha": expected_head_sha,
                            "path": file_path,
                            "source_sha256": hashlib.sha256(corrected_code.encode("utf-8")).hexdigest(),
                            "operation_id": operation_id, "idempotent_replay": True}
                return {"status": "unknown", "candidate_sha": existing_sha,
                        "candidate_branch": candidate_branch, "error_code": "candidate_ref_conflict"}
            blob = self._api_call("POST", path + "/git/blobs",
                                  {"content": corrected_code, "encoding": "utf-8"}, token=self.staging_token)
            blob_sha = blob.get("sha") if isinstance(blob, dict) else None
            if not _full_sha(blob_sha):
                return {"status": "failed", "error_code": "candidate_blob_invalid"}
            parent_commit = self._api_call("GET", path + "/git/commits/" + urllib.parse.quote(expected_head_sha, safe=""),
                                           token=self.staging_token)
            base_tree = parent_commit.get("tree", {}).get("sha") if isinstance(parent_commit, dict) else None
            if not _full_sha(base_tree):
                return {"status": "failed", "error_code": "candidate_parent_tree_invalid"}
            tree = self._api_call("POST", path + "/git/trees", {
                "base_tree": base_tree,
                "tree": [{"path": file_path, "mode": "100644", "type": "blob", "sha": blob_sha}],
            }, token=self.staging_token)
            tree_sha = tree.get("sha") if isinstance(tree, dict) else None
            entries = tree.get("tree") if isinstance(tree, dict) else None
            if not _full_sha(tree_sha) or not isinstance(entries, list) or not any(
                    isinstance(item, dict) and item.get("path") == file_path and item.get("sha") == blob_sha
                    for item in entries):
                return {"status": "failed", "error_code": "candidate_tree_invalid"}
            commit = self._api_call("POST", path + "/git/commits", {
                "message": f"CodeMentor candidate {operation_id}", "tree": tree_sha,
                "parents": [expected_head_sha],
            }, token=self.staging_token)
            candidate_sha = commit.get("sha") if isinstance(commit, dict) else None
            parents = commit.get("parents") if isinstance(commit, dict) else None
            if (not _full_sha(candidate_sha) or not isinstance(parents, list)
                    or [item.get("sha") for item in parents if isinstance(item, dict)] != [expected_head_sha]):
                return {"status": "failed", "error_code": "candidate_commit_invalid"}
            candidate_ref = self._api_call("POST", path + "/git/refs", {
                "ref": "refs/heads/" + candidate_branch, "sha": candidate_sha,
            }, token=self.staging_token)
            created_sha = candidate_ref.get("object", {}).get("sha") if isinstance(candidate_ref, dict) else None
            if created_sha != candidate_sha:
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "candidate_branch": candidate_branch, "error_code": "candidate_ref_write_unconfirmed"}
            return {"status": "created", "repository": repository, "candidate_sha": candidate_sha,
                    "candidate_branch": candidate_branch, "parent_sha": expected_head_sha,
                    "tree_sha": tree_sha, "blob_sha": blob_sha, "path": file_path,
                    "source_sha256": hashlib.sha256(corrected_code.encode("utf-8")).hexdigest(),
                    "operation_id": operation_id}
        except GitHubPRException as exc:
            status = "unknown" if exc.code in {"github_timeout", "github_network_error", "github_unavailable"} else "failed"
            return {"status": status, "error_code": exc.code,
                    "candidate_sha": candidate_sha, "candidate_branch": candidate_branch,
                    "parent_sha": expected_head_sha, "operation_id": operation_id}
        except Exception as exc:
            return {"status": "unknown", "error_code": f"candidate_{type(exc).__name__}",
                    "candidate_sha": candidate_sha, "candidate_branch": candidate_branch,
                    "parent_sha": expected_head_sha, "operation_id": operation_id}

    def reconcile_candidate_revision(self, ref: PullRequestRef, *, repository: str,
                                     operation_id: str, expected_candidate_sha: Optional[str]) -> dict[str, Any]:
        """Observe an uncertain temporary-ref write; never delete or retry it."""
        if (f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold()
                or not isinstance(operation_id, str)
                or not re.fullmatch(r"[a-f0-9]{32,64}", operation_id)):
            return {"status": "unknown", "error_code": "candidate_reconciliation_identity_invalid"}
        if not self.staging_token:
            return {"status": "unknown", "error_code": "candidate_reconciliation_token_unavailable"}
        candidate_branch = f"codementor-candidates/{operation_id}"
        try:
            result = self._api_call("GET", self._repo_path(ref) + "/git/ref/heads/" + _quote_branch(candidate_branch),
                                    token=self.staging_token)
            sha = result.get("object", {}).get("sha") if isinstance(result, dict) else None
            if not _full_sha(sha):
                return {"status": "unknown", "error_code": "candidate_ref_response_invalid"}
            if expected_candidate_sha and sha != expected_candidate_sha:
                return {"status": "unknown", "error_code": "candidate_ref_sha_mismatch"}
            return {"status": "candidate_staged", "candidate_branch": candidate_branch,
                    "candidate_sha": sha, "cleanup": "not_attempted"}
        except GitHubPRException as exc:
            if exc.code == "github_not_found":
                return {"status": "not_staged", "retry_safe": True}
            return {"status": "unknown", "error_code": exc.code}
        except Exception as exc:
            return {"status": "unknown", "error_code": f"candidate_reconciliation_{type(exc).__name__}"}

    def reconcile_commit(self, ref: PullRequestRef, *, repository: str, branch: str,
                         expected_head_sha: str, candidate_sha: str) -> dict[str, Any]:
        """Read remote refs after an ambiguous final update; never write/retry."""
        if (f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold()
                or not _full_sha(expected_head_sha) or not _full_sha(candidate_sha)
                or not isinstance(branch, str)):
            return {"status": "unknown", "error_code": "commit_reconciliation_identity_invalid"}
        if not self.commit_token:
            return {"status": "unknown", "error_code": "commit_reconciliation_token_unavailable"}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, None) or pull.get("head", {}).get("ref") != branch:
                return {"status": "stale", "error_code": "pull_or_branch_identity_changed"}
            current = self._api_call("GET", self._repo_path(ref) + "/git/ref/heads/" + _quote_branch(branch),
                                     token=self.commit_token)
            current_sha = current.get("object", {}).get("sha") if isinstance(current, dict) else None
            head_sha = pull.get("head", {}).get("sha")
            if current_sha == candidate_sha and head_sha == candidate_sha:
                return {"status": "published", "commit_sha": candidate_sha}
            if current_sha == expected_head_sha and head_sha == expected_head_sha:
                return {"status": "not_published", "retry_safe": True,
                        "error_code": "remote_ref_still_at_expected_head"}
            return {"status": "unknown", "error_code": "remote_commit_state_conflicted"}
        except GitHubPRException as exc:
            return {"status": "unknown", "error_code": exc.code}
        except Exception as exc:
            return {"status": "unknown", "error_code": f"commit_reconciliation_{type(exc).__name__}"}

    def execute_commit(self, *_args, **kwargs) -> GitHubActionResult:
        """Finalize an exact candidate only after all independent gates pass.

        Ref updates have no general compare-and-swap field. The candidate is
        required to have the captured head as its only parent, the PR and ref
        are checked immediately before PATCH, and `force:false` makes a race
        with a new PR commit fail as non-fast-forward rather than overwrite it.
        """
        ref, repository, branch = kwargs.get("ref"), kwargs.get("repository"), kwargs.get("branch")
        expected, candidate_sha = kwargs.get("expected_head_sha"), kwargs.get("candidate_sha")
        pr_number = ref.number if isinstance(ref, PullRequestRef) else 0
        if not self.commit_enabled or not self.commit_token:
            return self._commit_result("AUTONOMOUS_COMMIT_DISABLED", repository, pr_number,
                                       "final_commit_write_not_enabled")
        gates = kwargs.get("gates")
        required_gates = {"repository_opt_in", "eligible_low_risk_category", "calibration_supported",
                          "source_hashes_valid", "syntax_passed", "static_passed", "behavioral_passed",
                          "candidate_ci_passed", "scope_valid", "write_permission_valid",
                          "durable_record_reserved", "idempotency_reserved"}
        if (not isinstance(ref, PullRequestRef) or not isinstance(gates, Mapping)
                or any(gates.get(key) is not True for key in required_gates)):
            return self._commit_result("blocked", repository, pr_number, "required_gate_not_passed")
        operation_id, file_path = kwargs.get("operation_id"), kwargs.get("file_path")
        corrected_code, is_fork = kwargs.get("corrected_code"), kwargs.get("is_fork", False)
        if (not _full_sha(expected) or not _full_sha(candidate_sha)
                or not isinstance(operation_id, str) or not re.fullmatch(r"[a-f0-9]{32,64}", operation_id)
                or not isinstance(file_path, str) or not _safe_changed_path(file_path)
                or not isinstance(corrected_code, str) or not isinstance(branch, str) or is_fork is not False):
            return self._commit_result("blocked", repository, pr_number, "commit_identity_invalid")
        if (not _verified_behavioral_evidence(kwargs.get("behavioral_evidence"), corrected_code)
                or not _verified_ci_evidence(kwargs.get("ci_evidence"), candidate_sha,
                                             kwargs.get("required_checks", ()))
                or not _verified_confidence_evidence(kwargs.get("confidence_evidence"),
                                                      kwargs.get("confidence_threshold", 0.80))):
            return self._commit_result("blocked", repository, ref.number, "verification_evidence_invalid")
        confidence_evidence = kwargs["confidence_evidence"]
        decision_gates = dict(gates)
        decision_gates.update({
            "confidence": confidence_evidence["calibrated_confidence"],
            "changed_lines": kwargs.get("changed_lines"),
            "candidate_sha": candidate_sha,
            "ci_sha": kwargs["ci_evidence"]["candidate_sha"],
            "unexpected_files": kwargs.get("unexpected_files", []),
        })
        decision = commit_execution_decision(
            repository=repository, branch=branch, expected_head_sha=expected,
            current_head_sha=expected, is_fork=False,
            protected_branches=tuple(kwargs.get("protected_branches", ("main", "master"))),
            gates=decision_gates, execution_authorized=True,
        )
        if decision.get("eligible") is not True:
            return self._commit_result("blocked", repository, ref.number, "commit_policy_denied")
        if (not isinstance(kwargs.get("changed_lines"), int) or isinstance(kwargs.get("changed_lines"), bool)
                or not 1 <= kwargs["changed_lines"] <= 3
                or not isinstance(kwargs.get("permitted_branches"), (list, tuple))
                or branch not in kwargs["permitted_branches"]
                or kwargs.get("unexpected_files", []) not in ([], ())):
            return self._commit_result("blocked", repository, ref.number, "commit_scope_or_line_limit_invalid")
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected):
                if (pull.get("head", {}).get("sha") == candidate_sha
                        and pull.get("head", {}).get("ref") == branch):
                    current_ref = self._api_call(
                        "GET", self._repo_path(ref) + "/git/ref/heads/" + _quote_branch(branch),
                        token=self.commit_token,
                    )
                    if isinstance(current_ref, dict) and current_ref.get("object", {}).get("sha") == candidate_sha:
                        return GitHubActionResult("published", "commit", repository, ref.number,
                                                  commit_sha=candidate_sha,
                                                  detail={"operation_id": operation_id, "idempotent_replay": True})
                return self._commit_result("stale", repository, ref.number, "pr_head_changed")
            head, base = pull.get("head", {}), pull.get("base", {})
            if head.get("ref") != branch or base.get("ref") == branch:
                return self._commit_result("blocked", repository, ref.number, "branch_identity_or_base_branch_invalid")
            write_context = self._write_repository_context(ref, self.commit_token)
            if not write_context.get("push") or branch == write_context.get("default_branch"):
                return self._commit_result("blocked", repository, ref.number, "repository_write_permission_unverified")
            head_repo = head.get("repo", {})
            head_owner = head_repo.get("owner", {}).get("login") if isinstance(head_repo, dict) else None
            head_name = head_repo.get("name") if isinstance(head_repo, dict) else None
            if f"{head_owner}/{head_name}".casefold() != f"{ref.owner}/{ref.repository}".casefold():
                return self._commit_result("blocked", repository, ref.number, "fork_write_not_authorized")
            candidate_source = self.read_client.get_file(ref, file_path, candidate_sha,
                                                         max_source_bytes=256 * 1024)
            if candidate_source != corrected_code:
                return self._commit_result("blocked", repository, ref.number, "candidate_content_mismatch")
            repo_path = self._repo_path(ref)
            commit = self._api_call("GET", repo_path + "/git/commits/" + urllib.parse.quote(candidate_sha, safe=""),
                                    token=self.commit_token)
            parents = commit.get("parents") if isinstance(commit, dict) else None
            parent_shas = [item.get("sha") for item in parents if isinstance(item, dict)] if isinstance(parents, list) else []
            if parent_shas != [expected]:
                return self._commit_result("blocked", repository, ref.number, "candidate_parent_mismatch")
            latest = self.read_client.get_pull(ref)
            if not _pull_matches(latest, ref, expected) or latest.get("head", {}).get("ref") != branch:
                return self._commit_result("stale", repository, ref.number, "pr_head_changed_before_finalize")
            encoded_branch = _quote_branch(branch)
            current_ref = self._api_call("GET", repo_path + "/git/ref/heads/" + encoded_branch,
                                         token=self.commit_token)
            current_sha = current_ref.get("object", {}).get("sha") if isinstance(current_ref, dict) else None
            if current_sha == candidate_sha:
                return GitHubActionResult("published", "commit", repository, ref.number,
                                          commit_sha=candidate_sha,
                                          detail={"operation_id": operation_id, "idempotent_replay": True})
            if current_sha != expected:
                return self._commit_result("stale", repository, ref.number, "branch_ref_changed")
            updated = self._api_call("PATCH", repo_path + "/git/refs/heads/" + encoded_branch,
                                     {"sha": candidate_sha, "force": False}, token=self.commit_token)
            updated_sha = updated.get("object", {}).get("sha") if isinstance(updated, dict) else None
            if updated_sha != candidate_sha:
                return self._commit_result("unknown", repository, ref.number, "commit_write_response_unverifiable")
            return GitHubActionResult("published", "commit", repository, ref.number,
                                      commit_sha=candidate_sha,
                                      detail={"operation_id": operation_id, "force_push": False})
        except GitHubPRException as exc:
            status = ("unknown" if exc.code in {"github_timeout", "github_network_error", "github_unavailable"}
                      else "stale" if exc.code == "github_non_fast_forward" else "failed")
            return self._commit_result(status, repository, ref.number, exc.code)
        except Exception as exc:
            return self._commit_result("unknown", repository, ref.number, f"commit_{type(exc).__name__}")

    def _api_call(self, method: str, path: str, payload: Optional[Mapping[str, Any]] = None,
                  *, token: Optional[str]) -> Any:
        body = json.dumps(payload or {}, separators=(",", ":")).encode("utf-8") if method != "GET" else b""
        return self.transport(method, API_BASE + path, self._headers(token=token), body)

    def _write_repository_context(self, ref: PullRequestRef, token: Optional[str]) -> dict[str, Any]:
        if not token:
            return {"push": False}
        try:
            value = self._api_call("GET", self._repo_path(ref), token=token)
        except Exception:
            return {"push": False}
        full_name = value.get("full_name") if isinstance(value, dict) else None
        if not isinstance(full_name, str) or full_name.casefold() != f"{ref.owner}/{ref.repository}".casefold():
            return {"push": False}
        permissions = value.get("permissions")
        return {"push": isinstance(permissions, Mapping) and permissions.get("push") is True,
                "default_branch": value.get("default_branch")}

    @staticmethod
    def _commit_result(status: str, repository: Any, pr_number: int, error_code: str) -> GitHubActionResult:
        return GitHubActionResult(status, "commit", str(repository or ""), pr_number,
                                  error_code=error_code, detail={"force_push": False})

    def _revision_current(self, ref: PullRequestRef, expected_head_sha: str) -> bool:
        try:
            return _pull_matches(self.read_client.get_pull(ref), ref, expected_head_sha)
        except Exception:
            return False

    def _current_head(self, ref: PullRequestRef) -> Optional[str]:
        try:
            value = self.read_client.get_pull(ref)
            if _pull_matches(value, ref, None):
                return value.get("head", {}).get("sha")
        except Exception:
            return None
        return None

    @staticmethod
    def _repo_path(ref: PullRequestRef) -> str:
        return f"/repos/{urllib.parse.quote(ref.owner, safe='')}/{urllib.parse.quote(ref.repository, safe='')}"

    def _headers(self, *, token: Optional[str] = None) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "User-Agent": "AI-Code-Reviewer-MCP-Actions", "X-GitHub-Api-Version": API_VERSION,
        }
        credential = token if token is not None else self.token
        if credential:
            headers["Authorization"] = f"Bearer {credential}"
        return headers

    def _request(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if (method not in {"POST", "GET", "PATCH"} or parsed.scheme != "https" or parsed.netloc != "api.github.com"
                or not parsed.path.startswith("/repos/") or (method in {"POST", "PATCH"} and "Authorization" not in headers)):
            raise GitHubPRException("github_action_not_authorized", 403, "GitHub action is not authorized.")
        request = urllib.request.Request(url, data=body if method != "GET" else None,
                                         headers=dict(headers), method=method)
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=8) as response:
                raw = response.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise GitHubPRException("github_action_response_too_large", 502, "GitHub returned an oversized action response.")
                if not raw:
                    return {}
                try:
                    return json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GitHubPRException("github_action_response_invalid", 502, "GitHub returned an invalid action response.") from exc
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                code = "github_rate_limited" if exc.headers.get("X-RateLimit-Remaining") == "0" else "github_forbidden"
            elif exc.code == 404:
                code = "github_not_found"
            elif exc.code == 422:
                try:
                    error_text = exc.read(4096).decode("utf-8", errors="replace").casefold()
                except Exception:
                    error_text = ""
                code = "github_non_fast_forward" if "not a fast forward" in error_text else "github_action_rejected"
            elif exc.code >= 500:
                code = "github_unavailable"
            else:
                code = "github_action_failed"
            raise GitHubPRException(code, exc.code, "GitHub rejected the review action.") from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            raise GitHubPRException("github_timeout", 504, "GitHub action completion is unknown.") from exc

    @staticmethod
    def _result(status: str, ref: PullRequestRef, repository: str,
                finding_id: Optional[str], error_code: str) -> GitHubActionResult:
        return GitHubActionResult(status, "review_comment", repository, ref.number,
                                  finding_id=finding_id, error_code=error_code)


def _pull_matches(pull: Any, ref: PullRequestRef, expected_head_sha: Optional[str]) -> bool:
    if not isinstance(pull, dict) or pull.get("number") != ref.number or pull.get("state") != "open":
        return False
    base = pull.get("base") if isinstance(pull.get("base"), dict) else {}
    base_repo = base.get("repo") if isinstance(base.get("repo"), dict) else {}
    owner_data = base_repo.get("owner") if isinstance(base_repo.get("owner"), dict) else {}
    owner, name = owner_data.get("login"), base_repo.get("name")
    if not isinstance(owner, str) or not isinstance(name, str):
        return False
    if owner.casefold() != ref.owner.casefold():
        return False
    if name.casefold() != ref.repository.casefold():
        return False
    head_sha = pull.get("head", {}).get("sha") if isinstance(pull.get("head"), dict) else None
    return isinstance(head_sha, str) and (expected_head_sha is None or head_sha == expected_head_sha)


def _safe_changed_path(path: Any) -> bool:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        return False
    return all(part not in {"", ".", ".."} and not any(ord(char) < 32 for char in part)
               for part in path.split("/"))


def _valid_finding(finding: Mapping[str, Any]) -> bool:
    if (not finding.get("finding_id") or finding.get("origin") not in {"llm", "static"}
            or finding.get("uncertain") or finding.get("abstained")):
        return False
    if finding.get("origin") == "static" and not finding.get("rule_id"):
        return False
    if finding.get("origin") == "llm":
        evidence_ids = finding.get("evidence_ids")
        if not isinstance(evidence_ids, (list, tuple)) or not evidence_ids or any(not isinstance(value, str) or not value for value in evidence_ids):
            return False
    return all(isinstance(finding.get(key), str) for key in ("title", "explanation", "recommended_fix"))


def _comment_body(finding: Mapping[str, Any], *, operation_id: Optional[str] = None) -> str:
    def safe(value: Any, limit: int) -> str:
        text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or ""))
        text = re.sub(r"https?://\S+", "[link omitted]", text, flags=re.IGNORECASE)
        text = text.replace("<", "&lt;").replace(">", "&gt;")
        return text[:limit]
    title = safe(finding.get("title"), 300)
    explanation = safe(finding.get("explanation"), 3000)
    recommendation = safe(finding.get("recommended_fix"), 2000)
    origin = "Static Analysis" if finding.get("origin") == "static" else "AI/RAG"
    rule_id = finding.get("rule_id") if isinstance(finding.get("rule_id"), str) else None
    evidence = finding.get("evidence_ids", [])
    ids = [value for value in evidence if isinstance(value, str)][:8] if isinstance(evidence, (list, tuple)) else []
    lines = [f"### {title}", f"**Generated by AI Code Reviewer ({origin}).**"]
    if rule_id:
        lines.append(f"Rule: `{safe(rule_id, 60)}`")
    lines.extend(["", explanation, "", f"**Recommended action:** {recommendation}"])
    if ids:
        lines.extend(["", "Evidence references: " + ", ".join(f"`{safe(item, 80)}`" for item in ids)])
    if isinstance(operation_id, str) and re.fullmatch(r"[a-f0-9]{32,64}", operation_id):
        lines.append(f"\n<!-- ai-code-review-action:{operation_id} -->")
    return "\n".join(lines)[:6000]


def _explicit_env_enabled(name: str) -> bool:
    return os.getenv(name, "").strip().casefold() in {"1", "true", "yes"}


def _full_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-fA-F]{40,64}", value))


def _quote_branch(branch: str) -> str:
    if (not isinstance(branch, str) or not branch or branch.startswith("/")
            or branch.endswith("/") or ".." in branch.split("/")
            or any(part in {"", "."} for part in branch.split("/"))
            or any(ord(char) < 32 for char in branch)):
        raise ValueError("branch_name_invalid")
    return "/".join(urllib.parse.quote(part, safe="") for part in branch.split("/"))


def _repo_write_permission(read_client: Any, ref: PullRequestRef) -> bool:
    getter = getattr(read_client, "get_repository", None)
    if not callable(getter):
        return False
    try:
        repository = getter(ref.owner, ref.repository)
    except Exception:
        return False
    if not isinstance(repository, Mapping):
        return False
    actual = repository.get("full_name")
    permissions = repository.get("permissions")
    return (isinstance(actual, str) and actual.casefold() == f"{ref.owner}/{ref.repository}".casefold()
            and isinstance(permissions, Mapping) and permissions.get("push") is True)


def _verified_behavioral_evidence(evidence: Any, corrected_code: str) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    expected = hashlib.sha256(corrected_code.encode("utf-8")).hexdigest()
    return (evidence.get("status") == "passed" and evidence.get("candidate_sha256") == expected
            and evidence.get("executed") is True and evidence.get("exit_code") == 0
            and isinstance(evidence.get("test_identity"), str) and bool(evidence["test_identity"])
            and isinstance(evidence.get("verifier_id"), str) and bool(evidence["verifier_id"])
            and evidence.get("attestation_verified") is True)


def _verified_ci_evidence(evidence: Any, candidate_sha: str, required_checks: Any) -> bool:
    if not isinstance(evidence, Mapping) or evidence.get("status") != "passed" or evidence.get("candidate_sha") != candidate_sha:
        return False
    required = tuple(item for item in required_checks if isinstance(item, str)) if isinstance(required_checks, (tuple, list)) else ()
    checks = evidence.get("checks")
    if not required or not isinstance(checks, list):
        return False
    by_name = {item.get("name"): item for item in checks if isinstance(item, Mapping)}
    return all(name in by_name and by_name[name].get("status") == "success"
               and by_name[name].get("head_sha") == candidate_sha
               and by_name[name].get("workflow_identity") == evidence.get("workflow_identity")
               for name in required) and isinstance(evidence.get("workflow_identity"), str)


def _verified_confidence_evidence(evidence: Any, threshold: Any) -> bool:
    if not isinstance(evidence, Mapping):
        return False
    confidence = evidence.get("calibrated_confidence")
    return (evidence.get("calibrated") is True and evidence.get("manifest_verified") is True
            and isinstance(evidence.get("model_id"), str) and isinstance(evidence.get("dataset_id"), str)
            and isinstance(evidence.get("finding_category"), str)
            and isinstance(evidence.get("artifact_sha256"), str)
            and isinstance(confidence, (int, float)) and not isinstance(confidence, bool)
            and isinstance(threshold, (int, float)) and not isinstance(threshold, bool)
            and float(confidence) >= max(0.80, float(threshold)))


def _candidate_ci_wait_seconds() -> float:
    """Bounded wait for a dispatched candidate workflow; zero disables polling."""
    try:
        configured = float(os.getenv("GITHUB_CANDIDATE_CI_WAIT_SECONDS", "60"))
    except (TypeError, ValueError):
        configured = 60.0
    if not (configured >= 0 and configured < float("inf")):
        configured = 60.0
    return min(configured, 180.0)
