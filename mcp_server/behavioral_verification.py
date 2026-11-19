"""Fail-closed interface for isolated candidate behavioral verification.

No subprocess or test command is run in this process. A separately reviewed
sandbox broker must implement the protocol before a candidate can be tested.
"""

from __future__ import annotations

import hashlib
import base64
import hmac
import json
import os
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional, Protocol


MAX_SOURCE_BYTES = 2 * 1024 * 1024
MAX_TEST_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class BehavioralVerificationResult:
    status: str
    isolation: str
    source_sha256: Optional[str]
    candidate_sha256: Optional[str]
    changed_files: tuple[str, ...] = ()
    executed: bool = False
    exit_code: Optional[int] = None
    duration_seconds: Optional[float] = None
    output_excerpt: str = ""
    error_code: Optional[str] = None
    test_identity: Optional[str] = None
    verifier_id: Optional[str] = None
    attestation_verified: bool = False
    candidate_commit_sha: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IsolatedExecutionBroker(Protocol):
    """Trusted out-of-process executor with enforced resource/network limits."""

    def execute(self, *, source: bytes, tests: bytes, timeout_seconds: int,
                memory_bytes: int, cpu_seconds: int, network: bool,
                credentials: Mapping[str, str], changed_files: tuple[str, ...]) -> Mapping[str, Any]: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RemoteHTTPSExecutionBroker:
    """HMAC-authenticated client for a separately deployed trusted executor.

    The service must enforce isolation; this process only transports bounded
    candidate/test bytes and verifies a fresh signed attestation. It never
    executes the submitted code locally.
    """

    def __init__(self, endpoint: Optional[str] = None, *, key: Optional[str] = None,
                 timeout_seconds: int = 40) -> None:
        self.endpoint = endpoint if endpoint is not None else os.getenv("AI_CODE_REVIEWER_SANDBOX_URL")
        self.key = (key if key is not None else os.getenv("AI_CODE_REVIEWER_SANDBOX_HMAC_KEY", "")).encode()
        self.timeout_seconds = min(max(int(timeout_seconds), 1), 120)
        self._pending: dict[str, dict[str, Any]] = {}

    def execute(self, *, source: bytes, tests: bytes, timeout_seconds: int,
                memory_bytes: int, cpu_seconds: int, network: bool,
                credentials: Mapping[str, str], changed_files: tuple[str, ...]) -> Mapping[str, Any]:
        parsed = urllib.parse.urlsplit(self.endpoint or "")
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("sandbox_endpoint_or_authentication_invalid") from exc
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)
                or len(self.key) < 32 or network is not False or credentials):
            raise ValueError("sandbox_endpoint_or_authentication_invalid")
        request_id = uuid.uuid4().hex
        candidate_sha = hashlib.sha256(source).hexdigest()
        unsigned = {
            "request_id": request_id,
            "issued_at": time.time(),
            "candidate_sha256": candidate_sha,
            "source_base64": base64.b64encode(source).decode("ascii"),
            "tests_base64": base64.b64encode(tests).decode("ascii"),
            "changed_files": list(changed_files),
            "timeout_seconds": timeout_seconds,
            "memory_limit_bytes": memory_bytes,
            "cpu_limit_seconds": cpu_seconds,
            "network": False,
            "credentials": {},
        }
        # The verifier supplies changed file scope separately; the executor's
        # signed response binds it below. The request still contains candidate
        # digest and limits so a broker cannot silently change the artifact.
        unsigned["request_signature"] = hmac.new(
            self.key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()
        payload = json.dumps(unsigned, separators=(",", ":")).encode()
        request = urllib.request.Request(self.endpoint, data=payload, method="POST", headers={
            "Accept": "application/json", "Content-Type": "application/json",
            "User-Agent": "CodeMentor-Behavioral-Verification",
        })
        try:
            opener = urllib.request.build_opener(_NoRedirect())
            with opener.open(request, timeout=self.timeout_seconds) as response:
                raw = response.read(128 * 1024 + 1)
                if len(raw) > 128 * 1024:
                    raise ValueError("sandbox_attestation_oversized")
            report = json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            raise RuntimeError("sandbox_remote_unavailable") from exc
        if not isinstance(report, dict):
            raise ValueError("sandbox_attestation_invalid")
        if report.get("request_id") != request_id:
            raise ValueError("sandbox_request_identity_mismatch")
        if report.get("status") not in {"passed", "failed", "errored", "timeout"}:
            raise ValueError("sandbox_report_invalid")
        report["requested_candidate_sha256"] = candidate_sha
        self._pending[request_id] = {
            "candidate_sha256": candidate_sha,
            "test_identity": hashlib.sha256(tests).hexdigest(),
            "changed_files": list(changed_files),
            "timeout_seconds": timeout_seconds,
            "memory_limit_bytes": memory_bytes,
            "cpu_limit_seconds": cpu_seconds,
        }
        return report

    def verify_attestation(self, report: Mapping[str, Any], *, candidate_sha256: str,
                           changed_files: tuple[str, ...], timeout_seconds: int,
                           memory_bytes: int, cpu_seconds: int) -> bool:
        if not isinstance(report, Mapping) or len(self.key) < 32:
            return False
        request_id = report.get("request_id")
        signature = report.get("signature")
        timestamp = report.get("timestamp")
        pending = self._pending.get(request_id) if isinstance(request_id, str) else None
        if (not isinstance(request_id, str) or request_id not in self._pending
                or not isinstance(pending, Mapping)
                or not isinstance(signature, str) or not isinstance(timestamp, (int, float))
                or abs(time.time() - float(timestamp)) > 120):
            if isinstance(request_id, str):
                self._pending.pop(request_id, None)
            return False
        unsigned = {key: value for key, value in report.items()
                    if key not in {"signature", "requested_candidate_sha256"}}
        expected = hmac.new(self.key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
                            hashlib.sha256).hexdigest()
        valid = (hmac.compare_digest(signature, expected)
                 and report.get("candidate_sha256") == candidate_sha256
                 and report.get("requested_candidate_sha256") == candidate_sha256
                 and report.get("changed_files") == list(changed_files)
                 and report.get("test_identity") == pending.get("test_identity")
                 and pending.get("candidate_sha256") == candidate_sha256
                 and pending.get("changed_files") == list(changed_files)
                 and pending.get("timeout_seconds") == timeout_seconds
                 and pending.get("memory_limit_bytes") == memory_bytes
                 and pending.get("cpu_limit_seconds") == cpu_seconds
                 and report.get("timeout_enforced") is True
                 and report.get("timeout_seconds") == timeout_seconds
                 and report.get("memory_limit_bytes") == memory_bytes
                 and report.get("cpu_limit_seconds") == cpu_seconds
                 and isinstance(report.get("test_identity"), str) and bool(report["test_identity"])
                 and isinstance(report.get("verifier_id"), str) and bool(report["verifier_id"]))
        self._pending.pop(request_id, None)
        return valid


class BehavioralVerifier:
    """Verify source identity then delegate only to an explicitly supplied broker."""

    def __init__(self, broker: Optional[IsolatedExecutionBroker] = None, *,
                 timeout_seconds: int = 30, memory_bytes: int = 512 * 1024 * 1024,
                 cpu_seconds: int = 20) -> None:
        self.broker = broker
        self.timeout_seconds = min(max(int(timeout_seconds), 1), 120)
        self.memory_bytes = min(max(int(memory_bytes), 64 * 1024 * 1024), 2 * 1024 * 1024 * 1024)
        self.cpu_seconds = min(max(int(cpu_seconds), 1), self.timeout_seconds)

    def verify(self, *, original_source: bytes, candidate_source: bytes,
               expected_source_sha256: str, tests: bytes = b"",
               changed_files: tuple[str, ...] = ()) -> BehavioralVerificationResult:
        if (not isinstance(original_source, bytes) or not isinstance(candidate_source, bytes)
                or not isinstance(tests, bytes) or len(original_source) > MAX_SOURCE_BYTES
                or len(candidate_source) > MAX_SOURCE_BYTES or len(tests) > MAX_TEST_BYTES):
            return BehavioralVerificationResult("failed", "not_run", None, None,
                                                error_code="verification_input_invalid_or_oversized")
        original_hash = hashlib.sha256(original_source).hexdigest()
        candidate_hash = hashlib.sha256(candidate_source).hexdigest()
        if original_hash != expected_source_sha256:
            return BehavioralVerificationResult("failed", "not_run", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="source_revision_mismatch")
        if (not isinstance(changed_files, tuple) or not changed_files
                or not all(_safe_relative_path(path) for path in changed_files)
                or len(set(changed_files)) != len(changed_files)):
            return BehavioralVerificationResult("failed", "not_run", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="changed_file_scope_invalid")
        if self.broker is None:
            return BehavioralVerificationResult("not_run", "unavailable", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="isolated_execution_broker_unavailable")
        try:
            report = self.broker.execute(
                source=candidate_source, tests=tests,
                timeout_seconds=self.timeout_seconds, memory_bytes=self.memory_bytes,
                cpu_seconds=self.cpu_seconds, network=False, credentials={}, changed_files=changed_files,
            )
        except Exception as exc:
            return BehavioralVerificationResult("errored", "broker", original_hash,
                                                candidate_hash, changed_files,
                                                error_code=f"sandbox_broker_{type(exc).__name__}")
        status = report.get("status") if isinstance(report, Mapping) else None
        if status not in {"passed", "failed", "errored", "timeout"}:
            return BehavioralVerificationResult("errored", "broker", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="sandbox_report_invalid")
        verifier = getattr(self.broker, "verify_attestation", None)
        try:
            attestation_valid = callable(verifier) and verifier(
                report, candidate_sha256=candidate_hash, changed_files=changed_files,
                timeout_seconds=self.timeout_seconds, memory_bytes=self.memory_bytes,
                cpu_seconds=self.cpu_seconds,
            ) is True
        except Exception:
            attestation_valid = False
        if (report.get("candidate_sha256") != candidate_hash
                or report.get("changed_files") != list(changed_files)
                or report.get("network_isolated") is not True
                or report.get("credentials_injected") is not False
                or report.get("filesystem_restricted") is not True
                or report.get("process_cleanup") is not True
                or report.get("timeout_enforced") is not True
                or report.get("memory_limit_bytes") != self.memory_bytes
                or report.get("cpu_limit_seconds") != self.cpu_seconds
                or not attestation_valid
                or (status == "passed" and (report.get("executed") is not True
                                              or report.get("exit_code") != 0))):
            return BehavioralVerificationResult("errored", "unattested_broker", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="sandbox_guarantees_unverified")
        return BehavioralVerificationResult(
            "failed" if status == "timeout" else status, "restricted_broker",
            original_hash, candidate_hash, changed_files,
            executed=bool(report.get("executed")),
            exit_code=report.get("exit_code") if isinstance(report.get("exit_code"), int) else None,
            duration_seconds=report.get("duration_seconds") if isinstance(report.get("duration_seconds"), (int, float)) else None,
            output_excerpt=_bounded_output(report.get("output_excerpt")),
            error_code="sandbox_timeout" if status == "timeout" else None,
            test_identity=report.get("test_identity"), verifier_id=report.get("verifier_id"),
            attestation_verified=True,
        )


class GitHubActionsBehavioralVerifier:
    """Read and verify a signed result produced by the trusted workflow_run workflow.

    Candidate code is never run by this process. The fetcher must retrieve a
    single artifact from the configured attestation workflow, bound to the
    exact candidate workflow run and commit.
    """

    def __init__(self, fetch_attestation, *, key: Optional[str] = None,
                 expected_test_identity: Optional[str] = None,
                 attestation_workflow_id: Optional[str] = None,
                 max_age_seconds: int = 1800) -> None:
        self.fetch_attestation = fetch_attestation
        self.key = (key if key is not None else
                    os.getenv("GITHUB_BEHAVIORAL_ATTESTATION_HMAC_KEY", "")).encode()
        self.expected_test_identity = (expected_test_identity if expected_test_identity is not None
                                       else os.getenv("GITHUB_BEHAVIORAL_TEST_BUNDLE_SHA256", ""))
        self.attestation_workflow_id = (attestation_workflow_id if attestation_workflow_id is not None
                                        else os.getenv("GITHUB_BEHAVIORAL_ATTESTATION_WORKFLOW_ID", ""))
        self.max_age_seconds = min(max(int(max_age_seconds), 60), 3600)

    def verify(self, *, original_source: bytes, candidate_source: bytes,
               expected_source_sha256: str, changed_files: tuple[str, ...],
               repository: str, candidate_commit_sha: str, parent_sha: str,
               source_workflow_id: str, source_workflow_run_id: int) -> BehavioralVerificationResult:
        original_hash = hashlib.sha256(original_source).hexdigest() if isinstance(original_source, bytes) else None
        candidate_hash = hashlib.sha256(candidate_source).hexdigest() if isinstance(candidate_source, bytes) else None
        failed = lambda code: BehavioralVerificationResult(
            "errored", "github_hosted_unverified", original_hash, candidate_hash,
            changed_files if isinstance(changed_files, tuple) else (), error_code=code,
            candidate_commit_sha=candidate_commit_sha if isinstance(candidate_commit_sha, str) else None,
        )
        if (len(self.key) < 32 or not re.fullmatch(r"[0-9a-f]{64}", self.expected_test_identity or "")
                or not re.fullmatch(r"[0-9]{1,20}", self.attestation_workflow_id or "")
                or not isinstance(original_source, bytes) or not isinstance(candidate_source, bytes)
                or len(original_source) > MAX_SOURCE_BYTES or len(candidate_source) > MAX_SOURCE_BYTES
                or original_hash != expected_source_sha256
                or not isinstance(repository, str)
                or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repository)
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", candidate_commit_sha or "")
                or not re.fullmatch(r"[0-9a-fA-F]{40,64}", parent_sha or "")
                or not isinstance(source_workflow_id, str)
                or not re.fullmatch(r"[0-9]{1,20}", source_workflow_id)
                or isinstance(source_workflow_run_id, bool) or not isinstance(source_workflow_run_id, int)
                or source_workflow_run_id <= 0 or not _safe_relative_path_tuple(changed_files)):
            return failed("github_behavioral_configuration_or_identity_invalid")
        try:
            report = self.fetch_attestation(
                repository=repository, source_workflow_id=source_workflow_id,
                source_workflow_run_id=source_workflow_run_id,
                candidate_commit_sha=candidate_commit_sha,
                attestation_workflow_id=self.attestation_workflow_id,
            )
        except Exception as exc:
            return failed(f"github_behavioral_evidence_{type(exc).__name__}")
        if not isinstance(report, Mapping):
            return failed("github_behavioral_evidence_missing_or_ambiguous")
        signature = report.get("signature")
        unsigned = {key: value for key, value in report.items() if key != "signature"}
        expected_signature = hmac.new(
            self.key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(), hashlib.sha256
        ).hexdigest()
        issued_at = report.get("issued_at")
        claimed_repository = report.get("repository")
        claimed_candidate_sha = report.get("candidate_sha")
        claimed_parent_sha = report.get("parent_sha")
        claimed_attestation_run_id = report.get("attestation_run_id")
        common_valid = (
            isinstance(signature, str) and hmac.compare_digest(signature, expected_signature)
            and report.get("version") == "codementor-github-behavioral-v1"
            and isinstance(claimed_repository, str)
            and claimed_repository.casefold() == repository.casefold()
            and report.get("source_workflow_id") == source_workflow_id
            and report.get("source_workflow_run_id") == source_workflow_run_id
            and isinstance(claimed_candidate_sha, str)
            and claimed_candidate_sha.casefold() == candidate_commit_sha.casefold()
            and isinstance(claimed_parent_sha, str)
            and claimed_parent_sha.casefold() == parent_sha.casefold()
            and report.get("changed_files") == list(changed_files)
            and report.get("source_sha256") == candidate_hash
            and report.get("test_identity") == self.expected_test_identity
            and report.get("attestation_workflow_id") == self.attestation_workflow_id
            and isinstance(claimed_attestation_run_id, int) and not isinstance(claimed_attestation_run_id, bool)
            and isinstance(issued_at, (int, float)) and not isinstance(issued_at, bool)
            and 0 <= time.time() - float(issued_at) <= self.max_age_seconds
            and report.get("credentials_injected") is False
            and report.get("network_isolated") is True
            and report.get("filesystem_restricted") is True
            and report.get("timeout_enforced") is True
            and report.get("execution_mode") == "github_hosted_docker"
        )
        if not common_valid:
            return failed("github_behavioral_attestation_invalid_or_stale")
        status = report.get("status")
        if not isinstance(status, str) or status not in {"passed", "failed", "timeout", "errored"}:
            return failed("github_behavioral_status_invalid")
        exit_code = report.get("exit_code")
        if isinstance(exit_code, bool) or not isinstance(exit_code, int):
            return failed("github_behavioral_status_invalid")
        if status != "passed" or exit_code != 0:
            result_status = ("failed" if status in {"failed", "timeout"}
                             or (status == "passed" and exit_code != 0)
                             else "errored")
            return BehavioralVerificationResult(
                result_status, "github_hosted_docker",
                original_hash, candidate_hash, changed_files, executed=True,
                exit_code=report.get("exit_code") if isinstance(report.get("exit_code"), int) else None,
                error_code="github_behavioral_tests_not_passed",
                test_identity=self.expected_test_identity,
                verifier_id=f"github-actions:{self.attestation_workflow_id}",
                attestation_verified=True, candidate_commit_sha=candidate_commit_sha,
            )
        return BehavioralVerificationResult(
            "passed", "github_hosted_docker", original_hash, candidate_hash,
            changed_files, executed=True, exit_code=0,
            duration_seconds=report.get("duration_seconds") if isinstance(report.get("duration_seconds"), (int, float)) else None,
            output_excerpt=_bounded_output(report.get("output_excerpt")),
            test_identity=self.expected_test_identity,
            verifier_id=f"github-actions:{self.attestation_workflow_id}",
            attestation_verified=True, candidate_commit_sha=candidate_commit_sha,
        )


def _safe_relative_path_tuple(paths: Any) -> bool:
    return (isinstance(paths, tuple) and len(paths) == 1
            and all(_safe_relative_path(path) for path in paths))


def _bounded_output(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value[:4000]
    patterns = (
        (r"(?i)(bearer\s+)[A-Za-z0-9._~+/-]+=*", r"\1[REDACTED]"),
        (r"\b(?:ghp|github_pat|gsk|sk)-[A-Za-z0-9_-]{12,}\b", "[REDACTED]"),
        (r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;]+", r"\1[REDACTED]"),
    )
    for pattern, replacement in patterns:
        text = re.sub(pattern, replacement, text)
    return text


def _safe_relative_path(value: Any) -> bool:
    if (not isinstance(value, str) or not value or "\\" in value or "\x00" in value
            or value.startswith("/")):
        return False
    parts = value.split("/")
    return (all(part not in {"", ".", ".."} for part in parts)
            and PurePosixPath(value).as_posix() == value)
