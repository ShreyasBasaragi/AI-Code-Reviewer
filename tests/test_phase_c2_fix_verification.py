import json
import os
from dataclasses import replace

import pytest

from fix_generation import parse_fix_response
from fix_verification import (
    check_python_syntax,
    generate_unified_diff,
    verify_fix_candidate,
)
from mcp_server import orchestrator, server


def candidate(original="def value():\n    return 1\n", corrected="def value():\n    return 2\n"):
    response = json.dumps(
        {
            "finding_ref": "finding-1",
            "corrected_code": corrected,
            "explanation": "Return the selected value.",
            "assumptions": [],
            "evidence_ids": [],
        }
    )
    return parse_fix_response(
        response,
        finding_ref="finding-1",
        original_code=original,
        allowed_evidence_ids=[],
    )


def test_unified_diff_simple_change_has_meaningful_labels():
    diff = generate_unified_diff("x = 1\n", "x = 2\n")
    assert diff == (
        "--- a/candidate.py\n+++ b/candidate.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    )


def test_unified_diff_handles_insertions_and_deletions():
    diff = generate_unified_diff("a\nb\nc\n", "a\nc\nd\n")
    assert "-b\n" in diff
    assert "+d\n" in diff
    assert " c\n" in diff


def test_unified_diff_handles_multiline_modification():
    diff = generate_unified_diff("def f():\n    a = 1\n    return a\n", "def f():\n    a = 2\n    b = 3\n    return a + b\n")
    assert "-    a = 1\n" in diff
    assert "+    a = 2\n" in diff
    assert "+    b = 3\n" in diff
    assert "+    return a + b\n" in diff


@pytest.mark.parametrize(
    ("old", "new", "marker_count"),
    [
        ("x=1", "x=2", 2),
        ("x=1\n", "x=2\n", 0),
        ("x=1\r\n", "x=2\r\n", 0),
        ("x=1\n", "x=2", 1),
        ("x=1", "x=2\n", 1),
    ],
)
def test_diff_preserves_final_newline_semantics(old, new, marker_count):
    diff = generate_unified_diff(old, new)
    assert diff.count("\\ No newline at end of file") == marker_count
    assert diff.startswith("--- a/candidate.py\n+++ b/candidate.py\n")


def test_valid_python_syntax_passes():
    result = check_python_syntax("def f(x):\n    return x + 1\n")
    assert result.check_name == "ast.parse"
    assert result.status == "passed"
    assert result.error_type is None


def test_invalid_python_syntax_reports_location():
    result = check_python_syntax("def f(:\n    return 1\n")
    assert result.status == "failed"
    assert result.error_type == "SyntaxError"
    assert result.message
    assert result.line == 1
    assert result.column is not None


def test_invalid_syntax_input_type_is_reported_as_check_error():
    result = check_python_syntax(None)
    assert result.status == "errored"
    assert result.error_type == "TypeError"


def test_empty_corrected_code_fails_without_being_treated_as_valid_python():
    report = verify_fix_candidate(replace(candidate(), corrected_code="   "))
    assert report.verification_status == "syntax_failed"
    assert report.syntax_check.status == "failed"
    assert report.syntax_check.error_type == "EmptySourceError"


def test_candidate_id_mismatch_fails_safely():
    report = verify_fix_candidate(candidate(), expected_candidate_id="fix-wrong")
    assert report.verification_status == "verification_error"
    assert report.verification_errors[0].startswith("candidate_id_mismatch")
    assert report.syntax_check.status == "not_run"


def test_unknown_or_expired_mcp_handle_fails_safely():
    server._FIX_CANDIDATE_CACHE.pop("unknown-c2-handle", None)
    report = json.loads(server.verify_fix("unknown-c2-handle"))
    assert report["verification_status"] == "verification_error"
    assert report["syntax_check"]["status"] == "not_run"
    assert "unknown_or_expired" in report["verification_errors"][0]


def test_verification_does_not_modify_source_files_or_candidate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "submitted.py"
    source.write_text("untouched\n", encoding="utf-8")
    before = (set(os.listdir(tmp_path)), source.read_bytes())
    item = candidate()
    original_pair = (item.original_code, item.corrected_code)

    report = verify_fix_candidate(item)

    assert report.verification_status == "syntax_passed"
    assert report.static_analysis_status == "passed"
    assert (item.original_code, item.corrected_code) == original_pair
    assert (set(os.listdir(tmp_path)), source.read_bytes()) == before


def test_verification_service_does_not_call_llm_or_retriever(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("verification must be offline and model-free")

    monkeypatch.setattr(orchestrator, "_get_llm", forbidden)
    from rag import retrieve
    monkeypatch.setattr(retrieve, "retrieve_evidence", forbidden)
    report = orchestrator.verify_fix_candidate(candidate())
    assert report.verification_status == "syntax_passed"


def test_mcp_verifies_cached_candidate_without_regeneration(monkeypatch):
    item = candidate()
    server._FIX_CANDIDATE_CACHE[item.candidate_id] = item

    def forbidden(*args, **kwargs):
        raise AssertionError("verification must not regenerate the candidate")

    monkeypatch.setattr(server, "generate_fix_candidate", forbidden)
    report = json.loads(server.verify_fix(item.candidate_id))
    assert report["candidate_id"] == item.candidate_id
    assert report["verification_status"] == "syntax_passed"
    assert report["behavioral_tests_status"] == "not_run"
    assert report["static_analysis_status"] == "passed"
    server._FIX_CANDIDATE_CACHE.pop(item.candidate_id, None)


def test_verification_is_repeatable_and_diff_matches_candidate():
    item = candidate("x = 1\n", "x = 2\n")
    before = (item.original_code, item.corrected_code)
    first = verify_fix_candidate(item)
    second = verify_fix_candidate(item)
    assert first == second
    assert "-x = 1\n" in first.unified_diff
    assert "+x = 2\n" in first.unified_diff
    assert (item.original_code, item.corrected_code) == before
