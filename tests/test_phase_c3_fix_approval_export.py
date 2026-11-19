import json
import os

import pytest

from fix_generation import parse_fix_response
from fix_lifecycle import record_verification
from mcp_server import orchestrator, server


ORIGINAL = "def value():\n    return 1\n"
CORRECTED = "def value():\n    return 2\n"


def make_candidate(original=ORIGINAL, corrected=CORRECTED):
    response = json.dumps(
        {
            "finding_ref": "finding-1",
            "corrected_code": corrected,
            "explanation": "Return the corrected value.",
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


def verified(candidate):
    report = orchestrator.verify_fix_candidate(candidate)
    return record_verification(candidate, report)


def test_new_candidate_starts_pending():
    item = make_candidate()
    assert item.approval_status == "pending"
    assert item.approval_history == ()
    assert item.verification_status == "not_checked"


def test_successful_acceptance_requires_syntax_and_preserves_report():
    item = verified(make_candidate())
    saved_report = item.verification_report
    result = orchestrator.accept_fix(item)
    assert result.outcome == "accepted"
    assert result.approval_status == "accepted"
    assert "behavioral tests were not run" in result.message
    assert result.candidate.verification_report == saved_report
    assert result.candidate.approval_history[-1]["action"] == "accepted"


def test_rejection_is_explicit_and_does_not_require_syntax_check():
    result = orchestrator.reject_fix(make_candidate())
    assert result.approval_status == "rejected"
    assert result.outcome == "rejected"
    assert result.candidate.original_code == ORIGINAL
    assert "original review and source remain unchanged" in result.message


def test_repeated_consistent_decision_is_idempotent():
    accepted = orchestrator.accept_fix(verified(make_candidate())).candidate
    repeated = orchestrator.accept_fix(accepted)
    assert repeated.outcome == "already_applied"
    assert repeated.candidate == accepted


def test_conflicting_decision_returns_error_without_changing_state():
    accepted = orchestrator.accept_fix(verified(make_candidate())).candidate
    conflict = orchestrator.reject_fix(accepted)
    assert conflict.outcome == "conflict"
    assert conflict.error == "conflicting_decision"
    assert conflict.approval_status == "accepted"
    assert conflict.candidate.approval_status == "accepted"
    assert conflict.candidate.approval_history[-1]["outcome"] == "conflict"


def test_acceptance_blocked_when_syntax_fails():
    broken = verified(make_candidate(corrected="def value(:\n return 2\n"))
    result = orchestrator.accept_fix(broken)
    assert result.outcome == "blocked"
    assert result.approval_status == "pending"
    assert result.error == "required_verification_failed"


def test_acceptance_blocked_before_verification():
    result = orchestrator.accept_fix(make_candidate())
    assert result.outcome == "blocked"
    assert result.approval_status == "pending"
    assert result.candidate.verification_status == "not_checked"


def test_export_accepted_candidate_includes_code_diff_and_warning():
    accepted = orchestrator.accept_fix(verified(make_candidate())).candidate
    exported = orchestrator.export_fix(accepted).to_dict()
    assert exported["candidate_id"] == accepted.candidate_id
    assert exported["filename"] == "candidate.py"
    assert exported["corrected_code"] == CORRECTED
    assert "-    return 1" in exported["unified_diff"]
    assert "+    return 2" in exported["unified_diff"]
    assert exported["approval_status"] == "accepted"
    assert exported["syntax_verification_status"] == "syntax_passed"
    assert "Behavioral tests were not run" in exported["warning"]


@pytest.mark.parametrize("state", ["pending", "rejected"])
def test_export_blocked_for_pending_or_rejected_candidate(state):
    item = make_candidate()
    if state == "rejected":
        item = orchestrator.reject_fix(item).candidate
    with pytest.raises(ValueError, match="accepted candidate"):
        orchestrator.export_fix(item)


def test_unknown_or_expired_candidate_handle_fails_safely(monkeypatch):
    from mcp_server.state import PROCESS_STATE
    monkeypatch.setattr(PROCESS_STATE, "persistence_store", None)
    result = json.loads(server.accept_fix("unknown-c3-handle"))
    assert result["outcome"] == "error"
    assert "unknown_or_expired" in result["error"]
    result = json.loads(server.export_fix("unknown-c3-handle"))
    assert result["outcome"] == "error"


def test_export_uses_safe_generic_filename_not_model_path():
    # The generation schema has no filename field; export intentionally supplies a fixed name.
    accepted = orchestrator.accept_fix(verified(make_candidate())).candidate
    payload = orchestrator.export_fix(accepted).to_dict()
    assert payload["filename"] == "candidate.py"
    assert "/" not in payload["filename"]
    assert ".." not in payload["filename"]


def test_original_source_unchanged_and_approval_export_do_not_touch_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "original.py"
    source.write_text(ORIGINAL, encoding="utf-8")
    before = (set(os.listdir(tmp_path)), source.read_bytes())
    candidate = verified(make_candidate())
    accepted = orchestrator.accept_fix(candidate).candidate
    exported = orchestrator.export_fix(accepted)
    assert exported.corrected_code == CORRECTED
    assert (set(os.listdir(tmp_path)), source.read_bytes()) == before


def test_approval_and_export_do_not_call_model_or_retriever(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("approval/export must be local operations")

    monkeypatch.setattr(orchestrator, "_get_llm", forbidden)
    from rag import retrieve
    monkeypatch.setattr(retrieve, "retrieve_evidence", forbidden)
    accepted = orchestrator.accept_fix(verified(make_candidate())).candidate
    orchestrator.export_fix(accepted)


def test_mcp_verify_accept_export_and_reject_are_explicit_cache_operations(monkeypatch):
    from mcp_server.state import PROCESS_STATE
    monkeypatch.setattr(PROCESS_STATE, "persistence_store", None)
    candidate = make_candidate()
    server._FIX_CANDIDATE_CACHE[candidate.candidate_id] = candidate
    verification = json.loads(server.verify_fix(candidate.candidate_id))
    assert verification["verification_status"] == "syntax_passed"
    accepted = json.loads(server.accept_fix(candidate.candidate_id))
    assert accepted["approval_status"] == "accepted"
    exported = json.loads(server.export_fix(candidate.candidate_id))
    assert exported["corrected_code"] == CORRECTED

    other = make_candidate(original="def other():\n    return 3\n", corrected="def other():\n    return 4\n")
    server._FIX_CANDIDATE_CACHE[other.candidate_id] = other
    rejected = json.loads(server.reject_fix(other.candidate_id))
    assert rejected["approval_status"] == "rejected"
    for key in (candidate.candidate_id, other.candidate_id):
        server._FIX_CANDIDATE_CACHE.pop(key, None)
