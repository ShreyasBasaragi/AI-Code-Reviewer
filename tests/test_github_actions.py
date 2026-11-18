import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from fix_verification import generate_unified_diff
from mcp_server.github_action_coordinator import GitHubActionCoordinator
from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_policy import PolicyDecision, RepositoryActionConfig
from mcp_server.github_pr import GitHubPRException, PullRequestRef
from mcp_server.github_webhook import WebhookDeliveryStore


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
    assert prepared["commit_execution"] == "AUTONOMOUS_COMMIT_DISABLED"
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


def test_commit_preparation_checks_branch_identity_but_execution_is_always_disabled():
    read = ReadClient()
    calls = []
    adapter = GitHubMCPActionAdapter(read_client=read, token="t", transport=lambda *args: calls.append(args))
    plan = adapter.prepare_commit(
        REF, repository="acme/demo", branch="feature", expected_head_sha=SHA,
        is_fork=False, diff="--- a/src/app.py\n+++ b/src/app.py\n@@\n-a\n+b\n",
    )
    assert plan["status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert plan["commit_sha"] is None and plan["force_push"] is False
    assert "autonomous_commit_execution_disabled_in_j2" in plan["denial_reasons"]
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
    assert result["commit_execution"] == "AUTONOMOUS_COMMIT_DISABLED"
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
