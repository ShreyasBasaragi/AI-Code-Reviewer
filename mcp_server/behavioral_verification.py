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
        self._pending: set[str] = set()

    def execute(self, *, source: bytes, tests: bytes, timeout_seconds: int,
                memory_bytes: int, cpu_seconds: int, network: bool,
                credentials: Mapping[str, str], changed_files: tuple[str, ...]) -> Mapping[str, Any]:
        parsed = urllib.parse.urlsplit(self.endpoint or "")
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.port not in (None, 443)
                or len(self.key) < 32 or network is not False or credentials):
            raise ValueError("sandbox_endpoint_or_authentication_invalid")
        request_id = uuid.uuid4().hex
        candidate_sha = hashlib.sha256(source).hexdigest()
        unsigned = {
            "request_id": request_id,
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
        report["requested_candidate_sha256"] = candidate_sha
        self._pending.add(request_id)
        return report

    def verify_attestation(self, report: Mapping[str, Any], *, candidate_sha256: str,
                           changed_files: tuple[str, ...], timeout_seconds: int,
                           memory_bytes: int, cpu_seconds: int) -> bool:
        if not isinstance(report, Mapping) or len(self.key) < 32:
            return False
        request_id = report.get("request_id")
        signature = report.get("signature")
        timestamp = report.get("timestamp")
        if (not isinstance(request_id, str) or request_id not in self._pending
                or not isinstance(signature, str) or not isinstance(timestamp, (int, float))
                or abs(time.time() - float(timestamp)) > 120):
            return False
        unsigned = {key: value for key, value in report.items()
                    if key not in {"signature", "requested_candidate_sha256"}}
        expected = hmac.new(self.key, json.dumps(unsigned, sort_keys=True, separators=(",", ":")).encode(),
                            hashlib.sha256).hexdigest()
        valid = (hmac.compare_digest(signature, expected)
                 and report.get("candidate_sha256") == candidate_sha256
                 and report.get("requested_candidate_sha256") == candidate_sha256
                 and report.get("changed_files") == list(changed_files)
                 and report.get("timeout_enforced") is True
                 and report.get("timeout_seconds") == timeout_seconds
                 and report.get("memory_limit_bytes") == memory_bytes
                 and report.get("cpu_limit_seconds") == cpu_seconds
                 and isinstance(report.get("test_identity"), str) and bool(report["test_identity"])
                 and isinstance(report.get("verifier_id"), str) and bool(report["verifier_id"]))
        self._pending.discard(request_id)
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
        if (report.get("candidate_sha256") != candidate_hash
                or report.get("changed_files") != list(changed_files)
                or report.get("network_isolated") is not True
                or report.get("credentials_injected") is not False
                or report.get("filesystem_restricted") is not True
                or report.get("process_cleanup") is not True
                or report.get("timeout_enforced") is not True
                or report.get("memory_limit_bytes") != self.memory_bytes
                or report.get("cpu_limit_seconds") != self.cpu_seconds
                or not callable(getattr(self.broker, "verify_attestation", None))
                or self.broker.verify_attestation(
                    report, candidate_sha256=candidate_hash, changed_files=changed_files,
                    timeout_seconds=self.timeout_seconds, memory_bytes=self.memory_bytes,
                    cpu_seconds=self.cpu_seconds,
                ) is not True
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
