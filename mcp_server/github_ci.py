"""Read-only GitHub CI status verification pinned to one exact commit SHA."""

from __future__ import annotations

from dataclasses import asdict, dataclass
import re
from typing import Any, Callable, Mapping, Optional


CI_STATES = frozenset({"success", "failure", "pending", "skipped", "missing", "unknown"})
_SHA = re.compile(r"^[0-9a-fA-F]{40,64}$")


@dataclass(frozen=True)
class CheckResult:
    name: str
    status: str
    conclusion: Optional[str]
    head_sha: Optional[str]
    source: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class CIReport:
    status: str
    candidate_sha: str
    checks: tuple[CheckResult, ...]
    evidence: tuple[dict[str, Any], ...] = ()
    error_code: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {"status": self.status, "candidate_sha": self.candidate_sha,
                "checks": [item.to_dict() for item in self.checks],
                "evidence": list(self.evidence), "error_code": self.error_code}


def evaluate_required_checks(*, candidate_sha: str, required: tuple[str, ...],
                             check_runs: Any, statuses: Any,
                             response_sha: Optional[str]) -> CIReport:
    """Map GitHub check runs/statuses; missing, stale, skipped all fail closed."""
    if not isinstance(candidate_sha, str) or not _SHA.fullmatch(candidate_sha) or response_sha != candidate_sha:
        return CIReport("unknown", candidate_sha, (), error_code="candidate_sha_mismatch")
    names = tuple(dict.fromkeys(name.strip() for name in required if isinstance(name, str) and name.strip()))
    if not names or not isinstance(check_runs, list) or not isinstance(statuses, list):
        return CIReport("unknown", candidate_sha, (), error_code="ci_response_invalid")
    normalized: dict[str, list[CheckResult]] = {}
    for item in check_runs:
        if not isinstance(item, Mapping):
            continue
        name, sha = item.get("name"), item.get("head_sha")
        if not isinstance(name, str) or not isinstance(sha, str):
            continue
        state, conclusion = _check_run_state(item.get("status"), item.get("conclusion"))
        normalized.setdefault(name, []).append(CheckResult(name, state, conclusion, sha, "check_run"))
    for item in statuses:
        if not isinstance(item, Mapping):
            continue
        name, sha = item.get("context"), item.get("sha")
        if not isinstance(name, str) or not isinstance(sha, str):
            continue
        state = _status_state(item.get("state"))
        normalized.setdefault(name, []).append(CheckResult(name, state, item.get("state"), sha, "commit_status"))
    checks: list[CheckResult] = []
    for required_name in names:
        candidates = normalized.get(required_name, [])
        exact = [row for row in candidates if row.head_sha == candidate_sha]
        stale_only = bool(candidates) and not exact
        if not exact:
            checks.append(CheckResult(required_name, "unknown" if stale_only else "missing",
                                      None, candidates[-1].head_sha if candidates else None,
                                      "stale" if stale_only else "none"))
        else:
            # Multiple exact records must all be successful; never hide a failure.
            selected = exact[-1]
            state = "failure" if any(row.status == "failure" for row in exact) else selected.status
            if any(row.status in {"unknown", "missing"} for row in exact) and state == "success":
                state = "unknown"
            checks.append(CheckResult(required_name, state, selected.conclusion,
                                      candidate_sha, selected.source))
    final = "passed" if all(row.status == "success" for row in checks) else (
        "failed" if any(row.status == "failure" for row in checks) else
        "pending" if any(row.status == "pending" for row in checks) else "unknown"
    )
    return CIReport(final, candidate_sha, tuple(checks),
                    tuple({"name": row.name, "status": row.status,
                           "head_sha": row.head_sha, "source": row.source} for row in checks))


class GitHubCIClient:
    """Narrow read-only facade. Transport is injectable; no write capability exists."""

    def __init__(self, fetch: Callable[[str], Mapping[str, Any]]) -> None:
        self.fetch = fetch

    def verify(self, *, repository: str, candidate_sha: str,
               required: tuple[str, ...]) -> CIReport:
        if not isinstance(repository, str) or repository.count("/") != 1 or any(
                not part for part in repository.split("/")):
            return CIReport("unknown", candidate_sha if isinstance(candidate_sha, str) else "",
                            (), error_code="repository_identity_invalid")
        if not isinstance(candidate_sha, str) or not _SHA.fullmatch(candidate_sha):
            return CIReport("unknown", str(candidate_sha), (), error_code="candidate_sha_invalid")
        try:
            result = self.fetch(candidate_sha)
        except Exception as exc:
            return CIReport("unknown", candidate_sha, (),
                            error_code=f"github_ci_{type(exc).__name__}")
        if not isinstance(result, Mapping):
            return CIReport("unknown", candidate_sha, (), error_code="ci_response_invalid")
        return evaluate_required_checks(
            candidate_sha=candidate_sha, required=required,
            check_runs=result.get("check_runs"), statuses=result.get("statuses"),
            response_sha=result.get("head_sha"),
        )


def _check_run_state(status: Any, conclusion: Any) -> tuple[str, Optional[str]]:
    status = status.casefold() if isinstance(status, str) else ""
    conclusion = conclusion.casefold() if isinstance(conclusion, str) else None
    if status not in {"completed", "queued", "in_progress", "requested", "pending"}:
        return "unknown", conclusion
    if status != "completed":
        return "pending", conclusion
    if conclusion == "success":
        return "success", conclusion
    if conclusion in {"skipped", "neutral"}:
        return "skipped", conclusion
    if conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}:
        return "failure", conclusion
    return "unknown", conclusion


def _status_state(value: Any) -> str:
    if value == "success":
        return "success"
    if value == "failure":
        return "failure"
    if value == "pending":
        return "pending"
    return "unknown"
