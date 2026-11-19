import json
import sqlite3

import pytest

from fix_generation import parse_fix_response
from mcp_server.lifecycle_store import SQLiteLifecycleStore
from mcp_server.orchestrator import accept_fix, reject_fix, verify_fix_candidate
from mcp_server.state import LifecycleState
from review_schema import ReviewResponse, parse_review_response


def make_candidate(review_id="review-1"):
    raw = json.dumps({
        "finding_ref": "finding-1", "corrected_code": "def value():\n    return 2\n",
        "explanation": "Return the expected value.", "assumptions": [], "evidence_ids": [],
    })
    from dataclasses import replace
    candidate = parse_fix_response(raw, finding_ref="finding-1",
                                   original_code="def value():\n    return 1\n",
                                   allowed_evidence_ids=[])
    return replace(candidate, review_id=review_id)


def test_review_candidate_and_approval_survive_store_restart(tmp_path):
    path = tmp_path / "private" / "lifecycle.sqlite3"
    first = LifecycleState(persistence_store=SQLiteLifecycleStore(path))
    response = ReviewResponse(summary="One finding.", valid=True)
    first.put_review("review-1", "def value():\n    return 1\n", response,
                     repository="acme/demo", pr_number=4,
                     base_sha="a" * 40, head_sha="b" * 40)
    candidate = make_candidate()
    verified = verify_fix_candidate(candidate, expected_candidate_id=candidate.candidate_id)
    from fix_lifecycle import record_verification
    accepted = accept_fix(record_verification(candidate, verified), actor_id="administrator_session")
    first.put_candidate(accepted.candidate)

    restarted = LifecycleState(persistence_store=SQLiteLifecycleStore(path))
    restored_review = restarted.get_review("review-1")
    restored_candidate = restarted.get_candidate(candidate.candidate_id)
    assert restored_review is not None
    assert restored_review.source_sha256
    assert restored_review.repository == "acme/demo" and restored_review.pr_number == 4
    assert restored_candidate is not None and restored_candidate.approval_status == "accepted"
    event = restored_candidate.approval_history[-1]
    assert event["actor_id"] == "administrator_session"
    assert event["action"] == "accepted"
    assert event["source_sha256"] == accepted.candidate.verification_report["original_sha256"]
    persisted_events = restarted.persistence_store.decisions(candidate.candidate_id)
    assert len(persisted_events) == 1
    assert persisted_events[0]["actor_identity"] == "administrator_session"
    assert path.stat().st_mode & 0o777 == 0o600


def test_persisted_review_can_generate_fix_without_raw_provider_text(tmp_path):
    raw = json.dumps({"summary": "One finding.", "findings": [{
        "title": "Incorrect value", "issue_type": "bug", "explanation": "The return value differs.",
        "recommended_fix": "Return the expected value.", "affected_file": "submitted.py",
        "line_start": 2, "line_end": 2, "corrected_code": None, "evidence_ids": [],
        "uncertain": True, "abstained": False, "risk_priority": None,
        "verification_status": "not_checked",
    }]})
    response = parse_review_response(raw)
    first = LifecycleState(persistence_store=SQLiteLifecycleStore(tmp_path / "lifecycle.sqlite3"))
    first.put_review("review-1", "def value():\n    return 1\n", response)
    finding_id = response.findings[0].finding_id

    class OfflineFixProvider:
        def generate_fix(self, _code, finding, _context):
            return json.dumps({
                "finding_ref": finding.finding_id,
                "corrected_code": "def value():\n    return 2\n",
                "explanation": "Return the expected value.",
                "assumptions": [], "evidence_ids": [],
            })

    restarted = LifecycleState(persistence_store=SQLiteLifecycleStore(first.persistence_store.path))
    from mcp_server.http_api import ReviewFixService
    candidate = ReviewFixService(llm=OfflineFixProvider(), state=restarted).generate_fix(
        "review-1", finding_id,
    )
    assert candidate.generation_status == "generated"
    assert candidate.review_id == "review-1"


def test_rejected_candidate_remains_rejected_and_duplicate_write_is_idempotent(tmp_path):
    store = SQLiteLifecycleStore(tmp_path / "lifecycle.sqlite3")
    candidate = reject_fix(make_candidate(), actor_id="administrator_session").candidate
    state = LifecycleState(persistence_store=store)
    state.put_candidate(candidate)
    state.put_candidate(candidate)
    restored = LifecycleState(persistence_store=SQLiteLifecycleStore(store.path)).get_candidate(candidate.candidate_id)
    assert restored.approval_status == "rejected"
    assert len(SQLiteLifecycleStore(store.path).decisions(candidate.candidate_id)) == 1


def test_corrupt_and_incompatible_records_fail_safely(tmp_path):
    path = tmp_path / "lifecycle.sqlite3"
    store = SQLiteLifecycleStore(path)
    state = LifecycleState(persistence_store=store)
    candidate = make_candidate()
    state.put_candidate(candidate)
    with store._connection() as db:
        db.execute("UPDATE lifecycle_candidates SET candidate_json='not-json' WHERE candidate_id=?",
                   (candidate.candidate_id,))
    assert LifecycleState(persistence_store=store).get_candidate(candidate.candidate_id) is None

    with store._connection() as db:
        db.execute("PRAGMA user_version=99")
    with pytest.raises(RuntimeError, match="schema_newer"):
        store.load_candidate(candidate.candidate_id)


def test_retention_removes_expired_content_but_preserves_active_and_audit(tmp_path):
    clock = [10_000.0]
    store = SQLiteLifecycleStore(tmp_path / "lifecycle.sqlite3", retention_days=1,
                                 clock=lambda: clock[0])
    state = LifecycleState(persistence_store=store)
    state.put_review("review-1", "x = 1\n", ReviewResponse(summary="ok"))
    candidate = reject_fix(make_candidate(), actor_id="administrator_session").candidate
    store.save_candidate(candidate.to_dict())
    assert store.cleanup(now=clock[0] + 100)["reviews_removed"] == 0
    assert store.load_review("review-1") is not None
    assert store.load_candidate(candidate.candidate_id) is not None

    removed = store.cleanup(now=clock[0] + 86401)
    assert removed["reviews_removed"] == 1
    assert removed["candidates_removed"] == 1
    assert store.load_review("review-1") is None
    assert store.load_candidate(candidate.candidate_id) is None
    assert len(store.decisions(candidate.candidate_id)) == 1


def test_source_and_candidate_artifact_sizes_are_bounded(tmp_path):
    store = SQLiteLifecycleStore(tmp_path / "lifecycle.sqlite3")
    with pytest.raises(ValueError, match="source_exceeds_storage_limit"):
        store.save_review("too-large", "x" * (512 * 1024 + 1), {"summary": "ok"})
    large = make_candidate()
    from dataclasses import replace
    with pytest.raises(ValueError, match="candidate_source_exceeds_storage_limit"):
        store.save_candidate(replace(large, corrected_code="x" * (512 * 1024 + 1)).to_dict())
