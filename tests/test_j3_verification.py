import hashlib

from mcp_server.behavioral_verification import BehavioralVerifier
from mcp_server.confidence import LabeledConfidence, evaluate_calibration, independently_supported_confidence
from mcp_server.github_ci import GitHubCIClient, evaluate_required_checks
from mcp_server.github_policy import commit_execution_decision
from mcp_server.github_actions import GitHubMCPActionAdapter
from mcp_server.github_pr import PullRequestRef


SHA = "a" * 40
CANDIDATE = "b" * 40


def test_calibration_requires_independent_labeled_data_and_never_reuses_raw_score():
    no_data = evaluate_calibration([])
    assert not no_data.calibrated
    examples = [LabeledConfidence("model-v1", "reviewed-set-v1", 1.0, True, "independent_human_adjudication") for _ in range(250)]
    report = evaluate_calibration(examples)
    assert not report.calibrated
    assert report.sample_count == 250 and report.manifest_verified is False
    assert "independent_dataset_manifest_not_verified" in report.limitations
    assert independently_supported_confidence(.99, report, model_id="model-v1", dataset_id="reviewed-set-v1") is None
    untrusted = [LabeledConfidence("model-v1", "reviewed-set-v1", .99, True, "model_response") for _ in range(250)]
    assert not evaluate_calibration(untrusted).calibrated


def test_behavioral_verification_fails_closed_without_external_sandbox_broker():
    source = b"def f(): return 1\n"
    result = BehavioralVerifier().verify(
        original_source=source, candidate_source=b"def f(): return 2\n",
        expected_source_sha256=hashlib.sha256(source).hexdigest(),
        changed_files=("src/example.py",),
    )
    assert result.status == "not_run"
    assert not result.executed
    assert result.error_code == "isolated_execution_broker_unavailable"
    mismatch = BehavioralVerifier().verify(
        original_source=source, candidate_source=source, expected_source_sha256="wrong",
        changed_files=("src/example.py",),
    )
    assert mismatch.status == "failed"


def test_behavioral_verifier_requires_isolation_attestation_and_passes_no_secrets():
    original = b"value = 1\n"
    candidate = b"value = 2\n"

    class Broker:
        def __init__(self, status):
            self.status = status
            self.received = None

        def execute(self, **kwargs):
            self.received = kwargs
            return {
                "status": self.status, "executed": True, "exit_code": 0,
                "candidate_sha256": hashlib.sha256(candidate).hexdigest(),
                "changed_files": ["src/app.py"], "network_isolated": True,
                "credentials_injected": False, "filesystem_restricted": True,
                "process_cleanup": True, "timeout_enforced": True,
                "memory_limit_bytes": kwargs["memory_bytes"],
                "cpu_limit_seconds": kwargs["cpu_seconds"],
                "output_excerpt": "token=gsk-abcdefghijklmnop",
            }

    broker = Broker("passed")
    result = BehavioralVerifier(broker).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        tests=b"test fixture", changed_files=("src/app.py",),
    )
    assert result.status == "passed" and result.executed
    assert broker.received["network"] is False and broker.received["credentials"] == {}
    assert "[REDACTED]" in result.output_excerpt
    assert BehavioralVerifier(Broker("failed")).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        changed_files=("src/app.py",),
    ).status == "failed"
    assert BehavioralVerifier(Broker("timeout")).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        changed_files=("src/app.py",),
    ).status == "failed"

    class Unisolated(Broker):
        def execute(self, **kwargs):
            result = super().execute(**kwargs)
            result["network_isolated"] = False
            result["credentials_injected"] = True
            return result

    assert BehavioralVerifier(Unisolated("passed")).verify(
        original_source=original, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(original).hexdigest(),
        changed_files=("src/app.py",),
    ).status == "errored"


def test_ci_requires_named_checks_on_exact_candidate_revision():
    report = evaluate_required_checks(
        candidate_sha=CANDIDATE, required=("unit", "lint"), response_sha=CANDIDATE,
        check_runs=[{"name": "unit", "head_sha": CANDIDATE, "status": "completed", "conclusion": "success"}],
        statuses=[],
    )
    assert report.status == "unknown"
    assert [row.status for row in report.checks] == ["success", "missing"]
    stale = evaluate_required_checks(
        candidate_sha=CANDIDATE, required=("unit",), response_sha=CANDIDATE,
        check_runs=[{"name": "unit", "head_sha": SHA, "status": "completed", "conclusion": "success"}],
        statuses=[],
    )
    assert stale.checks[0].status == "unknown"
    assert evaluate_required_checks(candidate_sha=CANDIDATE, required=("unit",),
                                    check_runs=[], statuses=[], response_sha=SHA).status == "unknown"
    failed = evaluate_required_checks(
        candidate_sha=CANDIDATE, required=("unit",), response_sha=CANDIDATE,
        check_runs=[{"name": "unit", "head_sha": CANDIDATE, "status": "completed", "conclusion": "failure"}],
        statuses=[],
    )
    assert failed.checks[0].status == "failure" and failed.status == "failed"
    pending = evaluate_required_checks(
        candidate_sha=CANDIDATE, required=("unit",), response_sha=CANDIDATE,
        check_runs=[{"name": "unit", "head_sha": CANDIDATE, "status": "in_progress", "conclusion": None}],
        statuses=[],
    )
    assert pending.checks[0].status == "pending"
    skipped = evaluate_required_checks(
        candidate_sha=CANDIDATE, required=("unit",), response_sha=CANDIDATE,
        check_runs=[{"name": "unit", "head_sha": CANDIDATE, "status": "completed", "conclusion": "skipped"}],
        statuses=[],
    )
    assert skipped.checks[0].status == "skipped"
    assert GitHubCIClient(lambda _sha: (_ for _ in ()).throw(TimeoutError())).verify(
        repository="acme/demo", candidate_sha=CANDIDATE, required=("unit",),
    ).status == "unknown"


def test_adapter_rejects_ci_from_old_pr_head_as_candidate_evidence():
    class Read:
        def get_pull(self, ref):
            return {"number": ref.number, "state": "open",
                    "base": {"repo": {"owner": {"login": ref.owner}, "name": ref.repository}},
                    "head": {"sha": SHA}}

    adapter = GitHubMCPActionAdapter(read_client=Read(), token=None)
    old_head = adapter.verify_candidate_ci(
        PullRequestRef("acme", "demo", 3), candidate_sha=SHA,
        expected_pr_head_sha=SHA, required_checks=("unit",),
    )
    assert old_head["status"] == "unknown"
    assert old_head["error_code"] == "pre_patch_pr_head_is_not_candidate_revision"


def test_commit_gate_requires_every_independent_gate_and_remains_disabled():
    decision = commit_execution_decision(
        repository="acme/demo", branch="feature", expected_head_sha=SHA,
        current_head_sha=SHA, is_fork=False,
        gates={key: True for key in (
            "repository_opt_in", "eligible_low_risk_category", "calibration_supported",
            "source_hashes_valid", "syntax_passed", "static_passed", "behavioral_passed",
            "candidate_ci_passed", "scope_valid", "write_permission_valid",
            "durable_record_reserved", "idempotency_reserved",
        )} | {"confidence": .85, "changed_lines": 2, "candidate_sha": CANDIDATE,
              "ci_sha": CANDIDATE, "unexpected_files": []},
    )
    assert not decision["eligible"]
    assert decision["execution_enabled"] is False
    assert "autonomous_commit_execution_hard_disabled_pending_live_staging" in decision["denial_reasons"]
