import json
from pathlib import Path

from evaluation.metrics import aggregate_metrics, match_findings
from evaluation.runner import load_cases, run_evaluation


class FakeProvider:
    def __init__(self, review, fix=None):
        self.review = review
        self.fix = fix
        self.review_calls = 0
        self.fix_calls = 0

    def analyze_code(self, code, context):
        self.review_calls += 1
        return self.review

    def generate_fix(self, code, finding, context):
        self.fix_calls += 1
        return self.fix


def _review(issue_type="bug", evidence_ids=None, abstained=False):
    return json.dumps({"summary": "Scripted fixture", "findings": [{
        "title": "fixture", "issue_type": issue_type, "explanation": "fixture explanation",
        "recommended_fix": "fixture recommendation", "line_start": 2,
        "evidence_ids": evidence_ids or [], "uncertain": not bool(evidence_ids),
        "abstained": abstained,
    }]})


def test_benchmark_contains_30_originally_authored_cases_by_category():
    cases = load_cases()
    assert len(cases) == 30
    assert {c["category"] for c in cases} == {"security", "logic_runtime", "clean"}
    assert len({c["case_id"] for c in cases}) == 30
    assert not any("baseline_review" in c or "rag_review" in c for c in cases)


def test_matching_is_one_to_one_with_type_and_location_tolerance():
    case = {"expected_issue_type": "bug", "expected_line": 10}
    matched = match_findings(case, [{"issue_type": "bug", "line_start": 12},
                                   {"issue_type": "bug", "line_start": 10}])
    assert matched["true_positives"] == 1
    assert matched["false_positives"] == 1
    assert matched["false_negatives"] == 0


def test_clean_case_false_positive_rate_and_undefined_metrics():
    results = [
        {"category": "clean", "findings": [{"issue_type": "bug"}], "matching":
         {"true_positives": 0, "false_positives": 1, "false_negatives": 0},
         "schema_valid": True, "evidence_references_valid": True,
         "review_latency_seconds": 0.2, "fix_attempted": False,
         "fix_generated": False, "fix_syntax_status": None},
    ]
    metrics = aggregate_metrics(results)
    assert metrics["clean_case_false_positive_rate"] == 1.0
    assert metrics["finding_recall"] is None
    assert metrics["fix_generation_success_rate"] is None
    assert metrics["python_syntax_validity_rate"] is None
    assert metrics["review_latency_seconds"]["median"] == 0.2
    assert metrics["case_error_rate"] == 0.0


def test_failed_provider_cases_do_not_become_false_negatives_or_clean_true_negatives():
    results = [{
        "category": "security", "findings": [], "schema_valid": False,
        "processing_errors": ["model_failure: APIConnectionError"],
        "matching": {"true_positives": 0, "false_positives": 0, "false_negatives": 1},
        "review_latency_seconds": 1.0, "fix_attempted": False,
        "fix_generated": False, "fix_syntax_status": None,
    }, {
        "category": "clean", "findings": [], "schema_valid": False,
        "processing_errors": ["model_failure: APIConnectionError"],
        "matching": {"true_positives": 0, "false_positives": 0, "false_negatives": 0},
        "review_latency_seconds": 1.1, "fix_attempted": False,
        "fix_generated": False, "fix_syntax_status": None,
    }]
    metrics = aggregate_metrics(results)
    assert metrics["finding_precision"] is None
    assert metrics["finding_recall"] is None
    assert metrics["clean_case_false_positive_rate"] is None
    assert metrics["case_error_rate"] == 1.0


def test_invalid_json_is_reported_not_replaced_by_scripted_findings():
    case = load_cases()[0]
    report = run_evaluation(provider=FakeProvider("not json"), mode="test", cases=[case],
                            max_cases=1)
    record = report["case_results"][0]
    assert record["schema_valid"] is False
    assert record["findings"] == []
    assert "invalid_json" in record["processing_errors"][0]


def test_evaluation_runner_uses_shared_pipeline_without_external_calls():
    cases = load_cases()[:1]
    provider = FakeProvider(_review())
    provider.structured_output_metadata = {
        "mode": "json_schema", "strict": True,
        "schema_name": "code_review", "schema_version": "review-v1",
    }
    report = run_evaluation(provider=provider, mode="offline-test", cases=cases,
                            max_cases=1, rag_enabled=False)
    assert provider.review_calls == 1
    assert report["case_results"][0]["retrieved_evidence_count"] == 0
    assert report["case_results"][0]["schema_valid"] is True
    assert report["structured_output"]["schema_version"] == "review-v1"


def test_rag_disabled_run_does_not_call_available_retriever():
    called = []
    provider = FakeProvider(json.dumps({"summary": "No issues.", "findings": []}))
    report = run_evaluation(provider=provider, mode="offline-test", cases=load_cases()[-1:],
                            max_cases=1, retriever=lambda _code: called.append(True))
    assert called == []
    assert report["rag_enabled"] is False
    assert report["case_results"][0]["retrieved_evidence_count"] == 0


def test_provider_failure_is_reported_as_invalid_review():
    class BrokenProvider(FakeProvider):
        def analyze_code(self, code, context):
            raise TimeoutError("fixture provider timeout")

    report = run_evaluation(provider=BrokenProvider(""), mode="offline-test",
                            cases=[load_cases()[0]], max_cases=1)
    result = report["case_results"][0]
    assert result["schema_valid"] is False
    assert any("model_failure" in error and "TimeoutError" in error
               for error in result["processing_errors"])
    assert report["provider_request_counts"]["review"] == 1


def test_evidence_reference_validation_and_abstention_are_recorded():
    from review_schema import RetrievedEvidence

    evidence = RetrievedEvidence(evidence_id="ev-1", source_family="fixture", source_url=None,
                                 rule_identifier=None, text="example")

    class EvidenceProvider(FakeProvider):
        def analyze_code(self, code, context):
            self.review_calls += 1
            return _review("security", ["ev-1"], abstained=True)

    provider = EvidenceProvider("")
    report = run_evaluation(provider=provider, mode="offline-test", cases=[load_cases()[0]],
                            max_cases=1, rag_enabled=True, retriever=lambda _code: [evidence])
    record = report["case_results"][0]
    assert record["evidence_references_valid"] is True
    assert record["abstained"] is True
    assert record["retrieved_evidence_count"] == 1


def test_fix_generation_and_syntax_outcome_are_reported():
    case = load_cases()[0]
    corrected = case["source_code"].replace(
        "query = \"SELECT id FROM users WHERE name = '\" + name + \"'\"",
        "query = \"SELECT id FROM users WHERE name = ?\"",
    )
    fix = json.dumps({"finding_ref": "finding-1", "corrected_code": corrected,
                      "explanation": "Use parameter binding.", "assumptions": [],
                      "evidence_ids": []})
    provider = FakeProvider(_review("security"), fix)
    report = run_evaluation(provider=provider, mode="offline-test", cases=[case],
                            max_cases=1, include_fixes=True)
    result = report["case_results"][0]
    assert result["fix_attempted"] and result["fix_generated"]
    assert result["fix_syntax_status"] == "passed"
    assert "fix_diff" in result and result["fix_diff"]
    assert result["fix_diff_added_lines"] >= 1
    assert result["fix_assumptions"] == []
    assert result["fix_targeting_assessment"].startswith("not_automatically_assessed")
    assert provider.fix_calls == 1
