import hashlib
import hmac
import json

from mcp_server.behavioral_verification import BehavioralVerifier, MAX_SOURCE_BYTES
from mcp_server.confidence import (CALIBRATION_VERSION, LabeledConfidence, evaluate_calibration,
                                   independently_supported_confidence, load_calibration_report,
                                   persist_calibration_report)
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


def test_calibration_uses_disjoint_identified_splits_and_signed_report(monkeypatch, tmp_path):
    key = "k" * 40
    monkeypatch.setenv("AI_CODE_REVIEWER_CALIBRATION_VERIFY_KEY", key)
    rows = [
        LabeledConfidence("model-v1", "reviewed-v1", 1.0, True,
                          "independent_human_adjudication", split, kind,
                          f"{split}-{kind}-{index}")
        for split in ("calibration", "holdout")
        for kind in ("finding", "fix_safety")
        for index in range(2)
    ]
    payload = [
        {"model_id": row.model_id, "dataset_id": row.dataset_id,
         "confidence": float(row.confidence), "correct": row.correct,
         "label_source": row.label_source, "split": row.split,
         "prediction_type": row.prediction_type, "prediction_id": row.prediction_id}
        for row in rows
    ]
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    manifest = {"version": CALIBRATION_VERSION, "model_id": "model-v1",
                "dataset_id": "reviewed-v1", "sample_count": len(rows),
                "examples_sha256": digest,
                "label_source": "independent_human_adjudication", "reviewed_by": "reviewer"}
    manifest["signature"] = hmac.new(key.encode(), json.dumps(
        manifest, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256).hexdigest()
    report = evaluate_calibration(rows, minimum_examples=2, signed_manifest=manifest)
    assert report.calibrated
    assert report.calibration_count == 4 and report.holdout_count == 4
    assert report.metrics_by_prediction_type["fix_safety"]["holdout_count"] == 2
    artifact = persist_calibration_report(report, tmp_path / "calibration.json")
    assert artifact.stat().st_mode & 0o777 == 0o600
    assert load_calibration_report(artifact) == report
    artifact.write_text(artifact.read_text().replace('"calibrated":true', '"calibrated":false'))
    assert load_calibration_report(artifact) is None


def test_calibration_rejects_duplicate_prediction_identity():
    duplicate = LabeledConfidence("m", "d", .9, True, "independent_human_adjudication",
                                 "calibration", "finding", "same-id")
    report = evaluate_calibration([duplicate, duplicate])
    assert not report.calibrated
    assert "duplicate_independent_prediction_id" in report.limitations


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


def test_behavioral_verifier_rejects_unsafe_paths_and_oversized_inputs_without_broker():
    source = b"value = 1\n"
    verifier = BehavioralVerifier()
    for path in ("../escape.py", "/absolute.py", "src\\escape.py", "src//file.py"):
        result = verifier.verify(
            original_source=source, candidate_source=source,
            expected_source_sha256=hashlib.sha256(source).hexdigest(), changed_files=(path,),
        )
        assert result.status == "failed" and result.error_code == "changed_file_scope_invalid"
    oversized = verifier.verify(
        original_source=b"x" * (MAX_SOURCE_BYTES + 1), candidate_source=source,
        expected_source_sha256="unused", changed_files=("src/file.py",),
    )
    assert oversized.status == "failed"
    assert oversized.error_code == "verification_input_invalid_or_oversized"


def test_behavioral_verifier_never_executes_candidate_source(tmp_path):
    marker = tmp_path / "candidate-executed"
    source = b"value = 1\n"
    candidate = f"open({str(marker)!r}, 'w').write('executed')\n".encode()
    result = BehavioralVerifier().verify(
        original_source=source, candidate_source=candidate,
        expected_source_sha256=hashlib.sha256(source).hexdigest(),
        changed_files=("src/candidate.py",),
    )
    assert result.status == "not_run" and result.executed is False
    assert not marker.exists()


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
                "test_identity": "offline-test-suite-v1",
                "verifier_id": "test-isolated-broker",
            }

        def verify_attestation(self, *_args, **_kwargs):
            return True

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


def test_adapter_verifies_only_exact_candidate_ci_and_rechecks_pr_head():
    class Read:
        def __init__(self):
            self.pull_calls = 0
            self.fetched = []

        def get_pull(self, ref):
            self.pull_calls += 1
            return {"number": ref.number, "state": "open",
                    "base": {"repo": {"owner": {"login": ref.owner}, "name": ref.repository}},
                    "head": {"sha": SHA}}

        def get_ci_evidence(self, ref, sha):
            self.fetched.append((ref, sha))
            return {"head_sha": CANDIDATE,
                    "check_runs": [{"name": "unit", "head_sha": CANDIDATE,
                                    "status": "completed", "conclusion": "success"}],
                    "statuses": []}

    read = Read()
    result = GitHubMCPActionAdapter(read_client=read, token=None).verify_candidate_ci(
        PullRequestRef("acme", "demo", 3), candidate_sha=CANDIDATE,
        expected_pr_head_sha=SHA, required_checks=("unit",),
    )
    assert result["status"] == "passed"
    assert result["candidate_sha"] == CANDIDATE
    assert read.fetched == [(PullRequestRef("acme", "demo", 3), CANDIDATE)]
    assert read.pull_calls == 2


def test_commit_gate_requires_every_independent_gate_and_explicit_runtime_authorization():
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
    authorized = commit_execution_decision(
        repository="acme/demo", branch="feature", expected_head_sha=SHA,
        current_head_sha=SHA, is_fork=False,
        gates={key: True for key in (
            "repository_opt_in", "eligible_low_risk_category", "calibration_supported",
            "source_hashes_valid", "syntax_passed", "static_passed", "behavioral_passed",
            "candidate_ci_passed", "scope_valid", "write_permission_valid",
            "durable_record_reserved", "idempotency_reserved",
        )} | {"confidence": .85, "changed_lines": 2, "candidate_sha": CANDIDATE,
              "ci_sha": CANDIDATE, "unexpected_files": []},
        execution_authorized=True,
    )
    assert authorized["eligible"]
    assert authorized["status"] == "READY"
