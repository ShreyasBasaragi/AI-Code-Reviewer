"""Offline tests for the production autonomous-fix orchestration and GitHub API path."""
from __future__ import annotations

import hashlib
import json

from fix_generation import FixCandidate
from mcp_server.autonomous_fixes import AutonomousFixExecutor
from mcp_server.behavioral_verification import BehavioralVerifier
from mcp_server.github_action_coordinator import GitHubActionCoordinator
from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_policy import RepositoryActionConfig
from mcp_server.github_pr import PullRequestRef
from mcp_server.github_pr import GitHubPRException
from mcp_server.github_webhook import WebhookDeliveryStore
from review_schema import RetrievedEvidence, parse_review_response


HEAD = "a" * 40
CANDIDATE = "c" * 40
TREE = "d" * 40
BLOB = "e" * 40
REF = PullRequestRef("acme", "demo", 7)
ORIGINAL = "name=1\n"
CORRECTED = "name = 1\n"
PATH = "src/app.py"
OPERATION = "f" * 32


class ReadClient:
    def __init__(self, *, ci="passed", workflow="7", permissions=True,
                 fork=False, stale_after_stage=False):
        self.sha = HEAD
        self.branch_sha = HEAD
        self.ci = ci
        self.workflow = workflow
        self.permissions = permissions
        self.fork = fork
        self.stale_after_stage = stale_after_stage
        self.after_staging = False

    def get_pull(self, ref):
        head_owner = "other" if self.fork else "acme"
        sha = self.sha
        if self.after_staging and self.stale_after_stage:
            sha = "b" * 40
        return {
            "number": ref.number, "state": "open",
            "base": {"sha": "9" * 40, "ref": "main",
                     "repo": {"owner": {"login": "acme"}, "name": "demo"}},
            "head": {"sha": sha, "ref": "feature/fix",
                     "repo": {"owner": {"login": head_owner}, "name": "demo"}},
        }

    def get_file(self, ref, path, sha, *, max_source_bytes):
        if sha == CANDIDATE:
            return CORRECTED
        return ORIGINAL

    def get_files(self, ref, *, max_files, max_pages):
        return ([{"filename": PATH, "status": "modified"}], False, 1, None)

    def get_repository(self, owner, repository):
        return {"full_name": f"{owner}/{repository}", "default_branch": "main",
                "permissions": {"push": self.permissions}}

    def get_ci_evidence(self, ref, sha, expected_workflow_identity=None):
        status = self.ci
        actual_sha = sha if status != "sha_mismatch" else "b" * 40
        conclusion = "success" if status == "passed" else "failure" if status == "failed" else None
        check_status = "completed" if status in {"passed", "failed"} else "in_progress"
        return {"head_sha": actual_sha, "statuses": [], "workflow_runs": [{
            "workflow_id": int(expected_workflow_identity or 7), "head_sha": actual_sha,
            "event": "workflow_dispatch",
            "status": "completed" if status in {"passed", "failed"} else "in_progress",
            "conclusion": conclusion, "check_suite_id": 70,
        }], "check_runs": [{
            "name": "unit", "head_sha": actual_sha, "status": check_status,
            "conclusion": conclusion, "check_suite": {"id": 70},
            "workflow_identity": self.workflow,
        }]}


class APITransport:
    def __init__(self, read: ReadClient, *, invalid_tree=False, response_unknown=False,
                 concurrent_ref=None):
        self.read = read
        self.calls = []
        self.invalid_tree = invalid_tree
        self.response_unknown = response_unknown
        self.concurrent_ref = concurrent_ref
        self.candidate_ref_created = False

    def __call__(self, method, url, headers, body):
        payload = json.loads(body) if body else {}
        self.calls.append((method, url, headers, payload))
        if url.endswith("/repos/acme/demo") and method == "GET":
            return self.read.get_repository("acme", "demo")
        if url.endswith(f"/git/commits/{HEAD}") and method == "GET":
            return {"sha": HEAD, "tree": {"sha": TREE}, "parents": []}
        if url.endswith(f"/git/commits/{CANDIDATE}") and method == "GET":
            return {"sha": CANDIDATE, "tree": {"sha": TREE}, "parents": [{"sha": HEAD}]}
        if "/git/ref/heads/codementor-candidates/" in url and method == "GET":
            if self.candidate_ref_created:
                return {"object": {"sha": CANDIDATE}}
            raise GitHubPRException("github_not_found", 404, "not found")
        if url.endswith("/git/blobs") and method == "POST":
            assert payload == {"content": CORRECTED, "encoding": "utf-8"}
            return {"sha": BLOB}
        if url.endswith("/git/trees") and method == "POST":
            if self.invalid_tree:
                return {"sha": "invalid", "tree": []}
            assert payload["base_tree"] == TREE
            assert payload["tree"] == [{"path": PATH, "mode": "100644", "type": "blob", "sha": BLOB}]
            return {"sha": TREE, "tree": [{"path": PATH, "sha": BLOB}]}
        if url.endswith("/git/commits") and method == "POST":
            assert payload["parents"] == [HEAD]
            return {"sha": CANDIDATE, "parents": [{"sha": HEAD}]}
        if url.endswith("/git/refs") and method == "POST":
            self.candidate_ref_created = True
            self.read.after_staging = True
            return {"object": {"sha": CANDIDATE}}
        if "/actions/workflows/7/dispatches" in url and method == "POST":
            assert payload.get("ref", "").startswith("codementor-candidates/")
            assert headers.get("Authorization") == "Bearer stage-only"
            return {}
        if "/git/ref/heads/feature/fix" in url and method == "GET":
            if self.concurrent_ref:
                self.read.branch_sha = self.concurrent_ref
            return {"object": {"sha": self.read.branch_sha}}
        if "/git/refs/heads/feature/fix" in url and method == "PATCH":
            assert payload == {"sha": CANDIDATE, "force": False}
            if self.read.branch_sha != HEAD:
                raise AssertionError("non-fast-forward concurrent update must not be overwritten")
            self.read.branch_sha = CANDIDATE
            self.read.sha = CANDIDATE
            if self.response_unknown:
                raise TimeoutError("write response lost")
            return {"object": {"sha": CANDIDATE}}
        raise AssertionError(f"unexpected mocked GitHub request: {method} {url}")


class AttestedBroker:
    def execute(self, **kwargs):
        candidate_hash = hashlib.sha256(kwargs["source"]).hexdigest()
        return {
            "status": "passed", "executed": True, "exit_code": 0,
            "candidate_sha256": candidate_hash, "changed_files": list(kwargs["changed_files"]),
            "network_isolated": True, "credentials_injected": False,
            "filesystem_restricted": True, "process_cleanup": True,
            "timeout_enforced": True, "memory_limit_bytes": kwargs["memory_bytes"],
            "cpu_limit_seconds": kwargs["cpu_seconds"], "duration_seconds": 0.1,
            "test_identity": "tests-style-contract-v1", "verifier_id": "offline-attested-broker",
        }

    def verify_attestation(self, report, **kwargs):
        return (report.get("candidate_sha256") == kwargs["candidate_sha256"]
                and report.get("test_identity") == "tests-style-contract-v1")


class FakeFixProvider:
    def generate_fix(self, _source, selected_finding, _context):
        return json.dumps({
            "finding_ref": selected_finding.finding_id,
            "corrected_code": CORRECTED,
            "explanation": "Apply project assignment spacing.",
            "assumptions": [], "evidence_ids": ["rag-1"],
        })


def validated_review_response():
    evidence = RetrievedEvidence(
        evidence_id="rag-1", source_family="test", source_url=None,
        rule_identifier=None, text="Use spaces around assignment operators.",
    )
    raw = json.dumps({
        "summary": "One low-risk formatting finding.",
        "findings": [{
            "title": "Use the project assignment spacing", "issue_type": "style",
            "explanation": "The assignment uses inconsistent spacing.",
            "recommended_fix": "Use spaces around the assignment operator.",
            "affected_file": PATH, "line_start": 1, "line_end": 1,
            "corrected_code": None, "evidence_ids": ["rag-1"],
            "uncertain": False, "abstained": False,
            "risk_priority": None, "verification_status": "not_checked",
        }],
    })
    response = parse_review_response(raw, [evidence])
    assert response.valid
    return response


def finding(**updates):
    value = {
        "finding_id": "finding-1", "origin": "llm", "rule_id": None,
        "issue_type": "style", "title": "Use the project assignment spacing",
        "explanation": "The assignment uses inconsistent spacing.",
        "recommended_fix": "Use spaces around the assignment operator.",
        "evidence_ids": ["rag-1"], "uncertain": False, "abstained": False,
        "severity": "low", "path": PATH,
    }
    value.update(updates)
    return value


def config(**updates):
    value = RepositoryActionConfig(
        enable_autonomous_fixes=True, allowed_low_risk_categories=("style",),
        permitted_branches=("feature/fix",), required_checks=("syntax", "static", "behavioral", "ci"),
        required_github_checks=("unit",), confidence_threshold=0.80, max_changed_lines=3,
    )
    for key, item in updates.items():
        object.__setattr__(value, key, item)
    return value


def review_payload(item=None):
    return {
        "repository": "acme/demo", "head_sha": HEAD, "base_sha": "9" * 40,
        "head_repository": "acme/demo", "head_branch": "feature/fix",
        "provider_identity": "test-model-v1", "completion_status": "complete", "stale": False,
        "files": [{"path": PATH, "status": "modified", "evidence": [{"evidence_id": "rag-1"}],
                   "findings": [item or finding()]}],
    }


def make_coordinator(tmp_path, *, ci="passed", workflow="7", permissions=True,
                     fork=False, stale_after_stage=False, invalid_tree=False,
                     response_unknown=False, concurrent_ref=None, confidence=None,
                     staging=True, commits=True, categories=None):
    read = ReadClient(ci=ci, workflow=workflow, permissions=permissions, fork=fork,
                      stale_after_stage=stale_after_stage)
    transport = APITransport(read, invalid_tree=invalid_tree,
                              response_unknown=response_unknown, concurrent_ref=concurrent_ref)
    adapter = GitHubMCPActionAdapter(
        read_client=read, token="comment-token", staging_token="stage-only",
        commit_token="commit-only", staging_enabled=staging, commit_enabled=commits,
        transport=transport,
    )
    store = WebhookDeliveryStore(tmp_path / "actions.sqlite3")
    store.initialize()
    evidence = confidence or {
        "calibrated": True, "manifest_verified": True,
        "model_id": "test-model-v1", "dataset_id": "independent-v1",
        "finding_category": "style", "calibrated_confidence": 0.95,
        "sample_count": 400, "expected_calibration_error": 0.02,
        "artifact_sha256": "1" * 64,
    }
    executor = AutonomousFixExecutor(
        store=store, adapter=adapter,
        fix_generator=lambda response, finding_id, source: __import__(
            "mcp_server.orchestrator", fromlist=["generate_fix_candidate"]
        ).generate_fix_candidate(response, finding_id, source, llm=FakeFixProvider()),
        confidence_resolver=lambda _finding: evidence,
        behavioral_verifier=BehavioralVerifier(AttestedBroker()),
        behavioral_tests=lambda _path: b"signed test bundle",
        workflow_identity="7",
    )
    coordinator = GitHubActionCoordinator(
        store, read_client=read, adapter=adapter,
        configs={"acme/demo": config(allowed_low_risk_categories=categories or ("style",))},
        autonomous_executor=executor,
    )
    event = {"delivery_id": "delivery-1", "owner": "acme", "repository": "demo",
             "number": 7, "base_sha": "9" * 40, "head_sha": HEAD, "action": "opened"}
    runtime = {PATH: {"source_code": ORIGINAL, "review_response": validated_review_response()}}
    return coordinator, store, adapter, transport, event, runtime


def test_webhook_review_runs_full_mocked_fix_stage_ci_and_nonforce_commit(tmp_path):
    coordinator, store, adapter, transport, event, runtime = make_coordinator(tmp_path)
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = result["actions"][0]
    assert action["status"] == "published"
    assert action["commit_sha"] == CANDIDATE
    assert action["candidate_branch"] == f"codementor-candidates/{action['action_id']}"
    assert action["behavioral"]["attestation_verified"] is True
    assert action["ci_dispatch"]["status"] == "dispatched"
    assert action["ci"]["status"] == "passed"
    assert store.get_actions("delivery-1")[0]["commit_sha"] == CANDIDATE
    writes = [(method, url, headers, body) for method, url, headers, body in transport.calls
              if method in {"POST", "PATCH"}]
    assert [method for method, *_ in writes] == ["POST", "POST", "POST", "POST", "POST", "PATCH"]
    assert all(headers.get("Authorization") == "Bearer stage-only" for _, url, headers, _ in writes[:-1])
    assert writes[-1][2]["Authorization"] == "Bearer commit-only"
    assert writes[-1][3]["force"] is False
    gates = {key: True for key in (
        "repository_opt_in", "eligible_low_risk_category", "calibration_supported",
        "source_hashes_valid", "syntax_passed", "static_passed", "behavioral_passed",
        "candidate_ci_passed", "scope_valid", "write_permission_valid",
        "durable_record_reserved", "idempotency_reserved",
    )}
    replay = adapter.execute_commit(
        ref=REF, repository="acme/demo", branch="feature/fix", expected_head_sha=HEAD,
        candidate_sha=CANDIDATE, operation_id=action["action_id"], file_path=PATH,
        corrected_code=CORRECTED, behavioral_evidence=action["behavioral"],
        ci_evidence={**action["ci"], "workflow_identity": "7"},
        confidence_evidence=action["confidence"], confidence_threshold=.80,
        required_checks=("unit",), changed_lines=2, permitted_branches=("feature/fix",),
        gates=gates,
    )
    assert replay.status == "published"
    assert replay.detail["idempotent_replay"] is True
    assert sum(method == "PATCH" for method, *_ in transport.calls) == 1


def test_security_and_uncalibrated_findings_fall_back_without_github_writes(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path)
    result = coordinator.process_review(event, review_payload(finding(issue_type="security")), runtime_files=runtime)
    assert result["actions"][0]["status"] == "review_only"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path / "uncalibrated", confidence=None,
    )
    coordinator.autonomous_executor.confidence_resolver = lambda _finding: None
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert result["actions"][0]["manual_review_fallback"] is True
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_disabled_write_switches_and_fork_prs_never_write(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path, staging=False)
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path / "fork", fork=True)
    pr = review_payload()
    pr["head_repository"] = "other/demo"
    result = coordinator.process_review(event, pr, runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_candidate_specific_ci_and_workflow_identity_are_required(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path, ci="sha_mismatch",
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "PATCH" for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path / "wrong-workflow", workflow="8",
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "PATCH" for method, *_ in transport.calls)


def test_candidate_ci_dispatches_but_pending_checks_never_finalize(tmp_path, monkeypatch):
    monkeypatch.setenv("GITHUB_CANDIDATE_CI_WAIT_SECONDS", "0")
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path, ci="pending")
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert result["actions"][0]["ci"]["status"] == "pending"
    assert any("/actions/workflows/7/dispatches" in url for method, url, *_ in transport.calls
               if method == "POST")
    assert not any(method == "PATCH" for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path / "wrong-workflow", workflow="untrusted-workflow",
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "PATCH" for method, *_ in transport.calls)


def test_tree_and_permission_failures_do_not_finalize(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path, invalid_tree=True)
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "PATCH" for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path / "no-push", permissions=False,
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "POST" for method, *_ in transport.calls)


def test_stale_head_and_concurrent_branch_updates_are_never_overwritten(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path, stale_after_stage=True,
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "PATCH" for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(
        tmp_path / "race", concurrent_ref="b" * 40,
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "stale"
    assert not any(method == "PATCH" for method, *_ in transport.calls)


def test_ambiguous_commit_is_persisted_for_reconciliation_and_not_retried(tmp_path):
    coordinator, store, adapter, transport, event, runtime = make_coordinator(
        tmp_path, response_unknown=True,
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = store.get_actions("delivery-1")[0]
    assert result["actions"][0]["status"] == "unknown"
    assert action["publication_status"] == "requires_reconciliation"
    assert sum(method == "PATCH" for method, *_ in transport.calls) == 1


def test_duplicate_delivery_action_reservation_is_idempotent(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "duplicate.sqlite3")
    store.initialize()
    first, action_id = store.record_action(
        delivery_id="delivery-1", repository="acme/demo", pr_number=7, base_sha=HEAD,
        head_sha=HEAD, finding_id="finding-1", action_type="autonomous_fix",
        policy_decision={"action": "potential_autonomous_fix"}, publication_status="pending",
    )
    second, second_id = store.record_action(
        delivery_id="delivery-2", repository="acme/demo", pr_number=7, base_sha=HEAD,
        head_sha=HEAD, finding_id="finding-1", action_type="autonomous_fix",
        policy_decision={"action": "potential_autonomous_fix"}, publication_status="pending",
    )
    assert first and not second and action_id == second_id
    assert store.claim_action(action_id)
    assert not store.claim_action(second_id)


def test_inflight_candidate_commit_is_recognized_from_durable_ledger(tmp_path):
    store = WebhookDeliveryStore(tmp_path / "self-trigger.sqlite3")
    store.initialize()
    _created, action_id = store.record_action(
        delivery_id="delivery-1", repository="acme/demo", pr_number=7, base_sha=HEAD,
        head_sha=HEAD, finding_id="finding-1", action_type="autonomous_fix",
        policy_decision={"action": "potential_autonomous_fix"}, publication_status="pending",
    )
    candidate_sha = "c" * 40
    store.update_action_details(
        action_id, candidate_id="candidate-1", verification_status="passed",
        details={"candidate": {"candidate_sha": candidate_sha}, "commit_attempted": True},
    )
    assert store.is_codementor_commit("acme/demo", candidate_sha)
    assert not store.is_codementor_commit("acme/other", candidate_sha)
    assert not store.is_codementor_commit("acme/demo", "d" * 40)


def test_changed_line_cap_and_protected_path_fail_before_candidate_writes(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path)
    coordinator.autonomous_executor.fix_generator = lambda _response, finding_id, source: FixCandidate(
        candidate_id="too-large", finding_ref=finding_id, original_code=source,
        corrected_code="a\nb\nc\nd\ne\n", explanation="too large", assumptions=(),
        evidence_ids=("rag-1",), generation_status="generated",
    )
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "POST" for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path / "protected")
    file_result = {"path": "security/auth.py", "status": "modified",
                   "evidence": [{"evidence_id": "rag-1"}], "findings": [finding(path="security/auth.py")]}
    review = review_payload()
    review["files"] = [file_result]
    runtime = {"security/auth.py": {"source_code": ORIGINAL, "review_response": validated_review_response()}}
    result = coordinator.process_review(event, review, runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert not any(method == "POST" for method, *_ in transport.calls)


def test_runtime_calibration_loader_accepts_only_signed_current_matching_artifact(tmp_path, monkeypatch):
    import hmac
    import os
    import time
    from mcp_server.confidence import CALIBRATION_VERSION, load_runtime_confidence

    key = "calibration-test-key-that-is-long-enough-32"
    monkeypatch.setenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", key)
    document = {
        "version": CALIBRATION_VERSION, "model_id": "test-model-v1",
        "dataset_id": "independent-v1", "finding_category": "style",
        "calibrated_confidence": 0.91, "sample_count": 400,
        "expected_calibration_error": 0.02, "manifest_verified": True,
        "issued_at": time.time() - 1, "expires_at": time.time() + 3600,
    }
    canonical = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
    document["signature"] = hmac.new(key.encode(), canonical, hashlib.sha256).hexdigest()
    path = tmp_path / "calibration.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    result = load_runtime_confidence(path, model_id="test-model-v1", dataset_id="independent-v1",
                                    finding_category="style")
    assert result["calibrated"] and result["calibrated_confidence"] == 0.91
    assert result["artifact_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert load_runtime_confidence(path, model_id="other-model", dataset_id="independent-v1",
                                   finding_category="style") is None
    assert load_runtime_confidence(path, model_id="test-model-v1", dataset_id="independent-v1",
                                   finding_category="security") is None
    document["calibrated_confidence"] = 0.79
    path.write_text(json.dumps(document), encoding="utf-8")
    assert load_runtime_confidence(path, model_id="test-model-v1", dataset_id="independent-v1",
                                   finding_category="style") is None


def test_remote_behavioral_broker_binds_candidate_tests_and_signed_fresh_attestation(monkeypatch):
    import hmac
    import time
    from contextlib import contextmanager
    from mcp_server.behavioral_verification import RemoteHTTPSExecutionBroker

    key = "sandbox-hmac-key-that-is-longer-than-thirty-two-bytes"
    candidate = b"value = 2\n"
    original = b"value = 1\n"
    sha = hashlib.sha256(candidate).hexdigest()
    received = {}

    class Response:
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def read(self, _limit): return json.dumps(received["report"]).encode()

    class Opener:
        def open(self, request, timeout):
            received["request"] = json.loads(request.data)
            unsigned = {
                "request_id": received["request"]["request_id"],
                "status": "passed", "executed": True, "exit_code": 0,
                "candidate_sha256": sha, "changed_files": ["src/app.py"],
                "network_isolated": True, "credentials_injected": False,
                "filesystem_restricted": True, "process_cleanup": True,
                "timeout_enforced": True, "timeout_seconds": 30,
                "memory_limit_bytes": 512 * 1024 * 1024, "cpu_limit_seconds": 20,
                "test_identity": "offline-contract-tests-v1", "verifier_id": "sandbox-prod-v3",
                "timestamp": time.time(), "duration_seconds": 0.2,
            }
            unsigned["signature"] = hmac.new(
                key.encode(), json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
            received["report"] = unsigned
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    broker = RemoteHTTPSExecutionBroker("https://executor.example/verify", key=key)
    result = BehavioralVerifier(broker).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        tests=b"test bundle", changed_files=("src/app.py",),
    )
    assert result.status == "passed" and result.attestation_verified
    request = received["request"]
    assert request["candidate_sha256"] == sha
    assert request["network"] is False and request["credentials"] == {}
    assert request["changed_files"] == ["src/app.py"]
    received["report"]["candidate_sha256"] = "b" * 64
    assert broker.verify_attestation(received["report"], candidate_sha256=sha,
                                     changed_files=("src/app.py",), timeout_seconds=30,
                                     memory_bytes=512 * 1024 * 1024, cpu_seconds=20) is False
