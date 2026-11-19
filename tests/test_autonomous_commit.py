"""Offline tests for the production autonomous-fix orchestration and GitHub API path."""
from __future__ import annotations

import hashlib
import json
import base64
import time
from dataclasses import asdict

from fix_generation import FixCandidate
from mcp_server.autonomous_fixes import AutonomousFixExecutor
from mcp_server.behavioral_verification import BehavioralVerificationResult, BehavioralVerifier
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
                 fork=False, stale_after_stage=False, source=ORIGINAL,
                 corrected=CORRECTED, file_path=PATH, branch="feature/fix"):
        self.sha = HEAD
        self.branch_sha = HEAD
        self.ci = ci
        self.workflow = workflow
        self.permissions = permissions
        self.fork = fork
        self.stale_after_stage = stale_after_stage
        self.after_staging = False
        self.branch = branch
        self.source = source
        self.corrected = corrected
        self.file_path = file_path

    def get_pull(self, ref):
        head_owner = "other" if self.fork else "acme"
        sha = self.sha
        if self.after_staging and self.stale_after_stage:
            sha = "b" * 40
        return {
            "number": ref.number, "state": "open",
            "base": {"sha": "9" * 40, "ref": "main",
                     "repo": {"owner": {"login": "acme"}, "name": "demo"}},
            "head": {"sha": sha, "ref": self.branch,
                     "repo": {"owner": {"login": head_owner}, "name": "demo"}},
        }

    def get_file(self, ref, path, sha, *, max_source_bytes):
        if sha == CANDIDATE:
            return self.corrected
        return self.source

    def get_files(self, ref, *, max_files, max_pages):
        return ([{"filename": self.file_path, "status": "modified"}], False, 1, None)

    def get_repository(self, owner, repository):
        return {"full_name": f"{owner}/{repository}", "default_branch": "main",
                "permissions": {"push": self.permissions}}

    def get_ci_evidence(self, ref, sha, expected_workflow_identity=None,
                        *, workflow_run_branch=None, dispatched_after=None):
        status = self.ci
        actual_sha = sha if status != "sha_mismatch" else "b" * 40
        conclusion = "success" if status == "passed" else "failure" if status == "failed" else None
        check_status = "completed" if status in {"passed", "failed"} else "in_progress"
        return {"head_sha": actual_sha, "statuses": [], "workflow_runs": [{
            "id": 71,
            "workflow_id": int(expected_workflow_identity or 7), "head_sha": actual_sha,
            "head_branch": workflow_run_branch,
            "display_title": f"CodeMentor candidate verification {sha}",
            "candidate_sha": sha,
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 10)),
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
            assert payload == {"content": self.read.corrected, "encoding": "utf-8"}
            return {"sha": BLOB}
        if url.endswith("/git/trees") and method == "POST":
            if self.invalid_tree:
                return {"sha": "invalid", "tree": []}
            assert payload["base_tree"] == TREE
            assert payload["tree"] == [{"path": self.read.file_path, "mode": "100644", "type": "blob", "sha": BLOB}]
            return {"sha": TREE, "tree": [{"path": self.read.file_path, "sha": BLOB}]}
        if url.endswith("/git/commits") and method == "POST":
            assert payload["parents"] == [HEAD]
            return {"sha": CANDIDATE, "parents": [{"sha": HEAD}]}
        if url.endswith("/git/refs") and method == "POST":
            self.candidate_ref_created = True
            self.read.after_staging = True
            return {"object": {"sha": CANDIDATE}}
        if "/actions/workflows/7/dispatches" in url and method == "POST":
            assert payload.get("ref") == "main"
            assert payload.get("inputs", {}).get("candidate_sha") == CANDIDATE
            assert headers.get("Authorization") == "Bearer stage-only"
            return {}
        if f"/git/ref/heads/{self.read.branch}" in url and method == "GET":
            if self.concurrent_ref:
                self.read.branch_sha = self.concurrent_ref
            return {"object": {"sha": self.read.branch_sha}}
        if f"/git/refs/heads/{self.read.branch}" in url and method == "PATCH":
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


class MockGitHubHostedVerifier:
    def __init__(self, status="passed"):
        self.status = status
        self.arguments = None

    def verify(self, **kwargs):
        self.arguments = kwargs
        return BehavioralVerificationResult(
            self.status, "github_hosted_docker", hashlib.sha256(kwargs["original_source"]).hexdigest(),
            hashlib.sha256(kwargs["candidate_source"]).hexdigest(), kwargs["changed_files"],
            executed=True, exit_code=0 if self.status == "passed" else 1,
            test_identity="1" * 64, verifier_id="github-actions:99",
            attestation_verified=self.status == "passed",
            candidate_commit_sha=kwargs["candidate_commit_sha"],
        )


class FakeFixProvider:
    def generate_fix(self, _source, selected_finding, _context):
        return json.dumps({
            "finding_ref": selected_finding.finding_id,
            "corrected_code": CORRECTED,
            "explanation": "Apply project assignment spacing.",
            "assumptions": [], "evidence_ids": ["rag-1"],
        })


def validated_review_response():
    from dataclasses import replace
    from mcp_server.orchestrator import _static_review_finding
    from static_analysis import analyze_python

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
    static_findings = tuple(
        _static_review_finding(item)
        for item in analyze_python(ORIGINAL, file_path=PATH).findings
    )
    return replace(response, findings=(*response.findings, *static_findings),
                   static_analysis_status="failed" if static_findings else "passed")


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


def review_payload(item=None, *, file_path=PATH, branch="feature/fix"):
    return {
        "repository": "acme/demo", "head_sha": HEAD, "base_sha": "9" * 40,
        "head_repository": "acme/demo", "head_branch": branch,
        "provider_identity": "test-model-v1", "completion_status": "complete", "stale": False,
        "files": [{"path": file_path, "status": "modified", "evidence": [{"evidence_id": "rag-1"}],
                   "findings": [item or finding()]}],
    }


def make_coordinator(tmp_path, *, ci="passed", workflow="7", permissions=True,
                     fork=False, stale_after_stage=False, invalid_tree=False,
                     response_unknown=False, concurrent_ref=None, confidence=None,
                     staging=True, commits=True, categories=None, source=ORIGINAL,
                     corrected=CORRECTED, file_path=PATH, branch="feature/fix",
                     review_response=None, permitted_branches=None):
    read = ReadClient(ci=ci, workflow=workflow, permissions=permissions, fork=fork,
                      stale_after_stage=stale_after_stage, source=source,
                      corrected=corrected, file_path=file_path, branch=branch)
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
        "finding_category": (categories or ("style",))[0], "calibrated_confidence": 0.95,
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
        configs={"acme/demo": config(allowed_low_risk_categories=categories or ("style",),
                                      permitted_branches=permitted_branches or (branch,))},
        autonomous_executor=executor,
    )
    event = {"delivery_id": "delivery-1", "owner": "acme", "repository": "demo",
             "number": 7, "base_sha": "9" * 40, "head_sha": HEAD, "action": "opened"}
    runtime = {file_path: {"source_code": source,
                           "review_response": review_response or validated_review_response()}}
    return coordinator, store, adapter, transport, event, runtime


FORMAT_SAMPLE = (
    "def calculate_total(prices):\n"
    "    total=0\n"
    "    for price in prices:\n"
    "        total+=price\n"
    "    return total\n"
)
FORMAT_CORRECTED = (
    "def calculate_total(prices):\n"
    "    total = 0\n"
    "    for price in prices:\n"
    "        total+=price\n"
    "    return total\n"
)
FORMAT_PATH = "autocommit_demo/format_example.py"
FORMAT_BRANCH = "test/autocommit-final"


class NoFindingReviewProvider:
    def analyze_code(self, code, context):
        return json.dumps({"summary": "No model findings.", "findings": []})


class ExactFormattingFixProvider:
    def generate_fix(self, _source, selected_finding, _context):
        return json.dumps({
            "finding_ref": selected_finding.finding_id,
            "corrected_code": FORMAT_CORRECTED,
            "explanation": "Add the missing spaces around assignment operators.",
            "assumptions": [], "evidence_ids": [],
        })


def exact_formatting_case(tmp_path, *, branch=FORMAT_BRANCH, confidence=True):
    from mcp_server import orchestrator

    response = orchestrator.run_review_pipeline(
        FORMAT_SAMPLE, file_path=FORMAT_PATH, llm=NoFindingReviewProvider(),
        retriever=lambda _code: [], notify_desktop=False,
    ).review_response
    assert response.valid
    item = asdict(response.findings[0])
    item["path"] = FORMAT_PATH
    confidence_evidence = ({
        "calibrated": True, "manifest_verified": True,
        "confidence_source": "independent_calibration",
        "model_id": "test-model-v1", "dataset_id": "independent-formatting-test-v1",
        "finding_category": "formatting", "calibrated_confidence": 0.95,
        "sample_count": 400, "expected_calibration_error": 0.02,
        "artifact_sha256": "2" * 64,
    } if confidence else None)
    coordinator, store, adapter, transport, event, runtime = make_coordinator(
        tmp_path, confidence=confidence_evidence, categories=("formatting",),
        source=FORMAT_SAMPLE, corrected=FORMAT_CORRECTED,
        file_path=FORMAT_PATH, branch=branch, review_response=response,
        permitted_branches=(FORMAT_BRANCH,),
    )
    coordinator.autonomous_executor.fix_generator = lambda validated, finding_id, source: orchestrator.generate_fix_candidate(
        validated, finding_id, source, llm=ExactFormattingFixProvider(),
    )
    payload = review_payload(item, file_path=FORMAT_PATH, branch=branch)
    payload["files"][0]["evidence"] = []
    return coordinator, store, adapter, transport, event, runtime, payload


def test_exact_pr_formatting_finding_reaches_gated_commit_pipeline(tmp_path):
    coordinator, store, _adapter, transport, event, runtime, payload = exact_formatting_case(tmp_path)
    assert payload["files"][0]["findings"][0]["issue_type"] == "formatting"
    assert payload["files"][0]["findings"][0]["origin"] == "static"

    result = coordinator.process_review(event, payload, runtime_files=runtime)

    action = result["actions"][0]
    assert action["status"] == "published", action
    assert action["candidate_sha"] == CANDIDATE
    assert action["behavioral"]["attestation_verified"] is True
    assert action["ci"]["status"] == "passed"
    assert action["confidence"]["confidence_source"] == "independent_calibration"
    assert store.get_actions("delivery-1")[0]["publication_status"] == "published"
    assert sum(method == "PATCH" for method, *_ in transport.calls) == 1


def test_formatting_finding_still_blocks_without_confidence_evidence(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(
        tmp_path, confidence=False,
    )
    coordinator.autonomous_executor.confidence_resolver = lambda _finding: None
    result = coordinator.process_review(event, payload, runtime_files=runtime)
    assert result["actions"][0]["reason"] == "confidence_evidence_unavailable"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_formatting_fix_path_blocks_unsupported_category_and_unauthorized_branch(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(tmp_path)
    unsupported = json.loads(json.dumps(payload))
    unsupported["files"][0]["findings"][0]["issue_type"] = "security"
    result = coordinator.process_review(event, unsupported, runtime_files=runtime)
    assert result["actions"][0]["status"] == "review_only"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)

    coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(
        tmp_path / "unauthorized", branch="main",
    )
    result = coordinator.process_review(event, payload, runtime_files=runtime)
    assert result["actions"][0]["reason"] == "repository_scope_or_line_limit_denied"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_formatting_fix_path_blocks_when_behavioral_verification_is_missing(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(tmp_path)
    coordinator.autonomous_executor.behavioral_verifier = None
    coordinator.autonomous_executor.github_behavioral_verifier = None
    result = coordinator.process_review(event, payload, runtime_files=runtime)
    assert result["actions"][0]["reason"] == "behavioral_verification_provider_missing_or_ambiguous"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_webhook_review_runs_full_mocked_fix_stage_ci_and_nonforce_commit(tmp_path):
    coordinator, store, adapter, transport, event, runtime = make_coordinator(tmp_path)
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = result["actions"][0]
    assert action["status"] == "published", action
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


def _enable_formatting_prototype(monkeypatch):
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_POLICY", "academic_prototype")
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY", "acme/demo")
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH", FORMAT_BRANCH)


def test_static_formatting_prototype_requires_groq_assessment_and_preserves_origin(monkeypatch, tmp_path):
    _enable_formatting_prototype(monkeypatch)
    coordinator, store, _adapter, transport, event, runtime, payload = exact_formatting_case(tmp_path)
    coordinator.autonomous_executor.confidence_resolver = lambda _finding: None
    assessments = []
    coordinator.autonomous_executor.confidence_assessor = lambda finding, source: (
        assessments.append((finding["origin"], finding["rule_id"], source == FORMAT_SAMPLE))
        or {"assessment_source": "groq_independent_static_assessment",
            "assessment": "supports",
            "model_confidence_estimate": 0.91}
    )

    result = coordinator.process_review(event, payload, runtime_files=runtime)

    action = result["actions"][0]
    assert action["status"] == "published", action
    assert assessments == [("static", "PYFMT001", True)]
    assert action["confidence"]["confidence_source"] == "uncalibrated_model_estimate"
    assert action["confidence"]["calibrated"] is False
    assert action["confidence"]["finding_origin"] == "static"
    assert action["confidence"]["assessment_source"] == "groq_independent_static_assessment"
    assert action["commit_sha"] == CANDIDATE
    assert store.get_actions("delivery-1")[0]["commit_sha"] == CANDIDATE
    assert sum(method == "PATCH" for method, *_ in transport.calls) == 1


def test_static_formatting_prototype_blocks_missing_or_low_groq_assessment(monkeypatch, tmp_path):
    _enable_formatting_prototype(monkeypatch)
    for score in (None, 0.79):
        coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(
            tmp_path / str(score),
        )
        coordinator.autonomous_executor.confidence_resolver = lambda _finding: None
        coordinator.autonomous_executor.confidence_assessor = (
            lambda _finding, _source, score=score: None if score is None else {
                "assessment_source": "groq_independent_static_assessment",
                "assessment": "supports",
                "model_confidence_estimate": score,
            }
        )
        result = coordinator.process_review(event, payload, runtime_files=runtime)
        assert result["actions"][0]["reason"] == "confidence_evidence_unavailable"
        assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_static_formatting_assessor_is_not_called_outside_exact_prototype_branch(monkeypatch, tmp_path):
    _enable_formatting_prototype(monkeypatch)
    coordinator, _store, _adapter, transport, event, runtime, payload = exact_formatting_case(
        tmp_path, branch="test/other-disposable-branch",
    )
    coordinator.autonomous_executor.confidence_resolver = lambda _finding: None
    calls = []
    coordinator.autonomous_executor.confidence_assessor = lambda *_args: calls.append(True)
    result = coordinator.process_review(event, payload, runtime_files=runtime)
    assert calls == []
    assert result["actions"][0]["reason"] == "confidence_evidence_unavailable"
    assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


def test_tuple_evidence_ids_remain_valid_through_autonomous_fix_pipeline(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path)
    item = finding(evidence_ids=("rag-1",))
    result = coordinator.process_review(event, review_payload(item), runtime_files=runtime)
    assert result["actions"][0]["status"] == "published"
    assert result["actions"][0]["behavioral"]["attestation_verified"] is True
    assert any(method == "PATCH" for method, *_ in transport.calls)


def test_candidate_staging_and_ci_work_without_final_commit_authorization(tmp_path):
    coordinator, store, adapter, transport, event, runtime = make_coordinator(
        tmp_path, commits=False,
    )
    adapter.commit_token = None
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = result["actions"][0]
    assert action["status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert action["candidate_sha"] == CANDIDATE
    assert action["ci"]["status"] == "passed"
    assert action["manual_review_fallback"] is True
    assert any("/actions/workflows/7/dispatches" in url and method == "POST"
               for method, url, *_ in transport.calls)
    assert not any(method == "PATCH" for method, *_ in transport.calls)
    stored = store.get_actions("delivery-1")[0]
    assert stored["publication_status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert stored["verification_status"] == "passed"


def test_github_hosted_attestation_stays_staging_only_without_prototype_policy(tmp_path):
    coordinator, store, _adapter, transport, event, runtime = make_coordinator(tmp_path, commits=True)
    executor = coordinator.autonomous_executor
    hosted = MockGitHubHostedVerifier()
    executor.behavioral_verifier = None
    executor.github_behavioral_verifier = hosted
    executor.github_hosted_commit_policy = ""
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = result["actions"][0]
    assert action["status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert action["reason"] == "github_hosted_prototype_policy_not_selected"
    assert action["behavioral"]["isolation"] == "github_hosted_docker"
    assert action["behavioral"]["candidate_commit_sha"] == CANDIDATE
    assert hosted.arguments["source_workflow_run_id"] == 71
    assert not any(method == "PATCH" for method, *_ in transport.calls)
    assert store.get_actions("delivery-1")[0]["verification_status"] == "passed"


def test_unknown_github_hosted_commit_policy_fails_closed(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path, commits=True)
    executor = coordinator.autonomous_executor
    executor.behavioral_verifier = None
    executor.github_behavioral_verifier = MockGitHubHostedVerifier()
    executor.github_hosted_commit_policy = "future-policy"
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "AUTONOMOUS_COMMIT_DISABLED"
    assert not any(method == "PATCH" for method, *_ in transport.calls)


def test_github_hosted_prototype_policy_allows_gated_final_commit(tmp_path):
    coordinator, store, _adapter, transport, event, runtime = make_coordinator(tmp_path, commits=True)
    executor = coordinator.autonomous_executor
    hosted = MockGitHubHostedVerifier()
    executor.behavioral_verifier = None
    executor.github_behavioral_verifier = hosted
    executor.github_hosted_commit_policy = "prototype"
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    action = result["actions"][0]
    assert action["status"] == "published"
    assert action["commit_sha"] == CANDIDATE
    assert action["behavioral"]["isolation"] == "github_hosted_docker"
    assert hosted.arguments["source_workflow_run_id"] == 71
    assert sum(method == "PATCH" for method, *_ in transport.calls) == 1
    assert store.get_actions("delivery-1")[0]["publication_status"] == "published"


def test_failed_github_hosted_attestation_blocks_candidate_finalization(tmp_path):
    coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path, commits=True)
    executor = coordinator.autonomous_executor
    executor.behavioral_verifier = None
    executor.github_behavioral_verifier = MockGitHubHostedVerifier(status="failed")
    result = coordinator.process_review(event, review_payload(), runtime_files=runtime)
    assert result["actions"][0]["status"] == "blocked"
    assert result["actions"][0]["reason"] == "github_hosted_behavioral_verification_not_attested"
    assert not any(method == "PATCH" for method, *_ in transport.calls)


def test_invalid_or_untyped_evidence_ids_block_candidate_staging(tmp_path):
    for index, cited in enumerate((("missing",), (None,), (), "rag-1")):
        coordinator, _store, _adapter, transport, event, runtime = make_coordinator(tmp_path / str(index))
        result = coordinator.process_review(
            event, review_payload(finding(evidence_ids=cited)), runtime_files=runtime,
        )
        assert result["actions"][0]["status"] == "blocked"
        assert not any(method in {"POST", "PATCH"} for method, *_ in transport.calls)


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
            received["url"] = request.full_url
            unsigned = {
                "request_id": received["request"]["request_id"],
                "status": "passed", "executed": True, "exit_code": 0,
                "candidate_sha256": sha, "changed_files": ["src/app.py"],
                "network_isolated": True, "credentials_injected": False,
                "filesystem_restricted": True, "process_cleanup": True,
                "timeout_enforced": True, "timeout_seconds": 30,
                "memory_limit_bytes": 512 * 1024 * 1024, "cpu_limit_seconds": 20,
                "test_identity": hashlib.sha256(
                    base64.b64decode(received["request"]["tests_base64"])
                ).hexdigest(), "verifier_id": "sandbox-prod-v3",
                "timestamp": time.time(), "duration_seconds": 0.2,
            }
            unsigned["signature"] = hmac.new(
                key.encode(), json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
                hashlib.sha256,
            ).hexdigest()
            received["report"] = unsigned
            return Response()

    monkeypatch.setattr("urllib.request.build_opener", lambda *_handlers: Opener())
    broker = RemoteHTTPSExecutionBroker("https://executor.example:8443/v1/execute", key=key)
    result = BehavioralVerifier(broker).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        tests=b"test bundle", changed_files=("src/app.py",),
    )
    assert result.status == "passed" and result.attestation_verified
    request = received["request"]
    assert received["url"] == "https://executor.example:8443/v1/execute"
    assert request["candidate_sha256"] == sha
    assert isinstance(request["issued_at"], (int, float))
    assert request["network"] is False and request["credentials"] == {}
    assert request["changed_files"] == ["src/app.py"]
    received["report"]["candidate_sha256"] = "b" * 64
    assert broker.verify_attestation(received["report"], candidate_sha256=sha,
                                     changed_files=("src/app.py",), timeout_seconds=30,
                                     memory_bytes=512 * 1024 * 1024, cpu_seconds=20) is False
