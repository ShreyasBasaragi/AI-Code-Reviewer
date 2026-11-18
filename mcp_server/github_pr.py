"""Bounded, read-only GitHub pull-request review support."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from review_schema import ReviewFinding, ReviewResponse
from mcp_server.repository_context import (
    RepositoryContextLimits,
    RepositoryContextResult,
    collect_repository_context,
)
from static_analysis import analyze_python, static_fingerprint_counts

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"
API_VERSION = "2022-11-28"
_PR_URL = re.compile(r"^https://github\.com/([^/]+)/([^/]+)/pull/([1-9][0-9]*)/?$")
_REPO_PART = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


class GitHubPRException(Exception):
    def __init__(self, code: str, status_code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True)
class PullRequestRef:
    owner: str
    repository: str
    number: int


def parse_pull_request_url(value: str) -> PullRequestRef:
    if not isinstance(value, str) or len(value) > 2048:
        raise GitHubPRException("invalid_pr_url", 422, "Enter a valid GitHub pull request URL.")
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise GitHubPRException("invalid_pr_url", 422, "Enter a valid GitHub pull request URL.") from exc
    match = _PR_URL.fullmatch(value)
    try:
        port = parsed.port
    except ValueError:
        port = -1
    if (
        not match
        or parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
        or "%" in parsed.path
        or "\\" in parsed.path
    ):
        raise GitHubPRException("invalid_pr_url", 422, "Enter a canonical https://github.com/owner/repository/pull/number URL.")
    owner, repository, number_text = match.groups()
    if (
        owner in {".", ".."}
        or repository in {".", ".."}
        or repository.endswith(".git")
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", owner)
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", repository)
    ):
        raise GitHubPRException("invalid_pr_url", 422, "The pull request URL contains an invalid repository path.")
    return PullRequestRef(owner, repository, int(number_text))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GitHubRESTClient:
    """REST GET adapter which never follows redirects away from the fixed API host."""

    def __init__(self, token: Optional[str] = None, *, timeout: float = 8.0,
                 opener=None, max_response_bytes: int = 5 * 1024 * 1024) -> None:
        self.token = token if token is not None else os.getenv("GITHUB_TOKEN")
        self.timeout = timeout
        self.max_response_bytes = max_response_bytes
        self.opener = opener or urllib.request.build_opener(_NoRedirect())

    def _get(self, path: str, query: Optional[dict[str, str]] = None) -> tuple[Any, Any]:
        if not path.startswith("/") or ".." in path.split("/"):
            raise GitHubPRException("invalid_api_path", 500, "The GitHub API request could not be constructed safely.")
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "AI-Code-Reviewer-ReadOnly",
            "X-GitHub-Api-Version": API_VERSION,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(self.max_response_bytes + 1)
                if len(raw) > self.max_response_bytes:
                    raise GitHubPRException("github_response_too_large", 413, "GitHub returned a response larger than the configured limit.")
                try:
                    payload = json.loads(raw.decode("utf-8")) if raw else None
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise GitHubPRException("github_invalid_response", 502, "GitHub returned an invalid response.") from exc
                return payload, response.headers
        except GitHubPRException:
            raise
        except urllib.error.HTTPError as exc:
            remaining = exc.headers.get("X-RateLimit-Remaining") if exc.headers else None
            if exc.code == 401:
                code, status, message = "github_unauthorized", 401, "GitHub rejected the configured credential."
            elif exc.code == 404:
                code, status, message = "github_not_found", 404, "The pull request was not found or is not accessible with this credential."
            elif exc.code == 429 or (exc.code == 403 and remaining == "0"):
                code, status, message = "github_rate_limited", 429, "GitHub API rate limit reached. Try again later."
            elif exc.code == 403:
                code, status, message = "github_forbidden", 403, "The GitHub credential lacks permission to read this repository."
            elif exc.code in {301, 302, 303, 307, 308}:
                code, status, message = "github_redirect_blocked", 502, "GitHub redirected the API request; redirects are not followed."
            elif exc.code >= 500:
                code, status, message = "github_unavailable", 502, "GitHub could not complete the API request."
            else:
                code, status, message = "github_api_error", 502, "GitHub rejected the API request."
            raise GitHubPRException(code, status, message) from exc
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            reason = getattr(exc, "reason", None)
            if isinstance(reason, (TimeoutError, socket.timeout)) or isinstance(exc, (TimeoutError, socket.timeout)):
                raise GitHubPRException("github_timeout", 504, "GitHub did not respond before the request timeout.") from exc
            raise GitHubPRException("github_network_error", 502, "Could not connect to the GitHub API.") from exc

    @staticmethod
    def _repo_path(ref: PullRequestRef) -> str:
        return f"/repos/{urllib.parse.quote(ref.owner, safe='')}/{urllib.parse.quote(ref.repository, safe='')}"

    def get_authenticated_user(self) -> dict[str, Any]:
        """Explicit connection check against GitHub's fixed `/user` endpoint."""
        if not self.token:
            raise GitHubPRException("github_token_not_configured", 401,
                                    "Configure a GitHub credential before testing the connection.")
        payload, headers = self._get("/user")
        if not isinstance(payload, dict) or not isinstance(payload.get("login"), str) or not isinstance(payload.get("id"), int):
            raise GitHubPRException("github_invalid_response", 502, "GitHub returned incomplete account information.")
        return {"login": payload["login"], "id": payload["id"],
                "scopes": headers.get("X-OAuth-Scopes", ""),
                "accepted_permissions": headers.get("X-Accepted-GitHub-Permissions", "")}

    def list_accessible_repositories(self, *, max_pages: int = 10) -> list[dict[str, Any]]:
        if not self.token:
            raise GitHubPRException("github_token_not_configured", 401,
                                    "Configure a GitHub credential before discovering repositories.")
        repositories: list[dict[str, Any]] = []
        for page in range(1, max(1, min(max_pages, 10)) + 1):
            payload, _ = self._get("/user/repos", {"per_page": "100", "page": str(page), "sort": "full_name"})
            if not isinstance(payload, list):
                raise GitHubPRException("github_invalid_response", 502, "GitHub returned an invalid repository list.")
            for item in payload:
                if not isinstance(item, dict):
                    continue
                full_name, name, owner = item.get("full_name"), item.get("name"), item.get("owner")
                login = owner.get("login") if isinstance(owner, dict) else None
                if (not isinstance(full_name, str) or not isinstance(name, str) or not isinstance(login, str)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", full_name)
                        or full_name.casefold() != f"{login}/{name}".casefold()):
                    continue
                permissions = item.get("permissions")
                repositories.append({"id": item.get("id"), "full_name": full_name,
                                     "owner": login, "name": name,
                                     "private": bool(item.get("private")),
                                     "permissions": {key: bool(value) for key, value in permissions.items()
                                                     if key in {"pull", "push", "admin"} and isinstance(value, bool)}
                                     if isinstance(permissions, dict) else {}})
            if len(payload) < 100:
                break
        return repositories

    def get_repository(self, owner: str, repository: str) -> dict[str, Any]:
        if (not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", owner or "")
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", repository or "")):
            raise GitHubPRException("invalid_repository_identity", 422,
                                    "The repository identity is invalid.")
        payload, _ = self._get(f"/repos/{urllib.parse.quote(owner, safe='')}/{urllib.parse.quote(repository, safe='')}")
        if not isinstance(payload, dict) or not isinstance(payload.get("full_name"), str):
            raise GitHubPRException("github_invalid_response", 502, "GitHub returned invalid repository metadata.")
        if payload["full_name"].casefold() != f"{owner}/{repository}".casefold():
            raise GitHubPRException("github_repository_mismatch", 502,
                                    "GitHub returned a different repository than requested.")
        return payload

    def get_pull(self, ref: PullRequestRef) -> dict[str, Any]:
        payload, _ = self._get(f"{self._repo_path(ref)}/pulls/{ref.number}")
        if not isinstance(payload, dict):
            raise GitHubPRException("github_invalid_response", 502, "GitHub returned invalid pull request metadata.")
        base = payload.get("base")
        head = payload.get("head")
        required = (
            isinstance(payload.get("number"), int)
            and isinstance(payload.get("title"), str)
            and payload.get("state") in {"open", "closed"}
            and isinstance(base, dict) and isinstance(base.get("sha"), str)
            and isinstance(head, dict) and isinstance(head.get("sha"), str)
        )
        if not required:
            raise GitHubPRException("github_invalid_response", 502, "GitHub returned incomplete pull request metadata.")
        if payload["number"] != ref.number:
            raise GitHubPRException("github_repository_mismatch", 502, "GitHub returned a different pull request than requested.")
        base_repo = base.get("repo")
        if isinstance(base_repo, dict):
            actual_owner = base_repo.get("owner", {}).get("login") if isinstance(base_repo.get("owner"), dict) else None
            actual_repo = base_repo.get("name")
            if (isinstance(actual_owner, str) and actual_owner.casefold() != ref.owner.casefold()) or (
                isinstance(actual_repo, str) and actual_repo.casefold() != ref.repository.casefold()
            ):
                raise GitHubPRException("github_repository_mismatch", 502, "GitHub returned a pull request for a different repository.")
        return payload

    def get_ci_evidence(self, ref: PullRequestRef, commit_sha: str) -> dict[str, Any]:
        """Read check runs and commit statuses for one exact revision only."""
        if not isinstance(commit_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", commit_sha):
            raise GitHubPRException("invalid_commit_sha", 422, "A full commit SHA is required for CI verification.")
        encoded = urllib.parse.quote(commit_sha, safe="")
        check_payload, _ = self._get(f"{self._repo_path(ref)}/commits/{encoded}/check-runs",
                                     {"per_page": "100"})
        status_payload, _ = self._get(f"{self._repo_path(ref)}/commits/{encoded}/status")
        if (not isinstance(check_payload, dict) or not isinstance(check_payload.get("check_runs"), list)
                or not isinstance(status_payload, dict) or not isinstance(status_payload.get("statuses"), list)):
            raise GitHubPRException("github_ci_response_invalid", 502, "GitHub returned incomplete CI status data.")
        return {"head_sha": check_payload.get("sha"),
                "check_runs": check_payload["check_runs"],
                "statuses": [dict(item, sha=commit_sha) for item in status_payload["statuses"]
                             if isinstance(item, dict)]}

    def list_directory(self, ref: PullRequestRef, path: str, commit_sha: str,
                       *, max_entries: int = 100) -> tuple[list[dict[str, Any]], bool]:
        """List a small tree without following symlinks or downloading files."""
        parts = [] if not path else _safe_repository_path(path).split("/")
        if len(parts) > 2 or (parts and parts[0].casefold() != "docs"):
            raise GitHubPRException("context_path_not_allowlisted", 422, "Repository context directory is not allowlisted.")
        commit, _ = self._get(f"{self._repo_path(ref)}/git/commits/{urllib.parse.quote(commit_sha, safe='')}")
        tree_sha = commit.get("tree", {}).get("sha") if isinstance(commit, dict) and isinstance(commit.get("tree"), dict) else None
        if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree_sha):
            raise GitHubPRException("github_invalid_context_tree", 502, "GitHub returned invalid repository tree metadata.")
        for component in parts:
            tree, _ = self._get(f"{self._repo_path(ref)}/git/trees/{urllib.parse.quote(tree_sha, safe='')}")
            entries = tree.get("tree") if isinstance(tree, dict) else None
            if not isinstance(entries, list):
                raise GitHubPRException("github_invalid_context_tree", 502, "GitHub returned invalid repository tree data.")
            match = next((item for item in entries if isinstance(item, dict)
                          and item.get("path") == component and item.get("type") == "tree"
                          and item.get("mode") == "040000"), None)
            if match is None:
                return [], False
            tree_sha = match.get("sha")
            if not isinstance(tree_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", tree_sha):
                raise GitHubPRException("github_invalid_context_tree", 502, "GitHub returned invalid directory metadata.")
        tree, _ = self._get(f"{self._repo_path(ref)}/git/trees/{urllib.parse.quote(tree_sha, safe='')}")
        entries = tree.get("tree") if isinstance(tree, dict) else None
        if not isinstance(entries, list) or any(not isinstance(item, dict) for item in entries):
            raise GitHubPRException("github_invalid_context_tree", 502, "GitHub returned invalid repository tree data.")
        safe_entries = []
        for item in entries:
            name = item.get("path")
            mode = item.get("mode")
            if not isinstance(name, str) or "/" in name:
                continue
            try:
                if _safe_repository_path(name) != name:
                    continue
            except GitHubPRException:
                continue
            if mode == "040000" and item.get("type") == "tree":
                kind = "dir"
            elif mode in {"100644", "100755"} and item.get("type") == "blob":
                kind = "file"
            elif mode == "120000":
                kind = "symlink"
            else:
                kind = "unsupported"
            full_path = "/".join([*parts, name])
            safe_entries.append({"path": full_path, "name": name, "type": kind,
                                 "mode": mode, "size": item.get("size")})
        safe_entries.sort(key=lambda item: item["path"])
        return safe_entries[:max_entries], len(safe_entries) > max_entries

    def get_context_text_file(self, ref: PullRequestRef, path: str, commit_sha: str,
                              *, max_bytes: int) -> tuple[str, bool]:
        """Read and byte-bound an allowlisted text file at a pinned commit."""
        safe_path = _safe_repository_path(path)
        if not (safe_path.casefold() in {
            "readme.md", "contributing.md", "agents.md", "style_guide.md",
            "styleguide.md", "code_style.md", "architecture.md", "pyproject.toml",
            "setup.cfg", "tox.ini", ".flake8", "mypy.ini", ".editorconfig",
        } or (safe_path.casefold().startswith("docs/") and safe_path.rsplit(".", 1)[-1].casefold() in {"md", "rst", "txt", "adoc"})):
            raise GitHubPRException("context_path_not_allowlisted", 422, "Repository context file is not allowlisted.")
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in safe_path.split("/"))
        payload, _ = self._get(
            f"{self._repo_path(ref)}/contents/{encoded_path}", {"ref": commit_sha}
        )
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubPRException("github_invalid_context_file", 502, "GitHub did not return a regular file at the requested revision.")
        if payload.get("path") != safe_path:
            raise GitHubPRException("github_invalid_context_file", 502, "GitHub returned a different repository context path.")
        size = payload.get("size")
        # Base64 content is returned in one response. Decline very large files
        # rather than downloading them solely to truncate after receipt.
        if isinstance(size, int) and size > min(self.max_response_bytes * 3 // 4, 2 * 1024 * 1024):
            raise GitHubPRException("repository_context_file_too_large", 413, "Repository context file exceeds the safe fetch limit.")
        content, encoding = payload.get("content"), payload.get("encoding")
        if not isinstance(content, str) or encoding != "base64":
            raise GitHubPRException("github_invalid_context_file", 502, "GitHub returned unsupported repository context content.")
        try:
            decoded = base64.b64decode(content, validate=True)
            decoded.decode("utf-8")
        except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
            raise GitHubPRException("github_invalid_context_file", 502, "Repository context is not valid UTF-8 text.") from exc
        truncated = len(decoded) > max_bytes
        text = decoded[:max_bytes].decode("utf-8", errors="ignore")
        return text, truncated

    def get_files(self, ref: PullRequestRef, *, max_files: int,
                  max_pages: int = 20) -> tuple[list[dict[str, Any]], bool, int, Optional[str]]:
        per_page = min(100, max(1, max_files))
        files: list[dict[str, Any]] = []
        truncated = False
        retrieval_error: Optional[str] = None
        page = 1
        while page <= max_pages:
            try:
                payload, headers = self._get(
                    f"{self._repo_path(ref)}/pulls/{ref.number}/files",
                    {"per_page": str(per_page), "page": str(page)},
                )
            except GitHubPRException as exc:
                if not files:
                    raise
                truncated = True
                retrieval_error = exc.code
                break
            if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
                if not files:
                    raise GitHubPRException("github_invalid_response", 502, "GitHub returned invalid changed-file data.")
                truncated = True
                retrieval_error = "github_invalid_response"
                break
            link = headers.get("Link", "") if headers else ""
            has_next = bool(re.search(r"rel\s*=\s*\"next\"", link, re.IGNORECASE))
            for item in payload:
                if len(files) == max_files:
                    truncated = True
                    break
                files.append(item)
            if truncated:
                break
            if len(files) >= max_files and has_next:
                truncated = True
                break
            if not has_next:
                break
            page += 1
        else:
            truncated = True
        if page >= max_pages and has_next:
            truncated = True
        return files, truncated, page, retrieval_error

    def get_file(self, ref: PullRequestRef, path: str, commit_sha: str,
                 *, max_source_bytes: int) -> str:
        safe_path = _safe_repository_path(path)
        encoded_path = "/".join(urllib.parse.quote(part, safe="") for part in safe_path.split("/"))
        payload, _ = self._get(
            f"{self._repo_path(ref)}/contents/{encoded_path}",
            {"ref": commit_sha},
        )
        if not isinstance(payload, dict) or payload.get("type") != "file":
            raise GitHubPRException("github_invalid_source", 502, "GitHub did not return a file at the requested commit.")
        size = payload.get("size")
        if isinstance(size, int) and size > max_source_bytes:
            raise GitHubPRException("source_too_large", 413, "A changed Python source file exceeds the configured size limit.")
        content = payload.get("content")
        encoding = payload.get("encoding")
        if not isinstance(content, str) or encoding != "base64":
            raise GitHubPRException("github_invalid_source", 502, "GitHub returned unsupported file content.")
        try:
            decoded = base64.b64decode(content, validate=False)
        except (ValueError, binascii.Error) as exc:
            raise GitHubPRException("github_invalid_source", 502, "GitHub returned invalid encoded file content.") from exc
        if len(decoded) > max_source_bytes:
            raise GitHubPRException("source_too_large", 413, "A changed Python source file exceeds the configured size limit.")
        try:
            return decoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise GitHubPRException("source_not_utf8", 422, "The changed Python source is not valid UTF-8.") from exc


def _safe_repository_path(path: str) -> str:
    if not isinstance(path, str) or not path or path.startswith("/") or "\\" in path:
        raise GitHubPRException("unsafe_repository_path", 422, "GitHub returned an invalid file path.")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or any(ord(char) < 32 for char in part) for part in parts):
        raise GitHubPRException("unsafe_repository_path", 422, "GitHub returned an invalid file path.")
    return path


@dataclass(frozen=True)
class DiffMapping:
    status: str  # mapped, missing, invalid
    added_lines: frozenset[int] = frozenset()
    deleted_lines: frozenset[int] = frozenset()
    hunk_count: int = 0
    error: Optional[str] = None


def parse_unified_diff(patch: Optional[str]) -> DiffMapping:
    if not isinstance(patch, str) or not patch:
        return DiffMapping("missing", error="diff_patch_unavailable")
    old_line = new_line = old_left = new_left = 0
    added: set[int] = set()
    deleted: set[int] = set()
    hunk_count = 0
    active = False
    for line in patch.splitlines():
        match = _HUNK.match(line)
        if match:
            old_line, old_count, new_line, new_count = match.groups()
            old_line = int(old_line)
            new_line = int(new_line)
            old_left = int(old_count) if old_count is not None else 1
            new_left = int(new_count) if new_count is not None else 1
            hunk_count += 1
            active = True
            continue
        if line.startswith("@@"):
            return DiffMapping("invalid", error="malformed_hunk_header")
        if not active or line.startswith("\\ No newline at end of file"):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if new_left <= 0:
                return DiffMapping("invalid", error="hunk_line_count_mismatch")
            added.add(new_line)
            new_line += 1
            new_left -= 1
        elif line.startswith("-") and not line.startswith("---"):
            if old_left <= 0:
                return DiffMapping("invalid", error="hunk_line_count_mismatch")
            deleted.add(old_line)
            old_line += 1
            old_left -= 1
        elif line.startswith(" "):
            if old_left <= 0 or new_left <= 0:
                return DiffMapping("invalid", error="hunk_line_count_mismatch")
            old_line += 1
            new_line += 1
            old_left -= 1
            new_left -= 1
        else:
            return DiffMapping("invalid", error="invalid_diff_line")
    if hunk_count == 0:
        return DiffMapping("invalid", error="diff_has_no_hunks")
    if old_left or new_left:
        return DiffMapping("invalid", error="incomplete_hunk")
    return DiffMapping("mapped", frozenset(added), frozenset(deleted), hunk_count)


@dataclass(frozen=True)
class PRLimits:
    max_files: int = 50
    max_review_files: int = 5
    max_source_bytes: int = 256 * 1024
    max_aggregate_source_bytes: int = 1024 * 1024
    max_diff_bytes: int = 512 * 1024
    max_pages: int = 20

    @classmethod
    def from_environment(cls) -> "PRLimits":
        def get(name: str, default: int, minimum: int, maximum: int) -> int:
            try:
                value = int(os.getenv(name, str(default)))
            except ValueError:
                return default
            return min(max(value, minimum), maximum)
        return cls(
            max_files=get("GITHUB_PR_MAX_FILES", 50, 1, 200),
            max_review_files=get("GITHUB_PR_MAX_REVIEW_FILES", 5, 1, 20),
            max_source_bytes=get("GITHUB_PR_MAX_SOURCE_BYTES", 256 * 1024, 1024, 1024 * 1024),
            max_aggregate_source_bytes=get("GITHUB_PR_MAX_AGGREGATE_SOURCE_BYTES", 1024 * 1024, 4096, 5 * 1024 * 1024),
            max_diff_bytes=get("GITHUB_PR_MAX_DIFF_BYTES", 512 * 1024, 1024, 5 * 1024 * 1024),
            max_pages=get("GITHUB_PR_MAX_PAGES", 20, 1, 20),
        )


@dataclass
class PRFileResult:
    path: str
    status: str
    additions: int
    deletions: int
    review_status: str
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    previous_filename: Optional[str] = None
    skip_reason: Optional[str] = None
    error_code: Optional[str] = None
    diff_status: str = "missing"
    diff_error: Optional[str] = None
    review_summary: str = ""
    static_analysis_status: str = "not_run"
    static_analysis_error: Optional[dict[str, Any]] = None
    repository_context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PRAnalysisResult:
    repository: str
    owner: str
    repository_name: str
    number: int
    title: str
    state: str
    base_sha: str
    head_sha: str
    html_url: str
    total_changed_files: int
    files_received: int
    files_reviewed: int
    files_skipped: int
    files_failed: int
    completion_status: str
    pagination_truncated: bool
    pagination_error: Optional[str]
    findings_by_severity: dict[str, int]
    files: list[PRFileResult]
    repository_context: dict[str, Any] = field(default_factory=dict)
    head_repository: Optional[str] = None
    head_branch: Optional[str] = None
    provider_identity: Optional[str] = None
    stale: bool = False
    stale_reason: Optional[str] = None
    analyzed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["files"] = [item.to_dict() for item in self.files]
        return result


class GitHubPRReviewService:
    def __init__(self, client: Optional[GitHubRESTClient] = None,
                 limits: Optional[PRLimits] = None,
                 reviewer: Optional[Callable[[str, str], ReviewResponse]] = None,
                 context_limits: Optional[RepositoryContextLimits] = None) -> None:
        self.client = client or GitHubRESTClient()
        self.limits = limits or PRLimits.from_environment()
        self.reviewer = reviewer
        self.context_limits = context_limits or RepositoryContextLimits.from_environment()

    def analyze(self, pr_url: str, *, reviewer: Optional[Callable[[str, str], ReviewResponse]] = None,
                context_reviewer: Optional[Callable[[str, str, tuple], ReviewResponse]] = None,
                expected_owner: Optional[str] = None, expected_repository: Optional[str] = None,
                expected_number: Optional[int] = None, expected_base_sha: Optional[str] = None,
                expected_head_sha: Optional[str] = None) -> PRAnalysisResult:
        ref = parse_pull_request_url(pr_url)
        if expected_owner is not None and ref.owner.casefold() != expected_owner.casefold():
            raise GitHubPRException("webhook_repository_mismatch", 422, "Webhook repository does not match the requested pull request.")
        if expected_repository is not None and ref.repository.casefold() != expected_repository.casefold():
            raise GitHubPRException("webhook_repository_mismatch", 422, "Webhook repository does not match the requested pull request.")
        if expected_number is not None and ref.number != expected_number:
            raise GitHubPRException("webhook_pull_request_mismatch", 422, "Webhook pull request number does not match the requested pull request.")
        pull = self.client.get_pull(ref)
        self._validate_expected_revision(pull, ref, expected_base_sha, expected_head_sha)
        files, pagination_truncated, _pages, pagination_error = self.client.get_files(
            ref, max_files=self.limits.max_files, max_pages=self.limits.max_pages
        )
        metadata_owner = pull.get("base", {}).get("repo", {}).get("owner", {}).get("login", ref.owner)
        metadata_repo = pull.get("base", {}).get("repo", {}).get("name", ref.repository)
        base_sha = pull["base"]["sha"]
        head_sha = pull["head"]["sha"]
        head_repository = self._head_repository_ref(pull, ref)
        if expected_head_sha and pull.get("state") != "open":
            raise GitHubPRException("pull_request_not_open", 409, "The pull request is no longer open for review.")
        context_ref = head_repository
        callback = reviewer or self.reviewer
        context_result = collect_repository_context(self.client, context_ref, head_sha, self.context_limits)
        file_results: list[PRFileResult] = []
        eligible_review_count = 0
        aggregate_source_bytes = 0
        aggregate_diff_bytes = 0

        for item in files:
            path = item.get("filename")
            status = str(item.get("status", "unknown"))
            old_path = item.get("previous_filename") if isinstance(item.get("previous_filename"), str) else None
            additions = _safe_int(item.get("additions"))
            deletions = _safe_int(item.get("deletions"))
            result = PRFileResult(
                path=path if isinstance(path, str) else "(invalid path)",
                status=status,
                additions=additions,
                deletions=deletions,
                review_status="skipped",
                previous_filename=old_path,
                repository_context=context_result.diagnostics(),
            )
            file_results.append(result)
            if not isinstance(path, str):
                result.skip_reason = "invalid_file_metadata"
                continue
            try:
                _safe_repository_path(path)
            except GitHubPRException as exc:
                result.skip_reason = exc.code
                continue
            if not path.casefold().endswith(".py"):
                result.skip_reason = "unsupported_file_type"
                continue
            if status == "removed":
                result.skip_reason = "deleted_file"
                continue
            if status not in {"added", "modified", "renamed"}:
                result.skip_reason = "unsupported_change_status"
                continue
            if eligible_review_count >= self.limits.max_review_files:
                result.skip_reason = "review_file_limit_reached"
                continue
            patch_text = item.get("patch") if isinstance(item.get("patch"), str) else None
            patch_size = len(patch_text.encode("utf-8")) if patch_text else 0
            if aggregate_diff_bytes + patch_size > self.limits.max_diff_bytes:
                result.skip_reason = "aggregate_diff_limit_reached"
                continue
            aggregate_diff_bytes += patch_size
            mapping = parse_unified_diff(patch_text)
            patch_counts_mismatch = (
                mapping.status == "mapped"
                and (len(mapping.added_lines) != additions or len(mapping.deleted_lines) != deletions)
            )
            if patch_counts_mismatch:
                result.diff_error = "patch_counts_mismatch"
                # Retain the source review but suppress locations because the
                # API metadata and patch no longer agree on the changed lines.
                mapping = DiffMapping("invalid", error="patch_counts_mismatch")
            result.diff_status = mapping.status
            result.diff_error = mapping.error

            # Count an eligible file before any network/reviewer operation so a
            # failing callback cannot cause the configured workload limit to be
            # bypassed by subsequent files.
            eligible_review_count += 1
            try:
                head_source = self.client.get_file(head_repository, path, head_sha,
                                                   max_source_bytes=self.limits.max_source_bytes)
                aggregate_source_bytes += len(head_source.encode("utf-8"))
                if len(head_source.encode("utf-8")) > self.limits.max_source_bytes:
                    raise GitHubPRException("source_too_large", 413, "Source file is too large.")
                baseline_source = ""
                baseline_available = status == "added"
                if status != "added":
                    try:
                        baseline_source = self.client.get_file(
                            ref, old_path or path, base_sha,
                            max_source_bytes=self.limits.max_source_bytes,
                        )
                        aggregate_source_bytes += len(baseline_source.encode("utf-8"))
                        baseline_available = True
                    except GitHubPRException as exc:
                        if exc.status_code not in {404, 413}:
                            raise
                        result.diff_error = result.diff_error or (
                            "base_source_unavailable" if exc.status_code == 404 else "base_source_too_large"
                        )
                if aggregate_source_bytes > self.limits.max_aggregate_source_bytes:
                    result.skip_reason = "aggregate_source_limit_reached"
                    continue
            except GitHubPRException as exc:
                result.review_status = "failed"
                result.error_code = exc.code
                result.skip_reason = exc.code
                continue

            if callback is None:
                result.review_status = "failed"
                result.error_code = "reviewer_unavailable"
                continue
            try:
                if context_reviewer is not None:
                    supplemental = context_result.evidence_for(
                        head_source, path, f"{metadata_owner}/{metadata_repo}", self.context_limits
                    )
                    response = context_reviewer(head_source, path, supplemental)
                else:
                    response = callback(head_source, path)
            except Exception as exc:
                logger.warning("PR file review failed for %s (%s)", path, type(exc).__name__)
                result.review_status = "failed"
                result.error_code = "review_failed"
                continue
            if not isinstance(response, ReviewResponse) or not response.valid:
                result.review_status = "failed"
                result.error_code = "review_validation_failed"
                continue

            result.review_summary = response.summary
            result.evidence = [asdict(evidence) for evidence in response.evidence]
            result.static_analysis_status = response.static_analysis_status
            result.static_analysis_error = response.static_analysis_error
            findings = list(response.findings)
            if baseline_available:
                base_counts = static_fingerprint_counts(analyze_python(baseline_source, file_path=old_path or path))
                head_static = analyze_python(head_source, file_path=path)
                new_static = static_fingerprint_counts(head_static) - base_counts
                fingerprints_by_id = {item.finding_id: item.fingerprint for item in head_static.findings}
            else:
                new_static = Counter()
                fingerprints_by_id = {}
            for finding in findings:
                if finding.origin == "static" and baseline_available:
                    key = (finding.rule_id, fingerprints_by_id.get(finding.finding_id, ""))
                    is_new: Optional[bool] = new_static[key] > 0
                    if is_new:
                        new_static[key] -= 1
                    else:
                        # A matched base finding is contextual, not a newly introduced PR defect.
                        continue
                elif finding.origin == "static":
                    is_new = None
                elif mapping.status == "mapped":
                    is_new = finding.line_start in mapping.added_lines if finding.line_start else False
                else:
                    is_new = None
                item_payload = _finding_payload(finding, path, mapping, is_new)
                result.findings.append(item_payload)
            fully_mapped = mapping.status == "mapped" and not patch_counts_mismatch and baseline_available
            result.review_status = "reviewed" if fully_mapped else "partial"
            if result.review_status == "partial":
                if mapping.error:
                    result.skip_reason = mapping.error
                elif patch_counts_mismatch:
                    result.skip_reason = "patch_counts_mismatch"
                else:
                    result.skip_reason = result.diff_error or "base_source_unavailable"

        reviewed = sum(item.review_status in {"reviewed", "partial"} for item in file_results)
        skipped = sum(item.review_status == "skipped" for item in file_results)
        failed = sum(item.review_status == "failed" for item in file_results)
        incomplete = pagination_truncated or failed > 0 or any(
            item.review_status == "partial" or item.skip_reason in {
                "review_file_limit_reached", "aggregate_source_limit_reached",
                "aggregate_diff_limit_reached", "source_too_large", "diff_patch_unavailable",
                "base_source_unavailable", "base_source_too_large", "patch_counts_mismatch",
            }
            for item in file_results
        )
        eligible = sum(
            item.path.casefold().endswith(".py") and item.status in {"added", "modified", "renamed"}
            for item in file_results
        )
        if _safe_int(pull.get("changed_files"), len(files)) == 0 and not files:
            completion = "empty_pr"
        elif reviewed == 0 and eligible == 0:
            completion = "no_supported_python"
        elif reviewed == 0 and failed:
            completion = "failed"
        elif incomplete or skipped and reviewed < eligible:
            completion = "partial"
        else:
            completion = "complete"
        severity = Counter(
            str(finding.get("severity") or finding.get("risk_priority") or "unspecified").lower()
            for file_result in file_results for finding in file_result.findings
        )
        result = PRAnalysisResult(
            repository=f"{metadata_owner}/{metadata_repo}", owner=str(metadata_owner),
            repository_name=str(metadata_repo), number=ref.number,
            title=pull["title"], state=pull["state"], base_sha=base_sha,
            head_sha=head_sha,
            html_url=f"https://github.com/{urllib.parse.quote(ref.owner, safe='')}/{urllib.parse.quote(ref.repository, safe='')}/pull/{ref.number}",
            total_changed_files=_safe_int(pull.get("changed_files"), len(files)),
            files_received=len(files), files_reviewed=reviewed, files_skipped=skipped,
            files_failed=failed, completion_status=completion,
            pagination_truncated=pagination_truncated,
            pagination_error=pagination_error,
            findings_by_severity=dict(severity), files=file_results,
            repository_context=context_result.diagnostics(),
            head_repository=f"{head_repository.owner}/{head_repository.repository}",
            head_branch=(pull.get("head", {}).get("ref")
                         if isinstance(pull.get("head"), dict) and isinstance(pull.get("head", {}).get("ref"), str)
                         else None),
        )
        # A PR can move while files and evidence are being collected. Keep the
        # old-SHA findings explicitly stale instead of presenting them as latest.
        try:
            latest_pull = self.client.get_pull(ref)
            latest_base = latest_pull.get("base", {}).get("sha")
            latest_head = latest_pull.get("head", {}).get("sha")
            if latest_base != base_sha or latest_head != head_sha:
                result.stale = True
                result.stale_reason = "pull_request_revision_changed_during_review"
                result.completion_status = "stale"
        except Exception:
            result.stale = True
            result.stale_reason = "pull_request_revision_recheck_failed"
            result.completion_status = "stale"
        return result

    @staticmethod
    def _validate_expected_revision(pull: dict[str, Any], ref: PullRequestRef,
                                    expected_base_sha: Optional[str],
                                    expected_head_sha: Optional[str]) -> None:
        if pull.get("number") != ref.number:
            raise GitHubPRException("github_repository_mismatch", 502, "GitHub returned a different pull request than requested.")
        base_sha = pull.get("base", {}).get("sha") if isinstance(pull.get("base"), dict) else None
        head_sha = pull.get("head", {}).get("sha") if isinstance(pull.get("head"), dict) else None
        base_repo = pull.get("base", {}).get("repo") if isinstance(pull.get("base"), dict) else None
        if isinstance(base_repo, dict):
            owner = base_repo.get("owner", {}).get("login") if isinstance(base_repo.get("owner"), dict) else None
            repository = base_repo.get("name")
            if (isinstance(owner, str) and owner.casefold() != ref.owner.casefold()) or (
                isinstance(repository, str) and repository.casefold() != ref.repository.casefold()
            ):
                raise GitHubPRException("github_repository_mismatch", 502, "GitHub returned a different repository than requested.")
        if (expected_base_sha and base_sha != expected_base_sha) or (expected_head_sha and head_sha != expected_head_sha):
            raise GitHubPRException("stale_pr_revision", 409, "The pull request revision changed after this webhook was delivered.")

    @staticmethod
    def _head_repository_ref(pull: dict[str, Any], base_ref: PullRequestRef) -> PullRequestRef:
        head = pull.get("head") if isinstance(pull.get("head"), dict) else {}
        repository = head.get("repo") if isinstance(head.get("repo"), dict) else None
        if repository is None:
            return base_ref
        owner_data = repository.get("owner")
        owner = owner_data.get("login") if isinstance(owner_data, dict) else None
        name = repository.get("name")
        if (
            not isinstance(owner, str) or not _safe_repository_part(owner)
            or not isinstance(name, str) or not _safe_repository_part(name)
        ):
            raise GitHubPRException("github_invalid_head_repository", 502, "GitHub returned invalid pull request head repository metadata.")
        return PullRequestRef(owner, name, base_ref.number)


def _safe_repository_part(value: str) -> bool:
    return bool(
        _REPO_PART.fullmatch(value)
        and value not in {".", ".."}
        and not value.casefold().endswith(".git")
    )


def _safe_int(value: Any, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else default


def _finding_payload(finding: ReviewFinding, path: str, mapping: DiffMapping,
                     is_new: Optional[bool]) -> dict[str, Any]:
    line = finding.line_start
    in_diff = line in mapping.added_lines if line and mapping.status == "mapped" else None
    payload = asdict(finding)
    payload.update({
        "path": path,
        "source_line": line,
        "is_in_diff": in_diff,
        "is_new_in_pr": is_new,
        "inline_supported": bool(in_diff and is_new is True),
        "diff_line": line if in_diff else None,
        "change_relation": "added_or_modified" if in_diff else "outside_or_unmapped",
    })
    return payload
