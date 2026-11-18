"""Deterministic diff generation and non-executing Python syntax checks."""

from __future__ import annotations

import ast
import difflib
import hashlib
import time
from dataclasses import asdict, dataclass, field
from typing import Optional

from fix_generation import FixCandidate
from static_analysis import analyze_python, static_fingerprint_counts


@dataclass(frozen=True)
class SyntaxCheckResult:
    check_name: str
    status: str
    error_type: Optional[str] = None
    message: Optional[str] = None
    line: Optional[int] = None
    column: Optional[int] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class FixVerificationResult:
    candidate_id: str
    unified_diff: str
    syntax_check: SyntaxCheckResult
    verification_status: str
    original_sha256: Optional[str] = None
    corrected_sha256: Optional[str] = None
    behavioral_tests_status: str = "not_run"
    static_analysis_status: str = "not_run"
    pre_existing_static_issues: tuple[dict, ...] = ()
    introduced_static_issues: tuple[dict, ...] = ()
    static_analysis_errors: tuple[dict, ...] = ()
    verification_errors: tuple[str, ...] = ()
    verification_seconds: float = field(default=0.0, compare=False)

    def to_dict(self) -> dict:
        result = asdict(self)
        result["syntax_check"] = self.syntax_check.to_dict()
        result["verification_errors"] = list(self.verification_errors)
        result["pre_existing_static_issues"] = list(self.pre_existing_static_issues)
        result["introduced_static_issues"] = list(self.introduced_static_issues)
        result["static_analysis_errors"] = list(self.static_analysis_errors)
        return result


def generate_unified_diff(
    original_code: str,
    corrected_code: str,
    *,
    fromfile: str = "a/candidate.py",
    tofile: str = "b/candidate.py",
) -> str:
    """Diff exact source strings; only fixed, generic labels are accepted by default."""
    if not isinstance(original_code, str) or not isinstance(corrected_code, str):
        raise TypeError("diff inputs must be strings")
    lines = difflib.unified_diff(
        original_code.splitlines(keepends=True),
        corrected_code.splitlines(keepends=True),
        fromfile=fromfile,
        tofile=tofile,
        lineterm="\n",
    )
    rendered: list[str] = []
    for index, line in enumerate(lines):
        # difflib preserves a source line's terminator. Add the conventional marker
        # for an unterminated source line so output remains unambiguous and parseable.
        if index >= 2 and not line.endswith(("\n", "\r")):
            rendered.append(line + "\n\\ No newline at end of file\n")
        else:
            rendered.append(line)
    return "".join(rendered)


def check_python_syntax(source: str) -> SyntaxCheckResult:
    """Parse Python syntax only; never import, compile, or execute submitted code."""
    if not isinstance(source, str):
        return SyntaxCheckResult(
            check_name="ast.parse",
            status="errored",
            error_type="TypeError",
            message="source must be a string",
        )
    if not source.strip():
        return SyntaxCheckResult(
            check_name="ast.parse",
            status="failed",
            error_type="EmptySourceError",
            message="corrected Python source is empty",
        )
    try:
        ast.parse(source, filename="<fix-candidate>", mode="exec")
    except SyntaxError as exc:
        return SyntaxCheckResult(
            check_name="ast.parse",
            status="failed",
            error_type=type(exc).__name__,
            message=exc.msg,
            line=exc.lineno,
            column=exc.offset,
        )
    except Exception as exc:
        return SyntaxCheckResult(
            check_name="ast.parse",
            status="errored",
            error_type=type(exc).__name__,
            message=str(exc),
        )
    return SyntaxCheckResult(check_name="ast.parse", status="passed")


def verify_fix_candidate(
    candidate: FixCandidate, *, expected_candidate_id: Optional[str] = None
) -> FixVerificationResult:
    """Produce a repeatable verification report without changing the candidate."""
    verification_started = time.perf_counter()
    candidate_id = getattr(candidate, "candidate_id", "")
    if not isinstance(candidate, FixCandidate):
        return _verification_error("", "invalid_candidate: expected FixCandidate")
    if expected_candidate_id is not None and expected_candidate_id != candidate_id:
        return _verification_error(
            candidate_id, "candidate_id_mismatch: handle_does_not_match_candidate"
        )
    if candidate.generation_status != "generated" or candidate.corrected_code is None:
        return _verification_error(
            candidate_id, "candidate_not_generated: no_corrected_code_to_verify"
        )
    try:
        unified = generate_unified_diff(candidate.original_code, candidate.corrected_code)
        syntax = check_python_syntax(candidate.corrected_code)
        original_analysis = analyze_python(candidate.original_code)
        corrected_analysis = analyze_python(candidate.corrected_code)
    except Exception as exc:
        return _verification_error(
            candidate_id, f"verification_error: {type(exc).__name__}: {str(exc)[:200]}"
        )
    errors = tuple(
        {"source": source, "error_type": result.error_type,
         "message": result.error_message, "line": result.error_line,
         "column": result.error_column}
        for source, result in (("original", original_analysis), ("candidate", corrected_analysis))
        if result.status == "errored"
    )
    if errors:
        static_status = "errored"
    else:
        original_counts = static_fingerprint_counts(original_analysis)
        candidate_counts = static_fingerprint_counts(corrected_analysis)
        introduced_counts = candidate_counts - original_counts
        remaining = introduced_counts.copy()
        introduced = []
        pre_existing = []
        for item in corrected_analysis.findings:
            key = (item.rule_id, item.fingerprint)
            if remaining[key] > 0:
                introduced.append(item.to_dict())
                remaining[key] -= 1
            else:
                pre_existing.append(item.to_dict())
        static_status = "failed" if introduced else "passed"
    if syntax.status != "passed":
        overall = "syntax_failed"
    elif static_status == "errored":
        overall = "verification_error"
    elif static_status == "failed":
        overall = "static_analysis_failed"
    else:
        overall = "syntax_passed"
    return FixVerificationResult(
        candidate_id=candidate_id,
        unified_diff=unified,
        syntax_check=syntax,
        verification_status=overall,
        original_sha256=hashlib.sha256(candidate.original_code.encode("utf-8")).hexdigest(),
        corrected_sha256=hashlib.sha256(candidate.corrected_code.encode("utf-8")).hexdigest(),
        static_analysis_status=static_status,
        pre_existing_static_issues=tuple(pre_existing) if not errors else (),
        introduced_static_issues=tuple(introduced) if not errors else (),
        static_analysis_errors=errors,
        verification_seconds=time.perf_counter() - verification_started,
    )


def _verification_error(candidate_id: str, error: str) -> FixVerificationResult:
    return FixVerificationResult(
        candidate_id=candidate_id,
        unified_diff="",
        syntax_check=SyntaxCheckResult(
            check_name="ast.parse", status="not_run", message=error
        ),
        verification_status="verification_error",
        verification_errors=(error,),
        static_analysis_status="errored",
        static_analysis_errors=({"message": error},),
    )
