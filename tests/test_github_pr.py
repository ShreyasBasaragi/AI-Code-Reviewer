"""Offline coverage for the read-only GitHub pull request workflow."""

import base64
import json
import urllib.error
from email.message import Message

import pytest

from mcp_server.github_pr import (
    GitHubPRException, GitHubPRReviewService, GitHubRESTClient, PRLimits,
    PullRequestRef, parse_pull_request_url, parse_unified_diff,
)
from review_schema import ReviewFinding, ReviewResponse


def test_pr_url_validation_rejects_unsafe_urls():
    assert parse_pull_request_url("https://github.com/acme/project/pull/12/") == PullRequestRef("acme", "project", 12)
    for value in (
        "http://github.com/acme/project/pull/12",
        "https://github.com.evil.test/acme/project/pull/12",
        "https://user:pass@github.com/acme/project/pull/12",
        "https://github.com/acme/project/pull/0",
        "https://github.com/acme/%2e%2e/pull/12",
        "https://github.com/acme/project/pull/12?host=evil.test",
    ):
        with pytest.raises(GitHubPRException):
            parse_pull_request_url(value)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or Message()

    def read(self, size=-1):
        return self.payload[:size]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class FakeOpener:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def encoded_json(payload, headers=None):
    return FakeResponse(json.dumps(payload).encode(), headers)


def test_rest_client_authenticates_only_on_backend_and_uses_fixed_host():
    opener = FakeOpener([encoded_json({"ok": True})])
    GitHubRESTClient(token="secret", opener=opener, timeout=2.5)._get("/repos/acme/project")
    request, timeout = opener.requests[0]
    assert request.full_url.startswith("https://api.github.com/")
    assert request.get_header("Authorization") == "Bearer secret"
    assert timeout == 2.5
    public = FakeOpener([encoded_json({"ok": True})])
    GitHubRESTClient(token="", opener=public)._get("/repos/acme/project")
    assert public.requests[0][0].get_header("Authorization") is None


def test_rest_client_blocks_redirects_and_bad_responses():
    error = urllib.error.HTTPError("https://api.github.com/x", 302, "Found", Message(), None)
    with pytest.raises(GitHubPRException) as redirect:
        GitHubRESTClient(token="", opener=FakeOpener([error]))._get("/repos/x")
    assert redirect.value.code == "github_redirect_blocked"
    with pytest.raises(GitHubPRException) as oversized:
        GitHubRESTClient(token="", opener=FakeOpener([FakeResponse(b"12345")]), max_response_bytes=4)._get("/repos/x")
    assert oversized.value.code == "github_response_too_large"
    with pytest.raises(GitHubPRException) as malformed:
        GitHubRESTClient(token="", opener=FakeOpener([FakeResponse(b"not json")]))._get("/repos/x")
    assert malformed.value.code == "github_invalid_response"


def test_ci_evidence_binds_check_runs_to_successful_dispatched_workflow(monkeypatch):
    sha = "a" * 40
    client = GitHubRESTClient(token="read-token")
    calls = []

    def fake_get(path, query=None):
        calls.append((path, query))
        if path.endswith("/check-runs"):
            return {"sha": sha, "check_runs": [{
                "name": "unit", "head_sha": sha, "check_suite": {"id": 71},
            }, {
                "name": "untrusted", "head_sha": sha, "check_suite": {"id": 72},
            }]}, {}
        if path.endswith("/status"):
            return {"statuses": []}, {}
        if path.endswith("/actions/workflows/7/runs"):
            return {"workflow_runs": [{
                "workflow_id": 7, "head_sha": sha, "event": "workflow_dispatch",
                "status": "completed", "conclusion": "success", "check_suite_id": 71,
            }, {
                "workflow_id": 8, "head_sha": sha, "event": "workflow_dispatch",
                "status": "completed", "conclusion": "success", "check_suite_id": 72,
            }]}, {}
        raise AssertionError(path)

    monkeypatch.setattr(client, "_get", fake_get)
    evidence = client.get_ci_evidence(PullRequestRef("acme", "demo", 7), sha, "7")
    assert evidence["check_runs"][0]["workflow_identity"] == "7"
    assert "workflow_identity" not in evidence["check_runs"][1]
    assert calls[-1] == (
        "/repos/acme/demo/actions/workflows/7/runs",
        {"head_sha": sha, "per_page": "100"},
    )


@pytest.mark.parametrize("status,headers,expected", [
    (401, {}, "github_unauthorized"),
    (403, {"X-RateLimit-Remaining": "0"}, "github_rate_limited"),
    (403, {}, "github_forbidden"),
])
def test_rest_client_sanitizes_authentication_and_rate_limit_failures(status, headers, expected):
    message = Message()
    for key, value in headers.items():
        message[key] = value
    failure = urllib.error.HTTPError("https://api.github.com/x", status, "failure", message, None)
    with pytest.raises(GitHubPRException) as captured:
        GitHubRESTClient(token="secret", opener=FakeOpener([failure]))._get("/repos/a/b")
    assert captured.value.code == expected
    assert "secret" not in captured.value.message


def test_rest_client_maps_timeouts_without_retrying_or_external_fallback():
    opener = FakeOpener([TimeoutError("offline timeout fixture")])
    with pytest.raises(GitHubPRException) as captured:
        GitHubRESTClient(token="", opener=opener)._get("/repos/a/b")
    assert captured.value.code == "github_timeout"
    assert len(opener.requests) == 1


def test_file_pagination_is_bounded_and_uses_constructed_api_urls():
    first = Message()
    first["Link"] = '<https://evil.test/files?page=2>; rel="next"'
    opener = FakeOpener([encoded_json([{"filename": "one.py"}], first), encoded_json([{"filename": "two.py"}])])
    files, truncated, pages, error = GitHubRESTClient(token="", opener=opener).get_files(
        PullRequestRef("acme", "repo", 1), max_files=5, max_pages=3
    )
    assert [item["filename"] for item in files] == ["one.py", "two.py"]
    assert not truncated and pages == 2 and error is None
    assert "evil.test" not in opener.requests[1][0].full_url


def test_partial_pagination_failure_is_reported():
    headers = Message()
    headers["Link"] = '<https://api.github.com/next>; rel="next"'
    failure = urllib.error.HTTPError("https://api.github.com/next", 403, "Forbidden", Message(), None)
    files, truncated, pages, error = GitHubRESTClient(
        token="", opener=FakeOpener([encoded_json([{"filename": "one.py"}], headers), failure])
    ).get_files(PullRequestRef("a", "b", 1), max_files=5)
    assert len(files) == 1 and truncated and pages == 2 and error == "github_forbidden"


def test_contents_decode_and_unsafe_paths():
    source = "print('ok')\n"
    body = {"type": "file", "size": len(source), "encoding": "base64", "content": base64.b64encode(source.encode()).decode()}
    client = GitHubRESTClient(token="", opener=FakeOpener([encoded_json(body)]))
    assert client.get_file(PullRequestRef("a", "b", 1), "src/main.py", "abc", max_source_bytes=100) == source
    with pytest.raises(GitHubPRException):
        client.get_file(PullRequestRef("a", "b", 1), "../secret.py", "abc", max_source_bytes=100)


def test_unified_diff_maps_multiple_hunks_and_rejects_invalid_patches():
    patch = "@@ -1,3 +1,4 @@\n context\n-old\n+new\n+extra\n@@ -10,1 +11,1 @@\n-old2\n+new2\n\\ No newline at end of file"
    mapping = parse_unified_diff(patch)
    assert mapping.status == "mapped"
    assert mapping.added_lines == frozenset({2, 3, 11})
    assert mapping.deleted_lines == frozenset({2, 10})
    assert parse_unified_diff(None).status == "missing"
    assert parse_unified_diff("@@ -1,2 +1,2 @@\n+wrong-count").status == "invalid"


class FakePRClient:
    def __init__(self, files, sources, *, truncated=False, pagination_error=None):
        self.files, self.sources = files, sources
        self.truncated, self.pagination_error = truncated, pagination_error

    def get_pull(self, ref):
        return {
            "number": ref.number, "title": "Test change", "state": "open", "changed_files": len(self.files),
            "base": {"sha": "base-sha", "repo": {"name": ref.repository, "owner": {"login": ref.owner}}},
            "head": {"sha": "head-sha"},
        }

    def get_files(self, ref, *, max_files, max_pages):
        return self.files[:max_files], self.truncated or len(self.files) > max_files, 1, self.pagination_error

    def get_file(self, ref, path, commit_sha, *, max_source_bytes):
        value = self.sources[(commit_sha, path)]
        if len(value.encode()) > max_source_bytes:
            raise GitHubPRException("source_too_large", 413, "large")
        return value


def api_file(path="main.py", status="modified", patch="@@ -1,1 +1,1 @@\n-old = 1\n+eval('1')"):
    return {"filename": path, "status": status, "additions": 1, "deletions": 1, "patch": patch}


def test_pr_review_maps_new_static_findings_to_changed_source_lines():
    from mcp_server.orchestrator import _static_review_finding
    from static_analysis import analyze_python

    head, base = "value = eval('1')\n", "value = 1\n"
    finding = _static_review_finding(analyze_python(head, file_path="main.py").findings[0])
    response = ReviewResponse(summary="review", findings=(finding,), valid=True)
    service = GitHubPRReviewService(FakePRClient(
        [api_file()], {("head-sha", "main.py"): head, ("base-sha", "main.py"): base}
    ), reviewer=lambda *_: response)
    result = service.analyze("https://github.com/acme/repo/pull/4")
    item = result.files[0].findings[0]
    assert result.completion_status == "complete"
    assert item["origin"] == "static" and item["rule_id"] == "PYSEC001"
    assert item["is_new_in_pr"] is True and item["inline_supported"] is True


def test_pr_review_omits_preexisting_static_findings_and_keeps_contextual_llm():
    from mcp_server.orchestrator import _static_review_finding
    from static_analysis import analyze_python

    source = "value = eval('1')\n"
    static = _static_review_finding(analyze_python(source, file_path="main.py").findings[0])
    contextual = ReviewFinding(title="Context", issue_type="bug", explanation="Context", recommended_fix="Check", finding_id="llm-1", origin="llm", line_start=9)
    response = ReviewResponse(summary="review", findings=(static, contextual), valid=True)
    service = GitHubPRReviewService(FakePRClient(
        [api_file()], {("head-sha", "main.py"): source, ("base-sha", "main.py"): source}
    ), reviewer=lambda *_: response)
    findings = service.analyze("https://github.com/acme/repo/pull/4").files[0].findings
    assert [item["finding_id"] for item in findings] == ["llm-1"]
    assert findings[0]["is_in_diff"] is False and findings[0]["inline_supported"] is False


def test_diff_count_mismatch_disables_inline_locations():
    from mcp_server.orchestrator import _static_review_finding
    from static_analysis import analyze_python

    head, base = "value = eval('1')\n", "value = 1\n"
    finding = _static_review_finding(analyze_python(head, file_path="main.py").findings[0])
    file_info = api_file()
    file_info["additions"] = 2  # Diff only contains one added line.
    result = GitHubPRReviewService(FakePRClient(
        [file_info], {("head-sha", "main.py"): head, ("base-sha", "main.py"): base}
    ), reviewer=lambda *_: ReviewResponse(summary="review", findings=(finding,))).analyze(
        "https://github.com/acme/repo/pull/4"
    )
    file_result = result.files[0]
    assert file_result.review_status == "partial"
    assert file_result.findings[0]["is_in_diff"] is None
    assert file_result.findings[0]["inline_supported"] is False


def test_pr_workload_limits_and_partial_failures_are_visible():
    files = [api_file("readme.txt"), api_file("gone.py", "removed"), api_file("one.py"), api_file("two.py")]
    sources = {(sha, path): "x = 1\n" for path in ("one.py", "two.py") for sha in ("head-sha", "base-sha")}
    limits = PRLimits(max_files=10, max_review_files=1, max_source_bytes=100, max_aggregate_source_bytes=1000, max_diff_bytes=1000, max_pages=1)
    calls = []
    result = GitHubPRReviewService(FakePRClient(files, sources), limits=limits).analyze(
        "https://github.com/acme/repo/pull/4", reviewer=lambda code, path: calls.append(path) or ReviewResponse(summary="ok")
    )
    assert calls == ["one.py"] and result.completion_status == "partial"
    assert result.files[0].skip_reason == "unsupported_file_type"
    assert result.files[1].skip_reason == "deleted_file"
    assert result.files[3].skip_reason == "review_file_limit_reached"


def test_pr_reviews_multiple_python_and_renamed_files_and_skips_non_python_deleted_and_empty_patches():
    files = [
        api_file("src/one.py"),
        {**api_file("src/new_name.py"), "status": "renamed", "previous_filename": "src/old_name.py"},
        api_file("README.md"),
        api_file("removed.py", "removed"),
        {**api_file("empty.py"), "patch": ""},
    ]
    sources = {}
    for path in ("src/one.py", "src/new_name.py", "src/old_name.py", "empty.py"):
        sources[("head-sha", path)] = "value = 1\n"
        sources[("base-sha", path)] = "value = 0\n"
    reviewed = []
    service = GitHubPRReviewService(FakePRClient(files, sources))
    result = service.analyze(
        "https://github.com/acme/repo/pull/4",
        reviewer=lambda code, path: reviewed.append(path) or ReviewResponse(summary="offline"),
    )
    assert reviewed == ["src/one.py", "src/new_name.py", "empty.py"]
    assert result.files_reviewed == 3 and result.files_skipped == 2
    assert result.files[1].previous_filename == "src/old_name.py"
    assert result.files[2].skip_reason == "unsupported_file_type"
    assert result.files[3].skip_reason == "deleted_file"
    assert result.completion_status == "partial"


def test_pr_malformed_and_oversized_diffs_are_reported_and_locations_suppressed():
    malformed = {**api_file("bad.py"), "patch": "@@ malformed"}
    oversized = api_file("large.py")
    limits = PRLimits(max_files=10, max_review_files=10, max_source_bytes=100,
                      max_aggregate_source_bytes=1000, max_diff_bytes=20, max_pages=1)
    sources = {("head-sha", path): "x = 1\n" for path in ("bad.py", "large.py")}
    sources.update({("base-sha", path): "x = 0\n" for path in ("bad.py", "large.py")})
    result = GitHubPRReviewService(FakePRClient([malformed, oversized], sources), limits=limits,
                                   reviewer=lambda *_: ReviewResponse(summary="offline")).analyze(
        "https://github.com/acme/repo/pull/4"
    )
    assert result.files[0].diff_status == "invalid"
    assert result.files[0].review_status == "partial"
    assert result.files[1].skip_reason == "aggregate_diff_limit_reached"
    assert result.completion_status == "partial"


def test_empty_pull_request_is_not_reported_as_a_completed_review():
    result = GitHubPRReviewService(FakePRClient([], {})).analyze("https://github.com/acme/repo/pull/4")
    assert result.completion_status == "empty_pr"


def test_pr_api_route_returns_structured_response_without_provider_initialization(monkeypatch):
    from fastapi.testclient import TestClient
    from mcp_server.http_api import ReviewFixService, create_app
    from mcp_server.state import LifecycleState

    monkeypatch.delenv("GITHUB_ACTIONS_TOKEN", raising=False)
    monkeypatch.delenv("GITHUB_ACTION_POLICY_PATH", raising=False)

    fake = FakePRClient([api_file()], {("head-sha", "main.py"): "x = 1\n", ("base-sha", "main.py"): "x = 0\n"})
    adapter = GitHubPRReviewService(fake, reviewer=lambda *_: ReviewResponse(summary="ok"))
    api = TestClient(create_app(ReviewFixService(state=LifecycleState(), github_pr_service=adapter)))
    response = api.post("/api/github/pr-reviews", json={"url": "https://github.com/acme/repo/pull/4"})
    assert response.status_code == 200 and response.json()["head_sha"] == "head-sha"
    assert response.json()["action_policy"]["manual_pr_review"] == "review_only"
    assert response.json()["action_policy"]["autonomous_fixing"] == "disabled"
    assert response.json()["provider_identity"] == "injected:github_reviewer"
    assert api.get("/api/health").json()["provider_initialized"] is False
    assert api.post("/api/github/pr-reviews", json={"url": "https://example.com/pr/4"}).status_code == 422


def test_hosted_manual_pr_review_requires_repository_source_sharing_opt_in(monkeypatch):
    from fastapi.testclient import TestClient
    from mcp_server.http_api import ReviewFixService, create_app
    from mcp_server.state import LifecycleState
    import llm.provider_settings as settings_module
    from llm.provider_settings import ProviderSettingsStore

    monkeypatch.delenv("GITHUB_ACTION_POLICY_PATH", raising=False)
    monkeypatch.setattr(settings_module, "PROVIDER_SETTINGS", ProviderSettingsStore())
    service = ReviewFixService(state=LifecycleState(), github_pr_service=GitHubPRReviewService(FakePRClient([], {})))
    response = TestClient(create_app(service)).post(
        "/api/github/pr-reviews", json={"url": "https://github.com/acme/repo/pull/4"}
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "source_sharing_not_authorized"


def test_pr_review_rejects_webhook_revision_mismatch_before_review_callback():
    calls = []
    fake = FakePRClient([], {})
    service = GitHubPRReviewService(fake, reviewer=lambda *args: calls.append(args) or ReviewResponse(summary="called"))
    with pytest.raises(GitHubPRException) as stale:
        service.analyze(
            "https://github.com/acme/repo/pull/4",
            expected_owner="acme", expected_repository="repo", expected_number=4,
            expected_base_sha="0" * 40, expected_head_sha="head-sha",
        )
    assert stale.value.code == "stale_pr_revision"
    assert calls == []


def test_pr_review_marks_result_stale_if_head_changes_during_analysis():
    class MovingPRClient(FakePRClient):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.pull_reads = 0

        def get_pull(self, ref):
            self.pull_reads += 1
            result = super().get_pull(ref)
            if self.pull_reads > 1:
                result["head"]["sha"] = "f" * 40
            return result

    fake = MovingPRClient(
        [api_file()],
        {("head-sha", "main.py"): "value = 1\n", ("base-sha", "main.py"): "value = 0\n"},
    )
    result = GitHubPRReviewService(fake, reviewer=lambda *_: ReviewResponse(summary="offline")).analyze(
        "https://github.com/acme/repo/pull/4",
        expected_base_sha="base-sha", expected_head_sha="head-sha",
    )
    assert result.stale is True
    assert result.stale_reason == "pull_request_revision_changed_during_review"
    assert result.completion_status == "stale"


def test_git_tree_context_listing_uses_pinned_commit_and_omits_symlink_entries():
    commit_sha = "a" * 40
    root_tree = "b" * 40
    docs_tree = "c" * 40
    opener = FakeOpener([
        encoded_json({"tree": {"sha": root_tree}}),
        encoded_json({"tree": [
            {"path": "README.md", "mode": "100644", "type": "blob", "size": 12},
            {"path": "docs", "mode": "040000", "type": "tree", "sha": docs_tree},
            {"path": "AGENTS.md", "mode": "120000", "type": "blob", "size": 8},
        ]}),
    ])
    client = GitHubRESTClient(token="", opener=opener)
    entries, truncated = client.list_directory(PullRequestRef("acme", "repo", 2), "", commit_sha)
    assert not truncated
    assert [(entry["path"], entry["type"]) for entry in entries] == [
        ("AGENTS.md", "symlink"), ("README.md", "file"), ("docs", "dir")
    ]
    assert all(request.get_method() == "GET" for request, _timeout in opener.requests)
    assert all("api.github.com" in request.full_url for request, _timeout in opener.requests)
    assert commit_sha in opener.requests[0][0].full_url


def test_context_tree_refuses_paths_outside_docs_allowlist():
    client = GitHubRESTClient(token="", opener=FakeOpener([]))
    with pytest.raises(GitHubPRException) as denied:
        client.list_directory(PullRequestRef("acme", "repo", 2), "src", "a" * 40)
    assert denied.value.code == "context_path_not_allowlisted"
