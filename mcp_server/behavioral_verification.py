"""Fail-closed interface for isolated candidate behavioral verification.

No subprocess or test command is run in this process. A separately reviewed
sandbox broker must implement the protocol before a candidate can be tested.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional, Protocol


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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class IsolatedExecutionBroker(Protocol):
    """Trusted out-of-process executor with enforced resource/network limits."""

    def execute(self, *, source: bytes, tests: bytes, timeout_seconds: int,
                memory_bytes: int, cpu_seconds: int, network: bool,
                credentials: Mapping[str, str]) -> Mapping[str, Any]: ...


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
        original_hash = hashlib.sha256(original_source).hexdigest()
        candidate_hash = hashlib.sha256(candidate_source).hexdigest()
        if original_hash != expected_source_sha256:
            return BehavioralVerificationResult("failed", "not_run", original_hash,
                                                candidate_hash, changed_files,
                                                error_code="source_revision_mismatch")
        if not changed_files or len(set(changed_files)) != len(changed_files):
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
                cpu_seconds=self.cpu_seconds, network=False, credentials={},
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
                or report.get("memory_limit_bytes", 0) < self.memory_bytes
                or report.get("cpu_limit_seconds", 0) < self.cpu_seconds):
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
