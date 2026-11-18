"""Narrow, policy-gated GitHub action adapter for the existing webhook flow.

All write routes are fixed and inputs are constructed from validated PR data.
The adapter exposes no arbitrary URL, path, branch, or request operation.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.request
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

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
    """MCP-integrated capability boundary; only fixed review-comment writes exist."""

    def __init__(self, *, read_client: Any, token: Optional[str] = None,
                 transport: Optional[Callable[[str, str, Mapping[str, str], bytes], Any]] = None) -> None:
        self.read_client = read_client
        self.token = token
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
            status = "unknown" if exc.code in {"github_timeout", "github_network_error"} else "failed"
            return self._result(status, ref, repository, finding_id, exc.code)
        github_id = response.get("id") if isinstance(response, dict) and isinstance(response.get("id"), int) else None
        if github_id is None:
            return self._result("unknown", ref, repository, finding_id, "github_write_response_unverifiable")
        return GitHubActionResult("published", "review_comment", repository, ref.number,
                                  finding_id, github_id=github_id)

    def reconcile_inline_comment(self, ref: PullRequestRef, *, repository: str,
                                 expected_head_sha: str, operation_id: str) -> dict[str, Any]:
        """Check remote state by durable operation marker before any retry."""
        if not re.fullmatch(r"[a-f0-9]{32,64}", operation_id or ""):
            return {"status": "unknown", "error_code": "operation_id_invalid"}
        if f"{ref.owner}/{ref.repository}".casefold() != str(repository).casefold():
            return {"status": "unknown", "error_code": "repository_identity_mismatch"}
        try:
            pull = self.read_client.get_pull(ref)
            if not _pull_matches(pull, ref, expected_head_sha):
                return {"status": "stale", "error_code": "expected_revision_changed"}
            endpoint = f"{self._repo_path(ref)}/pulls/{ref.number}/comments"
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
                       and item.get("commit_id") == expected_head_sha]
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
            "commit_execution": "AUTONOMOUS_COMMIT_DISABLED",
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
        proposal["commit_execution"] = "AUTONOMOUS_COMMIT_DISABLED"
        return proposal

    def verify_candidate_ci(self, ref: PullRequestRef, *, candidate_sha: str,
                            expected_pr_head_sha: str,
                            required_checks: tuple[str, ...]) -> dict[str, Any]:
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
            report = GitHubCIClient(lambda sha: fetch(ref, sha)).verify(
                repository=repository, candidate_sha=candidate_sha,
                required=required_checks,
            )
            try:
                latest = self.read_client.get_pull(ref)
            except Exception:
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "pr_freshness_recheck_failed"}
            if not _pull_matches(latest, ref, expected_pr_head_sha):
                return {"status": "unknown", "candidate_sha": candidate_sha,
                        "checks": [], "error_code": "pr_head_changed_during_ci_verification"}
            return report.to_dict()
        except Exception as exc:
            return {"status": "unknown", "candidate_sha": candidate_sha,
                    "checks": [], "error_code": f"github_ci_{type(exc).__name__}"}

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

    def execute_commit(self, *_args, **_kwargs) -> GitHubActionResult:
        """Hard-disabled execution gate. J2 intentionally cannot create commits."""
        return GitHubActionResult(
            "AUTONOMOUS_COMMIT_DISABLED", "commit", "", 0,
            error_code="commit_execution_disabled_in_j2",
            detail={"execution_enabled": False, "force_push": False},
        )

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

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json", "Content-Type": "application/json",
            "User-Agent": "AI-Code-Reviewer-MCP-Actions", "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _request(self, method: str, url: str, headers: Mapping[str, str], body: bytes) -> Any:
        parsed = urllib.parse.urlsplit(url)
        if (method not in {"POST", "GET"} or parsed.scheme != "https" or parsed.netloc != "api.github.com"
                or not parsed.path.startswith("/repos/") or (method == "POST" and not self.token)):
            raise GitHubPRException("github_action_not_authorized", 403, "GitHub action is not authorized.")
        request = urllib.request.Request(url, data=body if method == "POST" else None,
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
                code = "github_action_rejected"
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
