import json
import threading
from dataclasses import asdict
from concurrent.futures import ThreadPoolExecutor

import pytest

from fix_verification import generate_unified_diff
from mcp_server.github_action_coordinator import GitHubActionCoordinator
from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_policy import PolicyDecision, RepositoryActionConfig
from mcp_server.github_pr import GitHubPRException, PullRequestRef
from mcp_server.github_pr import DiffMapping, PRAnalysisResult, PRFileResult, _finding_payload
from mcp_server.github_webhook import WebhookDeliveryStore
from review_schema import RetrievedEvidence, parse_review_response


SHA = "a" * 40
REF = PullRequestRef("acme", "demo", 17)


class ReadClient:
    def __init__(self, *, sha=SHA, branch="feature", owner="acme", repository="demo", base_repository="demo", state="open", source="name=1\n"):
        self.sha, self.branch, self.owner, self.repository, self.base_repository, self.state = sha, branch, owner, repository, base_repository, state
        self.source = source
        self.calls = 0

    def get_pull(self, ref):
        self.calls += 1
        return {
            "number": ref.number, "state": self.state,
            "base": {"sha": SHA, "repo": {"owner": {"login": "acme"}, "name": self.base_repository}},
            "head": {"sha": self.sha, "ref": self.branch,
                     "repo": {"owner": {"login": self.owner}, "name": self.repository}},
        }

    def get_files(self, ref, *, max_files, max_pages):
        return ([{"filename": "src/app.py", "status": "modified"}], False, 1, None)

    def get_file(self, ref, path, sha, *, max_source_bytes):
        return self.source


def valid_finding(**updates):
    finding = {
        "finding_id": "finding-1", "origin": "llm", "rule_id": None,
        "issue_type": "style",
        "title": "Prefer the project naming style", "explanation": "This name differs from project convention.",
        "recommended_fix": "Rename the local variable.", "evidence_ids": ["rag-1"],
        "uncertain": False, "abstained": False, "path": "src/app.py",
        "source_line": 4, "line_start": 4, "line_end": 4, "inline_supported": True,
    }
    finding.update(updates)
    return finding


def test_inline_comment_uses_validated_new_side_location_and_provenance():
    read = ReadClient()
    requests = []
    def transport(method, url, headers, body):
        requests.append((method, url, headers, json.loads(body)))
        return {"id": 1234}
    adapter = GitHubMCPActionAdapter(read_client=read, token="write-token", transport=transport)
    result = adapter.publish_inline_comment(
        REF, repository="acme/demo", expected_head_sha=SHA,
        finding=valid_finding(), changed_paths={"src/app.py"},
    )
    assert result.status == "published" and result.github_id == 1234
    method, url, headers, payload = requests[0]
    assert method == "POST" and url.endswith("/repos/acme/demo/pulls/17/comments")
    assert payload["line"] == 4 and payload["side"] == "RIGHT" and payload["commit_id"] == SHA
    assert "AI Code Reviewer (AI/RAG)" in payload["body"]
    assert "rag-1" in payload["body"]
    assert "Authorization" in headers
    assert read.calls == 1


def test_non_inline_finding_uses_revision_checked_general_issue_comment():
    read = ReadClient()
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=read, token="write-token",
        transport=lambda method, url, headers, body: (
            requests.append((method, url, json.loads(body))) or {"id": 1235}
        ),
    )
    result = adapter.publish_general_comment(
        REF, repository="acme/demo", expected_head_sha=SHA,
        finding=valid_finding(inline_supported=False), operation_id="d" * 32,
    )
    assert result.status == "published" and result.github_id == 1235
    assert result.detail["comment_kind"] == "general"
    method, url, payload = requests[0]
    assert method == "POST" and url.endswith("/repos/acme/demo/issues/17/comments")
    assert "AI Code Reviewer (AI/RAG)" in payload["body"]
    assert "ai-code-review-action:" + "d" * 32 in payload["body"]
    assert read.calls == 1


def test_general_comment_refuses_stale_revision_without_write():
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(sha="b" * 40), token="write-token",
        transport=lambda *args: requests.append(args),
    )
    result = adapter.publish_general_comment(
        REF, repository="acme/demo", expected_head_sha=SHA,
        finding=valid_finding(inline_supported=False),
    )
    assert result.status == "stale"
    assert requests == []


def test_ambiguous_comment_reconciliation_uses_operation_id_and_expected_sha():
    operation_id = "c" * 32
    marker = f"<!-- ai-code-review-action:{operation_id} -->"
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="action-token",
        transport=lambda method, url, headers, body: [{
            "id": 33, "body": marker, "commit_id": SHA,
        }],
    )
    result = adapter.reconcile_inline_comment(
        REF, repository="acme/demo", expected_head_sha=SHA, operation_id=operation_id,
    )
    assert result["status"] == "published" and result["github_comment_id"] == 33
    absent = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="action-token",
        transport=lambda *args: [],
    ).reconcile_inline_comment(
        REF, repository="acme/demo", expected_head_sha=SHA, operation_id=operation_id,
    )
    assert absent["status"] == "not_published" and absent["retry_safe"] is True


def test_general_comment_reconciliation_uses_issue_comment_endpoint_and_operation_marker():
    operation_id = "e" * 32
    marker = f"<!-- ai-code-review-action:{operation_id} -->"
    requests = []

    def transport(method, url, headers, body):
        requests.append((method, url))
        return [{"id": 34, "body": marker}]

    adapter = GitHubMCPActionAdapter(read_client=ReadClient(), token="action-token", transport=transport)
    result = adapter.reconcile_inline_comment(
        REF, repository="acme/demo", expected_head_sha=SHA,
        operation_id=operation_id, comment_kind="general",
    )
    assert result["status"] == "published" and result["github_comment_id"] == 34
    assert requests[0][1].startswith("https://api.github.com/repos/acme/demo/issues/17/comments?")


@pytest.mark.parametrize("changes", [
    {"path": "src/other.py"}, {"inline_supported": False}, {"source_line": None},
    {"line_end": 8}, {"path": "../secret.py"},
])
def test_unmappable_or_ambiguous_finding_is_skipped_without_write(changes):
    requests = []
    adapter = GitHubMCPActionAdapter(read_client=ReadClient(), token="t",
                                     transport=lambda *args: requests.append(args))
    result = adapter.publish_inline_comment(
        REF, repository="acme/demo", expected_head_sha=SHA,
        finding=valid_finding(**changes), changed_paths={"src/app.py"},
    )
    assert result.status == "skipped_unmappable"
    assert requests == []


def test_stale_pull_and_mismatched_repository_never_publish():
    requests = []
    stale = GitHubMCPActionAdapter(read_client=ReadClient(sha="b" * 40), token="t",
                                   transport=lambda *args: requests.append(args))
    result = stale.publish_inline_comment(REF, repository="acme/demo", expected_head_sha=SHA,
                                          finding=valid_finding(), changed_paths={"src/app.py"})
    assert result.status == "stale" and requests == []
    wrong = GitHubMCPActionAdapter(read_client=ReadClient(base_repository="other"), token="t",
                                   transport=lambda *args: requests.append(args))
    result = wrong.publish_inline_comment(REF, repository="acme/demo", expected_head_sha=SHA,
                                          finding=valid_finding(), changed_paths={"src/app.py"})
    assert result.status == "stale" and requests == []


@pytest.mark.parametrize(("code", "expected"), [
    ("github_forbidden", "failed"), ("github_rate_limited", "failed"),
    ("github_timeout", "unknown"), ("github_network_error", "unknown"),
])
def test_github_errors_are_sanitized_and_ambiguous_write_is_not_retried(code, expected):
    def fail(*_args):
        raise GitHubPRException(code, 503, "private provider details")
    adapter = GitHubMCPActionAdapter(read_client=ReadClient(), token="t", transport=fail)
    result = adapter.publish_inline_comment(REF, repository="acme/demo", expected_head_sha=SHA,
                                            finding=valid_finding(), changed_paths={"src/app.py"})
    assert result.status == expected and result.error_code == code


def test_comment_markdown_is_bounded_and_does_not_publish_raw_links_or_html():
    from mcp_server.github_actions import _comment_body
    body = _comment_body(valid_finding(
        explanation="<script>bad</script> http://evil.example/steal " + "x" * 5000,
    ))
    assert "<script>" not in body
    assert "http://evil.example" not in body
    assert len(body) <= 6000


def test_fix_preparation_enforces_path_size_syntax_static_and_revision_without_writing():
    read = ReadClient()
    writes = []
    adapter = GitHubMCPActionAdapter(read_client=read, token="t", transport=lambda *args: writes.append(args))
    prepared = adapter.prepare_change(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        original_code="name=1\n", corrected_code="name = 1\n", allowed_paths={"src/app.py"},
    )
    assert prepared["status"] == "PENDING_REQUIRED_VERIFICATION"
    assert prepared["behavioral_status"] == "not_run" and prepared["ci_status"] == "not_run"
    assert prepared["commit_execution"] == "requires_independent_runtime_gates"
    assert prepared["changed_lines"] == 2
    read.source = "a=1\nb=2\nc=3\n"
    too_large = adapter.prepare_change(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        original_code="a=1\nb=2\nc=3\n", corrected_code="a=4\nb=5\nc=6\n",
        allowed_paths={"src/app.py"},
    )
    assert too_large["status"] == "denied"
    path_denied = adapter.prepare_change(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path=".github/workflows/a.yml",
        original_code="a=1\n", corrected_code="a=2\n", allowed_paths={".github/workflows/a.yml"},
    )
    assert path_denied["reason"] == "protected_path"
    read.source = "a=1\n"
    syntax_denied = adapter.prepare_change(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        original_code="a=1\n", corrected_code="def broken(:\n", allowed_paths={"src/app.py"},
    )
    assert syntax_denied["reason"] == "syntax_verification_not_passed"
    read.source = "a=1\n"
    static_denied = adapter.prepare_change(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        original_code="a=1\n", corrected_code="a=eval(input())\n", allowed_paths={"src/app.py"},
    )
    assert static_denied["reason"] == "new_static_violation"
    assert writes == []


def test_commit_preparation_checks_branch_identity_and_default_runtime_gate_is_closed():
    read = ReadClient()
    calls = []
    adapter = GitHubMCPActionAdapter(read_client=read, token="t", transport=lambda *args: calls.append(args))
    plan = adapter.prepare_commit(
        REF, repository="acme/demo", branch="feature", expected_head_sha=SHA,
        is_fork=False, diff="--- a/src/app.py\n+++ b/src/app.py\n@@\n-a\n+b\n",
    )
    assert plan["status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert plan["commit_sha"] is None and plan["force_push"] is False
    assert "explicit_runtime_commit_authorization_required" in plan["denial_reasons"]
    result = adapter.execute_commit()
    assert result.status == "AUTONOMOUS_COMMIT_DISABLED" and calls == []
    wrong_branch = adapter.prepare_commit(
        REF, repository="acme/demo", branch="main", expected_head_sha=SHA,
        is_fork=False, diff="change",
    )
    assert "branch_identity_mismatch" in wrong_branch["denial_reasons"]


def test_existing_fix_candidate_must_pass_existing_verifier_before_patch_preparation():
    from fix_generation import FixCandidate
    candidate = FixCandidate(
        candidate_id="candidate-1", finding_ref="finding-1", original_code="name=1\n",
        corrected_code="name = 1\n", explanation="Formatting only.", assumptions=(),
        evidence_ids=("rag-1",), generation_status="generated",
    )
    allowed_policy = PolicyDecision(
        action="potential_autonomous_fix", eligible=True, status="PENDING_REQUIRED_VERIFICATION",
        required_checks=("syntax", "static", "behavioral", "ci"),
    )
    adapter = GitHubMCPActionAdapter(read_client=ReadClient(source="name=1\n"), token=None)
    result = adapter.prepare_fix_candidate(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        candidate=candidate, allowed_paths={"src/app.py"}, policy_decision=allowed_policy,
    )
    assert result["status"] == "PENDING_REQUIRED_VERIFICATION"
    assert result["candidate_id"] == "candidate-1"
    assert result["verification"]["syntax_check"]["status"] == "passed"
    assert result["verification"]["static_analysis_status"] == "passed"
    assert result["behavioral_status"] == "not_run" and result["ci_status"] == "not_run"
    assert result["commit_execution"] == "requires_independent_runtime_gates"
    denied = adapter.prepare_fix_candidate(
        REF, repository="acme/demo", expected_head_sha=SHA, file_path="src/app.py",
        candidate=candidate, allowed_paths={"src/app.py"},
        policy_decision=PolicyDecision(action="human_review", eligible=False, status="DENIED"),
    )
    assert denied["status"] == "denied"


def test_action_ledger_prevents_duplicate_comment_publication_and_records_outcome(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "actions.sqlite3")
    store.initialize()
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="write-token",
        transport=lambda method, url, headers, body: (requests.append((method, url, body)) or {"id": 7}),
    )
    config = RepositoryActionConfig(publish_review_comments=True)
    coordinator = GitHubActionCoordinator(store, read_client=adapter.read_client,
                                          adapter=adapter, configs={"acme/demo": config})
    event = {"delivery_id": "delivery-1", "owner": "acme", "repository": "demo", "number": 17,
             "base_sha": SHA, "head_sha": SHA}
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "completion_status": "complete", "files": [{
                  "path": "src/app.py", "status": "modified", "evidence": [{"evidence_id": "rag-1"}], "findings": [valid_finding()],
              }]}
    first = coordinator.process_review(event, review)
    second = coordinator.process_review(event, review)
    assert first["actions"][0]["status"] == "published"
    assert second["actions"][0]["status"] == "duplicate_suppressed"
    assert len(requests) == 1
    records = store.get_actions("delivery-1")
    assert records[0]["publication_status"] == "published"
    assert records[0]["github_comment_id"] == 7
    assert records[0]["commit_sha"] is None


def test_validated_pr_finding_tuple_evidence_reaches_inline_comment_adapter(tmp_path):
    evidence = RetrievedEvidence(
        evidence_id="rag-1", source_family="test", source_url=None,
        rule_identifier=None, text="A relevant code-review rule.",
    )
    model_response = json.dumps({
        "summary": "Security review finding.",
        "findings": [{
            "title": "Validate the input before processing",
            "issue_type": "security",
            "explanation": "The code uses input without validation.",
            "recommended_fix": "Validate the input before use.",
            "affected_file": "src/app.py", "line_start": 4, "line_end": 4,
            "corrected_code": None, "evidence_ids": ["rag-1"],
            "uncertain": False, "abstained": False,
            "risk_priority": None, "verification_status": "not_checked",
        }],
    })
    parsed = parse_review_response(model_response, [evidence])
    assert parsed.valid and parsed.findings[0].uncertain is False
    normalized_finding = _finding_payload(
        parsed.findings[0], "src/app.py",
        DiffMapping("mapped", added_lines=frozenset({4})), True,
    )
    assert normalized_finding["evidence_ids"] == ("rag-1",)

    file_result = PRFileResult(
        path="src/app.py", status="modified", additions=1, deletions=0,
        review_status="reviewed", findings=[normalized_finding],
        evidence=[asdict(evidence)],
    )
    review = PRAnalysisResult(
        repository="acme/demo", owner="acme", repository_name="demo", number=17,
        title="Review", state="open", base_sha=SHA, head_sha=SHA,
        html_url="https://github.com/acme/demo/pull/17", total_changed_files=1,
        files_received=1, files_reviewed=1, files_skipped=0, files_failed=0,
        completion_status="complete", pagination_truncated=False, pagination_error=None,
        findings_by_severity={"unknown": 1}, files=[file_result],
        head_repository="acme/demo", head_branch="feature",
    ).to_dict()
    review["provider_identity"] = "test-provider"
    assert isinstance(review["files"][0]["findings"][0]["evidence_ids"], tuple)

    store = WebhookDeliveryStore(tmp_path / "tuple-evidence.sqlite3")
    store.initialize()
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="mocked-action-token",
        transport=lambda method, url, headers, body: (
            requests.append((method, url, json.loads(body))) or {"id": 456}
        ),
    )
    coordinator = GitHubActionCoordinator(
        store, read_client=adapter.read_client, adapter=adapter,
        configs={"acme/demo": RepositoryActionConfig(publish_review_comments=True)},
    )
    event = {"delivery_id": "tuple-evidence-delivery", "owner": "acme",
             "repository": "demo", "number": 17, "base_sha": SHA, "head_sha": SHA}
    outcome = coordinator.process_review(event, review)

    assert outcome["actions"][0]["action"] == "review_comment"
    assert outcome["actions"][0]["eligible"] is True
    assert outcome["actions"][0]["status"] == "published"
    assert "security_or_high_severity" in outcome["actions"][0]["denial_reasons"]
    assert len(requests) == 1
    assert requests[0][0] == "POST"
    assert requests[0][1].endswith("/repos/acme/demo/pulls/17/comments")
    assert "rag-1" in requests[0][2]["body"]


@pytest.mark.parametrize("finding,evidence", [
    (valid_finding(evidence_ids=["not-present"]), [{"evidence_id": "rag-1"}]),
    (valid_finding(uncertain=True), [{"evidence_id": "rag-1"}]),
    (valid_finding(evidence_ids=[]), [{"evidence_id": "rag-1"}]),
])
def test_coordinator_keeps_invalid_or_uncertain_evidence_blocked(tmp_path, finding, evidence):
    store = WebhookDeliveryStore(tmp_path / f"evidence-denial-{finding['finding_id']}-{finding['uncertain']}-{len(finding['evidence_ids'])}.sqlite3")
    store.initialize()
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="mocked-action-token",
        transport=lambda *args: requests.append(args),
    )
    coordinator = GitHubActionCoordinator(
        store, read_client=adapter.read_client, adapter=adapter,
        configs={"acme/demo": RepositoryActionConfig(publish_review_comments=True)},
    )
    event = {"delivery_id": "invalid-evidence", "owner": "acme", "repository": "demo",
             "number": 17, "base_sha": SHA, "head_sha": SHA}
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "files": [{"path": "src/app.py", "status": "modified",
              "evidence": evidence, "findings": [finding]}]}
    result = coordinator.process_review(event, review)
    assert result["actions"][0]["action"] == "no_external_action"
    assert result["actions"][0]["denial_reasons"] == ["invalid_or_insufficient_finding_evidence"]
    assert requests == []


def test_coordinator_routes_unmappable_findings_to_one_policy_gated_general_comment(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "general-actions.sqlite3")
    store.initialize()
    requests = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="write-token",
        transport=lambda method, url, headers, body: (
            requests.append((method, url, json.loads(body))) or {"id": 99}
        ),
    )
    coordinator = GitHubActionCoordinator(
        store, read_client=adapter.read_client, adapter=adapter,
        configs={"acme/demo": RepositoryActionConfig(publish_review_comments=True)},
    )
    event = {"delivery_id": "delivery-general", "owner": "acme", "repository": "demo", "number": 17,
             "base_sha": SHA, "head_sha": SHA}
    finding = valid_finding(inline_supported=False, source_line=None, line_start=None, line_end=None)
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "files": [{"path": "src/app.py", "status": "modified",
              "evidence": [{"evidence_id": "rag-1"}], "findings": [finding]}]}

    first = coordinator.process_review(event, review)
    second = coordinator.process_review(event, review)
    assert first["actions"][0]["status"] == "published"
    assert second["actions"][0]["status"] == "duplicate_suppressed"
    assert len(requests) == 1 and requests[0][1].endswith("/issues/17/comments")
    record = store.get_actions("delivery-general")[0]
    assert record["policy_decision"]["comment_kind"] == "general"
    assert record["github_comment_id"] == 99


def test_ambiguous_comment_write_is_recorded_unknown_and_never_retried(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "unknown.sqlite3")
    store.initialize()
    calls = []
    def timeout(*_args):
        calls.append(1)
        raise GitHubPRException("github_timeout", 504, "unknown")
    adapter = GitHubMCPActionAdapter(read_client=ReadClient(), token="write-token", transport=timeout)
    coordinator = GitHubActionCoordinator(
        store, read_client=adapter.read_client, adapter=adapter,
        configs={"acme/demo": RepositoryActionConfig(publish_review_comments=True)},
    )
    event = {"delivery_id": "delivery-2", "owner": "acme", "repository": "demo", "number": 17,
             "base_sha": SHA, "head_sha": SHA}
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "files": [{"path": "src/app.py", "status": "modified",
              "evidence": [{"evidence_id": "rag-1"}], "findings": [valid_finding()]}]}
    assert coordinator.process_review(event, review)["actions"][0]["status"] == "unknown"
    assert coordinator.process_review(event, review)["actions"][0]["status"] == "duplicate_suppressed"
    assert calls == [1]


def test_concurrent_action_reservations_publish_one_comment(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "concurrent-actions.sqlite3")
    store.initialize()
    writes = []
    adapter = GitHubMCPActionAdapter(
        read_client=ReadClient(), token="write-token",
        transport=lambda method, url, headers, body: (writes.append(1) or {"id": 88}),
    )
    coordinator = GitHubActionCoordinator(
        store, read_client=adapter.read_client, adapter=adapter,
        configs={"acme/demo": RepositoryActionConfig(publish_review_comments=True)},
    )
    event = {"delivery_id": "delivery-concurrent", "owner": "acme", "repository": "demo", "number": 17,
             "base_sha": SHA, "head_sha": SHA}
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "files": [{"path": "src/app.py", "status": "modified",
              "evidence": [{"evidence_id": "rag-1"}], "findings": [valid_finding()]}]}
    barrier = threading.Barrier(2)
    def publish():
        barrier.wait()
        return coordinator.process_review(event, review)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: publish(), range(2)))
    assert len(writes) == 1
    statuses = [item["actions"][0]["status"] for item in results]
    assert statuses.count("published") == 1
    assert statuses.count("duplicate_suppressed") == 1


def test_unvalidated_llm_evidence_and_default_policy_never_publish(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "default-deny.sqlite3")
    store.initialize()
    calls = []
    adapter = GitHubMCPActionAdapter(read_client=ReadClient(), token="write-token",
                                     transport=lambda *args: calls.append(args))
    coordinator = GitHubActionCoordinator(store, read_client=adapter.read_client, adapter=adapter, configs={})
    event = {"delivery_id": "delivery-default", "owner": "acme", "repository": "demo", "number": 17,
             "base_sha": SHA, "head_sha": SHA}
    review = {"repository": "acme/demo", "head_sha": SHA, "head_repository": "acme/demo",
              "head_branch": "feature", "files": [{"path": "src/app.py", "status": "modified",
              "evidence": [], "findings": [valid_finding()]}]}
    result = coordinator.process_review(event, review)
    assert result["mode"] == "review_only"
    assert result["actions"][0]["action"] == "no_external_action"
    assert calls == []


def test_interrupted_inflight_write_becomes_unknown_after_restart(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "restart-actions.sqlite3")
    store.initialize()
    created, action_id = store.record_action(
        delivery_id="delivery-restart", repository="acme/demo", pr_number=17,
        base_sha=SHA, head_sha=SHA, finding_id="finding-1", action_type="review_comment",
        policy_decision={"action": "review_comment", "eligible": True},
        publication_status="pending",
    )
    assert created and store.claim_action(action_id)
    store.recover_after_restart()
    record = store.get_actions("delivery-restart")[0]
    assert record["publication_status"] == "requires_reconciliation"
    assert record["failure_code"] == "worker_interrupted_unknown_completion"
    assert record["lifecycle_state"] == "requires_reconciliation"


def test_only_known_failures_can_enter_bounded_retry_schedule(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "retry.sqlite3")
    store.initialize()
    _, action_id = store.record_action(
        delivery_id="delivery-retry", repository="acme/demo", pr_number=17,
        base_sha=SHA, head_sha=SHA, finding_id="finding-1", action_type="review_comment",
        policy_decision={"action": "review_comment"}, publication_status="pending",
    )
    assert store.claim_action(action_id)
    store.finish_action(action_id, "failed", failure_code="github_rate_limited")
    scheduled, run_at = store.schedule_action_retry(action_id, retryable=True)
    assert scheduled and run_at is not None
    assert store.claim_action(action_id) is False  # Backoff has not elapsed.
    store.finish_action(action_id, "requires_reconciliation")
    assert store.schedule_action_retry(action_id, retryable=True) == (False, None)


def test_action_lifecycle_transitions_are_durable_and_ordered(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "lifecycle.sqlite3")
    _, action_id = store.record_action(
        delivery_id="delivery-lifecycle", repository="acme/demo", pr_number=17,
        base_sha=SHA, head_sha=SHA, finding_id="finding-1", action_type="autonomous_fix",
        policy_decision={"action": "potential_autonomous_fix"},
        publication_status="pending_required_verification",
    )
    assert store.get_actions("delivery-lifecycle")[0]["lifecycle_state"] == "awaiting_verification"
    assert store.transition_action(action_id, "verified", verification_status="passed")
    assert not store.transition_action(action_id, "failed")
    assert store.transition_action(action_id, "publishing")
    assert store.transition_action(action_id, "completed")
    assert store.get_actions("delivery-lifecycle")[0]["lifecycle_state"] == "completed"
