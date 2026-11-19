import pytest

from mcp_server.confidence import (
    model_estimate_confidence_evidence,
    valid_model_confidence_estimate,
    verify_confidence_evidence,
)
from mcp_server.github_policy import commit_execution_decision


REPO = "ShreyasBasaragi/AI-Code-Reviewer"
BRANCH = "test/groq-confidence-demo"


def _enable(monkeypatch):
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_POLICY", "academic_prototype")
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_REPOSITORY", REPO)
    monkeypatch.setenv("GITHUB_MODEL_CONFIDENCE_PROTOTYPE_BRANCH", BRANCH)


@pytest.mark.parametrize("value", [True, "0.9", -0.01, 1.01, float("nan"), float("inf")])
def test_model_estimate_validator_rejects_non_numeric_non_finite_and_out_of_range(value):
    assert valid_model_confidence_estimate(value) is None


def test_model_estimate_validation_handles_extreme_numbers_and_untyped_categories(monkeypatch):
    assert valid_model_confidence_estimate(10 ** 1000) is None
    _enable(monkeypatch)
    assert model_estimate_confidence_evidence(
        {"issue_type": [], "model_confidence_estimate": 0.9},
        model_id="openai/gpt-oss-20b", repository=REPO,
        branch=BRANCH, permitted_branches=(BRANCH,),
    ) is None


def test_prototype_evidence_requires_explicit_exact_disposable_branch_opt_in(monkeypatch):
    finding = {"issue_type": "style", "model_confidence_estimate": 0.91}
    assert model_estimate_confidence_evidence(
        finding, model_id="openai/gpt-oss-20b", repository=REPO,
        branch=BRANCH, permitted_branches=(BRANCH,),
    ) is None

    _enable(monkeypatch)
    evidence = model_estimate_confidence_evidence(
        finding, model_id="openai/gpt-oss-20b", repository=REPO,
        branch=BRANCH, permitted_branches=(BRANCH,),
    )
    assert evidence == {
        "confidence_source": "uncalibrated_model_estimate",
        "calibrated": False,
        "manifest_verified": False,
        "model_id": "openai/gpt-oss-20b",
        "finding_category": "style",
        "model_confidence_estimate": 0.91,
        "prototype_policy": "academic_prototype",
        "prototype_repository": REPO,
        "prototype_branch": BRANCH,
    }
    assert verify_confidence_evidence(
        evidence, repository=REPO, branch=BRANCH,
        permitted_branches=(BRANCH,), threshold=0.80,
    )
    assert not verify_confidence_evidence(
        evidence, repository=REPO, branch="development",
        permitted_branches=(BRANCH, "development"), threshold=0.80,
    )


@pytest.mark.parametrize("finding", [
    {"issue_type": "security", "model_confidence_estimate": 0.99},
    {"issue_type": "style", "model_confidence_estimate": 0.79},
    {"issue_type": "style", "model_confidence_estimate": None},
    {"issue_type": "style", "model_confidence_estimate": True},
])
def test_prototype_evidence_fails_closed_for_ineligible_findings(monkeypatch, finding):
    _enable(monkeypatch)
    assert model_estimate_confidence_evidence(
        finding, model_id="openai/gpt-oss-20b", repository=REPO,
        branch=BRANCH, permitted_branches=(BRANCH,),
    ) is None


def test_commit_decision_revalidates_prototype_policy_and_keeps_other_gates(monkeypatch):
    gates = {
        "repository_opt_in": True,
        "eligible_low_risk_category": True,
        "prototype_confidence_policy_approved": True,
        "confidence_source": "uncalibrated_model_estimate",
        "confidence": 0.9,
        "source_hashes_valid": True,
        "syntax_passed": True,
        "static_passed": True,
        "behavioral_passed": True,
        "candidate_ci_passed": True,
        "scope_valid": True,
        "write_permission_valid": True,
        "durable_record_reserved": True,
        "idempotency_reserved": True,
        "changed_lines": 1,
        "candidate_sha": "a" * 40,
        "ci_sha": "a" * 40,
        "unexpected_files": [],
    }
    decision = commit_execution_decision(
        repository=REPO, branch=BRANCH, expected_head_sha="b" * 40,
        current_head_sha="b" * 40, is_fork=False, permitted_branches=(BRANCH,),
        gates=gates, execution_authorized=True,
    )
    assert not decision["eligible"]
    assert "required_gate_confidence_policy_not_passed" in decision["denial_reasons"]

    _enable(monkeypatch)
    allowed = commit_execution_decision(
        repository=REPO, branch=BRANCH, expected_head_sha="b" * 40,
        current_head_sha="b" * 40, is_fork=False, permitted_branches=(BRANCH,),
        gates=gates, execution_authorized=True,
    )
    assert allowed["eligible"]
