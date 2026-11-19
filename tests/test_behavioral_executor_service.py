"""Offline protocol and container-configuration tests; Docker is never started."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import subprocess
import time
from pathlib import Path

import pytest

from deploy.behavioral_executor.service import Executor, _canonical
from mcp_server.behavioral_verification import RemoteHTTPSExecutionBroker


KEY = "executor-test-key-with-at-least-thirty-two-bytes"
IMAGE = "registry.example/codementor-runner@sha256:" + "a" * 64


def test_trusted_runner_image_installs_and_checks_git():
    dockerfile_path = (
        Path(__file__).resolve().parents[1]
        / "deploy/behavioral_executor/Dockerfile"
    )
    dockerfile = dockerfile_path.read_text(encoding="utf-8")
    normalized = dockerfile.replace("\\\n", " ")

    assert "apt-get install -y --no-install-recommends git" in normalized
    assert "git --version" in normalized


def _test_bundle():
    import io
    import zipfile

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("tests/test_contract.py", "def test_contract(): assert True\n")
    return bundle.getvalue()


def request(source=b"value = 2\n", tests=None, **updates):
    tests = tests if tests is not None else _test_bundle()
    data = {
        "request_id": "1" * 32,
        "issued_at": time.time(),
        "candidate_sha256": hashlib.sha256(source).hexdigest(),
        "source_base64": base64.b64encode(source).decode(),
        "tests_base64": base64.b64encode(tests).decode(),
        "changed_files": ["src/app.py"],
        "timeout_seconds": 30,
        "memory_limit_bytes": 512 * 1024 * 1024,
        "cpu_limit_seconds": 20,
        "network": False,
        "credentials": {},
    }
    data.update(updates)
    data["request_signature"] = hmac.new(KEY.encode(), _canonical(data), hashlib.sha256).hexdigest()
    return data


def test_executor_requires_digest_pinned_image_and_key():
    with pytest.raises(ValueError, match="image_must_be_digest_pinned"):
        Executor(key=KEY, image="registry.example/runner:latest")
    with pytest.raises(ValueError, match="key_too_short"):
        Executor(key="short", image=IMAGE)


def test_test_bundle_archive_extracts_only_python_tests_without_traversal(tmp_path):
    import io
    import zipfile
    from deploy.behavioral_executor.service import _extract_test_bundle

    bundle = io.BytesIO()
    with zipfile.ZipFile(bundle, "w") as archive:
        archive.writestr("tests/", "")
        archive.writestr("tests/test_review.py", "def test_ok(): pass\n")
    root = tmp_path / "bundle"
    _extract_test_bundle(bundle.getvalue(), root)
    assert (root / "tests/test_review.py").is_file()
    for invalid_name in ("../../tmp/payload.py", "tests/payload.txt"):
        invalid = io.BytesIO()
        with zipfile.ZipFile(invalid, "w") as archive:
            archive.writestr(invalid_name, "ignored")
        with pytest.raises(ValueError):
            _extract_test_bundle(invalid.getvalue(), tmp_path / "invalid")


@pytest.mark.parametrize("change", [
    {"network": True}, {"credentials": {"GITHUB_TOKEN": "secret"}},
    {"changed_files": ["../escape.py"]}, {"timeout_seconds": 0},
    {"memory_limit_bytes": 1}, {"cpu_limit_seconds": 99},
])
def test_executor_rejects_unsafe_request_claims(change):
    payload = request(**change)
    # Re-sign so each case exercises policy validation rather than HMAC failure.
    unsigned = {key: value for key, value in payload.items() if key != "request_signature"}
    payload["request_signature"] = hmac.new(KEY.encode(), _canonical(unsigned), hashlib.sha256).hexdigest()
    executor = Executor(key=KEY, image=IMAGE)
    with pytest.raises(ValueError):
        executor._validate(payload)


def test_executor_rejects_forged_stale_and_replayed_requests():
    executor = Executor(key=KEY, image=IMAGE)
    forged = request()
    forged["source_base64"] = base64.b64encode(b"changed").decode()
    with pytest.raises(ValueError, match="authentication_invalid"):
        executor._validate(forged)
    stale = request(issued_at=1)
    unsigned = {key: value for key, value in stale.items() if key != "request_signature"}
    stale["request_signature"] = hmac.new(KEY.encode(), _canonical(unsigned), hashlib.sha256).hexdigest()
    with pytest.raises(ValueError, match="identity_or_age_invalid"):
        Executor(key=KEY, image=IMAGE, clock=lambda: 1_800_000_000.0)._validate(stale)
    current = request()
    executor._validate(current)
    with pytest.raises(ValueError, match="replay_rejected"):
        executor._validate(current)


def test_executor_request_signature_matches_client_for_unicode_paths():
    payload = request(changed_files=["src/café.py"])
    Executor(key=KEY, image=IMAGE)._validate(payload)


def test_successful_container_attestation_binds_request_and_enforced_settings(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = b"4 tests passed\n"
        stderr = b""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr("deploy.behavioral_executor.service.subprocess.run", fake_run)
    executor = Executor(key=KEY, image=IMAGE)
    payload = request()
    report = executor.execute(payload)
    assert report["status"] == "passed"
    assert report["candidate_sha256"] == payload["candidate_sha256"]
    assert report["changed_files"] == payload["changed_files"]
    tests = base64.b64decode(payload["tests_base64"])
    assert report["test_identity"] == hashlib.sha256(tests).hexdigest()
    assert report["network_isolated"] is True and report["credentials_injected"] is False
    assert report["filesystem_restricted"] is True and report["process_cleanup"] is True
    assert report["timeout_enforced"] is True
    assert len(calls) == 2 and calls[0][0][0:2] == ["docker", "run"]
    assert "--name" in calls[0][0]
    invocation = calls[0][0]
    for option in ("--pull=never", "--runtime=runsc", "--network=none", "--read-only", "--cap-drop=ALL",
                   "--security-opt=no-new-privileges", "--pids-limit=64", "--memory-swap"):
        assert option in invocation
    assert any("readonly" in argument for argument in invocation)
    assert IMAGE in invocation and "--rm" not in invocation
    assert calls[1][0][1:3] == ["rm", "--force"]

    broker = RemoteHTTPSExecutionBroker("https://executor.example/v1/execute", key=KEY)
    pending = {
        "candidate_sha256": payload["candidate_sha256"],
        "test_identity": report["test_identity"],
        "changed_files": ["src/app.py"],
        "timeout_seconds": 30,
        "memory_limit_bytes": 512 * 1024 * 1024,
        "cpu_limit_seconds": 20,
    }
    broker._pending[payload["request_id"]] = pending
    report["requested_candidate_sha256"] = payload["candidate_sha256"]
    assert broker.verify_attestation(
        report, candidate_sha256=payload["candidate_sha256"], changed_files=("src/app.py",),
        timeout_seconds=30, memory_bytes=512 * 1024 * 1024, cpu_seconds=20,
    ) is True
    broker._pending[payload["request_id"]] = pending
    forged = dict(report, status="failed")
    assert broker.verify_attestation(
        forged, candidate_sha256=payload["candidate_sha256"], changed_files=("src/app.py",),
        timeout_seconds=30, memory_bytes=512 * 1024 * 1024, cpu_seconds=20,
    ) is False
    misbound = dict(report, test_identity="f" * 64)
    unsigned = {key: value for key, value in misbound.items()
                if key not in {"signature", "requested_candidate_sha256"}}
    misbound["signature"] = hmac.new(KEY.encode(), _canonical(unsigned), hashlib.sha256).hexdigest()
    broker._pending[payload["request_id"]] = pending
    assert broker.verify_attestation(
        misbound, candidate_sha256=payload["candidate_sha256"], changed_files=("src/app.py",),
        timeout_seconds=30, memory_bytes=512 * 1024 * 1024, cpu_seconds=20,
    ) is False


def test_failed_or_unclean_execution_is_never_attested_as_a_pass(monkeypatch):
    class Result:
        def __init__(self, code):
            self.returncode, self.stdout, self.stderr = code, b"test failure", b""

    calls = iter((Result(1), Result(0)))
    monkeypatch.setattr("deploy.behavioral_executor.service.subprocess.run",
                        lambda *_a, **_k: next(calls))
    executor = Executor(key=KEY, image=IMAGE)
    report = executor.execute(request())
    assert report["status"] == "failed"
    assert report["exit_code"] == 1


def test_container_timeout_is_signed_and_force_cleaned(monkeypatch):
    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    def fake_run(command, **kwargs):
        if command[1] == "run":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return Result()

    monkeypatch.setattr("deploy.behavioral_executor.service.subprocess.run", fake_run)
    report = Executor(key=KEY, image=IMAGE).execute(request())
    assert report["status"] == "timeout"
    assert report["executed"] is True
    assert report["process_cleanup"] is True
    assert report["output_excerpt"] == "candidate_execution_timeout"


def test_container_cleanup_failure_marks_isolation_unverified(monkeypatch):
    class Result:
        def __init__(self, code):
            self.returncode, self.stdout, self.stderr = code, b"", b""

    calls = iter((Result(0), Result(1)))
    monkeypatch.setattr("deploy.behavioral_executor.service.subprocess.run",
                        lambda *_a, **_k: next(calls))
    report = Executor(key=KEY, image=IMAGE).execute(request())
    assert report["status"] == "errored"
    assert report["process_cleanup"] is False


def test_host_workspace_cleanup_failure_marks_isolation_unverified(monkeypatch, tmp_path):
    class Result:
        returncode = 0
        stdout = b""
        stderr = b""

    workspace = tmp_path / "executor-workspace"
    workspace.mkdir()
    monkeypatch.setattr("deploy.behavioral_executor.service.tempfile.mkdtemp",
                        lambda **_kwargs: str(workspace))
    monkeypatch.setattr("deploy.behavioral_executor.service.subprocess.run",
                        lambda *_args, **_kwargs: Result())
    monkeypatch.setattr("deploy.behavioral_executor.service.shutil.rmtree",
                        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("cleanup failed")))
    report = Executor(key=KEY, image=IMAGE).execute(request())
    assert report["status"] == "errored"
    assert report["process_cleanup"] is False
