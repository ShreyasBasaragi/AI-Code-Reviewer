from __future__ import annotations

import hashlib
import hmac
import json
import io
import os
import sys
import time
import zipfile
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp_server.behavioral_verification import (
    BehavioralVerificationResult, GitHubActionsBehavioralVerifier,
)
from mcp_server.github_actions import GitHubMCPActionAdapter
import mcp_server.github_actions as github_actions_module
from mcp_server.github_pr import GitHubPRException, GitHubRESTClient, PullRequestRef
from deploy.behavioral_executor.make_request import prepare as prepare_candidate_request
from deploy.behavioral_executor.sign_attestation import create_attestation
from deploy.behavioral_executor.runner import _pytest_result_status


KEY = "offline-test-attestation-key-at-least-32-bytes"
REPO = "acme/demo"
SOURCE_WORKFLOW = "42"
SOURCE_RUN = 9001
ATTESTATION_WORKFLOW = "43"
ATTESTATION_RUN = 9002
CANDIDATE_SHA = "c" * 40
PARENT_SHA = "a" * 40
PATH = "src/app.py"
ORIGINAL = b"before = 1\n"
CANDIDATE = b"after = 1\n"
TEST_IDENTITY = "1" * 64


def report(**updates):
    value = {
        "version": "codementor-github-behavioral-v1",
        "repository": REPO,
        "source_workflow_id": SOURCE_WORKFLOW,
        "source_workflow_run_id": SOURCE_RUN,
        "candidate_sha": CANDIDATE_SHA,
        "parent_sha": PARENT_SHA,
        "changed_files": [PATH],
        "source_sha256": hashlib.sha256(CANDIDATE).hexdigest(),
        "test_identity": TEST_IDENTITY,
        "status": "passed",
        "exit_code": 0,
        "duration_seconds": 10,
        "issued_at": int(time.time()),
        "attestation_workflow_id": ATTESTATION_WORKFLOW,
        "attestation_run_id": ATTESTATION_RUN,
        "execution_mode": "github_hosted_docker",
        "network_isolated": True,
        "credentials_injected": False,
        "filesystem_restricted": True,
        "timeout_enforced": True,
    }
    value.update(updates)
    unsigned = {key: val for key, val in value.items() if key != "signature"}
    value["signature"] = hmac.new(KEY.encode(), json.dumps(
        unsigned, sort_keys=True, separators=(",", ":")
    ).encode(), hashlib.sha256).hexdigest()
    return value


def verifier(fetch):
    return GitHubActionsBehavioralVerifier(
        fetch, key=KEY, expected_test_identity=TEST_IDENTITY,
        attestation_workflow_id=ATTESTATION_WORKFLOW,
    )


def verify(instance, **updates):
    return instance.verify(
        original_source=ORIGINAL, candidate_source=CANDIDATE,
        expected_source_sha256=hashlib.sha256(ORIGINAL).hexdigest(),
        changed_files=(PATH,), repository=REPO,
        candidate_commit_sha=updates.pop("candidate_commit_sha", CANDIDATE_SHA),
        parent_sha=PARENT_SHA, source_workflow_id=SOURCE_WORKFLOW,
        source_workflow_run_id=SOURCE_RUN,
    )


def test_signed_exact_workflow_attestation_passes():
    requested = []
    instance = verifier(lambda **kwargs: requested.append(kwargs) or report())
    result = verify(instance)
    assert result.status == "passed"
    assert result.attestation_verified is True
    assert result.candidate_commit_sha == CANDIDATE_SHA
    assert requested == [{
        "repository": REPO, "source_workflow_id": SOURCE_WORKFLOW,
        "source_workflow_run_id": SOURCE_RUN, "candidate_commit_sha": CANDIDATE_SHA,
        "attestation_workflow_id": ATTESTATION_WORKFLOW,
    }]


def test_missing_ambiguous_or_mismatched_attestation_fails_closed():
    for value in (None, [], report(candidate_sha="d" * 40),
                  report(source_workflow_run_id=SOURCE_RUN + 1),
                  report(parent_sha="b" * 40), report(changed_files=["other.py"]),
                  report(test_identity="2" * 64), report(attestation_workflow_id="99")):
        result = verify(verifier(lambda **_kwargs: value))
        assert result.status == "errored"
        assert result.attestation_verified is False


def test_invalid_signature_stale_future_failed_and_timeout_results_do_not_pass():
    malformed = report()
    malformed["source_sha256"] = "0" * 64
    malformed["signature"] = "0" * 64
    results = [
        malformed,
        report(issued_at=int(time.time()) - 1900),
        report(issued_at=int(time.time()) + 15),
        report(status="failed", exit_code=1),
        report(status="timeout", exit_code=124),
        report(status="errored", exit_code=125),
        report(status="passed", exit_code=1),
    ]
    for value in results:
        result = verify(verifier(lambda **_kwargs: value))
        assert result.status != "passed"


def test_source_revision_and_workflow_identity_are_required():
    instance = verifier(lambda **_kwargs: report())
    stale_source = instance.verify(
        original_source=b"not original", candidate_source=CANDIDATE,
        expected_source_sha256=hashlib.sha256(ORIGINAL).hexdigest(),
        changed_files=(PATH,), repository=REPO, candidate_commit_sha=CANDIDATE_SHA,
        parent_sha=PARENT_SHA, source_workflow_id=SOURCE_WORKFLOW,
        source_workflow_run_id=SOURCE_RUN,
    )
    missing_run = instance.verify(
        original_source=ORIGINAL, candidate_source=CANDIDATE,
        expected_source_sha256=hashlib.sha256(ORIGINAL).hexdigest(),
        changed_files=(PATH,), repository=REPO, candidate_commit_sha=CANDIDATE_SHA,
        parent_sha=PARENT_SHA, source_workflow_id=SOURCE_WORKFLOW,
        source_workflow_run_id=0,
    )
    assert stale_source.error_code == "github_behavioral_configuration_or_identity_invalid"
    assert missing_run.status == "errored"


def test_attestation_retrieval_infrastructure_errors_fail_closed():
    def unavailable(**_kwargs):
        raise TimeoutError("offline mocked timeout")
    result = verify(verifier(unavailable))
    assert result.status == "errored"
    assert result.attestation_verified is False
    assert result.error_code == "github_behavioral_evidence_TimeoutError"


def test_github_hosted_verifier_records_distinct_isolation_scope():
    result = verify(verifier(lambda **_kwargs: report()))
    assert result.isolation == "github_hosted_docker"
    assert result.verifier_id == f"github-actions:{ATTESTATION_WORKFLOW}"


def test_read_client_selects_one_trusted_workflow_run_and_artifact():
    data = report()
    data["signature"] = "signature-from-independent-signer"
    archive_buffer = io.BytesIO()
    with zipfile.ZipFile(archive_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("attestation.json", json.dumps(data))

    class Client(GitHubRESTClient):
        def __init__(self, duplicate=False):
            super().__init__(token="read-only")
            self.duplicate = duplicate
            self.requested = []

        def _get(self, path, query=None):
            self.requested.append((path, query))
            if path.endswith("/actions/workflows/43/runs"):
                run = {"id": ATTESTATION_RUN, "workflow_id": 43, "event": "workflow_run",
                       "display_title": f"CodeMentor behavioral attestation source-run-{SOURCE_RUN}",
                       "status": "completed", "conclusion": "success", "head_branch": "main"}
                return ({"workflow_runs": [run, dict(run, id=ATTESTATION_RUN + 1)] if self.duplicate else [run]}, {})
            if path.endswith(f"/actions/runs/{ATTESTATION_RUN}/artifacts"):
                return ({"artifacts": [{"id": 777, "name": f"codementor-behavioral-{SOURCE_RUN}",
                                        "expired": False}]}, {})
            raise AssertionError(path)

        def get_repository(self, owner, repository):
            assert (owner, repository) == ("acme", "demo")
            return {"default_branch": "main"}

        def _download_artifact(self, path):
            assert path.endswith("/actions/artifacts/777/zip")
            return archive_buffer.getvalue()

    client = Client()
    value = client.get_behavioral_attestation(
        owner="acme", repository="demo", source_workflow_id=SOURCE_WORKFLOW,
        source_workflow_run_id=SOURCE_RUN, candidate_commit_sha=CANDIDATE_SHA,
        attestation_workflow_id=ATTESTATION_WORKFLOW,
    )
    assert value["attestation_run_id"] == ATTESTATION_RUN
    assert value["candidate_sha"] == CANDIDATE_SHA
    with __import__("pytest").raises(GitHubPRException, match="ambiguous"):
        Client(duplicate=True).get_behavioral_attestation(
            owner="acme", repository="demo", source_workflow_id=SOURCE_WORKFLOW,
            source_workflow_run_id=SOURCE_RUN, candidate_commit_sha=CANDIDATE_SHA,
            attestation_workflow_id=ATTESTATION_WORKFLOW,
        )


def test_candidate_ci_requires_unique_run_from_this_dispatch_and_exact_branch(monkeypatch):
    monkeypatch.setenv("GITHUB_CANDIDATE_CI_WAIT_SECONDS", "0")
    ref = PullRequestRef("acme", "demo", 7)
    dispatched_at = 1_800_000_000.0

    class Read:
        def __init__(self, rows):
            self.rows = rows

        def get_pull(self, _ref):
            return {"number": 7, "state": "open",
                    "base": {"repo": {"owner": {"login": "acme"}, "name": "demo"}},
                    "head": {"sha": PARENT_SHA}}

        def get_ci_evidence(self, _ref, sha, _workflow_id, **_kwargs):
            rows = [dict(row, head_sha=sha, candidate_sha=sha,
                         display_title=f"CodeMentor candidate verification {sha}")
                    for row in self.rows]
            return {
                "head_sha": sha, "statuses": [], "workflow_runs": rows,
                "check_runs": [{"name": "candidate-syntax", "head_sha": sha,
                                "status": "completed", "conclusion": "success",
                                "workflow_identity": SOURCE_WORKFLOW,
                                "check_suite": {"id": 81}}],
            }

    def row(run_id=91, *, created=dispatched_at + 10, workflow=42, branch="main"):
        return {"id": run_id, "workflow_id": workflow, "head_sha": "d" * 40,
                "event": "workflow_dispatch", "head_branch": branch,
                "candidate_sha": CANDIDATE_SHA,
                "display_title": f"CodeMentor candidate verification {CANDIDATE_SHA}",
                "created_at": datetime.fromtimestamp(created, timezone.utc).isoformat(),
                "status": "completed", "conclusion": "success", "check_suite_id": 81}

    def verify(rows):
        return GitHubMCPActionAdapter(read_client=Read(rows)).verify_candidate_ci(
            ref, candidate_sha=CANDIDATE_SHA, expected_pr_head_sha=PARENT_SHA,
            required_checks=("candidate-syntax",), expected_workflow_identity=SOURCE_WORKFLOW,
                workflow_run_branch="main", dispatched_after=dispatched_at,
        )

    assert verify([row()])["workflow_run_id"] == 91
    stale = verify([row(created=dispatched_at - 60)])
    assert stale["status"] == "unknown"
    assert stale["error_code"] == "candidate_workflow_run_missing_stale_or_ambiguous"
    duplicate = verify([row(), row(run_id=92)])
    assert duplicate["status"] == "unknown"
    assert duplicate["error_code"] == "candidate_workflow_run_missing_stale_or_ambiguous"
    wrong_workflow = verify([row(workflow=99)])
    assert wrong_workflow["status"] == "unknown"


def test_dispatch_timestamp_is_taken_immediately_before_workflow_dispatch(monkeypatch):
    events = []

    class Read:
        def get_repository(self, owner, repository):
            events.append("repository")
            return {"default_branch": "main"}

    adapter = GitHubMCPActionAdapter(read_client=Read(), staging_token="staging",
                                     staging_enabled=True)

    def api_call(method, path, payload=None, *, token=None):
        events.append(("api", method))
        return {"object": {"sha": CANDIDATE_SHA}} if method == "GET" else {}

    monkeypatch.setattr(adapter, "_api_call", api_call)
    monkeypatch.setattr(github_actions_module.time, "time",
                        lambda: events.append("timestamp") or 1_800_000_000.0)
    result = adapter.dispatch_candidate_ci(
        PullRequestRef("acme", "demo", 7), candidate_sha=CANDIDATE_SHA,
        candidate_branch="codementor-candidates/" + "e" * 32,
        workflow_identity=SOURCE_WORKFLOW,
    )
    assert result["status"] == "dispatched"
    assert result["dispatched_at"] == 1_800_000_000.0
    assert events == [("api", "GET"), "repository", "timestamp", ("api", "POST")]


def test_rest_ci_evidence_binds_default_branch_workflow_suite_to_candidate_sha(monkeypatch):
    client = GitHubRESTClient(token="read-only")
    workflow_sha = "d" * 40
    calls = []

    def fake_get(path, query=None):
        calls.append((path, query))
        if path.endswith("/actions/workflows/42/runs"):
            return ({"workflow_runs": [{
                "id": 91, "workflow_id": 42, "event": "workflow_dispatch",
                "head_branch": "main", "head_sha": workflow_sha,
                "display_title": f"CodeMentor candidate verification {CANDIDATE_SHA}",
                "created_at": datetime.fromtimestamp(1_800_000_010, timezone.utc).isoformat(),
                "check_suite_id": 81,
            }]}, {})
        if path.endswith(f"/commits/{workflow_sha}/check-runs"):
            return ({"sha": workflow_sha, "check_runs": [{
                "name": "candidate-static", "head_sha": workflow_sha,
                "status": "completed", "conclusion": "success",
                "check_suite": {"id": 81},
            }, {
                "name": "unrelated-check", "head_sha": workflow_sha,
                "status": "completed", "conclusion": "success",
                "check_suite": {"id": 82},
            }]}, {})
        raise AssertionError(path)

    monkeypatch.setattr(client, "_get", fake_get)
    value = client.get_ci_evidence(
        PullRequestRef("acme", "demo", 7), CANDIDATE_SHA, "42",
        workflow_run_branch="main", dispatched_after=1_800_000_000,
    )
    assert value["head_sha"] == CANDIDATE_SHA
    assert value["workflow_runs"][0]["id"] == 91
    assert value["workflow_runs"][0]["candidate_sha"] == CANDIDATE_SHA
    assert [check["name"] for check in value["check_runs"]] == ["candidate-static"]
    assert value["check_runs"][0]["head_sha"] == CANDIDATE_SHA
    assert value["check_runs"][0]["workflow_identity"] == "42"
    assert calls[0][1] == {"branch": "main", "event": "workflow_dispatch", "per_page": "100"}


def _git(repository, *args):
    subprocess.run(["git", "-C", str(repository), *args], check=True,
                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def test_candidate_request_is_exact_sha_bounded_single_python_source_without_execution(tmp_path):
    repository = tmp_path / "checkout"
    repository.mkdir()
    _git(repository, "init", "-q")
    _git(repository, "config", "user.email", "offline@example.invalid")
    _git(repository, "config", "user.name", "Offline Test")
    (repository / "src").mkdir()
    (repository / "src/app.py").write_text("before = 1\n", encoding="utf-8")
    _git(repository, "add", "src/app.py")
    _git(repository, "commit", "--allow-empty", "-qm", "baseline")
    (repository / "src/app.py").write_text("raise RuntimeError('must stay data')\n", encoding="utf-8")
    _git(repository, "add", "src/app.py")
    _git(repository, "commit", "-qm", "candidate")
    candidate_sha = subprocess.check_output(
        ["git", "-C", str(repository), "rev-parse", "HEAD"], text=True,
    ).strip()
    request, identity = prepare_candidate_request(repository, candidate_sha)
    assert request["test_suite"] == "repository_tests_v1"
    assert request["changed_files"] == ["src/app.py"]
    assert identity["candidate_sha"] == candidate_sha
    assert identity["changed_file"] == "src/app.py"
    assert identity["source_sha256"] == hashlib.sha256(
        b"raise RuntimeError('must stay data')\n"
    ).hexdigest()
    with __import__("pytest").raises(ValueError, match="checkout_sha_mismatch"):
        prepare_candidate_request(repository, "b" * 40)


def test_runner_does_not_treat_candidate_early_exit_as_a_test_pass(tmp_path):
    overlay = tmp_path / "candidate"
    overlay.mkdir()
    baseline = tmp_path / "baseline"
    tests = baseline / "tests"
    tests.mkdir(parents=True)
    (overlay / "candidate_exit.py").write_text("import os\nos._exit(0)\n")
    (tests / "test_exit.py").write_text(
        "import candidate_exit\n"
        "def test_this_assertion_must_run():\n"
        "    assert False\n"
    )
    token = "a" * 64
    launcher = Path(__file__).resolve().parents[1] / "deploy/behavioral_executor/pytest_launcher.py"
    completed = subprocess.run(
        [sys.executable, "-I", str(launcher), str(overlay), str(baseline), str(tests)],
        env={**os.environ, "CODEMENTOR_PYTEST_COMPLETION_TOKEN": token},
        capture_output=True, check=False,
    )
    assert completed.returncode == 0
    assert _pytest_result_status(completed.returncode, completed.stdout, token) == "errored"


def test_runner_requires_positive_pytest_completion_marker():
    token = "b" * 64
    marker = f"CODEMENTOR_PYTEST_COMPLETE:{token}:0".encode()
    assert _pytest_result_status(0, marker + b"\n", token) == "passed"
    assert _pytest_result_status(1, f"CODEMENTOR_PYTEST_COMPLETE:{token}:1".encode(), token) == "failed"
    assert _pytest_result_status(0, b"", token) == "errored"


def test_signer_binds_exact_candidate_run_bundle_and_rejects_weak_inputs():
    values = {
        "repository": REPO, "source_workflow_id": SOURCE_WORKFLOW,
        "source_workflow_run_id": str(SOURCE_RUN), "candidate_sha": CANDIDATE_SHA,
        "parent_sha": PARENT_SHA, "changed_file": PATH,
        "source_sha256": hashlib.sha256(CANDIDATE).hexdigest(),
        "test_identity": TEST_IDENTITY, "status": "passed", "exit_code": "0",
        "duration_seconds": "10", "attestation_workflow_id": ATTESTATION_WORKFLOW,
        "attestation_run_id": str(ATTESTATION_RUN),
    }
    signed = create_attestation(values, KEY)
    unsigned = {key: value for key, value in signed.items() if key != "signature"}
    expected = hmac.new(KEY.encode(), json.dumps(
        unsigned, sort_keys=True, separators=(",", ":")
    ).encode(), hashlib.sha256).hexdigest()
    assert hmac.compare_digest(signed["signature"], expected)
    assert signed["candidate_sha"] == CANDIDATE_SHA
    assert signed["source_workflow_run_id"] == SOURCE_RUN
    with __import__("pytest").raises(ValueError, match="key_invalid"):
        create_attestation(values, "too-short")


def test_workflow_keeps_candidate_code_in_credential_free_restricted_container():
    root = Path(__file__).resolve().parents[1]
    candidate_workflow = (root / ".github/workflows/candidate-verification.yml").read_text()
    attestation_workflow = (root / ".github/workflows/behavioral-attestation.yml").read_text()
    untrusted_job = attestation_workflow.split("  sign-result:", 1)[0]
    signer_job = attestation_workflow.split("  sign-result:", 1)[1]
    for required_flag in (
        "--network=none", "--read-only", "--cap-drop=ALL", "--security-opt=no-new-privileges",
        "--pids-limit=64", "--memory=2g", "--cpus=1", "--ulimit cpu=180:181",
        "persist-credentials: false",
    ):
        assert required_flag in attestation_workflow
    assert "secrets." not in untrusted_job
    assert "CODEMENTOR_BEHAVIORAL_ATTESTATION_HMAC_KEY: ${{ secrets." in signer_job
    assert "path: candidate" in candidate_workflow
    assert "run-name: CodeMentor candidate verification ${{ inputs.candidate_sha }}" in candidate_workflow
    assert "ref: ${{ inputs.candidate_sha }}" in candidate_workflow
    assert "persist-credentials: false" in candidate_workflow
